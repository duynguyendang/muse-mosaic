#!/usr/bin/env python3
"""Hard-name benchmark: where does a model judge beat string rules?

The first benchmark (eval_muse_candidates.py) used typos, case and word order,
which string similarity handles well. This one uses what it does not:
nicknames (Robert / Bob), initials, surname-first order, dropped middle names,
Vietnamese order and diacritics, and HARD NEGATIVES that look alike but are
different people (a spouse at the same address, same name at another address).

Judge-only: each labelled pair goes straight to the judge, so the result
measures judgement, not the shortlist. Pairs carry only display name and
address, exactly what the product sends. Identifiers differ on purpose.

Judges compared (same pairs, same merge rule: "same" at "high" confidence):
  plain   mosaic-api/muse_candidates.mock_judge  (string similarity thresholds)
  oracle  a stronger rule judge built HERE with the exact nickname table used to
          generate the test (diacritic stripping, initials, order-free tokens).
          It knows every nickname in the test, so it is an upper bound for a
          dictionary approach, not a fair real-world baseline.
  muse    the live Meta model

English persons are REAL Kaggle Customer 360 customers (name, address) with
name variants written here. The Vietnamese set is SYNTHETIC and labelled so.

    python3 scripts/eval_hard_names.py --judge mock     # plain + oracle only
    python3 scripts/eval_hard_names.py --judge live     # adds Muse (needs .env)
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import random
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mosaic-api"))

import muse_candidates as mc  # noqa: E402
from config import Settings  # noqa: E402

NICK = {
    "robert": ["bob", "rob", "bobby"],
    "william": ["bill", "will", "billy"],
    "james": ["jim", "jimmy"],
    "michael": ["mike"],
    "joseph": ["joe"],
    "richard": ["rick", "dick"],
    "elizabeth": ["liz", "beth", "betty"],
    "katherine": ["kate", "kathy"],
    "kathryn": ["kate", "kathy"],
    "christopher": ["chris"],
    "jonathan": ["jon"],
    "andrew": ["andy"],
    "jessica": ["jess"],
    "stephanie": ["steph"],
    "samantha": ["sam"],
    "nathaniel": ["nate"],
    "jeffrey": ["jeff"],
    "ronald": ["ron"],
    "deborah": ["deb"],
    "barbara": ["barb"],
    "john": ["jack", "johnny"],
    "margaret": ["peggy", "maggie"],
    "thomas": ["tom"],
    "daniel": ["dan"],
    "matthew": ["matt"],
    "anthony": ["tony"],
    "patricia": ["pat", "trish"],
}
OTHER_FIRST = ["Alan", "Diana", "Peter", "Laura", "Victor", "Nora", "Hugo", "Iris", "Felix", "Ruth"]

# Synthetic Vietnamese persons: (diacritic full name, address with diacritics).
VN = [
    ("Nguyễn Văn An", "12 Lê Lợi, Quận 1, TP.HCM"),
    ("Trần Thị Lan", "45 Hai Bà Trưng, Quận 3, TP.HCM"),
    ("Lê Minh Quân", "8 Nguyễn Huệ, Quận 1, Hồ Chí Minh"),
    ("Phạm Thị Hoa", "101 Trần Phú, Hải Châu, Đà Nẵng"),
    ("Hoàng Văn Nam", "27 Lý Thường Kiệt, Hoàn Kiếm, Hà Nội"),
    ("Vũ Ngọc Trang", "3 Phan Chu Trinh, Hội An"),
    ("Đặng Quốc Bảo", "66 Điện Biên Phủ, Bình Thạnh, TP.HCM"),
    ("Bùi Thị Thảo", "19 Hùng Vương, Huế"),
    ("Đỗ Văn Hải", "5 Trần Hưng Đạo, Ninh Kiều, Cần Thơ"),
    ("Ngô Thị Linh", "88 Láng Hạ, Đống Đa, Hà Nội"),
    ("Dương Minh Tuấn", "14 Pasteur, Quận 1, Sài Gòn"),
    ("Lý Thị Mai", "73 Nguyễn Trãi, Thanh Xuân, Hà Nội"),
    ("Phan Văn Long", "31 Bạch Đằng, Hải Châu, Đà Nẵng"),
    ("Võ Thị Yến", "9 Võ Văn Tần, Quận 3, TP.HCM"),
    ("Trương Quốc Việt", "52 Kim Mã, Ba Đình, Hà Nội"),
    ("Huỳnh Ngọc Anh", "20 Lê Duẩn, Quận 1, TP.HCM"),
    ("Đinh Văn Khoa", "44 Nguyễn Văn Cừ, Long Biên, Hà Nội"),
    ("Mai Thị Hương", "7 Trường Chinh, Tân Bình, TP.HCM"),
    ("Cao Minh Đức", "60 Quang Trung, Vinh"),
    ("Tạ Thị Ngân", "15 Lê Hồng Phong, Hải Phòng"),
]
CITY_ALIAS = {
    "TP.HCM": ["Hồ Chí Minh", "Sài Gòn", "HCMC"],
    "Hà Nội": ["Ha Noi", "HN"],
    "Đà Nẵng": ["Da Nang"],
}


def strip(text: str) -> str:
    text = text.replace("Đ", "D").replace("đ", "d")
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c))


def fv(doc, name):
    raw = doc["fields"].get(name)
    return (raw.get("value") if isinstance(raw, dict) else raw) if raw else None


def make_doc(doc_id, name, address):
    return {
        "document_id": doc_id,
        "entity_type": "user",
        "fields": {"display_name": {"value": name}, "address": {"value": address}},
    }


def cluster_of(doc):
    return {"cluster_id": doc["document_id"], "entity_type": "user", "member_docs": [doc]}


# ------------------------------------------------------------------ variants


def english_variants(first, last, rng):
    """name variants of 'First Last'; each is (kind, name)."""
    out = []
    nick = NICK.get(first.lower())
    if nick:
        out.append(("nickname", f"{rng.choice(nick).title()} {last}"))
    out.append(("initial", f"{first[0]}. {last}"))
    out.append(("surname_first", f"{last}, {first}"))
    out.append(("middle_initial", f"{first} {rng.choice('ABCDEJKLMRST')}. {last}"))
    return out


def vn_variants(full):
    parts = full.split()
    family, given = parts[0], parts[-1]
    middle = parts[1:-1]
    out = [
        ("vn_no_diacritics", strip(full)),
        ("vn_given_first", f"{given} {family}"),
        ("vn_allcaps", strip(full).upper()),
    ]
    if middle:
        out.append(("vn_dropped_middle", f"{family} {given}"))
        out.append(("vn_initials", f"{family} {middle[0][0]}. {given}"))
        out.append(("vn_given_family_middle", f"{given} {family} {middle[0]}"))
    return out


def perturb_address(addr, rng):
    out = addr
    for city, aliases in CITY_ALIAS.items():
        if city in out and rng.random() < 0.7:
            out = out.replace(city, rng.choice(aliases))
    out = out.replace("Quận", "Q.").replace("Đường", "Đ.")
    return strip(out) if rng.random() < 0.5 else out


# ------------------------------------------------------------------- judges


def _tokens(name):
    name = strip(name).lower().replace(",", " ")
    return [t for t in re.sub(r"[^\w\s]", " ", name).split() if t]


def _canon(token):
    for formal, nicks in NICK.items():
        if token in nicks:
            return formal
    return token


def _matches(a, b):
    a, b = _canon(a), _canon(b)
    return a == b or (len(a) == 1 and b.startswith(a)) or (len(b) == 1 and a.startswith(b))


def names_compatible(x, y):
    """Order-free, initial-tolerant, nickname-aware: every token of the shorter
    name matches a distinct token of the longer, and at least two do."""
    ta, tb = _tokens(x), _tokens(y)
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    pool = list(long_)
    hit = 0
    for t in short:
        for i, u in enumerate(pool):
            if _matches(t, u):
                hit += 1
                pool.pop(i)
                break
    return hit == len(short) and hit >= 2


def oracle_judge(pairs):
    out = []
    for p in pairs:
        names_ok = any(
            names_compatible(a, b)
            for a in p.left_profile["names"]
            for b in p.right_profile["names"]
        )
        addr = mc._best(
            [strip(a) for a in p.left_profile["addresses"]],
            [strip(b) for b in p.right_profile["addresses"]],
        )
        if names_ok and addr >= 0.8:
            out.append(
                mc.Verdict(
                    pair_id=p.pair_id, verdict="same", confidence="high", reason="oracle rules"
                )
            )
        else:
            out.append(
                mc.Verdict(
                    pair_id=p.pair_id,
                    verdict="different",
                    confidence="medium",
                    reason="oracle rules",
                )
            )
    return out


# --------------------------------------------------------------------- main


def build_pairs(seed, n_english):
    rng = random.Random(seed)
    data = json.loads((ROOT / "data/kaggle/customer360_sample_200.json").read_text())["documents"]
    seen, people = set(), []
    for d in data:
        if d["entity_type"] != "user":
            continue
        name, addr = fv(d, "display_name"), fv(d, "address")
        if name and addr and re.fullmatch(r"[A-Z][a-z]+ [A-Z][a-z]+", name) and name not in seen:
            seen.add(name)
            people.append((name, addr))
    rng.shuffle(people)
    people = people[:n_english]

    labelled = []  # (kind, truth, docA, docB)
    uid = 0

    def doc(name, addr):
        nonlocal uid
        uid += 1
        return make_doc(f"d{uid}", name, addr)

    for name, addr in people:
        first, last = name.split()
        base = doc(name, addr)
        for kind, variant in english_variants(first, last, rng):
            labelled.append((kind, True, base, doc(variant, perturb_address(addr, rng))))
        # hard negatives
        other = rng.choice([f for f in OTHER_FIRST if f != first])
        labelled.append(
            ("household_neg", False, base, doc(f"{other} {last}", addr))
        )  # same address
        labelled.append(
            (
                "same_name_other_address_neg",
                False,
                base,
                doc(name, f"{rng.randrange(900)} Elsewhere Boulevard, Otherville"),
            )
        )
    for full, addr in VN:
        base = doc(full, addr)
        family = full.split()[0]
        for kind, variant in vn_variants(full):
            labelled.append((kind, True, base, doc(variant, perturb_address(addr, rng))))
        other_given = rng.choice(["Hùng", "Mai", "Sơn", "Thu", "Khánh"])
        labelled.append(("vn_household_neg", False, base, doc(f"{family} Văn {other_given}", addr)))
        labelled.append(
            (
                "vn_same_name_other_address_neg",
                False,
                base,
                doc(full, f"{rng.randrange(900)} Đường Khác, Quận 7, TP.HCM"),
            )
        )
    rng.shuffle(labelled)

    pairs, meta = [], {}
    for i, (kind, truth, a, b) in enumerate(labelled):
        if rng.random() < 0.5:
            a, b = b, a  # remove left/right ordering as a signal
        ca, cb = cluster_of(a), cluster_of(b)
        pa, pb = mc.cluster_profile(ca), mc.cluster_profile(cb)
        pair = mc.CandidatePair(
            f"p{i}",
            0,
            1,
            mc._best(pa["names"], pb["names"]),
            mc._best(pa["addresses"], pb["addresses"]),
            pa,
            pb,
        )
        pairs.append(pair)
        meta[pair.pair_id] = (kind, truth, pa, pb)
    return pairs, meta


def score(name, verdicts, meta, floor=2):
    by_id = {v.pair_id: v for v in verdicts}
    kinds = defaultdict(lambda: [0, 0])  # kind -> [correct, total]
    tp = fp = fn = tn = 0
    wrong = []
    for pid, (kind, truth, pa, pb) in meta.items():
        v = by_id.get(pid)
        merged = bool(v and v.verdict == "same" and mc.CONFIDENCE_RANK[v.confidence] >= floor)
        kinds[kind][1] += 1
        if merged == truth:
            kinds[kind][0] += 1
        if truth and merged:
            tp += 1
        elif truth:
            fn += 1
            wrong.append(
                ("MISSED", kind, pa["names"], pb["names"], v.reason if v else "no verdict")
            )
        elif merged:
            fp += 1
            wrong.append(("FALSE MERGE", kind, pa["names"], pb["names"], v.reason if v else ""))
        else:
            tn += 1
    return name, tp, fp, fn, tn, kinds, wrong


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--judge", choices=["mock", "live"], default="mock")
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--show", type=int, default=6, help="mistakes to print per judge")
    ap.add_argument("--dump", help="write every verdict and label to this JSON file")
    ap.add_argument("--from-dump", help="reuse Muse verdicts from a --dump file (no API calls)")
    args = ap.parse_args()

    pairs, meta = build_pairs(args.seed, args.n)
    pos = sum(1 for m in meta.values() if m[1])
    print(
        f"pairs={len(pairs)}  same-person={pos}  different-person={len(pairs) - pos}  "
        f"(English = real Kaggle customers; Vietnamese = synthetic)\n"
    )

    runs = [
        ("plain string rules", mc.mock_judge(pairs)),
        ("oracle rules (knows the nicknames)", oracle_judge(pairs)),
    ]
    if args.from_dump:
        stored = json.loads(Path(args.from_dump).read_text())
        name, rows = next((k, v) for k, v in stored.items() if k.startswith("Muse"))
        runs.append(
            (
                name,
                [
                    mc.Verdict(pair_id=pid, verdict=r[0], confidence=r[1], reason=r[2])
                    for pid, r in rows.items()
                ],
            )
        )
    elif args.judge == "live":
        settings = dataclasses.replace(Settings.from_env(), use_mock_muse=False)
        runs.append(("Muse " + settings.muse_model_id, await mc.live_judge(settings, pairs)))

    results = [score(n, v, meta) for n, v in runs]
    muse_run = next(((n, v) for n, v in runs if n.startswith("Muse")), None)
    if muse_run:  # same verdicts, looser merge rule
        results.append(
            score(muse_run[0].replace("Muse", "Muse@medium+", 1), muse_run[1], meta, floor=1)
        )
    if args.dump:
        Path(args.dump).write_text(
            json.dumps(
                {
                    name: {
                        v.pair_id: [
                            v.verdict,
                            v.confidence,
                            v.reason,
                            meta[v.pair_id][0],
                            meta[v.pair_id][1],
                        ]
                        for v in verdicts
                    }
                    for name, verdicts in runs
                },
                ensure_ascii=False,
            )
        )
    for name, verdicts in runs:
        if not name.startswith(("plain", "oracle", "Muse")):
            continue
        dist = Counter((v.verdict, v.confidence) for v in verdicts if meta[v.pair_id][1])
        print(
            f"verdicts on the {pos} same-person pairs, {name.split()[0]}: {dict(sorted(dist.items()))}"
        )
    print()
    print(
        f"{'judge':40}{'merged right':>14}{'missed':>8}{'false merges':>14}{'precision':>11}{'recall':>8}"
    )
    for name, tp, fp, fn, _tn, _, _ in results:
        prec = tp / (tp + fp) if tp + fp else 1.0
        print(f"{name:40}{tp:>14}{fn:>8}{fp:>14}{prec:>11.0%}{tp / (tp + fn):>8.0%}")
    print("\ncorrect decisions by pair kind (right / total):")
    kinds = sorted({k for r in results for k in r[5]})
    print(f"{'kind':34}" + "".join(f"{r[0].split()[0]:>12}" for r in results))
    for k in kinds:
        print(f"{k:34}" + "".join(f"{r[5][k][0]:>6}/{r[5][k][1]:<5}" for r in results))
    for name, *_rest, wrong in results:
        if wrong and args.show:
            print(f"\n{name}: first mistakes")
            for kind_, k, a, b, why in wrong[: args.show]:
                print(f"  {kind_:12} [{k}] {a} | {b} | {why[:90]}")


if __name__ == "__main__":
    asyncio.run(main())
