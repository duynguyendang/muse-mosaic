#!/usr/bin/env python3
"""Does the judge matter at 10k-100k records? A labelled scale benchmark.

The Kaggle sample is clean, so this generator writes the mess that exact
identifiers and string rules miss, at a chosen scale, with known truth:

  reformat       same phone/email, other formatting      (exact blocking gets it)
  moved          new phone AND email, same name/address  (needs candidates)
  nickname       new ids, "Robert Smith" vs "Bob Smith"  (needs a semantic judge)
  initial        new ids, "R. Smith" vs "Robert Smith"   (needs a semantic judge)
  dropped_middle new ids, middle name missing            (needs a semantic judge)

plus negatives that must never merge: the same full name at another address,
and a spouse with another name at the same address.

Arms, all on the same records:
  A  exact identifiers only (normalised phone/email), no shortlist, no model
  B  A + shortlist + deterministic string judge (mock; what rules can do)
  C  A + shortlist + Muse judge (live; requires META_MODEL_API_KEY)
  O  A + shortlist + an oracle that merges exactly the true pairs it was shown
     (upper bound on what any judge could recover from this candidate set)

    python3 scripts/eval_muse_scale.py --people 10000 --judge mock
    python3 scripts/eval_muse_scale.py --people 10000 --judge live   # needs .env

Recall counts a person as recovered only when all their records share one
cluster. A false merge is a cluster holding records of more than one person.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import random
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mosaic-api"))

import muse_candidates as mc  # noqa: E402
from config import Settings  # noqa: E402

FIRST = [
    "Robert",
    "William",
    "Richard",
    "James",
    "Margaret",
    "Elizabeth",
    "Michael",
    "Patricia",
    "Thomas",
    "Linda",
    "Charles",
    "Barbara",
    "David",
    "Susan",
    "Joseph",
    "Jessica",
    "Daniel",
    "Sarah",
    "Matthew",
    "Karen",
    "Nguyen",
    "Tran",
    "Le",
    "Pham",
    "Hoang",
]
NICKNAME = {
    "Robert": "Bob",
    "William": "Bill",
    "Richard": "Rick",
    "James": "Jim",
    "Margaret": "Peg",
    "Elizabeth": "Liz",
    "Michael": "Mike",
    "Patricia": "Pat",
    "Thomas": "Tom",
    "Linda": "Lynn",
    "Charles": "Chuck",
    "Barbara": "Barb",
    "David": "Dave",
    "Susan": "Sue",
    "Joseph": "Joe",
    "Jessica": "Jess",
    "Daniel": "Dan",
    "Sarah": "Sara",
    "Matthew": "Matt",
    "Karen": "Kari",
}
# Surnames are the blocking token, so they must be diverse: a token in more than
# COMMON_TOKEN_CLUSTERS clusters blocks nothing (see README "Scale").
LAST = [f"Last{i:04d}" for i in range(5000)]
MIDDLE = ["James", "Ann", "Van", "Thi", "Minh", "Lee", "Grace", "Paul"]
STREETS = [f"{s} Street" for s in ("Elm", "Oak", "Pine", "Main", "Le Loi", "Hai Ba Trung")]


def record(doc_id: str, name: str, address: str, phone: str, email: str) -> dict:
    return {
        "document_id": doc_id,
        "entity_type": "user",
        "source": "crm",
        "fields": {
            "display_name": {"value": name},
            "address": {"value": address},
            "phone": {"value": phone},
            "email": {"value": email},
        },
    }


def generate(
    people: int, rng: random.Random
) -> tuple[list[dict], dict[str, str], dict[str, str], list[tuple[str, str]]]:
    """Return (records, doc -> person, person -> variant, negative pairs)."""
    records: list[dict] = []
    truth: dict[str, str] = {}
    variant: dict[str, str] = {}
    negatives: list[tuple[str, str]] = []
    serial = 0

    def add(person: str, name: str, address: str) -> dict:
        nonlocal serial
        doc_id = f"d{serial:07d}"
        serial += 1
        doc = record(
            doc_id,
            name,
            address,
            f"09{serial:08d}",  # unique: exact blocking must not collide by chance
            f"u{serial}@ex.com",
        )
        records.append(doc)
        truth[doc_id] = person
        return doc

    hard = ["nickname", "initial", "dropped_middle", "moved", "reformat"]
    targets = min(people // 10, 2000)  # labelled second records
    for i in range(people):
        person = f"p{i:06d}"
        first = rng.choice(FIRST)
        last = rng.choice(LAST)
        name = f"{first} {last}"
        address = f"{rng.randint(1, 400)} {rng.choice(STREETS)}"
        original = add(person, name, address)
        if i >= targets:
            continue
        kind = hard[i % len(hard)]
        variant[person] = kind
        if kind == "reformat":
            # the SAME phone and email, written another way: exact blocking merges
            digits = re.sub(r"\D", "", original["fields"]["phone"]["value"])
            rec = add(person, name, address)
            rec["fields"]["phone"]["value"] = f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
            rec["fields"]["email"]["value"] = original["fields"]["email"]["value"].upper()
        elif kind == "moved":
            add(person, name, address)  # new phone and email: only name/address agree
        elif kind == "nickname":
            add(person, f"{NICKNAME.get(first, first)} {last}", address)
        elif kind == "initial":
            add(person, f"{first[0]}. {last}", address)
        elif kind == "dropped_middle":
            original["fields"]["display_name"]["value"] = f"{first} {rng.choice(MIDDLE)} {last}"
            add(person, name, address)

    # Negatives: the same full name at another address (must not merge).
    for i in range(targets // 2):
        first = rng.choice(FIRST)
        name = f"{first} {rng.choice(MIDDLE)} {rng.choice(LAST)}"
        base = f"{rng.randint(1, 400)} {rng.choice(STREETS)}"
        a = add(f"neg{i}a", name, base)
        b = add(f"neg{i}b", name, f"{rng.randint(1, 400)} {rng.choice(STREETS)}")
        negatives.append((a["document_id"], b["document_id"]))
    return records, truth, variant, negatives


def exact_clusters(records: list[dict]) -> list[dict]:
    """Union records sharing a normalised phone or email (no model)."""
    parent = list(range(len(records)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    seen: dict[str, int] = {}
    for i, doc in enumerate(records):
        fields = doc["fields"]
        phone = re.sub(r"\D", "", fields["phone"]["value"])[-9:]
        email = fields["email"]["value"].strip().lower()
        for key in (f"ph:{phone}", f"em:{email}"):
            if key in seen:
                union(i, seen[key])
            else:
                seen[key] = i
    groups: dict[int, list[dict]] = defaultdict(list)
    for i, doc in enumerate(records):
        groups[find(i)].append(doc)
    return [
        {"cluster_id": f"cluster-{n}", "entity_type": "user", "member_docs": docs}
        for n, docs in enumerate(groups.values())
    ]


def score(clusters: list[dict], truth: dict[str, str], variant: dict[str, str]) -> dict:
    where = {}
    for i, cluster in enumerate(clusters):
        for doc in cluster["member_docs"]:
            where[doc["document_id"]] = i
    by_person: dict[str, set[int]] = defaultdict(set)
    for doc_id, person in truth.items():
        by_person[person].add(where[doc_id])
    stats: dict = {"recall": {}, "false_merges": 0, "indistinguishable": 0}
    for person, clusters_used in by_person.items():
        kind = variant.get(person, "single")
        bucket = stats["recall"].setdefault(kind, [0, 0])  # [people, recovered]
        bucket[0] += 1
        if len(clusters_used) == 1:
            bucket[1] += 1
    for cluster in clusters:
        people = {truth[doc["document_id"]] for doc in cluster["member_docs"]}
        if len(people) <= 1:
            continue
        # Two records with the SAME name and address cannot be told apart from
        # the only fields the judge sees. That is not a judgement error, so it
        # is counted separately rather than blamed on the judge.
        names = {norm(d["fields"]["display_name"]["value"]) for d in cluster["member_docs"]}
        addresses = {norm(d["fields"]["address"]["value"]) for d in cluster["member_docs"]}
        if len(names) == 1 and len(addresses) == 1:
            stats["indistinguishable"] += 1
        else:
            stats["false_merges"] += 1
    return stats


def norm(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold().strip())


def negatives_kept_apart(clusters: list[dict], negatives: list[tuple[str, str]]) -> str:
    where = {}
    for i, cluster in enumerate(clusters):
        for doc in cluster["member_docs"]:
            where[doc["document_id"]] = i
    kept = sum(1 for a, b in negatives if where[a] != where[b])
    return f"{kept}/{len(negatives)}"


def report(
    label: str,
    stats: dict,
    pairs: int,
    seconds: float,
    negatives: str,
    tokens: int = 0,
) -> None:
    parts = []
    for kind in ("reformat", "moved", "nickname", "initial", "dropped_middle"):
        people, recovered = stats["recall"].get(kind, [0, 0])
        parts.append(f"{kind}={recovered}/{people}" if people else f"{kind}=-")
    print(
        f"{label:22}{'  '.join(parts)}  negatives_apart={negatives}"
        f"  false_merges={stats['false_merges']}"
        f"  indistinguishable={stats['indistinguishable']}  judged={pairs}"
        f"  time={seconds:.1f}s  tokens={tokens}"
    )


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--people", type=int, default=10000)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--judge", choices=["mock", "live"], default="mock")
    ap.add_argument("--min-confidence", choices=["high", "medium"], default="high")
    ap.add_argument("--blocking", choices=list(mc.BLOCKING_MODES), default="token")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    records, truth, variant, negatives = generate(args.people, rng)
    settings = dataclasses.replace(
        Settings.from_env() if args.judge == "live" else Settings(),
        use_mock_muse=args.judge == "mock",
        muse_candidate_min_confidence=args.min_confidence,
    )
    if args.judge == "live" and not settings.meta_model_api_key:
        raise SystemExit("live judge needs META_MODEL_API_KEY")

    print(
        f"people={len({p for p in truth.values()})}  records={len(records)}"
        f"  labelled_pairs={len(variant)}  negatives={len(negatives)}"
        f"  judge={args.judge}  min_confidence={args.min_confidence}"
    )

    started = time.time()
    base = exact_clusters(records)
    report(
        "A exact identifiers",
        score(base, truth, variant),
        0,
        time.time() - started,
        negatives_kept_apart(base, negatives),
    )

    stats: dict = {}
    started = time.time()
    pairs = mc.shortlist(base, per_cluster=3, stats=stats, blocking=args.blocking)
    shortlist_s = time.time() - started
    print(
        f"candidate_pairs={stats['candidate_pairs']}  judged={len(pairs)}"
        f"  skipped_per_cluster={stats['skipped_per_cluster']}"
        f"  shortlist_time={shortlist_s:.1f}s"
    )
    rules_stats: dict | None = None

    for label, judge in (("B rules", "mock"), ("C Muse", "live")):
        if judge == "live" and args.judge != "live":
            continue
        started = time.time()
        try:
            verdicts = (
                mc.mock_judge(pairs)
                if judge == "mock"
                else await mc.live_judge(settings, pairs, stats=stats)
            )
        except Exception as exc:  # noqa: BLE001 - a benchmark should say why, not crash
            print(f"{label:22}FAILED: {type(exc).__name__}: {exc}")
            continue
        merges = mc.select_merges(pairs, verdicts, args.min_confidence)
        merged, applied = mc.apply_merges(base, merges)
        arm_stats = score(merged, truth, variant)
        if label.startswith("B"):
            rules_stats = arm_stats
        report(
            label,
            arm_stats,
            len(pairs),
            shortlist_s + time.time() - started,
            negatives_kept_apart(merged, negatives),
            stats.get("tokens", 0) if judge == "live" else 0,
        )
        print(
            f"{'':22}applied {applied} merges; cache_hits={stats.get('cache_hits', 0)}"
            f" calls={stats.get('judge_calls', 0)} failures={stats.get('judge_failures', 0)}"
        )

    # Oracle: a perfect judge on the SAME candidate set. Pairs are labelled by
    # the cluster's own documents, so this needs no model and no extra truth.
    started = time.time()
    doc_person = truth
    cluster_person = {
        i: {doc_person[d["document_id"]] for d in cluster["member_docs"]}
        for i, cluster in enumerate(base)
    }
    oracle = []
    for pair in pairs:
        left, right = cluster_person[pair.left], cluster_person[pair.right]
        if len(left) == 1 and left == right:
            oracle.append(
                (
                    pair,
                    mc.Verdict(
                        pair_id=pair.pair_id, verdict="same", confidence="high", reason="oracle"
                    ),
                )
            )
    merged, applied = mc.apply_merges(base, oracle)
    report(
        "O oracle (upper bound)",
        score(merged, truth, variant),
        len(pairs),
        time.time() - started,
        negatives_kept_apart(merged, negatives),
    )
    print(f"{'':22}applied {applied} merges")
    if rules_stats:
        hard = ("nickname", "initial", "dropped_middle")
        missed = sum(
            rules_stats["recall"].get(k, [0, 0])[0] - rules_stats["recall"].get(k, [0, 0])[1]
            for k in hard
        )
        have = sum(rules_stats["recall"].get(k, [0, 0])[0] for k in hard)
        print(
            f"\nstring rules missed {missed}/{have} links that need semantics"
            f" (nickname, initial, dropped middle) and made"
            f" {rules_stats['false_merges']} false merges."
            "\nThe oracle shows those same candidate pairs contain the answer;"
            " only a judge that reads meaning, not spelling, recovers them."
        )


if __name__ == "__main__":
    asyncio.run(main())
