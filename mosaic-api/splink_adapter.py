"""Splink client — calls the standalone matching-engine (CSD 3.3, 12.7b).

Proposer only, never a decider. mosaic-api flattens fragments locally, POSTs
them to the service's /match with the policy's blocking/threshold, and merges
the returned candidates with Muse claims; the trust-engine still verifies
every hop (L1).

Routing (Dependency Inversion: orchestrator depends on the match interface,
not on Splink's Python API):
- disabled (SPLINK_ENABLED=false) -> unavailable, no matching at all;
- service_url set (three-container topology) -> HTTP POST /match;
- service_url empty (single-container topology) -> in-process fallback behind
  the same match interface (mosaic-api/splink_local.py, re-export of
  shared/splink_core.py — one implementation, two transports).

Fallback (S-04/S-05): service disabled, unreachable, slow, refusing, version
mismatch, or malformed output -> return status "unavailable" with an empty
candidate set, mark the run SPLINK_UNAVAILABLE, and let Muse + fuzzy propose
alone. Never crash, never publish on fallback alone, never hide the fallback
from the UI or GET /health.
"""

from __future__ import annotations

from typing import Any

STATUS_OK = "ok"
STATUS_UNAVAILABLE = "unavailable"

DEFAULT_TIMEOUT_SECONDS = 120.0

# Pinned versions (mirror requirements files). A mismatch between these and
# what the service reports fails the run: never silently use a mismatched
# matching engine.
EXPECTED_SPLINK_VERSION = "5.0.0"
EXPECTED_DUCKDB_VERSION = "2.0.0"


def _field_value(fields: dict[str, Any], *names: str) -> Any:
    """First non-empty field value across candidate names (None if absent)."""
    for name in names:
        field = fields.get(name)
        if isinstance(field, dict) and field.get("value") not in (None, ""):
            return field["value"]
    return None


def flatten_documents(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten documents to the fragment rows the matcher consumes.

    Columns: fragment_id, entity_type, name, email, phone, address,
    order_id, category. Missing fields become None (never empty string:
    two empty strings must not match).
    """
    rows = []
    for document in documents:
        fields = document.get("fields", {})
        rows.append(
            {
                "fragment_id": document.get("document_id", ""),
                "entity_type": document.get("entity_type", "user"),
                "name": _field_value(fields, "display_name", "name", "full_name", "reviewer"),
                "email": _field_value(fields, "email", "customer_email"),
                "phone": _field_value(fields, "phone", "customer_phone", "mobile"),
                "address": _field_value(fields, "address"),
                "order_id": _field_value(fields, "order_id", "order"),
                "category": _field_value(fields, "category"),
            }
        )
    return rows


def _unavailable(detail: str) -> dict[str, Any]:
    return {
        "status": STATUS_UNAVAILABLE,
        "clusters": [],
        "pairs": [],
        "detail": detail,
        "splink_version": None,
        "duckdb_version": None,
    }


def _check_versions(payload: dict[str, Any]) -> str | None:
    """Return a mismatch message, or None when versions match the pins."""
    service_splink = payload.get("splink_version")
    service_duckdb = payload.get("duckdb_version")
    if service_splink != EXPECTED_SPLINK_VERSION or service_duckdb != EXPECTED_DUCKDB_VERSION:
        return (
            f"SPLINK_UNAVAILABLE — version mismatch "
            f"(service Splink {service_splink}/DuckDB {service_duckdb} vs pinned "
            f"Splink {EXPECTED_SPLINK_VERSION}/DuckDB {EXPECTED_DUCKDB_VERSION}); "
            "fallback to Muse + fuzzy, never silently use a mismatched version"
        )
    return None


def _run_local(
    rows: list[dict[str, Any]],
    *,
    threshold: float,
    blocking: list[str],
    policy_version: str,
    work_id: str,
) -> dict[str, Any]:
    """Single-container fallback: verbatim matching logic, in-process."""
    try:
        from splink_local import run_match
    except ImportError as exc:
        return _unavailable(
            f"SPLINK_UNAVAILABLE — in-process matcher unavailable ({exc}); "
            "fallback to Muse + fuzzy"
        )
    out = run_match(rows, threshold=threshold, blocking=blocking)
    if out.get("status") != STATUS_OK:
        return _unavailable(out.get("detail", "in-process matcher failed"))
    try:
        from importlib import metadata as _metadata

        local_splink = _metadata.version("splink")
    except Exception:
        local_splink = "unknown"
    try:
        import duckdb as _duckdb

        local_duckdb = _duckdb.__version__
    except Exception:
        local_duckdb = "unknown"
    mismatch = _check_versions({"splink_version": local_splink, "duckdb_version": local_duckdb})
    if mismatch is not None:
        return _unavailable(mismatch)
    return {
        "status": STATUS_OK,
        "clusters": out.get("clusters", []),
        "pairs": out.get("pairs", []),
        "detail": out.get("detail", ""),
        "splink_version": local_splink,
        "duckdb_version": local_duckdb,
        "work_id": work_id,
        "policy_version": policy_version,
    }


def run_splink(
    rows: list[dict[str, Any]],
    *,
    enabled: bool,
    threshold: float,
    blocking: list[str],
    unique_id: str = "fragment_id",
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    service_url: str = "http://127.0.0.1:8081",
    policy_version: str = "unknown",
    work_id: str = "",
    known_policy_versions: list[str] | None = None,
) -> dict[str, Any]:
    """Match via the standalone engine, or the in-process fallback.

    Always returns a status-tagged result, never raises.
    Returns {"status", "clusters": [{cluster_id, member_ids}],
    "pairs": [{left, right, match_weight, match_probability, meets_threshold,
    blocking_rule, proposer}], "detail": str, "splink_version"?, "duckdb_version"?}.
    On any failure: status "unavailable" with the SPLINK_UNAVAILABLE fallback note.
    """
    _ = unique_id
    if not enabled:
        return _unavailable("SPLINK_UNAVAILABLE — fallback to Muse + fuzzy")
    if not rows:
        return {
            "status": STATUS_OK,
            "clusters": [],
            "pairs": [],
            "detail": "no input rows",
            "splink_version": None,
            "duckdb_version": None,
        }
    if not (service_url or "").strip():
        return _run_local(
            rows,
            threshold=threshold,
            blocking=blocking,
            policy_version=policy_version,
            work_id=work_id,
        )
    import httpx

    base = service_url.rstrip("/")
    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            response = client.post(
                f"{base}/match",
                json={
                    "fragments": rows,
                    "policy_version": policy_version,
                    "known_policy_versions": known_policy_versions or [],
                    "blocking": blocking or ["email", "phone", "order_id"],
                    "threshold": threshold,
                    "work_id": work_id,
                },
            )
    except Exception as exc:
        return _unavailable(
            f"SPLINK_UNAVAILABLE — matching-engine unreachable ({type(exc).__name__}); "
            "fallback to Muse + fuzzy"
        )
    if response.status_code != 200:
        return _unavailable(
            f"SPLINK_UNAVAILABLE — matching-engine refused (HTTP {response.status_code}); "
            "fallback to Muse + fuzzy"
        )
    try:
        payload = response.json()
        clusters = payload.get("clusters", [])
        pairs = payload.get("pairs", [])
        if not isinstance(clusters, list) or not isinstance(pairs, list):
            raise ValueError("bad /match shape")
        if payload.get("status") != STATUS_OK:
            return _unavailable(
                "SPLINK_UNAVAILABLE — matching-engine reported failure "
                f"({payload.get('detail', 'no detail')}); fallback to Muse + fuzzy"
            )
        mismatch = _check_versions(payload)
        if mismatch is not None:
            return _unavailable(mismatch)
        return {
            "status": STATUS_OK,
            "clusters": clusters,
            "pairs": pairs,
            "detail": str(payload.get("detail", "")),
            "splink_version": payload.get("splink_version"),
            "duckdb_version": payload.get("duckdb_version"),
            "work_id": payload.get("work_id", ""),
            "policy_version": payload.get("policy_version", policy_version),
        }
    except Exception as exc:
        return _unavailable(
            f"SPLINK_UNAVAILABLE — matching-engine output unusable ({exc}); "
            "fallback to Muse + fuzzy"
        )
