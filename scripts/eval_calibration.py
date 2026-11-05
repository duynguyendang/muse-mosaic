#!/usr/bin/env python3
"""Precision calibration for the Muse auto-apply threshold (M3).

Measures, per dataset and per confidence floor shipped to the judge
(``high`` and ``medium``), the numbers that decide whether Muse may ever
merge on its own:

  precision / recall / F1, impure clusters, candidate recall, judge
  economics (calls, cache hits, tokens)

Datasets with ground truth (union-find over the published pairs):

  amazon-google     Amazon vs Google product titles (1,167 positive pairs)
  dblp-acm-dirty    DBLP vs ACM papers (2,220 positive pairs)
  messy-customers   Kaggle synthetic-messy customers (666 duplicated ids)

Usage:

    python3 scripts/eval_calibration.py                    # mock judge, free
    python3 scripts/eval_calibration.py --dataset dblp-acm-dirty --judge live
    python3 scripts/eval_calibration.py --judge live --max-pairs 200

``--judge live`` needs META_MODEL_API_KEY / MUSE_MODEL_ID in .env; mock is
deterministic and measures the plumbing, not the model. Judge the floor by
precision at the shipped confidence, not by F1: the auto threshold exists so
a wrong merge never ships.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import dataclasses
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mosaic-api"))
sys.path.insert(0, str(ROOT))

import muse_candidates as mc  # noqa: E402
from config import Settings  # noqa: E402

import scripts.eval_deepmatcher as ed  # noqa: E402

CONFIDENCES = ("high", "medium")


def _rows(name: str, args: argparse.Namespace):
    spec = ed.DATASETS[name]
    docs, truth, stats = ed.load(spec)
    entity_type = spec["entity_type"]
    # same base the pipeline gets: blocking + exact groups, judge on top
    pairs = ed.blocking_candidates(docs, spec.get("muse_base", "exact_norm"))
    base = ed.pipeline_clusters(docs, pairs, entity_type)
    return docs, truth, stats, entity_type, base


async def _run_arm(base: list[dict], confidence: str, judge: str, max_pairs: int):
    judge_live = judge == "live"
    settings = dataclasses.replace(
        Settings.from_env() if judge_live else Settings(),
        use_mock_muse=not judge_live,
        muse_candidate_min_confidence=confidence,
        muse_candidate_max_pairs=max_pairs,
    )
    merged, info = await mc.propose_and_merge(settings, copy.deepcopy(base))
    clusters = [[d["document_id"] for d in c["member_docs"]] for c in merged]
    return clusters, info


async def calibrate(name: str, args: argparse.Namespace) -> None:
    docs, truth, stats, entity_type, base = _rows(name, args)
    print(f"\n=== {name} ===")
    print(
        f"documents={stats['documents']:,} | positive pairs={stats['pairs_positive']:,} "
        f"| entity={entity_type} | base={len(base):,} clusters"
    )
    print(
        f"{'arm':34}{'precision':>11}{'recall':>9}{'F1':>8}"
        f"{'clusters':>10}{'impure':>8}{'judged':>8}{'merged':>8}{'tokens':>10}"
    )
    for confidence in CONFIDENCES:
        clusters, info = await _run_arm(base, confidence, args.judge, args.max_pairs)
        scored = ed.score(clusters, truth)
        note = "" if args.judge == "live" else "  [mock: not a floor witness]"
        print(
            f"{'M Muse (' + args.judge + ', min_confidence=' + confidence + ')':<34}"
            f"{scored['precision']:>11.1%}{scored['recall']:>9.1%}{scored['f1']:>8.1%}"
            f"{scored['clusters']:>10,}{scored['impure_clusters']:>8,}"
            f"{info['judged']:>8,}{info['merged']:>8,}{info['tokens']:>10,}"
            + note
            + (f" | {info['skipped_reason']}" if info.get("skipped_reason") else "")
        )
    floor_note = (
        "auto threshold = the lowest shipped confidence at which precision meets\n"
        "the floor (>= 99.5% for a live rollout). The floor is witnessed by the\n"
        "live arm only; the mock judge is not calibrated and OVER-MERGES on the\n"
        "product/publication suites (see above) — never read a mock row as a\n"
        "floor pass/fail."
        if args.judge == "mock"
        else "auto threshold = the lowest shipped confidence at which precision meets\n"
        "the floor (>= 99.5% for a live rollout)."
    )
    print(floor_note)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", choices=[*ed.DATASETS, "all"], default="all")
    ap.add_argument("--judge", choices=["mock", "live"], default="mock")
    ap.add_argument("--max-pairs", type=int, default=0, help="cap judged pairs (0 = uncapped)")
    args = ap.parse_args()
    names = list(ed.DATASETS) if args.dataset == "all" else [args.dataset]
    for name in names:
        asyncio.run(calibrate(name, args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
