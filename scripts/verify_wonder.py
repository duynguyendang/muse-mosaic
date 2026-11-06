#!/usr/bin/env python3
"""Verify the pinned WONDER_MODEL_ID live (CSD A-19) and log the evidence.

Three calls, exactly the two governance positions Wonder is mounted on:

  1. Policy Copilot, in-vocabulary: one instruction -> closed PolicySpec ->
     deterministic compile -> `mkit eval` verdict (local, free).
  2. Policy Copilot, out-of-vocabulary: a time-window instruction Wonder
     MUST refuse (`unsupported`) — the closed vocabulary holds live.
  3. Escalation adjudication: `wonder_adjudicate` returns a steward note
     (never a publish, never a winner).

Run log: docs/runs/<ts>-wonder-<sha>.json — SHA, date, model ids, per-call
status, usage tokens, and outputs. Exit 0 only when all three behave:

    set -a && . ./.env && set +a
    python3 scripts/verify_wonder.py

Exit codes: 0 verified · 1 a call misbehaved · 2 not applicable here ·
3 no Wonder id / no API key configured.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mosaic-api"))
sys.path.insert(0, str(ROOT))

IN_VOCABULARY = "treat crm as tier 3 and treat marketplace-like as tier 2"
OUT_OF_VOCABULARY = "expire every fact 24 hours after it arrives"

ADJUDICATION_EVIDENCE = {
    "decision_id": "wonder-verify-escalation",
    "attribute": "phone",
    "candidates": [
        {"value": "+84901234567", "source": "sap-like", "authority_rank": 1, "verified": True},
        {"value": "+84907654321", "source": "ticket", "authority_rank": 3, "verified": False},
    ],
    "fallback": "do not use the disputed value until a steward decides",
}


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:  # noqa: BLE001 - a missing sha is a logged unknown, not a crash
        return "unknown"


def _git_dirty() -> bool | None:
    try:
        out = subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
        )
        return bool(out.strip())
    except Exception:  # noqa: BLE001
        return None


async def _run(settings) -> list[dict[str, Any]]:
    import meta_tooling
    import policy_copilot

    calls: list[dict[str, Any]] = []

    # 1. Copilot, in vocabulary: Wonder maps, the engine verifies.
    draft = await policy_copilot.draft(settings, IN_VOCABULARY, "retail_balanced")
    calls.append(
        {
            "name": "copilot_in_vocabulary",
            "instruction": IN_VOCABULARY,
            "status": draft.get("status"),
            "proposer": draft.get("proposer"),
            "usage": draft.get("usage"),
            "spec": draft.get("spec"),
            "diff": draft.get("diff"),
            "checks": draft.get("checks"),
            "applied": draft.get("applied"),
        }
    )

    # 2. Copilot, out of vocabulary: the closed vocabulary must hold live.
    refused = await policy_copilot.draft(settings, OUT_OF_VOCABULARY, "retail_balanced")
    calls.append(
        {
            "name": "copilot_out_of_vocabulary",
            "instruction": OUT_OF_VOCABULARY,
            "status": refused.get("status"),
            "proposer": refused.get("proposer"),
            "message": refused.get("message"),
            "usage": refused.get("usage"),
            "applied": refused.get("applied"),
        }
    )

    # 3. Escalation adjudication: a note for the steward, never a publish.
    note = await meta_tooling.wonder_adjudicate(
        settings,
        attribute="phone",
        entity_type="user",
        entity_id="WONDER-VERIFY-1",
        evidence=ADJUDICATION_EVIDENCE,
        wonder_model_id=settings.wonder_model_id,
    )
    calls.append(
        {
            "name": "escalation_adjudication",
            "status": note.get("status"),
            "model": note.get("model") or settings.wonder_model_id,
            "usage": note.get("usage"),
            "note": note.get("note"),
            "stronger_side": note.get("stronger_side"),
            "missing_evidence": note.get("missing_evidence"),
            "check_first": note.get("check_first"),
        }
    )
    return calls


def main() -> int:
    from config import Settings

    settings = dataclasses.replace(Settings.from_env(), use_mock_muse=False)
    if not settings.wonder_model_id.strip():
        print("WONDER_MODEL_ID is not set; refusing to 'verify' a stub.", file=sys.stderr)
        return 3
    if not settings.meta_model_api_key.strip():
        print("META_MODEL_API_KEY is not set; cannot verify live.", file=sys.stderr)
        return 3

    calls = asyncio.run(_run(settings))
    ok = (
        calls[0]["status"] == "compiled"
        and calls[0].get("proposer") == "wonder"
        and calls[1]["status"] == "unsupported"
        and calls[2]["status"] == "adjudicated"
    )
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    sha = _git_sha()
    record: dict[str, Any] = {
        "harness": "scripts/verify_wonder.py",
        "date_utc": stamp,
        "git_sha": sha,
        "git_dirty": _git_dirty(),
        "model_id": settings.wonder_model_id,
        "muse_model_id": settings.muse_model_id,
        "result": "verified" if ok else "failed",
        "calls": calls,
    }
    runs_dir = ROOT / "docs" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    path = runs_dir / f"{stamp}-wonder-{sha[:12]}.json"
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    for call in calls:
        print(f"  {call['name']}: {call['status']} ({call.get('proposer') or call.get('model')})")
    print(f"run log written: {path}")
    if not ok:
        print("NOT verified: one or more Wonder calls misbehaved; exit 1", file=sys.stderr)
        return 1
    print("Wonder verified live: Copilot mapping + closed-vocab refusal + adjudication.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
