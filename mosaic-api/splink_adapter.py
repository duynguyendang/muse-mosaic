"""Splink matcher adapter — proposer only, never a decider (CSD 3.3, 12.7b).

DuckDB backend in v1. Pipeline: flatten documents to a fragment table,
block on exact identifiers (policy splink.blocking), propose candidate
clusters via deterministic exact-identifier linkage (connected components
over blocked pairs), and attach EM match weights/probabilities best-effort
as candidate signals only.

Why deterministic primary: on demo-scale synthetic data EM estimation is
degenerate (singletons at any threshold), while exact email/phone/order
matches carry overwhelming evidence. The threshold in the policy file is a
demo parameter for the EM weights path, not a calibrated production
threshold. A high Splink score is a candidate signal; a low score is not a
rejection (exact proof found by Muse or exact SQL still publishes).

Fallback (S-04): if Splink is disabled, fails, or times out, return an empty
candidate set with status "unavailable", mark the run SPLINK_UNAVAILABLE,
and let Muse + fuzzy + vector propose alone. Never crash, never publish on
fallback alone, never hide the fallback from the UI or the ledger.
"""

from __future__ import annotations

import time
from typing import Any

STATUS_OK = "ok"
STATUS_UNAVAILABLE = "unavailable"


def flatten_documents(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten documents to the fragment table Splink consumes.

    Columns: fragment_id, entity_type, name, email, phone, address,
    order_id, category. Missing fields become None (never empty string:
    two empty strings must not match).
    """
    rows = []
    for document in documents:
        fields = document.get("fields", {})
        def value(*names: str) -> Any:
            for name in names:
                field = fields.get(name)
                if isinstance(field, dict) and field.get("value") not in (None, ""):
                    return field["value"]
            return None
        rows.append({
            "fragment_id": document.get("document_id", ""),
            "entity_type": document.get("entity_type", "user"),
            "name": value("display_name", "name", "full_name", "reviewer"),
            "email": value("email", "customer_email"),
            "phone": value("phone", "customer_phone", "mobile"),
            "address": value("address",),
            "order_id": value("order_id", "order"),
            "category": value("category",),
        })
    return rows


def _blocking_rules(blocking: list[str]) -> list[str]:
    columns = blocking or ["email", "phone", "order_id"]
    return [f"l.{column} = r.{column}" for column in columns]


def _comparisons():
    import splink.comparison_library as cl

    return [
        cl.ExactMatch("email"),
        cl.ExactMatch("phone"),
        cl.ExactMatch("order_id"),
        cl.JaroWinklerAtThresholds("name", [0.9, 0.7]),
        cl.JaroWinklerAtThresholds("address", [0.9, 0.7]),
    ]


def run_splink(
    rows: list[dict[str, Any]],
    *,
    enabled: bool,
    threshold: float,
    blocking: list[str],
    unique_id: str = "fragment_id",
    timeout_seconds: float = 120.0,
    max_pairs_u: int = 10000,
) -> dict[str, Any]:
    """Run Splink; always return a status-tagged result, never raise.

    Returns {"status", "clusters": [{cluster_id, member_ids}],
    "pairs": [{left, right, match_weight, match_probability}],
    "detail": str}. On any failure: status "unavailable", empty clusters.
    """
    if not enabled:
        return {
            "status": STATUS_UNAVAILABLE,
            "clusters": [],
            "pairs": [],
            "detail": "SPLINK_UNAVAILABLE — fallback to Muse + fuzzy",
        }
    if not rows:
        return {"status": STATUS_OK, "clusters": [], "pairs": [],
                "detail": "no input rows"}
    started = time.time()
    try:
        from splink.internals.duckdb.database_api import DuckDBAPI
        from splink import Linker

        blocking_rules = _blocking_rules(blocking)
        settings = {
            "link_type": "dedupe_only",
            "unique_id_column_name": unique_id,
            "comparisons": _comparisons(),
            "blocking_rules_to_generate_predictions": blocking_rules,
            "em_convergence": 0.001,
            "max_iterations": 20,
        }
        db_api = DuckDBAPI()
        sdf = db_api.register(rows)
        linker = Linker(sdf, settings, log_level=40)

        # Deterministic exact-identifier linkage: the cluster proposal.
        deterministic = linker.inference.deterministic_link()
        id_left, id_right = f"{unique_id}_l", f"{unique_id}_r"
        pair_keys: set[tuple[str, str]] = set()
        for record in deterministic.as_record_list():
            left, right = record.get(id_left), record.get(id_right)
            if left and right:
                pair_keys.add((min(left, right), max(left, right)))

        # EM weights best-effort, as candidate signals only.
        weights: dict[tuple[str, str], dict[str, Any]] = {}
        try:
            linker.training.estimate_u_using_random_sampling(max_pairs=max_pairs_u)
            linker.training.estimate_parameters_using_expectation_maximisation(
                blocking_rules[0]
            )
            scored = linker.inference.predict(threshold_match_probability=0.0)
            id_left, id_right = f"{unique_id}_l", f"{unique_id}_r"
            for record in scored.as_record_list():
                left, right = record.get(id_left), record.get(id_right)
                if left and right:
                    weights[(min(left, right), max(left, right))] = {
                        "match_weight": record.get("match_weight"),
                        "match_probability": record.get("match_probability"),
                    }
        except Exception:
            weights = {}

        clusters = _connected_components(
            [row["fragment_id"] for row in rows], pair_keys
        )
        pairs = [
            {
                "left": left,
                "right": right,
                "match_weight": (weights.get((left, right)) or {}).get("match_weight"),
                "match_probability": (weights.get((left, right)) or {}).get("match_probability"),
            }
            for left, right in sorted(pair_keys)
        ]
        elapsed = time.time() - started
        if elapsed > timeout_seconds:
            return {
                "status": STATUS_UNAVAILABLE,
                "clusters": [],
                "pairs": [],
                "detail": f"Splink timed out after {elapsed:.1f}s — fallback to Muse + fuzzy",
            }
        return {
            "status": STATUS_OK,
            "clusters": clusters,
            "pairs": pairs,
            "detail": f"{len(clusters)} clusters, {len(pairs)} pairs (deterministic exact linkage; EM weights best-effort)",
        }
    except Exception as exc:
        return {
            "status": STATUS_UNAVAILABLE,
            "clusters": [],
            "pairs": [],
            "detail": f"Splink failed ({type(exc).__name__}: {exc}) — fallback to Muse + fuzzy",
        }


def _connected_components(
    nodes: list[str], pairs: set[tuple[str, str]]
) -> list[dict[str, Any]]:
    """Deterministic connected components over linked pairs."""
    parent = {node: node for node in nodes}

    def find(node: str) -> str:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for left, right in pairs:
        if left in parent and right in parent:
            parent[find(left)] = find(right)
    groups: dict[str, list[str]] = {}
    for node in nodes:
        groups.setdefault(find(node), []).append(node)
    ordered = sorted((sorted(members) for members in groups.values()),
                     key=lambda members: members[0])
    return [
        {"cluster_id": f"splink-cluster-{index + 1}", "member_ids": members}
        for index, members in enumerate(ordered)
    ]
