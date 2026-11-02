#!/usr/bin/env python3
"""Messy-customer benchmark built on the real Kaggle Customer 360 sample.

The sample itself has no near-duplicates (each customer uses one phone/email
format everywhere), so there is nothing for a semantic matcher to find in it.
This script takes N REAL customers and adds 2-4 labelled variants each, of
three kinds:

  format        same phone digits and email in another format, perturbed name
  country_code  phone with a +1 prefix, email with a typo, perturbed name/address
  moved         new phone AND new email; only name/address still agree

plus same-name-different-person distractors. Ground truth is known.

Arms (record level: is each variant in the same cluster as its origin?):
  A  exact blocking on RAW identifier values
  B  exact blocking on NORMALISED values (deterministic, no model)
  C  B + Muse candidate matching (shortlist -> judge -> merge)

    python3 scripts/eval_muse_candidates.py --judge mock
    python3 scripts/eval_muse_candidates.py --judge live      # needs .env
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mosaic-api"))

import muse_candidates as mc  # noqa: E402
from config import Settings  # noqa: E402
from ingest import normalize_value  # noqa: E402

PROV = {
    "authority_rank": 1,
    "event_time": "2026-10-27T09:00:00+07:00",
    "verified": True,
    "ttl_seconds": 3600,
}


def fld(value):
    """A field with full provenance, so injected records pass /build validation."""
    return {"value": value, **PROV}


ABBREV = [("street", "st"), ("road", "rd"), ("avenue", "ave"), ("apartment", "apt")]


def fv(doc, name):
    raw = doc["fields"].get(name)
    return (raw.get("value") if isinstance(raw, dict) else raw) if raw else None


def exact_clusters(docs, normalise):
    parent = {d["document_id"]: d["document_id"] for d in docs}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    seen = {}
    for d in docs:
        for k in ("email", "phone", "order_id"):
            v = fv(d, k)
            if not v:
                continue
            v = normalize_value(k, v) if normalise else v
            if (k, v) in seen:
                parent[find(d["document_id"])] = find(seen[(k, v)])
            else:
                seen[(k, v)] = d["document_id"]
    groups = defaultdict(list)
    for d in docs:
        groups[find(d["document_id"])].append(d)
    return [
        {"cluster_id": f"c{i + 1}", "entity_type": "user", "member_docs": m}
        for i, m in enumerate(groups.values())
    ]


def perturb_name(name, rng):
    parts = name.split()
    choice = rng.choice(["case", "swap", "typo", "initial", "accent"])
    if choice == "case":
        return name.upper()
    if choice == "swap" and len(parts) >= 2:
        return " ".join(reversed(parts))
    if choice == "typo" and len(name) > 4:
        i = rng.randrange(1, len(name) - 2)
        return name[:i] + name[i + 1] + name[i] + name[i + 2 :]
    if choice == "initial" and len(parts) >= 2:
        return f"{parts[0][0]}. {' '.join(parts[1:])}"
    if choice == "accent":
        return name.replace("e", "é", 1).replace("a", "á", 1)
    return name


def perturb_addr(addr, rng):
    out = addr.lower()
    for long, short in ABBREV:
        out = out.replace(long, short)
    return out.replace(",", "") if rng.random() < 0.5 else out


def make_variant(src, kind, i, rng):
    name = perturb_name(fv(src, "display_name"), rng)
    phone = re.sub(r"\D", "", (fv(src, "phone") or "").split("x")[0])
    email = fv(src, "email") or f"u{i}@example.com"
    f = {"display_name": name}
    if kind == "format":
        if len(phone) == 10:
            f["phone"] = f"({phone[:3]}) {phone[3:6]}-{phone[6:]}"
        f["email"] = email.upper()
        f["address"] = fv(src, "address")
    elif kind == "country_code":
        f["phone"] = "+1" + phone
        local, _, dom = email.partition("@")
        f["email"] = (local[:-1] + "q" if len(local) > 3 else local + "q") + "@" + dom
        f["address"] = perturb_addr(fv(src, "address") or "", rng)
    else:  # moved
        f["phone"] = f"09{rng.randrange(10**8, 10**9)}"
        f["email"] = f"new{i}@moved.example"
        f["address"] = perturb_addr(fv(src, "address") or "", rng)
    return {
        "document_id": f"v{i}-{kind}",
        "entity_type": "user",
        "source": "crm",
        "synthetic": True,
        "fields": {k: fld(v) for k, v in f.items() if v},
    }


def build_dataset(n, seed):
    """Real Kaggle customers + labelled variants. Returns the pieces to score."""
    rng = random.Random(seed)

    path = ROOT / "data" / "kaggle" / "customer360_sample_200.json"
    docs = [d for d in json.loads(path.read_text())["documents"] if d["entity_type"] == "user"]
    base = exact_clusters(docs, normalise=False)
    ok = [
        c
        for c in base
        if any(
            fv(d, "display_name") and fv(d, "address") and fv(d, "phone") and fv(d, "email")
            for d in c["member_docs"]
        )
    ]
    rng.shuffle(ok)
    chosen, distract = ok[:n], ok[n : n + n // 4]

    identity = {}  # doc id -> person
    for c in base:
        for d in c["member_docs"]:
            identity[d["document_id"]] = c["cluster_id"]
    all_docs = list(docs)
    variants = []  # (variant doc id, origin person, kind)
    for i, c in enumerate(chosen):
        src = next(
            d
            for d in c["member_docs"]
            if fv(d, "display_name") and fv(d, "address") and fv(d, "phone") and fv(d, "email")
        )
        for j in range(rng.randint(2, 4)):
            kind = rng.choice(["format", "country_code", "moved"])
            v = make_variant(src, kind, f"{i}_{j}", rng)
            all_docs.append(v)
            identity[v["document_id"]] = c["cluster_id"]
            variants.append((v["document_id"], c["cluster_id"], kind))
    for i, c in enumerate(distract):  # same NAME, a different person
        src = next(d for d in c["member_docs"] if fv(d, "display_name"))
        d = {
            "document_id": f"dis-{i}",
            "entity_type": "user",
            "source": "crm",
            "synthetic": True,
            "fields": {
                "display_name": fld(fv(src, "display_name")),
                "address": fld(f"{rng.randrange(900)} Different Avenue, Elsewhere"),
                "email": fld(f"dis{i}@other.example"),
                "phone": fld(f"08{rng.randrange(10**8, 10**9)}"),
            },
        }
        all_docs.append(d)
        identity[d["document_id"]] = f"distractor-{i}"

    return all_docs, identity, variants, base, chosen, distract


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--judge", choices=["mock", "live"], default="mock")
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    all_docs, identity, variants, base, chosen, distract = build_dataset(args.n, args.seed)
    settings = Settings.from_env() if args.judge == "live" else Settings()
    settings = dataclasses.replace(settings, use_mock_muse=args.judge == "mock")

    arm_a = exact_clusters(all_docs, normalise=False)
    arm_b = exact_clusters(all_docs, normalise=True)
    arm_c, info = await mc.propose_and_merge(settings, arm_b)

    def where(clusters):
        return {d["document_id"]: c["cluster_id"] for c in clusters for d in c["member_docs"]}

    def score(clusters):
        loc = where(clusters)
        origin_doc = {}
        for c in base:
            for d in c["member_docs"]:
                origin_doc.setdefault(c["cluster_id"], d["document_id"])
        found, total = Counter(), Counter()
        for vid, person, kind in variants:
            total[kind] += 1
            if loc[vid] == loc[origin_doc[person]]:
                found[kind] += 1
        false = sum(
            1 for c in clusters if len({identity[d["document_id"]] for d in c["member_docs"]}) > 1
        )
        return found, total, false, len(clusters)

    print(
        f"judge={args.judge}  customers={len(chosen)}  variants={len(variants)}  "
        f"distractors={len(distract)}  records={len(all_docs)}"
    )
    print(
        f"Muse reviewed {info['shortlisted']} cluster pairs of "
        f"{len(arm_b) * (len(arm_b) - 1) // 2:,} possible; tiers={info.get('tiers')}; "
        f"skipped={info['skipped_reason']}\n"
    )
    kinds = ["format", "country_code", "moved"]
    print(
        f"{'arm':34}"
        + "".join(f"{k:>14}" for k in kinds)
        + f"{'ALL':>9}{'wrong':>7}{'clusters':>9}"
    )
    for label, clusters in (
        ("A  exact, raw values", arm_a),
        ("B  exact, normalised (no model)", arm_b),
        ("C  B + Muse candidates", arm_c),
    ):
        found, total, false, n = score(clusters)
        cells = "".join(f"{found[k]:>6}/{total[k]:<7}" for k in kinds)
        print(f"{label:34}{cells}{sum(found.values()):>4}/{sum(total.values()):<4}{false:>7}{n:>9}")
    print("\nwrong = clusters that contain two different people (false merges)")
    for c in arm_c:
        people = {identity[d["document_id"]] for d in c["member_docs"]}
        if len(people) > 1:
            print(
                "  WRONG CLUSTER:",
                sorted({str(fv(d, "display_name")) for d in c["member_docs"]}),
                "| links:",
                [(m["tier"], m["reason"]) for m in c.get("muse_links", [])],
            )


if __name__ == "__main__":
    asyncio.run(main())
