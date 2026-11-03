#!/usr/bin/env python3
"""Taxonomy-gap benchmark: how many products end up with a CERTIFIED category?

Without a term in the taxonomy the engine holds `category` (rule T1). This
measures what four ways of extending the taxonomy achieve, through the real
/build path and a live trust-engine:

  A  fixed        the shipped taxonomy, no extension
  B  rules        deterministic grouping + the evidence verifier (no model)
  C  muse+check   Muse proposes, the SAME evidence verifier decides
  D  muse, no check   Muse proposes and every proposal is accepted. Shown only to
                  measure the risk; the product never does this.

Two datasets:
  kaggle     the 389 REAL products of the Kaggle Customer 360 sample: 8 categories
             written 37 ways (clo, 3l3ctronics, kitch3n, TOYS, beauty_ ...).
             Labels were assigned by hand from the 37 values.
  semantic   SYNTHETIC, labelled: categories described in words the data cannot
             tie together lexically (Galaxy Fold, điện thoại gập, iPad Pro, ...).

Scoring is by RECORDS and naming-agnostic: a value counts as correct when its
certified term is dominated by the same true class (purity), so "Foldable
Smartphone" vs "Foldable Phone" is not penalised.

    python3 scripts/eval_taxonomy_gap.py --dataset kaggle --arms A B
    python3 scripts/eval_taxonomy_gap.py --dataset semantic --arms A B C D   # Muse needs .env
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mosaic-api"))

import taxonomy_proposals as tp  # noqa: E402
from config import Settings  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from main import create_app  # noqa: E402
from schemas import Taxonomy  # noqa: E402
from taxonomies import load_taxonomies  # noqa: E402

STAMP = {
    "authority_rank": 1,
    "verified": True,
    "event_time": "2026-10-27T09:00:00+07:00",
    "ttl_seconds": 3600,
}

# Hand labels for the 37 Kaggle values (assigned by reading them, not by a rule).
KAGGLE_TRUTH = {}
for _canon, _vals in {
    "clothing": "clothing CLOTHING clo clothing- clothing_",
    "automotive": "automotive automotiv3 AUTOMOTIVE aut automotive_",
    "sports": "sports SPORTS sports-",
    "beauty": "beauty bea b3auty beauty_ BEAUTY beauty-",
    "kitchen": "kitchen KITCHEN kit kitch3n kitchen-",
    "toys": "toys TOYS toy toys-",
    "electronics": "electronics 3l3ctronics ELECTRONICS ele",
    "home": "home hom3 HOME hom home_",
}.items():
    for _v in _vals.split():
        KAGGLE_TRUTH[_v] = _canon

# Synthetic semantic set: raw value -> (true class, records)
SEMANTIC = {
    "Smartphone": ("phone", 5),
    "smartphone": ("phone", 3),
    "iPhone 14": ("phone", 4),
    "điện thoại": ("phone", 3),
    "Laptop": ("laptop", 5),
    "laptop": ("laptop", 3),
    "MacBook Pro": ("laptop", 4),
    "notebook": ("laptop", 3),
    "Foldable Phone": ("foldable", 6),
    "foldable phone": ("foldable", 3),
    "Foldable Phones": ("foldable", 2),
    "Galaxy Fold 5": ("foldable", 4),
    "điện thoại gập": ("foldable", 3),
    "Razr+ foldable": ("foldable", 2),
    "Tablet": ("tablet", 6),
    "tablet": ("tablet", 2),
    "Tablets": ("tablet", 2),
    "iPad Pro": ("tablet", 3),
    "máy tính bảng": ("tablet", 3),
    "2-in-1 Laptop": ("two_in_one", 5),
    "2 in 1 laptop": ("two_in_one", 3),
    "Surface Pro 9 detachable keyboard": ("two_in_one", 4),
    "Wireless Earbuds": ("earbuds", 5),
    "wireless earbuds": ("earbuds", 2),
    "AirPods Pro": ("earbuds", 3),
}


def doc(doc_id, value):
    return {
        "document_id": doc_id,
        "entity_type": "product",
        "source": "catalog",
        "synthetic": True,
        "fields": {"category": {"value": value, **STAMP}},
    }


def load_dataset(name):
    if name == "kaggle":
        raw = json.loads((ROOT / "data/kaggle/customer360_sample_200.json").read_text())[
            "documents"
        ]
        docs = [d for d in raw if d["entity_type"] == "product"]
        truth = {}
        for d in docs:
            v = d["fields"]["category"]["value"]
            truth[v] = KAGGLE_TRUTH[v]
        return docs, truth
    docs, truth, n = [], {}, 0
    for value, (cls, count) in SEMANTIC.items():
        truth[value] = cls
        for _ in range(count):
            docs.append(doc(f"sg-{n:03d}", value))
            n += 1
    return docs, truth


# ---------------------------------------------------------------- engine


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def find_mkit():
    for cand in (os.environ.get("MANGLEKIT_BIN"), shutil.which("mkit"), "/tmp/opencode/te/mkit"):
        if cand and Path(cand).exists():
            return cand
    sys.exit("mkit binary not found: set MANGLEKIT_BIN")


class Engine:
    def __init__(self, binary, tax_dir):
        self.port = free_port()
        env = dict(
            os.environ,
            PORT=str(self.port),
            POLICY_DIR=str(ROOT / "trust-engine/policies"),
            TAXONOMY_DIR=str(tax_dir),
            MANGLEKIT_BIN=find_mkit(),
        )
        self.proc = subprocess.Popen(
            [binary], env=env, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=1)
                return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("trust-engine did not start")

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/derive"

    def stop(self):
        self.proc.terminate()
        self.proc.wait(timeout=5)


# ------------------------------------------------------------------ arms


async def prepare(arm, docs, tax_dir, settings):
    base_dir = ROOT / "data/mock/taxonomy"
    for p in base_dir.glob("*.json"):
        shutil.copy(p, tax_dir / p.name)
    base = next(t for t in load_taxonomies(tax_dir).values() if t.entity_type == "product")
    if arm == "A":
        return {"mode": "none", "accepted": 0, "rejected": 0, "rejected_examples": []}
    mode = "rules" if arm == "B" else "muse"
    report = await tp.extend_taxonomy(
        settings, base, docs, tax_dir, mode=mode, min_support=3, check=arm != "D"
    )
    report["rejected_examples"] = [
        (v.term, s, why) for v in report.pop("verdicts") for s, why in v.rejected
    ]
    return report


def build(docs, tax_dir, engine, tmp):
    settings = Settings(
        use_mock_muse=True,
        trust_engine_url=engine.url,
        database_path=str(Path(tmp) / "m.db"),
        enable_background_tasks=False,
        splink_enabled=False,
    )
    for d in docs:
        d["content_hash"] = d["document_id"]
    with TestClient(create_app(settings)) as client:
        r = client.post(
            "/build",
            json={"work_id": "w", "idempotency_key": "k", "dataset": {"documents": docs}},
            timeout=900,
        )
    assert r.status_code == 200, r.text[:300]
    return r.json()


def score(out, docs, truth):
    """Records by outcome. A certified value is

      unified     its term is dominated by its true class AND that term is the
                  one holding most of the class (synonyms ended up together)
      fragmented  pure but split: the class got several terms, so downstream still
                  sees one category as many (certified, but not what was wanted)
      wrong       its term is dominated by a DIFFERENT class (a bad merge)

    held records had no certified category. Naming-agnostic.
    """
    by_doc = {d["document_id"]: d["fields"]["category"]["value"] for d in docs}
    count = Counter(by_doc.values())
    cert = {}  # raw value -> certified term
    for g in out["goldens"]:
        if not g.get("golden_id"):
            continue
        m = re.search(r"cluster-\d+-(.+)-category$", g["attributes"]["category"]["winner_fact_id"])
        if m and m.group(1) in by_doc:
            cert[by_doc[m.group(1)]] = g["attributes"]["category"]["value"]
    by_term = defaultdict(Counter)
    by_class = defaultdict(Counter)
    for value, term in cert.items():
        by_term[term][truth[value]] += count[value]
        by_class[truth[value]][term] += count[value]
    term_class = {t: c.most_common(1)[0][0] for t, c in by_term.items()}
    class_term = {c: t.most_common(1)[0][0] for c, t in by_class.items()}
    unified = fragmented = wrong = 0
    for value, term in cert.items():
        n = count[value]
        if term_class[term] != truth[value]:
            wrong += n
        elif class_term[truth[value]] != term:
            fragmented += n
        else:
            unified += n
    total = sum(count.values())
    return (
        unified,
        fragmented,
        wrong,
        total - unified - fragmented - wrong,
        total,
        sorted(set(cert.values())),
    )


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", choices=["kaggle", "semantic"], default="kaggle")
    ap.add_argument("--arms", nargs="+", default=["A", "B"], choices=list("ABCD"))
    args = ap.parse_args()
    docs, truth = load_dataset(args.dataset)
    classes = len(set(truth.values()))
    total = len(docs)
    print(
        f"dataset={args.dataset}  records={total}  distinct raw values={len(truth)}  true classes={classes}\n"
    )

    live = any(a in args.arms for a in "CD")
    settings = Settings.from_env() if live else Settings()
    settings = dataclasses.replace(settings, use_mock_muse=not live)
    with tempfile.TemporaryDirectory() as bin_dir:
        binary = Path(bin_dir) / "trust-engine"
        subprocess.run(
            ["go", "build", "-o", str(binary), "."], cwd=ROOT / "trust-engine", check=True
        )
        labels = {
            "A": "A fixed taxonomy",
            "B": "B rules + evidence check",
            "C": "C Muse + evidence check",
            "D": "D Muse, NO check (risk)",
        }
        print(
            f"{'arm':28}{'unified':>13}{'fragmented':>13}{'wrong':>7}{'held':>13}{'terms':>7}{'syn.':>6}{'refused':>9}{'vocab':>9}"
        )
        # vocab = certified terms / true classes: 1.0 is a clean vocabulary; above 1
        # means classes were split over several terms (what "fragmented" counts in records)
        details = []
        for arm in args.arms:
            with tempfile.TemporaryDirectory() as tmp:
                tax_dir = Path(tmp) / "taxonomy"
                tax_dir.mkdir()
                report = await prepare(arm, docs, tax_dir, settings)
                os.environ["TAXONOMY_DIR"] = str(
                    tax_dir
                )  # the API reads the same directory as the engine
                engine = Engine(str(binary), tax_dir)
                try:
                    out = build([dict(d) for d in docs], tax_dir, engine, tmp)
                finally:
                    engine.stop()
                ok, frag, wrong, held, tot, terms = score(out, docs, truth)
                new_terms = len(
                    set(terms)
                    - {
                        t.term
                        for t in Taxonomy.model_validate_json(
                            (
                                ROOT / "data/mock/taxonomy/taxonomy-product-2026-10-27-v1.json"
                            ).read_text()
                        ).terms
                    }
                )
                print(
                    f"{labels[arm]:28}{ok:>6} ({ok / tot:>4.0%}){frag:>6} ({frag / tot:>3.0%}){wrong:>7}"
                    f"{held:>7} ({held / tot:>3.0%}){new_terms:>7}{report['accepted']:>6}{report['rejected']:>9}"
                    f"{(len(terms) / classes if terms else 0):>9.2f}"
                )
                details.append((arm, terms, report["rejected_examples"]))
    for arm, terms, rejected in details:
        if arm in "CD" and rejected:
            print(f"\n{labels[arm]}: first refused proposals")
            for term, syn, why in rejected[:8]:
                print(f"  {syn!r} -> {term!r}: {why}")
        if arm in "CD":
            print(f"{labels[arm]}: certified terms {terms}")


if __name__ == "__main__":
    asyncio.run(main())
