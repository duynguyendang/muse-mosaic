"""SQLite ledger access.

This module owns schema creation and read helpers used by the scaffold. The
publisher (and its idempotency gate) is not implemented yet; see publisher.py
and CSD 12.5. Everything here is exact-SQL, never vector nearest neighbour.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(database_path: str | Path) -> sqlite3.Connection:
    """Open the ledger with deterministic, local-first SQLite settings."""
    path = str(database_path)
    if path != ":memory:":
        Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def init_db(database_path: str | Path) -> None:
    """Create the ledger schema if it does not exist.

    Includes the idempotent v2.4 upgrade: content_sha256 on golden_records.
    Existing ledgers keep serving their rows (NULL hash = legacy, unverified
    but not rejected); new rows always carry the hash.
    """
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    connection = connect(database_path)
    try:
        connection.executescript(schema)
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(golden_records)").fetchall()
        }
        if "content_sha256" not in columns:
            connection.execute("ALTER TABLE golden_records ADD COLUMN content_sha256 TEXT")
        connection.commit()
    finally:
        connection.close()


def fetch_latest_golden(
    database_path: str | Path, entity_type: str, entity_id: str
) -> dict[str, Any] | None:
    """Return the latest golden record for an entity, or None."""
    connection = connect(database_path)
    try:
        row = connection.execute(
            """
            SELECT golden_id, entity_type, entity_id, version, attributes,
                    decision_id, policy_version, taxonomy_version,
                    content_sha256, created_at, updated_at
            FROM golden_records
            WHERE entity_type = ? AND entity_id = ?
            ORDER BY version DESC
            LIMIT 1
            """,
            (entity_type, entity_id),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    record = dict(row)
    record["attributes"] = json.loads(record["attributes"])
    return record


def fetch_decision(database_path: str | Path, decision_id: str) -> dict[str, Any] | None:
    """Return a decision row by exact decision id (never by vector search)."""
    connection = connect(database_path)
    try:
        row = connection.execute(
            "SELECT * FROM decisions WHERE decision_id = ?", (decision_id,)
        ).fetchone()
    finally:
        connection.close()
    return dict(row) if row is not None else None


def fetch_decision_by_key(database_path: str | Path, idempotency_key: str) -> dict[str, Any] | None:
    """Return a decision row by exact idempotency key, or None."""
    connection = connect(database_path)
    try:
        row = connection.execute(
            "SELECT * FROM decisions WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
    finally:
        connection.close()
    return dict(row) if row is not None else None


def fetch_goldens_by_decision(database_path: str | Path, decision_id: str) -> list[dict[str, Any]]:
    """Return all golden records written under one decision id."""
    connection = connect(database_path)
    try:
        rows = connection.execute(
            """
            SELECT golden_id, entity_type, entity_id, version, attributes,
                   decision_id, policy_version, taxonomy_version,
                   created_at, updated_at
            FROM golden_records WHERE decision_id = ? ORDER BY version
            """,
            (decision_id,),
        ).fetchall()
    finally:
        connection.close()
    records = []
    for row in rows:
        record = dict(row)
        record["attributes"] = json.loads(record["attributes"])
        records.append(record)
    return records


def fetch_golden_history(
    database_path: str | Path, entity_type: str, entity_id: str
) -> list[dict[str, Any]]:
    """Golden versions for one entity, newest first (Explain history view)."""
    connection = connect(database_path)
    try:
        rows = connection.execute(
            """
            SELECT version, decision_id, policy_version, taxonomy_version,
                   attributes, created_at
            FROM golden_records
            WHERE entity_type = ? AND entity_id = ?
            ORDER BY version DESC
            """,
            (entity_type, entity_id),
        ).fetchall()
    finally:
        connection.close()
    history = []
    for row in rows:
        record = dict(row)
        try:
            attributes = json.loads(record.get("attributes") or "{}")
        except ValueError:
            attributes = {}
        record["attributes"] = sorted(attributes.keys()) if isinstance(attributes, dict) else []
        history.append(record)
    return history


def fetch_stewards_by_decision(database_path: str | Path, decision_id: str) -> list[dict[str, Any]]:
    connection = connect(database_path)
    try:
        rows = connection.execute(
            "SELECT * FROM steward_queue WHERE decision_id = ? ORDER BY attribute",
            (decision_id,),
        ).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in rows]


def fetch_conflicts_by_decision(
    database_path: str | Path, decision_id: str
) -> list[dict[str, Any]]:
    connection = connect(database_path)
    try:
        rows = connection.execute(
            "SELECT * FROM conflicts WHERE decision_id = ? ORDER BY attribute",
            (decision_id,),
        ).fetchall()
    finally:
        connection.close()
    conflicts = []
    for row in rows:
        record = dict(row)
        record["fact_ids"] = json.loads(record["fact_ids"])
        conflicts.append(record)
    return conflicts


def fetch_candidate_facts(
    database_path: str | Path,
    entity_type: str,
    entity_id: str,
    fact_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Return candidate facts for an entity, optionally restricted by id."""
    connection = connect(database_path)
    try:
        if fact_ids:
            placeholders = ",".join("?" for _ in fact_ids)
            rows = connection.execute(
                f"SELECT * FROM candidate_facts WHERE fact_id IN ({placeholders})",
                tuple(fact_ids),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM candidate_facts WHERE entity_type = ? AND entity_id = ?",
                (entity_type, entity_id),
            ).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in rows]


def fetch_entities(database_path: str | Path) -> list[tuple[str, str]]:
    """Distinct (entity_type, entity_id) pairs present in candidate_facts."""
    connection = connect(database_path)
    try:
        rows = connection.execute(
            "SELECT DISTINCT entity_type, entity_id FROM candidate_facts "
            "ORDER BY entity_type, entity_id"
        ).fetchall()
    finally:
        connection.close()
    return [(row["entity_type"], row["entity_id"]) for row in rows]


def fetch_pending_stewards(
    database_path: str | Path,
    entity_type: str | None = None,
    entity_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return pending steward items, optionally scoped to one entity."""
    query = "SELECT * FROM steward_queue WHERE status = 'pending'"
    params: list[str] = []
    if entity_type is not None:
        query += " AND entity_type = ?"
        params.append(entity_type)
    if entity_id is not None:
        query += " AND entity_id = ?"
        params.append(entity_id)
    query += " ORDER BY deadline"
    connection = connect(database_path)
    try:
        rows = connection.execute(query, tuple(params)).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in rows]


def update_steward_status(database_path: str | Path, steward_id: str, status: str) -> bool:
    """Transition one steward item. Returns False when the id is unknown."""
    connection = connect(database_path)
    try:
        cursor = connection.execute(
            "UPDATE steward_queue SET status = ? WHERE steward_id = ?",
            (status, steward_id),
        )
        connection.commit()
        return cursor.rowcount > 0
    finally:
        connection.close()


def supersede_sibling_stewards(
    database_path: str | Path,
    entity_type: str,
    entity_id: str,
    attribute: str,
    keep_id: str,
) -> list[str]:
    """Mark stale duplicate questions as superseded.

    One steward decision answers the question for its (entity, attribute):
    other still-pending items asking the same thing (created by earlier
    derivations of the same conflict) are closed as superseded so the gap
    leaves every surface at once. The rows stay in the ledger for audit;
    only ``pending`` items drive the queue, the summary, and the UI.
    Returns the superseded steward ids.
    """
    connection = connect(database_path)
    try:
        rows = connection.execute(
            "SELECT steward_id FROM steward_queue "
            "WHERE status = 'pending' AND entity_type = ? AND entity_id = ? "
            "AND attribute = ? AND steward_id != ?",
            (entity_type, entity_id, attribute, keep_id),
        ).fetchall()
        ids = [row["steward_id"] for row in rows]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            connection.execute(
                f"UPDATE steward_queue SET status = 'superseded' WHERE steward_id IN ({placeholders})",
                tuple(ids),
            )
            connection.commit()
        return ids
    finally:
        connection.close()


def fetch_steward(database_path: str | Path, steward_id: str) -> dict[str, Any] | None:
    connection = connect(database_path)
    try:
        row = connection.execute(
            "SELECT * FROM steward_queue WHERE steward_id = ?", (steward_id,)
        ).fetchone()
    finally:
        connection.close()
    return dict(row) if row is not None else None


def store_agent_trace(
    database_path: str | Path,
    decision_id: str,
    kind: str,
    payload: dict[str, Any],
    created_at: str,
) -> None:
    """Append-only agent trace row (subagents / wonder / prompt_guard)."""
    connection = connect(database_path)
    try:
        with connection:
            insert_agent_trace(connection, decision_id, kind, payload, created_at)
    finally:
        connection.close()


def insert_agent_trace(
    connection, decision_id: str, kind: str, payload: dict[str, Any], created_at: str
) -> None:
    """Insert one agent trace row on an already-open connection (no commit)."""
    connection.execute(
        """
        INSERT OR REPLACE INTO agent_traces
          (decision_id, kind, payload, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (decision_id, kind, json.dumps(payload, ensure_ascii=False), created_at),
    )


def insert_tombstone(
    connection, tombstone_id: str, loser_id: str, golden_id: str, decision_id: str, reason: str
) -> None:
    """Tombstone one loser fact on an already-open connection (no commit).

    Losers are tombstoned, never deleted: the golden keeps its history.
    """
    connection.execute(
        """
        INSERT OR IGNORE INTO tombstones
          (tombstone_id, loser_fact_id, survivor_golden_id,
           decision_id, reason)
        VALUES (?, ?, ?, ?, ?)
        """,
        (tombstone_id, loser_id, golden_id, decision_id, reason),
    )


def fetch_agent_traces(database_path: str | Path, decision_id: str) -> list[dict[str, Any]]:
    """Return stored agent traces for one decision (Explain replay)."""
    connection = connect(database_path)
    try:
        rows = connection.execute(
            "SELECT kind, payload, created_at FROM agent_traces WHERE decision_id = ?",
            (decision_id,),
        ).fetchall()
    finally:
        connection.close()
    traces = []
    for row in rows:
        record = dict(row)
        record["payload"] = json.loads(record["payload"])
        traces.append(record)
    return traces


def fetch_tombstones_for_golden(database_path: str | Path, golden_id: str) -> list[dict[str, Any]]:
    """Return tombstone rows kept in history under one golden record."""
    connection = connect(database_path)
    try:
        rows = connection.execute(
            "SELECT * FROM tombstones WHERE survivor_golden_id = ? ORDER BY tombstone_id",
            (golden_id,),
        ).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Background jobs (autonomy): durable, idempotent build runs.
# ---------------------------------------------------------------------------

JOB_STATUSES = ("queued", "running", "published", "escalated", "denied", "failed", "cancelled")

JOB_TERMINAL = ("published", "escalated", "denied", "failed", "cancelled")


def create_job(
    database_path: str | Path,
    *,
    job_id: str,
    kind: str,
    idempotency_key: str,
    payload_hash: str,
    payload: dict[str, Any],
    created_at: str,
) -> dict[str, Any]:
    """Insert one job row. Raises sqlite3.IntegrityError on key reuse."""
    connection = connect(database_path)
    try:
        with connection:
            connection.execute(
                """
                INSERT INTO jobs
                  (job_id, kind, status, idempotency_key, payload_hash,
                   payload, attempts, tokens_used, created_at, updated_at)
                VALUES (?, ?, 'queued', ?, ?, ?, 0, 0, ?, ?)
                """,
                (
                    job_id,
                    kind,
                    idempotency_key,
                    payload_hash,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    created_at,
                    created_at,
                ),
            )
    finally:
        connection.close()
    job = fetch_job(database_path, job_id)
    assert job is not None
    return job


def fetch_job(database_path: str | Path, job_id: str) -> dict[str, Any] | None:
    connection = connect(database_path)
    try:
        row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    finally:
        connection.close()
    return _job_row(row)


def fetch_job_by_key(database_path: str | Path, idempotency_key: str) -> dict[str, Any] | None:
    connection = connect(database_path)
    try:
        row = connection.execute(
            "SELECT * FROM jobs WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
    finally:
        connection.close()
    return _job_row(row)


def _job_row(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    record = dict(row)
    record["payload"] = json.loads(record["payload"])
    if record.get("result") is not None:
        record["result"] = json.loads(record["result"])
    return record


def list_jobs(
    database_path: str | Path, status: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    query = "SELECT * FROM jobs"
    params: tuple = ()
    if status is not None:
        query += " WHERE status = ?"
        params = (status,)
    query += " ORDER BY created_at DESC LIMIT ?"
    connection = connect(database_path)
    try:
        rows = connection.execute(query, params + (limit,)).fetchall()
    finally:
        connection.close()
    return [record for record in (_job_row(row) for row in rows) if record is not None]


def claim_next_job(database_path: str | Path, now_iso: str) -> dict[str, Any] | None:
    """Atomically move one due queued job to running. Single-writer safe."""
    connection = connect(database_path)
    try:
        with connection:
            row = connection.execute(
                """
                SELECT job_id FROM jobs
                WHERE status = 'queued'
                  AND (next_run_at IS NULL OR next_run_at <= ?)
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (now_iso,),
            ).fetchone()
            if row is None:
                return None
            updated = connection.execute(
                "UPDATE jobs SET status = 'running', updated_at = ? "
                "WHERE job_id = ? AND status = 'queued'",
                (now_iso, row["job_id"]),
            ).rowcount
            if not updated:
                return None
            job_row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (row["job_id"],)
            ).fetchone()
    finally:
        connection.close()
    return _job_row(job_row)


def update_job(database_path: str | Path, job_id: str, now_iso: str, **fields: Any) -> None:
    """Update whitelisted job columns (status/attempts/tokens_used/result/error/next_run_at)."""
    allowed = {"status", "attempts", "tokens_used", "result", "error", "next_run_at"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"cannot update job columns: {sorted(unknown)}")
    values: dict[str, Any] = dict(fields)
    if "result" in values and values["result"] is not None:
        values["result"] = json.dumps(values["result"], ensure_ascii=False)
    assignments = ", ".join(f"{column} = ?" for column in values) + ", updated_at = ?"
    connection = connect(database_path)
    try:
        with connection:
            connection.execute(
                f"UPDATE jobs SET {assignments} WHERE job_id = ?",
                tuple(values.values()) + (now_iso, job_id),
            )
    finally:
        connection.close()


def upsert_steward_items(database_path: str | Path, items: list[dict[str, Any]]) -> int:
    """Insert steward items, ignoring ones already present (deterministic ids).

    Analyser findings use content-derived ids, so re-runs are no-ops.
    Returns the number of newly inserted rows.
    """
    if not items:
        return 0
    connection = connect(database_path)
    inserted = 0
    try:
        with connection:
            for item in items:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO steward_queue
                      (steward_id, entity_type, entity_id, attribute, owner,
                       deputy, deadline, fallback, status, decision_id,
                       suggested_action)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item["steward_id"],
                        item["entity_type"],
                        item["entity_id"],
                        item["attribute"],
                        item.get("owner", "steward-001"),
                        item.get("deputy"),
                        item["deadline"],
                        item["fallback"],
                        item.get("status", "pending"),
                        item["decision_id"],
                        item.get("suggested_action"),
                    ),
                )
                inserted += cursor.rowcount
    finally:
        connection.close()
    return inserted


def sum_job_tokens_since(database_path: str | Path, since_iso: str) -> int:
    """Total tokens_used by jobs created at/after an ISO timestamp."""
    connection = connect(database_path)
    try:
        row = connection.execute(
            "SELECT COALESCE(SUM(tokens_used), 0) AS total FROM jobs WHERE created_at >= ?",
            (since_iso,),
        ).fetchone()
    finally:
        connection.close()
    return int(row["total"])


def record_merge_proposal(
    database_path: str | Path,
    *,
    proposal_id: str,
    entity_type: str,
    left_cluster: str,
    right_cluster: str,
    left_docs: list[str],
    right_docs: list[str],
    confidence: str,
    tier: str,
    name_score: float | None,
    address_score: float | None,
    reason: str | None,
    model: str | None,
    policy_version: str | None,
    created_at: str,
) -> dict[str, Any]:
    """Record one suggested merge (idempotent on the unique key)."""
    connection = connect(database_path)
    try:
        with connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO merge_proposals
                  (proposal_id, entity_type, left_cluster, right_cluster,
                   left_docs, right_docs, confidence, tier,
                   name_score, address_score, reason, model, policy_version,
                   status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    proposal_id,
                    entity_type,
                    left_cluster,
                    right_cluster,
                    json.dumps(left_docs, ensure_ascii=False),
                    json.dumps(right_docs, ensure_ascii=False),
                    confidence,
                    tier,
                    name_score,
                    address_score,
                    reason,
                    model,
                    policy_version,
                    created_at,
                ),
            )
        row = connection.execute(
            "SELECT * FROM merge_proposals WHERE entity_type = ? AND left_cluster = ? AND right_cluster = ?",
            (entity_type, left_cluster, right_cluster),
        ).fetchone()
    finally:
        connection.close()
    return _proposal_row(row) if row else {"proposal_id": proposal_id, "status": "pending"}


def fetch_merge_proposals(
    database_path: str | Path,
    *,
    status: str | None = None,
    entity_type: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    connection = connect(database_path)
    try:
        clauses, params = [], []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if entity_type:
            clauses.append("entity_type = ?")
            params.append(entity_type)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = connection.execute(
            f"SELECT * FROM merge_proposals {where} ORDER BY created_at DESC LIMIT ?",
            (*params, int(limit)),
        ).fetchall()
    finally:
        connection.close()
    return [_proposal_row(row) for row in rows]


def fetch_merge_proposal(database_path: str | Path, proposal_id: str) -> dict[str, Any] | None:
    connection = connect(database_path)
    try:
        row = connection.execute(
            "SELECT * FROM merge_proposals WHERE proposal_id = ?", (proposal_id,)
        ).fetchone()
    finally:
        connection.close()
    return _proposal_row(row) if row else None


def decide_merge_proposal(
    database_path: str | Path,
    proposal_id: str,
    *,
    decision: str,
    resolved_by: str,
    note: str | None = None,
    resolved_at: str | None = None,
) -> dict[str, Any] | None:
    """Accept or reject a proposal exactly once. Returns the updated row.

    Returns ``None`` when the proposal does not exist; raises ValueError when
    it was already resolved (a decision id replay is never applied twice).
    """
    if decision not in ("accepted", "rejected"):
        raise ValueError(f"decision must be accepted or rejected, got {decision!r}")
    resolved_at = resolved_at or datetime.now(UTC).isoformat()
    connection = connect(database_path)
    try:
        with connection:
            cursor = connection.execute(
                """
                UPDATE merge_proposals
                SET status = ?, resolved_at = ?, resolved_by = ?, resolution_note = ?
                WHERE proposal_id = ? AND status = 'pending'
                """,
                (decision, resolved_at, resolved_by, note, proposal_id),
            )
        if cursor.rowcount == 0:
            existing = connection.execute(
                "SELECT status FROM merge_proposals WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
    finally:
        connection.close()
    if cursor.rowcount == 0:
        if existing is None:
            return None
        raise ValueError(
            f"proposal {proposal_id} is already {existing[0]}; a decision is applied once"
        )
    return fetch_merge_proposal(database_path, proposal_id)


def _proposal_row(row) -> dict[str, Any]:
    record = dict(row)
    for key in ("left_docs", "right_docs"):
        try:
            record[key] = json.loads(record.get(key) or "[]")
        except json.JSONDecodeError:
            record[key] = []
    return record
