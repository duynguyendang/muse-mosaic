"""One payload that answers: did the model help, and where?

Built from the build's own reports, so every number is something that ran in
that build. When a feature was off, or ran without a model, the payload says so
instead of implying the model did the work.
"""

from __future__ import annotations

from typing import Any

from config import Settings


def _who(settings: Settings, used_mock: bool) -> dict[str, Any]:
    return {
        "mode": "mock" if used_mock else "live",
        "model": None if used_mock else (settings.muse_model_id or None),
    }


def build(
    settings: Settings,
    candidates: dict[str, Any] | None,
    taxonomy: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "grouping": _grouping(settings, candidates),
        "taxonomy": _taxonomy(settings, taxonomy),
    }


def _grouping(settings: Settings, info: dict[str, Any] | None) -> dict[str, Any]:
    if not info:
        return {
            "status": "off",
            "note": "Candidate matching did not run: only exact identifiers (email, phone, "
            "order id) grouped records. Set MUSE_CANDIDATE_MATCHING=true to let Muse "
            "judge near-matches.",
        }
    skipped = info.get("skipped_reason")
    mock = info.get("mode") == "mock"
    # Muse runs once per entity type and reports each run under ``by_entity``:
    # the build's status is what actually happened overall, while the per-entity
    # reasons stay visible in ``skipped_reason``. One entity type failing must
    # neither erase the run that succeeded nor be reported as if it helped.
    runs = list((info.get("by_entity") or {}).values()) or [info]
    ran = any(not run.get("skipped_reason") for run in runs)
    status = ("mock" if mock else "live") if ran else "skipped"
    before, after = info.get("clusters_in"), info.get("clusters_out", info.get("clusters_in"))
    return {
        "status": status,
        **_who(settings, mock),
        "skipped_reason": skipped,
        "warning": info.get("warning"),
        "clusters_before": before,
        "clusters_after": after,
        "candidate_pairs": info.get("candidate_pairs", 0),
        "judged": info.get("judged", 0),
        "merged": info.get("merged", 0),
        "records_regrouped": info.get("records_regrouped", 0),
        "tiers": info.get("tiers", {}),
        "blocking": info.get("blocking"),
        "judge_calls": info.get("judge_calls", 0),
        "cache_hits": info.get("cache_hits", 0),
        "tokens": info.get("tokens", 0),
        "elapsed_s": info.get("elapsed_s", 0),
        "min_confidence": settings.muse_candidate_min_confidence,
        "merges": info.get("merges", []),
        "merges_total": info.get("merges_total", 0),
        "declined": info.get("declined", []),
    }


def _taxonomy(settings: Settings, ext: dict[str, Any] | None) -> dict[str, Any]:
    if not ext or ext.get("mode") in (None, "off"):
        return {
            "status": "off",
            "note": "Taxonomy extension did not run: a category the taxonomy does not "
            "contain is held. Set MUSE_TAXONOMY_PROPOSALS=muse to let Muse propose terms "
            "that the data then verifies.",
        }
    proposer = ext.get("proposer", ext.get("mode"))
    skipped = ext.get("skipped_reason")
    return {
        "status": "skipped" if skipped else ("live" if proposer == "muse" else "rules"),
        "proposer": proposer,
        "model": settings.muse_model_id or None if proposer == "muse" else None,
        "skipped_reason": skipped,
        "downgraded_reason": ext.get("downgraded_reason"),
        "unknown_values": ext.get("unknown_values", 0),
        "unknown_records": ext.get("unknown_records", 0),
        "accepted": ext.get("accepted", 0),
        "rejected": ext.get("rejected", 0),
        "version": ext.get("version"),
        "terms": ext.get("terms", []),
    }
