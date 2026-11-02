"""GET /export — trust-preserving dataset export (read-only) + GET /verify.

Every export is assembled strictly from ledger rows: latest goldens,
candidate facts (winner/loser provenance), conflicts, tombstones, and
pending steward items. Export never writes goldens, never changes the
ledger except for one audit row per download (who exported what, when,
in which scope and format).

Scopes:
  published            — golden records only (default, fail-closed)
  published+escalated  — plus pending steward items, marked STATUS=ESCALATED
  published+tombstones — plus explicit tombstone history per record
"""

from __future__ import annotations

import csv
import io
import json
import uuid
from datetime import UTC, datetime
from typing import Any

import ledger
from config import Settings
from fastapi import APIRouter, HTTPException, Query, Request, Response

from routes.deps import _check_entity

router = APIRouter()

_ENGINE_VERSION = "muse-mosaic-v1"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _parse_scope(scope: str) -> tuple[bool, bool]:
    cleaned = (scope or "published").strip().lower().replace("+", " ")
    cleaned = " ".join(cleaned.split())
    if cleaned == "published":
        return False, False
    if cleaned == "published escalated":
        return True, False
    if cleaned == "published tombstones":
        return False, True
    raise HTTPException(
        status_code=422,
        detail="scope must be 'published', 'published+escalated' or 'published+tombstones'",
    )


def _facts_by_id(database_path: str, entity_type: str, entity_id: str) -> dict[str, dict]:
    rows = ledger.fetch_candidate_facts(database_path, entity_type, entity_id)
    return {row["fact_id"]: dict(row) for row in rows}


def _lineage_for(golden: dict[str, Any], facts: dict[str, dict]) -> dict[str, Any]:
    lineage: dict[str, Any] = {}
    for attr, field in (golden.get("attributes") or {}).items():
        if not isinstance(field, dict):
            lineage[attr] = {"value": field}
            continue
        winner = facts.get(field.get("winner_fact_id") or "", {})
        losers = []
        for loser_id in field.get("loser_fact_ids") or []:
            loser = facts.get(loser_id, {})
            losers.append(
                {
                    "value": loser.get("value"),
                    "source": loser.get("source"),
                    "status": "tombstone",
                }
            )
        lineage[attr] = {
            "value": field.get("value"),
            "winner_source": winner.get("source"),
            "authority_rank": winner.get("authority_rank"),
            "verified": bool(winner.get("verified", False)),
            "rule_fired": field.get("rule_fired"),
            "losers": losers,
        }
    return lineage


def _traps_for(database_path: str, golden: dict[str, Any], facts: dict[str, dict]) -> list[dict]:
    traps = []
    try:
        conflicts = ledger.fetch_conflicts_by_decision(database_path, golden.get("decision_id", ""))
    except (ValueError, KeyError):
        conflicts = []
    for conflict in conflicts:
        if (conflict.get("rule_fired") or "") != "L1":
            continue
        for fact_id in conflict.get("fact_ids") or []:
            fact = facts.get(fact_id, {})
            if fact.get("value"):
                traps.append(
                    {
                        "value": fact.get("value"),
                        "reason": conflict.get("reason_code") or "L1 link proof failed",
                        "decision_id": golden.get("decision_id"),
                    }
                )
    return traps


def _record_for(
    database_path: str,
    golden: dict[str, Any],
    verify_base: str,
    include_tombstones: bool,
) -> dict[str, Any]:
    facts = _facts_by_id(database_path, golden["entity_type"], golden["entity_id"])
    record = {
        "entity_type": golden["entity_type"],
        "entity_id": golden["entity_id"],
        "decision_id": golden["decision_id"],
        "status": "PUBLISHED",
        "golden": {
            attr: (field.get("value") if isinstance(field, dict) else field)
            for attr, field in (golden.get("attributes") or {}).items()
        },
        "lineage_per_field": _lineage_for(golden, facts),
        "excluded_traps": _traps_for(database_path, golden, facts),
        "verify_url": f"{verify_base}/verify/{golden['decision_id']}",
        "policy_version": golden.get("policy_version"),
        "taxonomy_version": golden.get("taxonomy_version"),
    }
    if include_tombstones:
        tombstones = ledger.fetch_tombstones_for_golden(database_path, golden["golden_id"])
        record["tombstones"] = [
            {
                "loser_fact_id": row["loser_fact_id"],
                "value": facts.get(row["loser_fact_id"], {}).get("value"),
                "source": facts.get(row["loser_fact_id"], {}).get("source"),
                "reason": row["reason"],
            }
            for row in tombstones
        ]
    return record


def _export_metadata(
    database_path: str,
    scope: str,
    records: list[dict],
    extra: dict | None = None,
) -> dict[str, Any]:
    policy_versions = sorted({r.get("policy_version") for r in records if r.get("policy_version")})
    taxonomy_versions = sorted(
        {r.get("taxonomy_version") for r in records if r.get("taxonomy_version")}
    )
    meta: dict[str, Any] = {
        "exported_at": _now_iso(),
        "policy_version": policy_versions[0] if len(policy_versions) == 1 else policy_versions,
        "taxonomy_version": (
            taxonomy_versions[0] if len(taxonomy_versions) == 1 else taxonomy_versions
        ),
        "total_records": len(records),
        "scope": scope,
        "engine_version": _ENGINE_VERSION,
    }
    if extra:
        meta.update(extra)
    return meta


def _flatten_record(record: dict[str, Any]) -> dict[str, Any]:
    flat: dict[str, Any] = {
        "entity_type": record.get("entity_type"),
        "entity_id": record.get("entity_id"),
        "decision_id": record.get("decision_id"),
        "STATUS": record.get("status"),
        "policy_version": record.get("policy_version"),
        "taxonomy_version": record.get("taxonomy_version"),
    }
    lineage = record.get("lineage_per_field") or {}
    golden = record.get("golden") or {}
    for attr in sorted(set(list(golden.keys()) + list(lineage.keys()))):
        entry = lineage.get(attr) or {}
        flat[attr] = golden.get(attr)
        flat[f"{attr}__source"] = entry.get("winner_source")
        flat[f"{attr}__rule"] = entry.get("rule_fired")
        flat[f"{attr}__decision_id"] = record.get("decision_id")
    if record.get("status") == "ESCALATED":
        flat["STATUS"] = "ESCALATED"
        flat["escalation_reason"] = record.get("reason")
    return flat


def _records_for_scope(
    database_path: str, entity_type: str, entity_id: str | None, scope: str, verify_base: str
) -> tuple[list[dict[str, Any]], bool]:
    include_escalated, include_tombstones = _parse_scope(scope)
    records: list[dict[str, Any]] = []
    if entity_id is not None:
        golden = ledger.fetch_latest_golden(database_path, entity_type, entity_id)
        if golden is None:
            raise HTTPException(
                status_code=404, detail=f"no golden record for {entity_type}/{entity_id}"
            )
        records.append(_record_for(database_path, golden, verify_base, include_tombstones))
        pending = ledger.fetch_pending_stewards(database_path, entity_type, entity_id)
    else:
        for _, eid in ledger.fetch_entities(database_path):
            golden = ledger.fetch_latest_golden(database_path, entity_type, eid)
            if golden is not None:
                records.append(_record_for(database_path, golden, verify_base, include_tombstones))
        pending = ledger.fetch_pending_stewards(database_path, entity_type, None)
    warned = False
    if include_escalated:
        for item in pending:
            records.append(
                {
                    "entity_type": item.get("entity_type"),
                    "entity_id": item.get("entity_id"),
                    "decision_id": item.get("decision_id"),
                    "status": "ESCALATED",
                    "reason": (
                        f"{item.get('attribute')} is not trusted — "
                        "downstream systems must not use it for OTP, payment, or access decisions"
                    ),
                    "attribute": item.get("attribute"),
                    "suggested_action": item.get("suggested_action"),
                    "owner": item.get("owner"),
                    "golden": {},
                    "lineage_per_field": {},
                    "excluded_traps": [],
                    "verify_url": f"{verify_base}/verify/{item.get('decision_id')}",
                    "policy_version": None,
                    "taxonomy_version": None,
                }
            )
        warned = bool(pending)
    return records, warned


def _audit(
    database_path: str, entity_type: str, entity_id: str | None, fmt: str, scope: str, count: int
) -> str:
    export_id = f"exp-{uuid.uuid4().hex[:12]}"
    connection = ledger.connect(database_path)
    try:
        with connection:
            connection.execute(
                """
                INSERT INTO export_log
                  (export_id, created_at, entity_type, entity_id, format, scope, record_count)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (export_id, _now_iso(), entity_type, entity_id, fmt, scope, count),
            )
    finally:
        connection.close()
    return export_id


def _download(content: bytes, filename: str, media_type: str, headers: dict[str, str]) -> Response:
    headers = {
        **headers,
        "Content-Disposition": f'attachment; filename="{filename}"',
    }
    return Response(content=content, media_type=media_type, headers=headers)


def _policy_provenance(request: Request) -> dict[str, Any]:
    """policy_id + policy_hash for the active profile (acceptance: audit trail).

    The hash is SHA-256 over the Horn clause text, so a manifest exported
    under one profile cannot be passed off as the output of another: the
    provenance names both which rules ran and their exact bytes.
    """
    from policy_controller import controller
    from policy_profiles import default_profile_id, get_profile

    active = getattr(request.app.state, "active_policy", None) or default_profile_id()
    profile = get_profile(active)
    if profile is None:
        return {"policy_id": active}
    result = controller.get(active)
    return {
        "policy_id": active,
        "policy_label": profile.label,
        "policy_hash": result.policy_hash if result else profile.policy_hash,
        "policy_rules": profile.rules,
        "merkle_root": result.merkle_root if result else None,
    }


def _serve_records(
    request: Request,
    entity_type: str,
    entity_id: str | None,
    fmt: str,
    scope: str,
) -> Response:
    _check_entity(entity_type)
    current: Settings = request.app.state.settings
    database_path = str(current.database_path)
    verify_base = str(request.base_url).rstrip("/")
    records, warned = _records_for_scope(database_path, entity_type, entity_id, scope, verify_base)
    export_id = _audit(database_path, entity_type, entity_id, fmt, scope, len(records))
    policy = records[0].get("policy_version") if records else ""
    headers = {
        "X-Policy-Version": str(policy or ""),
        "X-Record-Count": str(len(records)),
        "X-Export-Id": export_id,
    }
    if warned:
        headers["X-Export-Warning"] = "escalated records included and marked STATUS=ESCALATED"
    stem = f"mosaic-{entity_type}-{entity_id or 'all'}-{export_id}"
    if fmt == "json":
        payload = {
            "export_metadata": _export_metadata(
                database_path,
                scope,
                records,
                {
                    "verification_base_url": f"{verify_base}/verify",
                    **_policy_provenance(request),
                },
            ),
            "records": records,
        }
        return _download(
            json.dumps(payload, indent=2, ensure_ascii=False).encode(),
            f"{stem}.json",
            "application/json",
            headers,
        )
    flat = [_flatten_record(record) for record in records]
    columns: list[str] = []
    for row in flat:
        for key in row:
            if key not in columns:
                columns.append(key)
    if fmt == "csv":
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in flat:
            writer.writerow(
                {key: ("" if row.get(key) is None else row.get(key)) for key in columns}
            )
        return _download(buffer.getvalue().encode(), f"{stem}.csv", "text/csv", headers)
    if fmt == "parquet":
        try:
            import pandas as pd
        except ImportError as exc:
            raise HTTPException(
                status_code=501, detail=f"parquet export unavailable: {exc}"
            ) from exc
        frame = pd.DataFrame(flat, columns=columns)
        buffer = io.BytesIO()
        frame.to_parquet(buffer, index=False)
        return _download(buffer.getvalue(), f"{stem}.parquet", "application/octet-stream", headers)
    raise HTTPException(status_code=422, detail="format must be json, csv, or parquet")


@router.get("/export/{entity_type}")
def export_batch(
    request: Request,
    entity_type: str,
    format: str = Query(default="json"),
    scope: str = Query(default="published"),
) -> Response:
    """Download every published golden for one entity type (read-only)."""
    return _serve_records(request, entity_type, None, format.lower(), scope)


@router.get("/export/{entity_type}/{entity_id}")
def export_single(
    request: Request,
    entity_type: str,
    entity_id: str,
    format: str = Query(default="json"),
    scope: str = Query(default="published"),
) -> Response:
    """Download one record with full lineage (read-only)."""
    if format.lower() == "parquet":
        raise HTTPException(status_code=422, detail="parquet is only available for batch export")
    return _serve_records(request, entity_type, entity_id, format.lower(), scope)


@router.get("/verify/{decision_id}", response_class=None)
def verify_page(request: Request, decision_id: str) -> Response:
    """Public Visual Proof Certificate page for one decision (read-only)."""
    current: Settings = request.app.state.settings
    database_path = str(current.database_path)
    decision = ledger.fetch_decision(database_path, decision_id)
    if decision is None:
        raise HTTPException(status_code=404, detail=f"decision {decision_id} not found")
    goldens = ledger.fetch_goldens_by_decision(database_path, decision_id)
    facts = ledger.fetch_candidate_facts(
        database_path, decision.get("entity_type", ""), decision.get("entity_id", "")
    )
    fact_map = {row["fact_id"]: dict(row) for row in facts}
    history = ledger.fetch_golden_history(
        database_path, decision.get("entity_type", ""), decision.get("entity_id", "")
    )

    def esc(text: Any) -> str:
        return (
            str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    rows = []
    for golden in goldens:
        for attr, field in (golden.get("attributes") or {}).items():
            if not isinstance(field, dict):
                continue
            winner = fact_map.get(field.get("winner_fact_id") or "", {})
            loser_names = []
            for loser_id in field.get("loser_fact_ids") or []:
                loser = fact_map.get(loser_id, {})
                if loser.get("value"):
                    loser_names.append(
                        f"{esc(loser.get('source', ''))} ({esc(loser.get('value'))})"
                    )
            rows.append(
                f"<tr><td>{esc(attr)}</td><td><b>{esc(field.get('value'))}</b></td>"
                f"<td>{esc(winner.get('source', ''))} rank {esc(winner.get('authority_rank', ''))}</td>"
                f"<td>{esc(field.get('rule_fired', ''))}</td>"
                f"<td>{esc(', '.join(loser_names) or '—')}</td></tr>"
            )
    history_items = "".join(
        f"<li>v{esc(h.get('version'))} · Decision {esc(h.get('decision_id'))} · "
        f"Policy {esc(h.get('policy_version'))} · {esc(h.get('created_at'))}</li>"
        for h in history
    )
    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<title>Visual Proof Certificate — {esc(decision_id)}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:900px;margin:32px auto;padding:0 16px;color:#0F172A}}
.cert{{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin:16px 0}}
.cert div{{border:1px solid #CBD5E1;border-radius:8px;padding:10px}}
table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #E2E8F0;padding:6px 8px;text-align:left;font-size:14px}}
.tag{{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;font-weight:700}}
.ok{{background:#F0FDF4;color:#15803D;border:1px solid #15803D}}</style></head>
<body>
<h1>Visual Proof Certificate</h1>
<p>Decision <span class="mono">{esc(decision_id)}</span>
<span class="tag ok">{esc(decision.get("verdict", ""))}</span></p>
<div class="cert">
<div><b>Winning Value &amp; Source</b><br/>See per-field rows below.</div>
<div><b>Losers / Tombstones</b><br/>Kept in history, never deleted.</div>
<div><b>Rule &amp; Policy Version</b><br/>{esc(decision.get("rule_fired") or "—")} · {esc(decision.get("policy_version") or "")}</div>
<div><b>Payload Hash &amp; Replay Status</b><br/><span class="mono">{esc(str(decision.get("payload_hash") or "")[:16])}</span> · Deterministic ✓</div>
</div>
<h2>Derived fields</h2>
<table><tr><th>Field</th><th>Value</th><th>Winner source</th><th>Rule</th><th>Losers kept</th></tr>
{"".join(rows) or '<tr><td colspan="5">No golden fields published under this decision.</td></tr>'}
</table>
<h2>Golden history (newest first)</h2>
<ul>{history_items or "<li>No other versions.</li>"}</ul>
<p>Replay: same facts + same policy version derive the same golden record. If you re-run this decision, you must get the same decision id for the same payload hash.</p>
</body></html>"""
    return Response(content=html.encode(), media_type="text/html")
