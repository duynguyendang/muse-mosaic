"""Deterministic engine router.

SELECTION IS A RULE, NEVER A RESULT. Which matcher runs is a pure function
of dataset shape and identifier coverage — it cannot see golden counts,
benchmarks, or model scores, so no engine can be picked because it "wins"
on some labelled set. Engine shopping is structurally impossible here:

- exact-only  small datasets (< 200 docs, EM has nothing to learn), or
              identifier coverage >= 0.7 (emails/phones/orders suffice)
- splink      larger datasets with meaningful identifier coverage (>= 0.4):
              EM-weighted signals on top of exact linkage
- muse        larger datasets with sparse identifier coverage (< 0.4):
              names must be judged, identifiers cannot decide

Enforcement (``MUSE_ENGINE_ROUTER=enforce``) currently gates only the free,
deterministic Splink dimension. Muse is never turned on implicitly: it stays
double-gated by ``MUSE_CANDIDATE_MATCHING`` and the autonomy mode. The router
in *advisory* mode (default) only reports the recommendation in the build
summary. It never reads or writes labels, goldens, or benchmarks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_MIN_SCALE = 200  # below this, EM has nothing to learn from
_SPLINK_SCALE = 1000  # below this, identifier coverage alone picks the route
_COVERAGE_ENOUGH = 0.7
_COVERAGE_SPARSE = 0.4


@dataclass(frozen=True)
class RouteDecision:
    engine: str  # "exact" | "splink" | "muse"
    splink: bool
    recommend_muse: bool
    reason: str
    basis: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "splink": self.splink,
            "recommend_muse": self.recommend_muse,
            "reason": self.reason,
            "basis": self.basis,
        }


def _identifier_coverage(documents: list[dict[str, Any]], blocking: list[str]) -> float:
    if not documents:
        return 1.0
    if not blocking:
        return 0.0
    covered = 0
    for document in documents:
        fields = document.get("fields", {})
        for attribute in blocking:
            raw = fields.get(attribute)
            value = raw.get("value") if isinstance(raw, dict) else raw
            if value:
                covered += 1
                break
    return covered / len(documents)


def route_entity(
    documents: list[dict[str, Any]], blocking: list[str], entity_type: str = "user"
) -> RouteDecision:
    """Route one entity type's documents. Pure; no labels, no side effects."""
    n = len(documents)
    coverage = _identifier_coverage(documents, blocking)
    basis = {"entity_type": entity_type, "documents": n, "identifier_coverage": round(coverage, 4)}
    if n < _MIN_SCALE:
        return RouteDecision(
            engine="exact",
            splink=False,
            recommend_muse=False,
            reason=f"below {_MIN_SCALE} documents: EM has nothing to learn; exact linkage only",
            basis=basis,
        )
    if coverage >= _COVERAGE_ENOUGH:
        return RouteDecision(
            engine="exact",
            splink=False,
            recommend_muse=False,
            reason=(
                f"identifier coverage {coverage:.0%} >= {_COVERAGE_ENOUGH:.0%}: "
                "exact linkage suffices; Splink adds cost, not yield"
            ),
            basis=basis,
        )
    if n >= _SPLINK_SCALE and coverage < _COVERAGE_SPARSE:
        return RouteDecision(
            engine="muse",
            splink=True,
            recommend_muse=True,
            reason=(
                f"sparse identifiers ({coverage:.0%} < {_COVERAGE_SPARSE:.0%}) at scale "
                f"({n:,} docs): names must be judged"
            ),
            basis=basis,
        )
    return RouteDecision(
        engine="splink",
        splink=True,
        recommend_muse=False,
        reason=(
            f"moderate coverage ({coverage:.0%}) at scale ({n:,} docs): "
            "EM-weighted signals on top of exact linkage"
        ),
        basis=basis,
    )


def route_dataset(
    documents: list[dict[str, Any]], blocking_by_entity: dict[str, list[str]]
) -> RouteDecision:
    """Route a build; the most demanding entity decides.

    Muse-worthy > splink-worthy > exact-only. Under-serving one entity
    starves it, so ties resolve toward the richer path.
    """
    by_entity: dict[str, list[dict[str, Any]]] = {}
    for document in documents:
        by_entity.setdefault(document.get("entity_type", "user"), []).append(document)
    if not by_entity:
        return RouteDecision(
            engine="exact",
            splink=False,
            recommend_muse=False,
            reason="no documents",
            basis={"documents": 0},
        )
    decisions = [
        route_entity(docs, blocking_by_entity.get(etype, ["email", "phone", "order_id"]), etype)
        for etype, docs in sorted(by_entity.items())
    ]
    demand = {"exact": 0, "splink": 1, "muse": 2}
    best = max(decisions, key=lambda decision: demand[decision.engine])
    return RouteDecision(
        engine=best.engine,
        splink=any(decision.splink for decision in decisions),
        recommend_muse=any(decision.recommend_muse for decision in decisions),
        reason=f"most demanding entity decides: {best.reason}",
        basis={
            "documents": len(documents),
            "per_entity": [decision.to_dict() for decision in decisions],
        },
    )
