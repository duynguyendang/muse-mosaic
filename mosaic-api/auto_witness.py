"""Witness verification for AUTONOMY_MODE=auto (M3 floor gate).

Auto-apply may only start on evidence that a *live* calibration run met the
precision floor — not on an F1, not on a mock judge, not on a claim. A
witness is a run log written by ``scripts/eval_calibration.py --judge live
--run-log``; it carries the SHA, the date, the model id, and the full
breakdown (precision, recall, F1, impure clusters, judged pairs, tokens).
Every dataset the judge was actually asked about must meet the floor;
datasets where no pair reached the judge carry no evidence and are excluded
(name recorded in ``excluded_datasets``), never counted as passes.

Verification is fail-closed: any defect (missing file, mock judge, unknown
model id, an arm below the floor, no judged pairs at all) refuses auto at
startup.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

WITNESS_HARNESS = "scripts/eval_calibration.py"


class WitnessError(RuntimeError):
    """The auto witness is missing or does not meet the floor."""


def _arm_precision(arm: Any) -> float | None:
    if not isinstance(arm, dict):
        return None
    precision = arm.get("precision")
    try:
        value = float(precision)
    except (TypeError, ValueError):
        return None
    return value


def verify_auto_witness(path: str | Path, floor: float = 0.995) -> dict[str, Any]:
    """Validate a calibration run log; return a summary or raise WitnessError."""
    witness_path = Path(path)
    if not str(path).strip():
        raise WitnessError(
            "AUTONOMY_MODE=auto requires MUSE_AUTO_WITNESS to point at a live "
            "calibration run log (scripts/eval_calibration.py --judge live --run-log)"
        )
    if not witness_path.is_file():
        raise WitnessError(f"MUSE_AUTO_WITNESS {witness_path} is not a file; refusing auto")
    try:
        log = json.loads(witness_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise WitnessError(f"MUSE_AUTO_WITNESS {witness_path} is not readable JSON: {exc}") from exc
    if not isinstance(log, dict):
        raise WitnessError(f"MUSE_AUTO_WITNESS {witness_path} must contain a JSON object")
    harness = str(log.get("harness") or "")
    if harness != WITNESS_HARNESS:
        raise WitnessError(
            f"witness harness must be {WITNESS_HARNESS!r}, got {harness!r}; refusing auto"
        )
    if str(log.get("judge") or "") != "live":
        raise WitnessError(
            f"witness must be a live-judge run (judge={log.get('judge')!r}); "
            "the mock judge is not a floor witness"
        )
    sha = str(log.get("git_sha") or "").strip()
    if not sha or sha == "unknown":
        raise WitnessError("witness carries no usable git_sha; refusing auto")
    model_id = str(log.get("model_id") or "").strip()
    if not model_id or "mock" in model_id.lower():
        raise WitnessError(f"witness model_id {model_id!r} is not a live model id; refusing auto")
    if not str(log.get("date_utc") or "").strip():
        raise WitnessError("witness carries no date_utc; refusing auto")
    try:
        recorded_floor = float(log.get("floor"))
    except (TypeError, ValueError) as exc:
        raise WitnessError("witness carries no numeric floor; refusing auto") from exc
    if recorded_floor + 1e-12 < floor:
        raise WitnessError(
            f"witness was calibrated at floor {recorded_floor}, below the required {floor}"
        )
    datasets = log.get("datasets")
    if not isinstance(datasets, list) or not datasets:
        raise WitnessError("witness carries no dataset results; refusing auto")
    best_overall: float | None = None
    judged_total = 0
    excluded: list[str] = []
    for entry in datasets:
        if not isinstance(entry, dict):
            raise WitnessError("witness dataset entries must be objects; refusing auto")
        name = str(entry.get("dataset") or "?")
        arms = entry.get("arms")
        if not isinstance(arms, list) or not arms:
            raise WitnessError(f"witness dataset {name!r} has no arms; refusing auto")
        best: float | None = None
        dataset_judged = 0
        for arm in arms:
            precision = _arm_precision(arm)
            judged = int(arm.get("judged") or 0) if isinstance(arm, dict) else 0
            dataset_judged += max(judged, 0)
            judged_total += max(judged, 0)
            if precision is None or judged <= 0:
                continue
            best = precision if best is None else max(best, precision)
        if best is None:
            # No judged arm: the judge was never asked (e.g. identifier-only
            # set with no middle band). That carries no floor evidence — it
            # neither passes nor fails the floor, it is excluded.
            if dataset_judged == 0:
                excluded.append(name)
                continue
            raise WitnessError(f"witness dataset {name!r} has no judged arm; refusing auto")
        if best + 1e-12 < floor:
            raise WitnessError(
                f"witness dataset {name!r} best arm precision {best:.4f} "
                f"is below the floor {floor}"
            )
        best_overall = best if best_overall is None else min(best_overall, best)
    if judged_total <= 0:
        raise WitnessError("witness records zero judged pairs; refusing auto")
    if best_overall is None:
        raise WitnessError("witness carries no judgeable dataset; refusing auto")
    return {
        "path": str(witness_path),
        "git_sha": sha,
        "date_utc": log["date_utc"],
        "model_id": model_id,
        "floor": floor,
        "datasets": [str(entry.get("dataset")) for entry in datasets],
        "excluded_datasets": excluded,
        "min_arm_precision": best_overall,
        "judged_pairs": judged_total,
    }
