"""Scheduled hygiene: orphans, stale facts, duplicates + overdue stewards.

Wires the three graph analysers (previously defined but never called) into
an unattended run, plus an overdue-steward scan. Findings become steward
items with content-derived ids, so re-runs are no-ops (INSERT OR IGNORE).
Nothing here publishes, derives, or changes a verdict — it only surfaces
work for stewards. Overdue deputies are notified via the steward webhook;
without one, the digest is logged once per item (never spammed).
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import ledger
from config import Settings
from graph import analyse_duplicates, analyse_orphans, analyse_stale, fetch_graph
from schemas import CandidateFact, ProposedLink

logger = logging.getLogger("mosaic-api")

# Mirrors derive.go timeBase: whole minutes since 2026-01-01T00:00+07:00.
_TRUST_BASE = datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=7)))

ANALYSER_DECISION_ID = "analyser"
OVERDUE_KIND_PREFIX = "overdue-notified:"


def _finding_id(kind: str, entity_type: str, entity_id: str, key: str) -> str:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
    return f"st-analyser-{kind}-{entity_type}-{entity_id}-{digest}"


def _now_min(now: datetime) -> int:
    return int((now.timestamp() - _TRUST_BASE.timestamp()) // 60)


def _row_to_fact(row: dict[str, Any]) -> CandidateFact:
    return CandidateFact(
        fact_id=row["fact_id"],
        entity_type=row["entity_type"],
        entity_id=row["entity_id"],
        attribute=row["attribute"],
        value=row["value"],
        source=row["source"],
        source_id=row["source_id"],
        authority_rank=row["authority_rank"],
        event_time=row["event_time"],
        verified=bool(row["verified"]),
        ttl_seconds=row["ttl_seconds"],
    )


def _entity_links(database_path: str, entity_type: str, entity_id: str) -> list[ProposedLink]:
    """Rebuild link claims from persisted graph edges for orphan analysis."""
    _nodes, edges = fetch_graph(database_path, entity_type, entity_id)
    prefix = f"nd-value-{entity_type}-{entity_id}-"
    links: list[ProposedLink] = []
    for edge in edges:
        if edge.get("edge_type") not in ("proposed_link", "verified_link"):
            continue
        frm, to = str(edge.get("from_node_id", "")), str(edge.get("to_node_id", ""))
        if not frm.startswith(prefix) or not to.startswith(prefix):
            continue
        try:
            links.append(
                ProposedLink(
                    from_fact_id=frm[len(prefix) :],
                    to_fact_id=to[len(prefix) :],
                    proof=str(edge.get("proof") or "vector_only"),
                    proposer=str(edge.get("proposer") or "graph"),
                )
            )
        except ValueError:
            continue
    # Deduplicate: the same hop may be stored once per direction/decision.
    seen: set[tuple[str, str, str]] = set()
    unique: list[ProposedLink] = []
    for link in links:
        key = (
            min(link.from_fact_id, link.to_fact_id),
            max(link.from_fact_id, link.to_fact_id),
            link.proof,
        )
        if key not in seen:
            seen.add(key)
            unique.append(link)
    return unique


def analyse_entity(
    database_path: str, entity_type: str, entity_id: str, now: datetime
) -> dict[str, Any]:
    """Run the three analysers for one entity; return findings (no writes)."""
    facts = [
        _row_to_fact(row)
        for row in ledger.fetch_candidate_facts(database_path, entity_type, entity_id)
    ]
    links = _entity_links(database_path, entity_type, entity_id)
    orphans = analyse_orphans(facts, links)
    stale = analyse_stale(facts, _now_min(now))
    duplicates = analyse_duplicates(facts)
    deadline = (now + timedelta(hours=24)).isoformat()
    items: list[dict[str, Any]] = []
    for fact_id in orphans:
        fact = next(f for f in facts if f.fact_id == fact_id)
        items.append(
            {
                "steward_id": _finding_id("orphan", entity_type, entity_id, fact_id),
                "entity_type": entity_type,
                "entity_id": entity_id,
                "attribute": fact.attribute,
                "owner": "steward-001",
                "deputy": "steward-002",
                "deadline": deadline,
                "fallback": (
                    f"fragment {fact_id} is in no cluster: confirm it belongs "
                    "to this entity or remove it; do not merge on similarity alone"
                ),
                "status": "pending",
                "decision_id": ANALYSER_DECISION_ID,
                "suggested_action": None,
            }
        )
    for fact_id in stale:
        fact = next(f for f in facts if f.fact_id == fact_id)
        items.append(
            {
                "steward_id": _finding_id("stale", entity_type, entity_id, fact_id),
                "entity_type": entity_type,
                "entity_id": entity_id,
                "attribute": fact.attribute,
                "owner": "steward-001",
                "deputy": "steward-002",
                "deadline": deadline,
                "fallback": (
                    f"fragment {fact_id} is past its TTL: re-verify the value "
                    "or let it stay expired; do not act on it downstream"
                ),
                "status": "pending",
                "decision_id": ANALYSER_DECISION_ID,
                "suggested_action": None,
            }
        )
    for group in duplicates:
        attribute = next(f.attribute for f in facts if f.fact_id == group[0])
        items.append(
            {
                "steward_id": _finding_id(
                    "duplicate", entity_type, entity_id, ",".join(sorted(group))
                ),
                "entity_type": entity_type,
                "entity_id": entity_id,
                "attribute": attribute,
                "owner": "steward-001",
                "deputy": "steward-002",
                "deadline": deadline,
                "fallback": (
                    f"near-duplicate fragments {', '.join(sorted(group))} are not "
                    "provable enough to merge: steward decides, engine never guesses"
                ),
                "status": "pending",
                "decision_id": ANALYSER_DECISION_ID,
                "suggested_action": None,
            }
        )
    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "orphans": orphans,
        "stale": stale,
        "duplicates": duplicates,
        "items": items,
    }


def _is_overdue(item: dict[str, Any], now: datetime) -> bool:
    try:
        deadline = datetime.fromisoformat(str(item.get("deadline", "")))
    except ValueError:
        return False
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    return deadline < now


def _already_notified(database_path: str, item: dict[str, Any]) -> bool:
    kind = f"{OVERDUE_KIND_PREFIX}{item['steward_id']}"
    return any(
        trace.get("kind") == kind
        for trace in ledger.fetch_agent_traces(database_path, str(item.get("decision_id")))
    )


def run_analyser_once(settings: Settings, *, now: datetime | None = None) -> dict[str, Any]:
    """Analyse every entity, upsert findings, notify newly overdue stewards."""
    moment = now or datetime.now(UTC)
    database_path = settings.database_path
    inserted = 0
    findings: list[dict[str, Any]] = []
    for entity_type, entity_id in ledger.fetch_entities(database_path):
        report = analyse_entity(database_path, entity_type, entity_id, moment)
        findings.append({k: v for k, v in report.items() if k != "items"})
        inserted += ledger.upsert_steward_items(database_path, report["items"])
    overdue = [
        item
        for item in ledger.fetch_pending_stewards(database_path)
        if _is_overdue(item, moment) and not _already_notified(database_path, item)
    ]
    notified = 0
    if overdue:
        from notify import notify_overdue_digest

        sent = notify_overdue_digest(settings, overdue)
        # Mark items we surfaced so the next run does not repeat them.
        # Failed deliveries are NOT marked: they retry next run. Without a
        # webhook there is nothing to retry, so the log line is the notice.
        mark = sent or not settings.steward_webhook_url
        if mark:
            notified = len(overdue)
            for item in overdue:
                ledger.store_agent_trace(
                    database_path,
                    str(item.get("decision_id")),
                    f"{OVERDUE_KIND_PREFIX}{item['steward_id']}",
                    {"notified_at": moment.isoformat(), "deputy": item.get("deputy")},
                    moment.isoformat(),
                )
    logger.info(
        "analyser run: %d entities, %d new steward items, %d overdue (%d notified)",
        len(findings),
        inserted,
        len(overdue),
        notified,
    )
    return {
        "entities": len(findings),
        "findings": findings,
        "inserted": inserted,
        "overdue": [item["steward_id"] for item in overdue],
        "overdue_notified": notified,
    }
