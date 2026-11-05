#!/usr/bin/env python3
"""Ground-truth entity-resolution benchmark on the DeepMatcher suites.

The two datasets already in data/deepmatcher/ carry a complete labelled pair
set, so unlike the Kaggle samples there is a real truth to measure against:
nothing here is synthesised and nothing is sampled.

  amazon-google    1,363 + 3,226 rows, 11,460 labelled pairs, 1,167 positives
  dblp-acm-dirty   2,616 + 2,294 rows, 12,360 labelled pairs, 2,220 positives

Each row of tableA/tableB becomes one Mosaic fragment (entity_type product,
source = the table it came from); the labelled pairs give the truth clusters
by union-find. Metrics are the standard pairwise ones plus candidate recall,
which is the number that decides whether a judge can help at all: a golden
pair that blocking never proposes cannot be recovered by any judge.

Arms, all on the same documents:
  A  exact blocking on the RAW title
  B  exact blocking on the NORMALISED title (NFKD, casefold, punctuation off)
  T0 token blocking on any shared title token (the pipeline's own shortlist
     index, hot-token guard included) -- this is candidate generation
  T  T0 plus a string-similarity gate >= 0.9 (what rules can decide alone)
  M  Muse on top of B: exact blocking forms the clusters, the shortlist picks
     who is worth judging (strong-key split on maker/venue for big buckets),
     and the judge decides the middle band a threshold drops. Judged by the
     live model with --judge live, or by the deterministic mock with --judge
     mock; the mock returns 'unsure' for the middle band by construction, so
     only the live run measures the model.
  O  oracle over T0's candidate set: merge exactly the true pairs it proposed
     (upper bound on what any judge could recover from those candidates)

    python3 scripts/eval_deepmatcher.py --dataset amazon-google            # mock judge, free
    python3 scripts/eval_deepmatcher.py --dataset all --judge live         # needs .env
    python3 scripts/eval_deepmatcher.py --dataset all --judge live --min-confidence high
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import dataclasses
import io
import sys
import zipfile
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mosaic-api"))

import muse_candidates as mc  # noqa: E402  # the pipeline's own text normaliser
from config import Settings  # noqa: E402
from schema_contract import DEFAULT_SCHEMA_SPECS, get_schema_spec  # noqa: E402

DATA = ROOT / "data" / "deepmatcher"

# zip name, inner prefix, tableA/tableB source labels, fields, entity type
DATASETS: dict[str, dict] = {
    "amazon-google": {
        "zip": "amazon_google_exp_data.zip",
        "prefix": "",
        "sources": ("amazon", "google"),
        "fields": ("title", "manufacturer", "price"),
        "entity_type": "product",
    },
    "dblp-acm-dirty": {
        "zip": "dirty_dblp_acm_exp_data.zip",
        "prefix": "exp_data/",
        "sources": ("dblp", "acm"),
        "fields": ("title", "authors", "venue", "year"),
        "entity_type": "publication",
    },
}

SIM_THRESHOLD = 0.9  # string-rules arm: title similarity at or above this merges


def _rows(zf: zipfile.ZipFile, name: str) -> list[dict]:
    raw = zf.read(name).decode("utf-8", "replace")
    return list(csv.DictReader(io.StringIO(raw)))


def load(spec: dict) -> tuple[list[dict], dict[str, str], dict]:
    """Fragments (one per table row), truth[doc_id] -> truth cluster key, stats.

    Truth clusters come from union-find over every labelled pair with label=1,
    across train+valid+test: that is the full published pair set.
    """
    zf = zipfile.ZipFile(DATA / spec["zip"])
    prefix = spec["prefix"]
    table_a, table_b = _rows(zf, prefix + "tableA.csv"), _rows(zf, prefix + "tableB.csv")
    left_src, right_src = spec["sources"]

    docs: list[dict] = []
    for table, source, tag in ((table_a, left_src, "A"), (table_b, right_src, "B")):
        for row in table:
            fields = {}
            for field in spec["fields"]:
                value = (row.get(field) or "").strip()
                if value:
                    # title -> name: the attribute Mosaic's blocking vocabulary
                    # already knows (BLOCKING_COLUMNS), so the mapping is a
                    # rename, not a new engine concept.
                    # the schema's own primary label: `name` for a product,
                    # `title` for a publication -- cluster_profile reads
                    # canonical names, so emitting `name` for a paper would
                    # hand the judge an empty sheet.
                    key = (
                        get_schema_spec(spec["entity_type"]).primary_label.canonical_name
                        if field == "title"
                        else field
                    )
                    fields[key] = {"value": value}
            if not fields:
                continue
            docs.append(
                {
                    "document_id": f"{tag}-{row['id']}",
                    "entity_type": spec["entity_type"],
                    "source": source,
                    "fields": fields,
                }
            )

    parent = {d["document_id"]: d["document_id"] for d in docs}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    positives = 0
    pairs_seen = 0
    for split in ("train", "valid", "test"):
        for row in _rows(zf, prefix + f"{split}.csv"):
            pairs_seen += 1
            if (row.get("label") or "").strip() != "1":
                continue
            positives += 1
            left, right = f"A-{row['ltable_id']}", f"B-{row['rtable_id']}"
            if left in parent and right in parent:
                union(left, right)

    truth: dict[str, str] = {}
    for doc in docs:
        truth[doc["document_id"]] = find(doc["document_id"])

    stats = {
        "rows_a": len(table_a),
        "rows_b": len(table_b),
        "documents": len(docs),
        "pairs_labelled": pairs_seen,
        "pairs_positive": positives,
        "truth_clusters": len(set(truth.values())),
    }
    return docs, truth, stats


def title_of(doc: dict) -> str:
    """The label text being matched.

    Products label themselves ``name`` and publications ``title`` -- their
    schema's primary label, so a document that declares its entity reads its
    own contract. A bare document (no ``entity_type``) is matched against
    whichever declared label it actually carries, so a fixture that only says
    ``name`` still means what it always meant.
    """
    fields = doc.get("fields", {})
    primary = get_schema_spec(doc.get("entity_type")).primary_label.canonical_name
    label_fields = (primary,) + tuple(
        sorted(
            {
                spec.primary_label.canonical_name
                for spec in DEFAULT_SCHEMA_SPECS.values()
                if spec.primary_label.canonical_name != primary
            }
        )
    )
    for key in label_fields:
        raw = fields.get(key)
        value = raw.get("value") if isinstance(raw, dict) else raw
        if value:
            return str(value)
    return ""


def normalised(doc: dict) -> str:
    return mc._norm(title_of(doc))


def tokens_of(doc: dict) -> set[str]:
    return {t for t in normalised(doc).split() if len(t) >= 3}


def blocking_candidates(docs: list[dict], strategy: str) -> set[tuple[str, str]]:
    """Pairs a strategy would ever propose, before any judging happens."""
    candidates: set[tuple[str, str]] = set()

    def add(key: str, doc_id: str, bucket: dict[str, list[str]]) -> None:
        bucket.setdefault(key, []).append(doc_id)

    if strategy in ("exact_raw", "exact_norm", "tokens_any", "tokens"):
        if strategy in ("exact_raw", "exact_norm"):
            bucket: dict[str, list[str]] = {}
            for doc in docs:
                key = title_of(doc) if strategy == "exact_raw" else normalised(doc)
                if key:
                    add(key, doc["document_id"], bucket)
            for members in bucket.values():
                for i, a in enumerate(members):
                    for b in members[i + 1 :]:
                        candidates.add((a, b))
            return candidates

        # Token blocking: same index the pipeline's shortlist builds from
        # cluster_profile()["names"], including its hot-token guard.
        bucket = {}
        for doc in docs:
            for token in tokens_of(doc):
                add(token, doc["document_id"], bucket)
        if strategy == "tokens_any":
            for members in bucket.values():
                if len(members) > 50:
                    continue
                for i, a in enumerate(members):
                    for b in members[i + 1 :]:
                        candidates.add((a, b))
            return candidates

        by_id = {d["document_id"]: d for d in docs}
        for members in bucket.values():
            if len(members) > 50:  # hot token: matches the pipeline's own guard
                continue
            for i, a in enumerate(members):
                for b in members[i + 1 :]:
                    pair = (a, b)
                    if pair in candidates:
                        continue
                    # cheap prefilter, then the rules-style similarity score
                    if (
                        SequenceMatcher(None, normalised(by_id[a]), normalised(by_id[b])).ratio()
                        >= SIM_THRESHOLD
                    ):
                        candidates.add(pair)
        return candidates

    raise ValueError(f"unknown strategy {strategy!r}")


def clusters_from_pairs(docs: list[dict], pairs: set[tuple[str, str]]) -> list[list[str]]:
    parent = {d["document_id"]: d["document_id"] for d in docs}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in pairs:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    groups: dict[str, list[str]] = defaultdict(list)
    for doc in docs:
        groups[find(doc["document_id"])].append(doc["document_id"])
    return list(groups.values())


def pipeline_clusters(
    docs: list[dict], pairs: set[tuple[str, str]], entity_type: str
) -> list[dict]:
    """The same grouping, shaped the way propose_and_merge consumes clusters."""
    by_id = {d["document_id"]: d for d in docs}
    return [
        {
            "cluster_id": f"cluster-{i + 1}",
            "entity_type": entity_type,
            "member_docs": [by_id[doc_id] for doc_id in members],
        }
        for i, members in enumerate(clusters_from_pairs(docs, pairs))
    ]


def judged_doc_pairs(pairs: list[Any], base: list[dict]) -> set[tuple[str, str]]:
    """The document pairs the shortlist actually put in front of the judge.

    Candidate pairs index clusters, not documents, so a shortlisted cluster
    pair means every cross product of its members was shown to the judge.
    """
    out: set[tuple[str, str]] = set()
    for pair in pairs:
        for left in base[pair.left]["member_docs"]:
            for right in base[pair.right]["member_docs"]:
                out.add((left["document_id"], right["document_id"]))
    return out


def score(clusters: list[list[str]], truth: dict[str, str]) -> dict:
    """Pairwise precision/recall/F1 plus cluster purity."""
    predicted_pairs = sum(len(c) * (len(c) - 1) // 2 for c in clusters)

    truth_groups: dict[str, list[str]] = defaultdict(list)
    for doc_id, key in truth.items():
        truth_groups[key].append(doc_id)
    true_pairs = sum(len(v) * (len(v) - 1) // 2 for v in truth_groups.values())

    tp = 0
    impure = 0
    for members in clusters:
        by_truth: dict[str, int] = defaultdict(int)
        for doc_id in members:
            by_truth[truth[doc_id]] += 1
        tp += sum(n * (n - 1) // 2 for n in by_truth.values())
        if len(by_truth) > 1:
            impure += 1

    precision = tp / predicted_pairs if predicted_pairs else 0.0
    recall = tp / true_pairs if true_pairs else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {
        "clusters": len(clusters),
        "predicted_pairs": predicted_pairs,
        "true_pairs": true_pairs,
        "true_positives": tp,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "impure_clusters": impure,
    }


def candidate_recall(
    docs: list[dict], candidates: set[tuple[str, str]], truth: dict[str, str]
) -> tuple[float, int, int]:
    """Of the golden pairs, how many this blocking strategy even proposes."""
    golden = 0
    found = 0
    truth_groups: dict[str, list[str]] = defaultdict(list)
    for doc_id, key in truth.items():
        truth_groups[key].append(doc_id)
    for members in truth_groups.values():
        for i, a in enumerate(members):
            for b in members[i + 1 :]:
                golden += 1
                if (a, b) in candidates or (b, a) in candidates:
                    found += 1
    return (found / golden if golden else 0.0), found, golden


def build_settings(judge: str, min_confidence: str, max_pairs: int) -> Settings:
    """Judge mode and the merge floor.

    ``medium`` is the middle-band decision: ``same`` at medium confidence
    joins, ``low`` never does, in either mode.
    """
    settings = Settings.from_env() if judge == "live" else Settings()
    return dataclasses.replace(
        settings,
        use_mock_muse=judge == "mock",
        muse_candidate_min_confidence=min_confidence,
        muse_candidate_max_pairs=max_pairs,
    )


async def run(name: str, args: argparse.Namespace) -> None:
    spec = DATASETS[name]
    docs, truth, stats = load(spec)
    print(f"\n=== {name} ===")
    print(
        f"rows A={stats['rows_a']:,} B={stats['rows_b']:,} -> documents={stats['documents']:,} | "
        f"labelled pairs={stats['pairs_labelled']:,} (positive={stats['pairs_positive']:,}) | "
        f"truth clusters={stats['truth_clusters']:,} | entity={spec['entity_type']}"
    )

    strategies = (
        ("A  exact, raw title", "exact_raw"),
        ("B  exact, normalised title", "exact_norm"),
        ("T0 token block, any shared token", "tokens_any"),
        ("T  token block + similarity >= 0.9", "tokens"),
    )
    print(
        f"\n{'arm':38}{'cand recall':>14}{'found/golden':>14}"
        f"{'precision':>11}{'recall':>9}{'F1':>8}{'clusters':>10}{'impure':>8}"
    )
    pipeline_candidates: set[tuple[str, str]] = set()
    for label, strategy in strategies:
        candidates = blocking_candidates(docs, strategy)
        if strategy == "tokens_any":
            pipeline_candidates = candidates
        recall_c, found, golden = candidate_recall(docs, candidates, truth)
        stats_row = score(clusters_from_pairs(docs, candidates), truth)
        print(
            f"{label:38}{recall_c:>13.1%}{f'{found:,}/{golden:,}':>14}"
            f"{stats_row['precision']:>11.1%}{stats_row['recall']:>9.1%}"
            f"{stats_row['f1']:>8.1%}{stats_row['clusters']:>10,}{stats_row['impure_clusters']:>8,}"
        )

    # M: the Muse arm. Base clusters come from deterministic exact blocking,
    # the way the pipeline works; the shortlist decides who is worth judging
    # and the judge adds the links it is sure of.
    entity_type = spec["entity_type"]
    base_pairs = blocking_candidates(docs, "exact_norm")
    base = pipeline_clusters(docs, base_pairs, entity_type)
    settings = build_settings(args.judge, args.min_confidence, args.max_pairs)
    shortlist_stats: dict[str, Any] = {}
    shortlisted = mc.shortlist(
        base,
        max_pairs=settings.muse_candidate_max_pairs or None,
        per_cluster=settings.muse_candidate_per_cluster,
        blocking=settings.muse_blocking_mode,
        stats=shortlist_stats,
    )
    covered: set[tuple[str, str]] = set(base_pairs)
    for cluster in base:
        ids = [d["document_id"] for d in cluster["member_docs"]]
        covered.update((a, b) for i, a in enumerate(ids) for b in ids[i + 1 :])
    covered |= judged_doc_pairs(shortlisted, base)
    reach_recall, reach_found, reach_golden = candidate_recall(docs, covered, truth)

    merged, info = await mc.propose_and_merge(settings, base)
    muse_clusters = [[d["document_id"] for d in c["member_docs"]] for c in merged]
    muse = score(muse_clusters, truth)
    print(
        f"{'M  Muse on top of B (' + args.judge + ')':38}{reach_recall:>13.1%}"
        f"{f'{reach_found:,}/{reach_golden:,}':>14}"
        f"{muse['precision']:>11.1%}{muse['recall']:>9.1%}{muse['f1']:>8.1%}"
        f"{muse['clusters']:>10,}{muse['impure_clusters']:>8,}"
    )
    print(
        f"    base={len(base):,} clusters | shortlisted={info['shortlisted']:,} of "
        f"{shortlist_stats.get('candidate_pairs', 0):,} candidates "
        f"(split_buckets={shortlist_stats.get('split_buckets', 0):,}, "
        f"hot_skipped={shortlist_stats.get('hot_keys_skipped', 0):,}) | "
        f"judged={info['judged']:,} merged={info['merged']:,} | "
        f"calls={info['judge_calls']:,} cache_hits={info['cache_hits']:,} "
        f"tokens={info['tokens']:,} | min_confidence={args.min_confidence}"
        + (f" | {info['skipped_reason']}" if info.get("skipped_reason") else "")
    )

    # Oracle over T0's candidates: keep only the true pairs it proposed. This
    # is the ceiling on recall for any judge fed this candidate set.
    oracle_pairs = {
        (a, b)
        for a, b in pipeline_candidates
        if truth.get(a) is not None and truth.get(a) == truth.get(b)
    }
    oracle = score(clusters_from_pairs(docs, oracle_pairs), truth)
    recall_c, found, golden = candidate_recall(docs, pipeline_candidates, truth)
    print(
        f"{'O  oracle over T0 candidates':38}{recall_c:>13.1%}{f'{found:,}/{golden:,}':>14}"
        f"{oracle['precision']:>11.1%}{oracle['recall']:>9.1%}"
        f"{oracle['f1']:>8.1%}{oracle['clusters']:>10,}{oracle['impure_clusters']:>8,}"
    )
    print(
        f"\ngolden pairs = transitive closure of the {stats['pairs_positive']:,} labelled positives "
        f"= {golden:,}; a truth cluster of 3 rows contributes 3 pairs.\n"
        "M cand recall = golden pairs grouped by exact blocking OR shown to the judge; "
        "O is a ceiling (true pairs kept only), not a result."
    )


async def _amain(args: argparse.Namespace) -> int:
    missing = [
        name
        for name in (DATASETS if args.dataset == "all" else [args.dataset])
        if not (DATA / DATASETS[name]["zip"]).is_file()
    ]
    if missing:
        print(
            "missing raw data for: "
            + ", ".join(missing)
            + f"\nfetch into {DATA}/ (gitignored) — see docs/internal/Kaggle-test-dataset.md",
            file=sys.stderr,
        )
        return 1
    names = list(DATASETS) if args.dataset == "all" else [args.dataset]
    for name in names:
        await run(name, args)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", choices=[*DATASETS, "all"], default="all")
    ap.add_argument("--judge", choices=["mock", "live"], default="mock")
    ap.add_argument(
        "--min-confidence",
        choices=["high", "medium"],
        default="medium",
        help="merge floor: 'medium' lets the judge decide the middle band (default)",
    )
    ap.add_argument("--max-pairs", type=int, default=0, help="cap judged pairs (0 = uncapped)")
    args = ap.parse_args()
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
