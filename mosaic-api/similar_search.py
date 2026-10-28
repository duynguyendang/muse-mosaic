"""Hybrid similar search over the SQLite vector column (CSD 5.3a).

v1 implements steps 1 (SQL exact filter) and 3 (structured rerank) over
candidate facts of other entities of the same type. Step 2 (vector recall)
is deferred: no embedding model is pinned (README open question O-3), so no
vectors exist to compare and no query embedding can be produced. The response
says so explicitly instead of fabricating scores.

Order of authority is preserved: vector similarity, when it lands, will only
ever propose candidates; structured checks and the trust-engine decide.
"""

from __future__ import annotations

import re
from typing import Any

import ledger

_TOKEN = re.compile(r"[a-z0-9]+")


def tokens(value: str) -> set[str]:
    return set(_TOKEN.findall(value.lower()))


def similar(
    database_path: str,
    *,
    entity_type: str,
    entity_id: str,
    limit: int = 20,
) -> dict[str, Any]:
    """Return structured similar candidates with their match evidence."""
    connection = ledger.connect(database_path)
    try:
        rows = connection.execute(
            """
            SELECT * FROM candidate_facts
            WHERE entity_type = ? AND entity_id != ?
            ORDER BY entity_id, attribute
            """,
            (entity_type, entity_id),
        ).fetchall()
    finally:
        connection.close()

    own = ledger.fetch_candidate_facts(database_path, entity_type, entity_id)
    own_values = {row["attribute"]: row["value"] for row in own}

    scored: list[dict[str, Any]] = []
    for row in rows:
        fact_tokens = tokens(row["value"])
        evidence: dict[str, Any] = {
            "same_attribute": row["attribute"] in own_values,
            "shared_tokens": [],
            "authority_rank": row["authority_rank"],
            "verified": bool(row["verified"]),
        }
        score = 0.0
        if row["attribute"] in own_values:
            own_tokens = tokens(own_values[row["attribute"]])
            shared = sorted(fact_tokens & own_tokens)
            evidence["shared_tokens"] = shared
            union = fact_tokens | own_tokens
            score = (len(shared) / len(union)) if union else 0.0
            if row["value"] == own_values[row["attribute"]]:
                score += 1.0
        evidence["token_jaccard"] = round(score, 4)
        scored.append(
            {
                "fact_id": row["fact_id"],
                "attribute": row["attribute"],
                "value": row["value"],
                "source": row["source"],
                "authority_rank": row["authority_rank"],
                "verified": bool(row["verified"]),
                "event_time": row["event_time"],
                "evidence": evidence,
                "vector_score": None,
                "vector_note": (
                    "vector recall skipped: embedding model/version/dim are "
                    "not pinned (README O-3), so no vectors exist to compare"
                ),
                "_score": score,
            }
        )
    scored.sort(key=lambda item: (-item["_score"], item["fact_id"]))
    results = []
    for item in scored[:limit]:
        item.pop("_score", None)
        results.append(item)
    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "results": results,
        "vector_recall": "skipped — embedding not configured",
    }
