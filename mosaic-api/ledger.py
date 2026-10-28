"""SQLite ledger access.

This module owns schema creation and read helpers used by the scaffold. The
publisher (and its idempotency gate) is not implemented yet; see publisher.py
and CSD 12.5. Everything here is exact-SQL, never vector nearest neighbour.
"""

from __future__ import annotations

import json
import sqlite3
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
    """Create the ledger schema if it does not exist."""
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    connection = connect(database_path)
    try:
        connection.executescript(schema)
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
                    created_at, updated_at
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


def fetch_decision(
    database_path: str | Path, decision_id: str
) -> dict[str, Any] | None:
    """Return a decision row by exact decision id (never by vector search)."""
    connection = connect(database_path)
    try:
        row = connection.execute(
            "SELECT * FROM decisions WHERE decision_id = ?", (decision_id,)
        ).fetchone()
    finally:
        connection.close()
    return dict(row) if row is not None else None


def fetch_decision_by_key(
    database_path: str | Path, idempotency_key: str
) -> dict[str, Any] | None:
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


def fetch_goldens_by_decision(
    database_path: str | Path, decision_id: str
) -> list[dict[str, Any]]:
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


def fetch_stewards_by_decision(
    database_path: str | Path, decision_id: str
) -> list[dict[str, Any]]:
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


def update_steward_status(
    database_path: str | Path, steward_id: str, status: str
) -> bool:
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


def fetch_steward(
    database_path: str | Path, steward_id: str
) -> dict[str, Any] | None:
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


def insert_agent_trace(connection, decision_id: str, kind: str,
                       payload: dict[str, Any], created_at: str) -> None:
    """Insert one agent trace row on an already-open connection (no commit)."""
    connection.execute(
        """
        INSERT OR REPLACE INTO agent_traces
          (decision_id, kind, payload, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (decision_id, kind, json.dumps(payload, ensure_ascii=False), created_at),
    )


def fetch_agent_traces(
    database_path: str | Path, decision_id: str
) -> list[dict[str, Any]]:
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


