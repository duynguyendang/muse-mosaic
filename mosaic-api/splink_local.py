"""In-process Splink fallback — verbatim mirror of the matching-engine core.

Single-container topology: same match interface (same input shape, same output
shape) behind the HTTP-less function call. This file MUST stay identical in
behaviour to `matching-engine/splink_engine.py`; the Liskov parity test fails
the build if the two ever disagree on the same input. Only mosaic-api's
in-process fallback path imports this module (lazy import); the three-container
flow never calls it.
"""

from __future__ import annotations

import time
from typing import Any

BLOCKING_COLUMNS = ("email", "phone", "order_id", "category", "name")
SUPPORTED_COMPARISONS = ("exact", "jaro_winkler")
STATUS_OK = "ok"
STATUS_UNAVAILABLE = "unavailable"


def blocking_rules(blocking: list[str]) -> list[str]:
    columns = blocking or ["email", "phone", "order_id"]
    return [f"l.{column} = r.{column}" for column in columns]


def comparisons():
    import splink.comparison_library as cl

    return [
        cl.ExactMatch("email"),
        cl.ExactMatch("phone"),
        cl.ExactMatch("order_id"),
        cl.JaroWinklerAtThresholds("name", [0.9, 0.7]),
        cl.JaroWinklerAtThresholds("address", [0.9, 0.7]),
    ]


def connected_components(
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


def blocking_rule_for(
    rows_by_id: dict[str, dict[str, Any]], left: str, right: str
) -> str:
    """Which identifier(s) actually join this pair (evidence, not proof)."""
    left_row = rows_by_id.get(left, {})
    right_row = rows_by_id.get(right, {})
    shared = [
        column for column in ("email", "phone", "order_id")
        if left_row.get(column) and left_row.get(column) == right_row.get(column)
    ]
    return "+".join(shared)


def run_match(
    rows: list[dict[str, Any]],
    *,
    threshold: float,
    blocking: list[str],
    unique_id: str = "fragment_id",
    timeout_seconds: float = 120.0,
    max_pairs_u: int = 10000,
) -> dict[str, Any]:
    """Run Splink; always return a status-tagged result, never raise.

    Deterministic exact-identifier pairs are always reported as candidates:
    on demo-scale data EM estimation is degenerate (probabilities ~1e-4 for
    exact matches), so gating exact-proof pairs on the policy threshold would
    delete the cluster proposal the threshold is meant to score. Each pair
    therefore carries `meets_threshold` (probability unknown or at/above the
    policy threshold) alongside the raw EM numbers — the gate, not Splink,
    decides what publishes. A low score never rejects an exact proof (§3.3).
    """
    if not rows:
        return {"status": STATUS_OK, "clusters": [], "pairs": [],
                "detail": "no input rows"}
    started = time.time()
    try:
        from splink.internals.duckdb.database_api import DuckDBAPI
        from splink import Linker

        rules = blocking_rules(blocking)
        settings = {
            "link_type": "dedupe_only",
            "unique_id_column_name": unique_id,
            "comparisons": comparisons(),
            "blocking_rules_to_generate_predictions": rules,
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
                rules[0]
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

        # Threshold evaluation (policy demo parameter): each pair records
        # whether its EM probability is unknown or at/above threshold.
        # Deterministic exact pairs are always reported — on demo-scale data
        # EM is degenerate, so gating on it would delete the very clusters
        # the threshold is meant to score. The gate decides; Splink proposes.
        rows_by_id = {row["fragment_id"]: row for row in rows}
        clusters = connected_components(
            [row["fragment_id"] for row in rows], pair_keys
        )
        pairs = []
        for left, right in sorted(pair_keys):
            prob = (weights.get((left, right)) or {}).get("match_probability")
            pairs.append(
                {
                    "left": left,
                    "right": right,
                    "match_weight": (weights.get((left, right)) or {}).get("match_weight"),
                    "match_probability": prob,
                    "meets_threshold": prob is None or prob >= threshold,
                    "blocking_rule": blocking_rule_for(rows_by_id, left, right),
                    "proposer": "Splink",
                }
            )
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
