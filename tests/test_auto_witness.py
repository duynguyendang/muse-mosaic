"""The auto gate: AUTONOMY_MODE=auto refuses to start without a live witness."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from auto_witness import WitnessError, verify_auto_witness
from conftest import write_witness


def _mutate(path: Path, **changes) -> str:
    log = json.loads(path.read_text(encoding="utf-8"))
    log.update(changes)
    path.write_text(json.dumps(log), encoding="utf-8")
    return str(path)


def test_valid_witness_passes(tmp_path) -> None:
    summary = verify_auto_witness(write_witness(tmp_path))
    assert summary["judged_pairs"] == 18500
    assert summary["min_arm_precision"] == pytest.approx(0.999)


def test_missing_witness_refused(tmp_path) -> None:
    with pytest.raises(WitnessError):
        verify_auto_witness("")
    with pytest.raises(WitnessError):
        verify_auto_witness(str(Path(tmp_path) / "nope.json"))


def test_mock_witness_refused(tmp_path) -> None:
    log = {
        "harness": "scripts/eval_calibration.py",
        "date_utc": "20261106T000000Z",
        "git_sha": "0" * 40,
        "judge": "mock",
        "floor": 0.995,
        "model_id": "mock (deterministic, no model)",
        "datasets": [],
    }
    path = Path(tmp_path) / "mock.json"
    path.write_text(json.dumps(log), encoding="utf-8")
    with pytest.raises(WitnessError, match="live"):
        verify_auto_witness(str(path))


def test_below_floor_witness_refused(tmp_path) -> None:
    path = Path(write_witness(tmp_path))
    log = json.loads(path.read_text(encoding="utf-8"))
    log["datasets"][0]["arms"][0]["precision"] = 0.90
    log["datasets"][0]["best_precision"] = 0.90
    path.write_text(json.dumps(log), encoding="utf-8")
    with pytest.raises(WitnessError, match="below the floor"):
        verify_auto_witness(str(path))


def test_witness_without_judged_pairs_refused(tmp_path) -> None:
    path = Path(write_witness(tmp_path))
    log = json.loads(path.read_text(encoding="utf-8"))
    for arm in log["datasets"][0]["arms"]:
        arm["judged"] = 0
    path.write_text(json.dumps(log), encoding="utf-8")
    with pytest.raises(WitnessError):
        verify_auto_witness(str(path))


def test_wrong_harness_refused(tmp_path) -> None:
    path = Path(write_witness(tmp_path))
    _mutate(path, harness="scripts/eval_deepmatcher.py")
    with pytest.raises(WitnessError, match="harness"):
        verify_auto_witness(str(path))


def _settings_for_auto(tmp_path, **overrides):
    from config import Settings

    base = Settings(
        use_mock_muse=True,
        trust_engine_url="http://127.0.0.1:1/derive",
        database_path=str(Path(tmp_path) / "m.db"),
        enable_background_tasks=False,
        autonomy_mode="auto",
    )
    return dataclasses.replace(base, **overrides)


def test_validate_startup_refuses_auto_without_witness(tmp_path) -> None:
    from config import ConfigurationError

    settings = _settings_for_auto(tmp_path)
    with pytest.raises(ConfigurationError, match="auto"):
        settings.validate_startup()


def test_validate_startup_accepts_auto_with_witness(tmp_path) -> None:
    settings = _settings_for_auto(tmp_path, muse_auto_witness=write_witness(tmp_path))
    settings.validate_startup()


def test_validate_startup_refuses_auto_override_without_witness(tmp_path) -> None:
    from config import ConfigurationError

    settings = _settings_for_auto(tmp_path, autonomy_mode="off", autonomy_modes={"product": "auto"})
    with pytest.raises(ConfigurationError, match="auto"):
        settings.validate_startup()


def test_validate_startup_off_mode_never_requires_witness(tmp_path) -> None:
    settings = _settings_for_auto(tmp_path, autonomy_mode="off")
    settings.validate_startup()


def test_unjudged_datasets_are_excluded_not_passed(tmp_path) -> None:
    """A set where no pair reached the judge carries no floor evidence:
    excluded by name, never counted as a pass — and the witness still holds
    on the judgeable datasets."""
    path = Path(write_witness(tmp_path))
    log = json.loads(path.read_text(encoding="utf-8"))
    log["datasets"].append(
        {
            "dataset": "messy-customers",
            "entity_type": "user",
            "documents": 10666,
            "pairs_positive": 666,
            "floor": 0.995,
            "best_precision": None,
            "arms": [
                {
                    "arm": "M Muse (live, min_confidence=high)",
                    "confidence": "high",
                    "precision": 1.0,
                    "recall": 0.919,
                    "f1": 0.957,
                    "clusters": 10054,
                    "impure_clusters": 0,
                    "judged": 0,
                    "merged": 0,
                    "judge_calls": 0,
                    "cache_hits": 0,
                    "tokens": 0,
                }
            ],
        }
    )
    path.write_text(json.dumps(log), encoding="utf-8")
    summary = verify_auto_witness(str(path))
    assert "messy-customers" in summary["excluded_datasets"]
    assert summary["min_arm_precision"] == pytest.approx(0.999)


def test_all_unjudged_witness_refused(tmp_path) -> None:
    path = Path(write_witness(tmp_path))
    log = json.loads(path.read_text(encoding="utf-8"))
    for dataset in log["datasets"]:
        for arm in dataset["arms"]:
            arm["judged"] = 0
    path.write_text(json.dumps(log), encoding="utf-8")
    with pytest.raises(WitnessError):
        verify_auto_witness(str(path))
