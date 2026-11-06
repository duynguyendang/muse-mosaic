#!/usr/bin/env python3
"""Precision calibration for the Muse auto-apply threshold (M3).

Measures, per dataset and per confidence floor shipped to the judge
(``high`` and ``medium``), the numbers that decide whether Muse may ever
merge on its own:

  precision / recall / F1, impure clusters, candidate recall, judge
  economics (calls, cache hits, tokens, judged pairs)

Datasets with ground truth (union-find over the published pairs):

  amazon-google     Amazon vs Google product titles (1,167 positive pairs)
  dblp-acm-dirty    DBLP vs ACM papers (2,220 positive pairs)
  messy-customers   Kaggle synthetic-messy customers (666 duplicated ids)

Usage:

    python3 scripts/eval_calibration.py                    # mock judge, plumbing check
    python3 scripts/eval_calibration.py --judge live --run-log   # the floor witness

``--judge live`` needs META_MODEL_API_KEY / MUSE_MODEL_ID in .env. The mock
judge is not calibrated and OVER-MERGES on the product/publication suites; a
mock run never witnesses the floor. Exit codes:

  0  certificate: every requested dataset met the floor with a live arm
     (only with --judge live; the run log is the auto-apply witness)
  1  live run, at least one dataset did NOT meet the floor
  2  mock run completed; it measures the plumbing, never the floor
  3  preconditions missing (raw data absent)

The auto gate (``mosaic-api/auto_witness.py``) consumes the ``--run-log``
record. Judge the floor by precision at the shipped confidence, not by F1:
the auto threshold exists so a wrong merge never ships.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import dataclasses
import json
import sys
from datetime import UTC, datetime
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


async def calibrate(name: str, args: argparse.Namespace) -> dict:
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
    arms = []
    best_precision: float | None = None
    judged_total = 0
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
        arms.append(
            {
                "arm": f"M Muse ({args.judge}, min_confidence={confidence})",
                "confidence": confidence,
                "precision": scored["precision"],
                "recall": scored["recall"],
                "f1": scored["f1"],
                "clusters": scored["clusters"],
                "impure_clusters": scored["impure_clusters"],
                "judged": info["judged"],
                "merged": info["merged"],
                "judge_calls": info["judge_calls"],
                "cache_hits": info["cache_hits"],
                "tokens": info["tokens"],
            }
        )
        judged_total += int(info["judged"] or 0)
        # Floor evidence only from arms the judge was actually asked about:
        # an unjudged set's purity proves nothing about the judge.
        if info["judged"] > 0:
            best_precision = (
                scored["precision"]
                if best_precision is None
                else max(best_precision, scored["precision"])
            )
    print(
        f"floor = precision >= {args.floor:.1%} at a shipped confidence. "
        + (
            "The floor is witnessed by the live arm only; never read a mock row as pass/fail."
            if args.judge == "mock"
            else "This run is a floor witness if it exits 0."
        )
    )
    return {
        "dataset": name,
        "entity_type": entity_type,
        "documents": stats["documents"],
        "pairs_positive": stats["pairs_positive"],
        "floor": args.floor,
        "best_precision": best_precision,
        "judged_total": judged_total,
        "arms": arms,
    }


async def _amain(args: argparse.Namespace) -> int:
    names = list(ed.DATASETS) if args.dataset == "all" else [args.dataset]
    records = []
    skipped: list[str] = []
    for name in names:
        try:
            records.append(await calibrate(name, args))
        except FileNotFoundError as exc:
            print(f"skipping {name}: {exc}", file=sys.stderr)
            skipped.append(name)
    if not records:
        print("no datasets could be loaded; nothing to calibrate", file=sys.stderr)
        return 3
    if args.run_log:
        sha = ed._git_sha()
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        if args.judge == "live":
            settings = Settings.from_env()
            model_id = getattr(settings, "muse_model_id", "") or "unknown"
        else:
            model_id = "mock (deterministic, no model)"
        log = {
            "harness": "scripts/eval_calibration.py",
            "date_utc": stamp,
            "git_sha": sha,
            "git_dirty": ed._git_dirty(),
            "judge": args.judge,
            "floor": args.floor,
            "max_pairs": args.max_pairs,
            "model_id": model_id,
            "datasets": records,
        }
        runs_dir = ROOT / "docs" / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        slug = args.dataset if args.dataset != "all" else "all"
        path = runs_dir / f"{stamp}-calibration-{slug}-{args.judge}-{sha[:12]}.json"
        path.write_text(json.dumps(log, indent=2) + "\n", encoding="utf-8")
        print(f"\nrun log written: {path}")
    if args.judge == "mock":
        print("\nmock run: not a floor witness (use --judge live --run-log)")
        return 2
    failed = []
    excluded = []
    for record in records:
        if int(record.get("judged_total") or 0) == 0:
            # no pair reached the judge: no evidence, neither pass nor fail
            excluded.append(str(record["dataset"]))
            continue
        if record["best_precision"] is None or record["best_precision"] + 1e-12 < args.floor:
            failed.append(str(record["dataset"]))
    if failed:
        print(
            f"\nfloor NOT met (>= {args.floor:.1%}) on: {', '.join(failed)}; exit 1",
            file=sys.stderr,
        )
        return 1
    if skipped:
        print(
            f"\nfloor met, but datasets were skipped: {', '.join(skipped)}; exit 1",
            file=sys.stderr,
        )
        return 1
    if excluded:
        print(
            f"\nno pairs reached the judge on: {', '.join(excluded)} "
            "(excluded from the floor — no evidence, never counted as a pass)"
        )
    if all(int(record.get("judged_total") or 0) == 0 for record in records):
        print("\nno judgeable dataset was measured; exit 1", file=sys.stderr)
        return 1
    print(
        f"\nfloor met (>= {args.floor:.1%}) on every judgeable dataset; " "this run is the witness"
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", choices=[*ed.DATASETS, "all"], default="all")
    ap.add_argument("--judge", choices=["mock", "live"], default="mock")
    ap.add_argument("--max-pairs", type=int, default=0, help="cap judged pairs (0 = uncapped)")
    ap.add_argument(
        "--floor",
        type=float,
        default=0.995,
        help="precision floor for the witness (default 0.995)",
    )
    ap.add_argument(
        "--run-log",
        action="store_true",
        help="write a run record under docs/runs/ (required for the auto witness)",
    )
    args = ap.parse_args()
    try:
        return asyncio.run(_amain(args))
    except FileNotFoundError as exc:  # missing raw data
        print(f"preconditions missing: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
