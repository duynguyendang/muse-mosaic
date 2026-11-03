#!/usr/bin/env python3
"""Do G1 (field mapping) and G2 (rarest_pair blocking) change the outcome?

Vietnamese names with a realistic surname distribution (Nguyen ~38%), so almost
every name token is "hot". Labelled people have a second record written given-name
first with the middle name dropped, same address, new phone and email: exact
identifiers cannot link them, only name + address can.

Arms (same records, same judge):
  token          MUSE_BLOCKING_MODE=token        (previous default)
  rarest_pair    MUSE_BLOCKING_MODE=rarest_pair  (new, opt-in)
  alien schema   the records renamed to full_name / address_line / mobile / email_address,
                 shortlisted with and without canonicalize_documents (G1)

    python3 scripts/eval_vn_blocking.py --people 5000 --judge mock
    python3 scripts/eval_vn_blocking.py --people 5000 --judge live   # needs .env
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import dataclasses
import json
import random
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mosaic-api"))

import muse_candidates as mc  # noqa: E402
from config import Settings  # noqa: E402
from schemas import Taxonomy  # noqa: E402

SURNAMES = [
    ("Nguyen", 38), ("Tran", 11), ("Le", 10), ("Pham", 7), ("Hoang", 5), ("Phan", 4),
    ("Vu", 4), ("Dang", 3), ("Bui", 3), ("Do", 2), ("Ho", 2), ("Ngo", 2), ("Duong", 2),
    ("Ly", 2), ("Truong", 2),
]  # fmt: skip
BASE_GIVEN = [
    "An", "Binh", "Chi", "Dung", "Giang", "Hoa", "Hung", "Lan", "Linh", "Long", "Mai",
    "Minh", "Nam", "Nga", "Ngan", "Nhung", "Phong", "Phuong", "Quan", "Quang", "Son",
    "Tam", "Thao", "Thang", "Thu", "Trang", "Tuan", "Tu", "Van", "Vy", "Yen",
]  # fmt: skip
SECOND = [
    "", "Anh", "Bao", "Chau", "Diem", "Duc", "Ha", "Hai", "Hanh", "Hieu", "Huan", "Huong",
    "Khanh", "Khoa", "Lam", "Loi", "Luan", "My", "Ngoc", "Nhi", "Nhu", "Phuc", "Quynh",
    "Thanh", "Thinh", "Thuy", "Tien", "Toan", "Trinh", "Truc", "Tung", "Vi", "Vinh", "Xuan",
]  # fmt: skip
STREETS = ["Le Loi", "Hai Ba Trung", "Nguyen Trai", "Tran Hung Dao", "Pasteur", "Dien Bien Phu"]
GIVEN = [f"{a} {b}".strip() for a in BASE_GIVEN for b in SECOND][:200]


def pick_surname(rng: random.Random) -> str:
    total = sum(w for _, w in SURNAMES)
    point, upto = rng.uniform(0, total), 0
    for name, weight in SURNAMES:
        upto += weight
        if point <= upto:
            return name
    return SURNAMES[-1][0]


def doc(doc_id, name, address, phone, email, keys):
    n, a, p, e = keys
    return {
        "document_id": doc_id,
        "entity_type": "user",
        "source": "crm",
        "fields": {
            n: {"value": name},
            a: {"value": address},
            p: {"value": phone},
            e: {"value": email},
        },
    }


def generate(people: int, seed: int):
    rng = random.Random(seed)
    keys = ("display_name", "address", "phone", "email")
    docs, truth = [], {}
    labelled = []
    serial = 0

    def add(person, name, address):
        nonlocal serial
        serial += 1
        d = doc(f"d{serial:06d}", name, address, f"09{serial:08d}", f"u{serial}@ex.com", keys)
        docs.append(d)
        truth[d["document_id"]] = person

    for i in range(people):
        person = f"p{i:05d}"
        surname, given = pick_surname(rng), rng.choice(GIVEN)
        address = f"{rng.randint(1, 300)} {rng.choice(STREETS)}"
        add(person, f"{surname} Thi {given}", address)
        if i < people // 20:  # 5% labelled
            labelled.append(person)
            add(person, f"{given} {surname}", address)
    return docs, truth, labelled


def rename(docs, mapping):
    out = copy.deepcopy(docs)
    for d in out:
        d["fields"] = {mapping.get(k, k): v for k, v in d["fields"].items()}
    return out


def exact_clusters(docs):
    return [
        {"cluster_id": f"cluster-{i}", "entity_type": "user", "member_docs": [d]}
        for i, d in enumerate(docs)
    ]  # no shared identifiers by construction: every record starts alone


def score(clusters, truth, labelled):
    where = {}
    for i, c in enumerate(clusters):
        for d in c["member_docs"]:
            where[d["document_id"]] = i
    by_person = defaultdict(set)
    for doc_id, person in truth.items():
        by_person[person].add(where[doc_id])
    recovered = sum(1 for p in labelled if len(by_person[p]) == 1)
    false = indist = 0
    for c in clusters:
        people = {truth[d["document_id"]] for d in c["member_docs"]}
        if len(people) <= 1:
            continue
        names = {
            " ".join(d["fields"]["display_name"]["value"].split()).casefold()
            for d in c["member_docs"]
        }
        addrs = {d["fields"]["address"]["value"].casefold() for d in c["member_docs"]}
        if len(names) == 1 and len(addrs) == 1:
            indist += 1
        else:
            false += 1
    return recovered, false, indist


async def run_arm(label, docs, truth, labelled, settings, blocking, judge, canonical):
    started = time.time()
    clusters = exact_clusters(docs)
    stats: dict = {}
    pairs = mc.shortlist(clusters, per_cluster=3, blocking=blocking, stats=stats)
    pair_set = {(p.left, p.right) for p in pairs}
    where = {c["member_docs"][0]["document_id"]: i for i, c in enumerate(clusters)}
    by_person = defaultdict(list)
    for doc_id, person in truth.items():
        by_person[person].append(where[doc_id])
    reach = sum(1 for p in labelled if (min(by_person[p]), max(by_person[p])) in pair_set)
    line = f"{label:34} candidates={len(pairs):6d} candidate_recall={reach}/{len(labelled)}"
    if stats.get("warning"):
        line += "  WARNING"
    if judge is None or not pairs:
        print(line)
        return
    run_stats: dict = {}
    verdicts = (
        mc.mock_judge(pairs)
        if judge == "mock"
        else await mc.live_judge(settings, pairs, stats=run_stats)
    )
    merged, _ = mc.apply_merges(clusters, mc.select_merges(pairs, verdicts, "high"))
    got, false, indist = score(merged, truth, labelled)
    print(
        f"{line}  recovered={got}/{len(labelled)} false_merges={false} indistinguishable={indist}"
        f"  calls={run_stats.get('judge_calls', 0)} tokens={run_stats.get('tokens', 0)}"
        f" time={time.time() - started:.0f}s"
    )


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--people", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=3)
    ap.add_argument("--judge", choices=["mock", "live"], default="mock")
    args = ap.parse_args()
    settings = dataclasses.replace(
        Settings.from_env() if args.judge == "live" else Settings(),
        use_mock_muse=args.judge == "mock",
    )
    if args.judge == "live" and not settings.meta_model_api_key:
        raise SystemExit("live judge needs META_MODEL_API_KEY")
    docs, truth, labelled = generate(args.people, args.seed)
    print(
        f"people={args.people} records={len(docs)} labelled={len(labelled)} judge={args.judge}"
        f" (Vietnamese order + dropped middle name, same address, new phone/email)\n"
    )
    for mode in ("token", "rarest_pair"):
        await run_arm(
            f"G2 blocking={mode}", docs, truth, labelled, settings, mode, args.judge, True
        )

    alien = rename(
        docs,
        {
            "display_name": "full_name",
            "address": "address_line",
            "phone": "mobile",
            "email": "email_address",
        },
    )
    tax = Taxonomy.model_validate(
        json.loads((ROOT / "data/mock/taxonomy/taxonomy-user-2026-10-27-v1.json").read_text())
    )
    print()
    # G1: without mapping the shortlist reads nothing; with it, same as the canonical run.
    blank = exact_clusters(alien)
    stats: dict = {}
    mc.shortlist(blank, blocking="rarest_pair", stats=stats)
    print(
        f"{'G1 alien schema, no mapping':34} candidates={stats['kept_pairs']:6d}  {stats.get('warning', '')[:90]}"
    )
    mapped = mc.canonicalize_documents(alien, tax)
    await run_arm(
        "G1 alien schema + mapping (pair)",
        mapped,
        truth,
        labelled,
        settings,
        "rarest_pair",
        args.judge,
        True,
    )


if __name__ == "__main__":
    asyncio.run(main())
