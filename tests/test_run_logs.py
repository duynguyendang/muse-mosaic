"""Run-log discipline: every measured claim needs a reproducible record.

These tests do not re-run the benchmarks (the raw data is gitignored); they
hold the two invariants that keep the docs honest:

1. Anything under docs/runs/ is a complete run record — harness, date, SHA,
   model id. Eval records (``scripts/eval_*``) additionally carry per-arm
   precision/recall/F1 with the false-merge breakdown; verification records
   (``scripts/verify_*``) carry their per-call outcomes and usage instead.
2. The benchmark pages never smuggle a bare "measured (live)" claim again:
   the old, untraceable provenance phrasing is banned, and the pages must
   point at docs/runs/ or at the word "unlogged" for live-arm numbers.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "docs" / "runs"
DOC_PAGES = [ROOT / "README.md", ROOT / "docs" / "TEST-REPORT.md", ROOT / "docs" / "ROADMAP.md"]

REQUIRED_KEYS = {"harness", "date_utc", "git_sha", "model_id"}
ARM_METRIC_KEYS = {"precision", "recall", "f1"}


def _logs() -> list[Path]:
    return sorted(RUNS.glob("*.json")) if RUNS.is_dir() else []


def test_run_logs_are_complete_records() -> None:
    logs = _logs()
    assert logs, "no run logs under docs/runs/ — measured claims have no evidence"
    for path in logs:
        log = json.loads(path.read_text(encoding="utf-8"))
        missing = REQUIRED_KEYS - set(log)
        assert not missing, f"{path.name} missing keys: {sorted(missing)}"
        assert re.fullmatch(r"[0-9a-f]{40}", log["git_sha"]), f"{path.name}: bad sha"
        assert log["model_id"], f"{path.name}: empty model_id"
        harness = str(log["harness"])
        if harness.startswith("scripts/eval_"):
            # eval records: every measured arm carries the floor metrics
            for entry in log["datasets"]:
                assert entry.get("dataset"), f"{path.name}: dataset entry without name"
                arms = entry.get("arms")
                assert arms, f"{path.name}/{entry.get('dataset')}: no arms"
                for arm in arms:
                    missing_metrics = ARM_METRIC_KEYS - set(arm)
                    assert not missing_metrics, (
                        f"{path.name}/{entry.get('dataset')}/{arm.get('arm')}: "
                        f"missing metrics {sorted(missing_metrics)}"
                    )
        elif harness.startswith("scripts/verify_"):
            # verification records: live tool checks, each with an outcome
            calls = log.get("calls")
            assert calls, f"{path.name}: verification record without calls"
            for call in calls:
                assert call.get("name") and call.get(
                    "status"
                ), f"{path.name}: every verification call needs a name and a status"
        else:
            raise AssertionError(
                f"{path.name} has unknown harness {harness!r}; "
                "add a schema branch to tests/test_run_logs.py"
            )


def test_docs_do_not_claim_untraceable_live_provenance() -> None:
    banned = "measured (live, recorded run)"
    for page in DOC_PAGES:
        text = page.read_text(encoding="utf-8")
        assert banned not in text, f"{page.name} still carries the banned phrase {banned!r}"
        assert (
            "docs/runs/" in text or "unlogged" in text
        ), f"{page.name} cites no run-log evidence and no unlogged marker"
