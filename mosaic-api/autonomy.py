"""Autonomy controller v1 (M3 / G6 slice).

The autonomy ladder advances per entity type, never as one global switch:

  off      Muse proposals are not applied at all for the entity
  shadow   Muse judges and records; nothing merges from its verdicts
  suggest  like shadow, plus the would-merge pairs are persisted as proposals
           (the G4 review queue): a human labels them accepted/rejected at
           /api/v1/suggestions — labels are recorded, never applied
  auto     Muse-merged clusters are applied as today (MUSE_CANDIDATE_MATCHING)

``AUTONOMY_MODE`` sets the build-wide default; ``AUTONOMY_MODE_<ENTITY>``
overrides it for one entity type (e.g. ``AUTONOMY_MODE_PRODUCT=auto``).
Legacy ``manual``/``supervised`` normalise to ``off``/``suggest``.

The process-local kill switch (``/api/v1/autonomy/freeze`` or
``AUTONOMY_KILL_SWITCH=true``) freezes every auto-apply without stopping
reads: an ``auto`` entity degrades to ``shadow`` and the report says so. A
restart always recovers, exactly like the engine kill switch.

Breakers are graded from the build's own per-entity Muse reports — no extra
model calls — and are **enforced**: when one fires, the build stops before
any golden is derived (see ``enforce_breakers``):

  zero_candidates_at_scale  >= 200 clusters produced 0 candidate pairs
  judge_failure_rate        judge failures exceeded 10% of judge calls
  entity_run_failed         the entity's Muse run failed outright (outage,
                            output rejected)

The build summary still reports the breakers for post-mortem; the report no
longer doubles as the only tripwire.

Audit sampling is deterministic (sha256 of the decision id): a 1% sample of
published decisions plus **every withheld decision** (steward queue) is
returned for replay. The ledger is the source of truth; this module writes
nothing.
"""

from __future__ import annotations

import hashlib
import logging
from time import perf_counter
from typing import Any

import muse_candidates as mc
from config import Settings

logger = logging.getLogger("mosaic-api.autonomy")

MODES = ("off", "shadow", "suggest", "auto")
_LEGACY = {"manual": "off", "supervised": "suggest"}

ZERO_WARN_CLUSTERS = 200  # same family as ZERO_CANDIDATE_WARN_CLUSTERS
JUDGE_FAILURE_LIMIT = 0.10


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------


def normalize_mode(raw: Any) -> str:
    if raw is None:
        return "off"
    value = str(raw).strip().lower()
    value = _LEGACY.get(value, value)
    if value not in MODES:
        raise ValueError(f"autonomy mode must be one of {MODES}, got {raw!r}")
    return value


def resolve_mode(settings: Settings, entity_type: str | None) -> str:
    """The mode for one entity type: override, else build-wide, else off."""
    modes = getattr(settings, "autonomy_modes", None) or {}
    entity = (entity_type or "").strip().lower()
    if entity and entity in modes:
        return normalize_mode(modes[entity])
    return normalize_mode(getattr(settings, "autonomy_mode", "off"))


def resolve_modes(settings: Settings, entity_types: list[str]) -> dict[str, str]:
    return {entity: resolve_mode(settings, entity) for entity in sorted(set(entity_types))}


def is_frozen(settings: Settings, app_state: Any | None = None) -> bool:
    """Kill switch: env (AUTONOMY_KILL_SWITCH) or the runtime flag on app.state."""
    if app_state is not None and bool(getattr(app_state, "autonomy_frozen", False)):
        return True
    return bool(getattr(settings, "autonomy_kill_switch", False))


def effective_mode(settings: Settings, entity_type: str | None, *, frozen: bool) -> str:
    """The mode actually executed: ``auto`` demotes to ``shadow`` when frozen."""
    mode = resolve_mode(settings, entity_type)
    return "shadow" if frozen and mode == "auto" else mode


# ---------------------------------------------------------------------------
# Audit sampling (deterministic; ledger is the source of truth)
# ---------------------------------------------------------------------------

_MOD = 10_000  # 4-decimal sampling resolution


def _sample_threshold(rate: float) -> int:
    if rate <= 0.0:
        return 0
    return max(1, min(_MOD, int(round(rate * _MOD))))


def audit_sample(
    decision_ids: list[str],
    *,
    rate: float = 0.01,
    always: list[str] | None = None,
) -> list[dict[str, str]]:
    """Deterministic sample: `rate` fraction of published + all withheld ids.

    Sampling depends only on the id itself, never on list order, so the same
    build replays the same sample. Withheld decisions (the ``always`` list)
    are always included — those are the ones a steward opened.
    """
    if not 0.0 <= rate <= 1.0:
        raise ValueError(f"audit rate must be in [0, 1], got {rate}")
    threshold = _sample_threshold(rate)
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for did in dict.fromkeys(str(d) for d in (always or []) if d):
        seen.add(did)
        out.append({"decision_id": did, "reason": "withheld"})
    for did in sorted({str(d) for d in decision_ids if d}):
        if did in seen or threshold == 0:
            continue
        digest = int(hashlib.sha256(did.encode("utf-8")).hexdigest()[:8], 16) % _MOD
        if digest < threshold:
            seen.add(did)
            out.append({"decision_id": did, "reason": "sampled_auto"})
    out.sort(key=lambda entry: (entry["reason"], entry["decision_id"]))
    return out


def report_audit(
    goldens: list[dict[str, Any]],
    stewards: list[dict[str, Any]],
    *,
    rate: float = 0.01,
) -> list[dict[str, str]]:
    published = [g.get("decision_id") for g in goldens if isinstance(g, dict)]
    withheld = [s.get("decision_id") for s in stewards if isinstance(s, dict)]
    return audit_sample(published, rate=rate, always=withheld)


# ---------------------------------------------------------------------------
# Breakers, graded from the build's own report (no extra model calls)
# ---------------------------------------------------------------------------


def breaker_overrides(policy: Any) -> dict[str, Any]:
    """Per-entity M3 breaker knobs from policy ``thresholds.m3_breakers``.

    Anything missing or malformed falls back to the code constants (never to
    a looser bound than the policy declares — validation already rejects
    out-of-range values at load).
    """
    raw = (getattr(policy, "thresholds", None) or {}).get("m3_breakers") or {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    clusters = raw.get("zero_candidates_clusters")
    if isinstance(clusters, int) and not isinstance(clusters, bool) and clusters >= 1:
        out["zero_warn"] = clusters
    limit = raw.get("judge_failure_limit")
    if (
        isinstance(limit, (int, float))
        and not isinstance(limit, bool)
        and 0.0 < float(limit) <= 1.0
    ):
        out["judge_failure_limit"] = float(limit)
    return out


def evaluate_breakers(
    by_entity: dict[str, Any] | None,
    *,
    zero_warn: int = ZERO_WARN_CLUSTERS,
    judge_failure_limit: float = JUDGE_FAILURE_LIMIT,
    overrides: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Grade each entity's folded Muse report against the M3 breakers."""
    breakers: list[dict[str, Any]] = []
    overrides = overrides or {}
    for name, info in (by_entity or {}).items():
        if not isinstance(info, dict):
            continue
        override = overrides.get(name) or {}
        entity_zero_warn = override.get("zero_warn", zero_warn)
        entity_failure_limit = override.get("judge_failure_limit", judge_failure_limit)
        reason = str(info.get("skipped_reason") or "")
        if reason.startswith("autonomy "):
            # a deliberate off/shadow label set by the controller itself is
            # configuration, not a failure — breakers grade malfunction only
            continue
        if reason:
            breakers.append(
                {
                    "breaker": "entity_run_failed",
                    "entity_type": name,
                    "detail": reason[:300],
                }
            )
            continue
        clusters = int(info.get("clusters_in") or 0)
        candidate_pairs = int(info.get("candidate_pairs") or 0)
        if clusters >= entity_zero_warn and candidate_pairs == 0:
            breakers.append(
                {
                    "breaker": "zero_candidates_at_scale",
                    "entity_type": name,
                    "detail": f"0 candidate pairs from {clusters} clusters at scale",
                }
            )
        judge_calls = int(info.get("judge_calls") or 0)
        judge_failures = int(info.get("judge_failures") or 0)
        if judge_calls > 0 and judge_failures / judge_calls > entity_failure_limit:
            breakers.append(
                {
                    "breaker": "judge_failure_rate",
                    "entity_type": name,
                    "detail": (
                        f"{judge_failures}/{judge_calls} judge calls failed "
                        f"(> {entity_failure_limit:.0%})"
                    ),
                }
            )
    return breakers


class BreakerTripped(RuntimeError):
    """A build-halting M3 breaker fired; the build must stop.

    Carries the graded breakers so the job ledger / HTTP layer can record
    exactly which one fired. Never retried: raising the scale/failure
    budget and re-running is the operator's explicit act.
    """

    def __init__(self, breakers: list[dict[str, Any]]):
        self.breakers = list(breakers)
        detail = "; ".join(
            f"{b.get('entity_type')}: {b.get('breaker')} ({b.get('detail')})" for b in self.breakers
        )
        super().__init__(f"M3 breaker(s) tripped; build halted before derivation: {detail}")


def enforce_breakers(
    by_entity: dict[str, Any] | None,
    *,
    zero_warn: int = ZERO_WARN_CLUSTERS,
    judge_failure_limit: float = JUDGE_FAILURE_LIMIT,
    overrides: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Raise BreakerTripped if any breaker fires; else return the (empty) list."""
    breakers = evaluate_breakers(
        by_entity,
        zero_warn=zero_warn,
        judge_failure_limit=judge_failure_limit,
        overrides=overrides,
    )
    if breakers:
        raise BreakerTripped(breakers)
    return breakers


# ---------------------------------------------------------------------------
# Shadow mode: judge and report; nothing merges
# ---------------------------------------------------------------------------


def _base_info(mode: str, clusters_in: int, *, autonomy_mode: str, frozen: bool) -> dict[str, Any]:
    return {
        "enabled": True,
        "mode": mode,
        "clusters_in": clusters_in,
        "candidate_pairs": 0,
        "shortlisted": 0,
        "judged": 0,
        "merged": 0,
        "would_merge": 0,
        "clusters_out": clusters_in,
        "cache_hits": 0,
        "judge_calls": 0,
        "judge_failures": 0,
        "tokens": 0,
        "elapsed_s": 0.0,
        "skipped_reason": None,
        "merges": [],
        "merges_total": 0,
        "records_regrouped": 0,
        "tiers": {},
        "declined": [],
        "autonomy": {"mode": autonomy_mode, "frozen": frozen},
    }


async def shadow_scan(
    settings: Settings,
    clusters: list[dict[str, Any]],
    *,
    mode: str,
    frozen: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run the full Muse pipeline in observation mode.

    Same shortlist, same judge, same token accounting as the applied path —
    but merges are computed and discarded, never applied. Returns the
    clusters unchanged and a fold-compatible report.
    """
    started = perf_counter()
    info = _base_info(
        "mock" if settings.use_mock_muse else "live",
        len(clusters),
        autonomy_mode=mode,
        frozen=frozen,
    )
    pairs = mc.shortlist(
        clusters,
        max_pairs=settings.muse_candidate_max_pairs or None,
        per_cluster=settings.muse_candidate_per_cluster or None,
        blocking=settings.muse_blocking_mode,
        max_block=settings.muse_blocking_max_block or None,
        stats=info,
    )
    info["shortlisted"] = len(pairs)
    if not pairs:
        info["elapsed_s"] = round(perf_counter() - started, 1)
        return clusters, info
    try:
        verdicts = (
            mc.mock_judge(pairs)
            if settings.use_mock_muse
            else await mc.live_judge(settings, pairs, stats=info)
        )
    except Exception as exc:  # noqa: BLE001 - a model outage must degrade, never break, the build
        kind = getattr(exc, "kind", None)
        info["skipped_reason"] = (
            f"Muse unavailable: {type(exc).__name__}" + (f"[{kind}]" if kind else "") + f": {exc}"
        )
        info["elapsed_s"] = round(perf_counter() - started, 1)
        return clusters, info
    merges = mc.select_merges(pairs, verdicts, settings.muse_candidate_min_confidence)
    info["judged"] = len(verdicts)
    info["would_merge"] = len(merges)
    # suggest mode is the review-queue rung: the proposals are fully described
    # so the build can persist them as labels for a human (never applied)
    if mode == "suggest" and merges:
        info["held_merges"] = mc.held_merge_report(clusters, merges)
    info["elapsed_s"] = round(perf_counter() - started, 1)
    return clusters, info


def off_scan(
    clusters: list[dict[str, Any]], *, mode: str, frozen: bool
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Report the entity as not-run; Muse is not applied at all."""
    info = _base_info("off", len(clusters), autonomy_mode=mode, frozen=frozen)
    info["enabled"] = False
    info["skipped_reason"] = f"autonomy {mode}: Muse not applied for this entity"
    return clusters, info
