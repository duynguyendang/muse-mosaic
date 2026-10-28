#!/usr/bin/env python3
"""Build a frozen Mosaic sample from the Kaggle Customer 360 dump.

Reads the raw CSVs (never modifies them), deterministically selects:
  - 10 duplicate-customer_id groups WITH conflicting names,
  - 10 duplicate-customer_id groups that are exact duplicates,
  - unique-customer rows to fill up to --crm-rows CRM rows,
plus all in-scope orders of those customers (valid order_date only),
the referenced products, and writes ONE frozen JSON file:
  {"documents": [...], "meta": {...provenance, counts, dropped...}}.

Document design (mirrors data/mock/hidden fixtures):
  - identity doc per CRM row WITHOUT orders (source crm),
  - per (CRM row, order): one B-doc (identity fields + that order_id, source crm)
    and one O-doc (order_id + product_ref, source orders) sharing the order_id,
    so verified purchase edges are provable exactly like F2<->F5;
  - one P-doc per referenced product with a category (source catalog).

Honesty rules enforced here, not in prose:
  - no invented contact values: ticket table is NOT used (no mappable v1 field);
  - event_time is the import batch timestamp for every fact (ANCHOR_MAX).
    The source dates (signup 2013-2018, order dates) span ~2.6M minutes and the
    TTL-adjusted times fall outside the engine's mangle number bound (+/-1e6,
    G-20): a real engine constraint this import hits honestly. Spreading them
    would either expire almost everything (R3, standard 1h TTL) or require an
    invented TTL. Batch timestamp keeps every fact live; recency is therefore
    NOT asserted from this data (R1 decides on authority+verified, ties -> R2);
  - TTL 3600s, identical to the synthetic fixtures;
  - verified=true for crm/catalog/orders facts: the CRM master, order and
    catalog tables are each the owning system's own record (the sap-like role
    in the synthetic fixtures: rank 1 + verified). Unverified is reserved for
    unattested claims (tickets, injections) — of which this sample has none,
    since tickets carry no mappable field. R2/R4 still fire on conflicts and
    unknown taxonomy terms; unanimous verified facts publish via R1.
  - orders with missing order_date and products with missing category are
    DROPPED and counted (they cannot form valid facts), never guessed;
  - sources are crm/orders/catalog (the dataset's own tables).

Usage:
  python3 scripts/kaggle_customer360_sample.py [--crm-rows 200] [--out FILE]
  [--c360 DIR] [--stats-only]
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

VNH = timezone(timedelta(hours=7))
ANCHOR_MAX = datetime(2026, 10, 27, 9, 0, 0, tzinfo=VNH)
TTL_SECONDS = 3600  # same as the synthetic fixtures

BASE_DEFAULT = Path.home() / ".cache/kagglehub/datasets/vinaykandimalla/customer-360/versions/1"


def read_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8", errors="replace") as fh:
        return list(csv.DictReader(fh))


def parse_dt(raw: str) -> datetime | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=VNH)
        except ValueError:
            continue
    return None


def field(value: str, event_iso: str, rank: int = 1) -> dict:
    return {
        "value": value,
        "authority_rank": rank,
        "event_time": event_iso,
        "verified": True,
        "ttl_seconds": TTL_SECONDS,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--crm-rows", type=int, default=200)
    parser.add_argument("--out", type=str, default="data/kaggle/customer360_sample_200.json")
    parser.add_argument("--c360", type=str, default=str(BASE_DEFAULT))
    parser.add_argument("--stats-only", action="store_true")
    args = parser.parse_args()

    base = Path(args.c360)
    crm = read_csv(base / "crm_50000_customers_dirty_v3.csv")
    orders = read_csv(base / "orders_300k_dirty.csv")
    products = read_csv(base / "product_catalog_dirty_30pct.csv")

    by_id: dict[str, list[tuple[int, dict]]] = defaultdict(list)
    for i, row in enumerate(crm):
        by_id[row["customer_id"]].append((i, row))

    dup_groups = {k: v for k, v in by_id.items() if len(v) > 1}
    name_conflict, exact_dup, casevariant = [], [], []
    for key in sorted(dup_groups):
        group = dup_groups[key]
        names = {
            ((r["first_name"] or "") + " " + (r["last_name"] or "")).strip().lower()
            for _, r in group
        }
        if len(names) > 1:
            name_conflict.append(key)
            continue
        full = {tuple(sorted((kk, (vv or "").strip()) for kk, vv in r.items())) for _, r in group}
        if len(full) == 1:
            exact_dup.append(key)
        else:
            # Same person up to whitespace/case only: the v1 normalizer keeps
            # them distinct -> honest R2 material, like the name conflicts.
            casevariant.append(key)

    picked: list[tuple[int, dict]] = []
    seen: set[int] = set()
    for key in name_conflict[:10] + exact_dup[:10] + casevariant[:10]:
        for i, row in by_id[key]:
            if i not in seen:
                seen.add(i)
                picked.append((i, row))
    uniques = [(i, r) for i, r in enumerate(crm) if len(by_id[r["customer_id"]]) == 1]
    for i, row in uniques:
        if len(picked) >= args.crm_rows:
            break
        seen.add(i)
        picked.append((i, row))
    picked.sort(key=lambda item: item[0])

    picked_ids = {row["customer_id"] for _, row in picked}
    cust_orders: dict[str, list[dict]] = defaultdict(list)
    dropped_orders = 0
    for order in orders:
        if order["customer_id"] not in picked_ids:
            continue
        if not (order.get("order_date") or "").strip() or parse_dt(order["order_date"]) is None:
            dropped_orders += 1
            continue
        cust_orders[order["customer_id"]].append(order)
    for key in cust_orders:
        cust_orders[key].sort(key=lambda o: o["order_id"] or "")

    referenced_products = sorted(
        {
            o["product_id"]
            for docs in cust_orders.values()
            for o in docs
            if (o.get("product_id") or "").strip()
        }
    )
    prod_by_id = {p["product_id"]: p for p in products}
    kept_products = [prod_by_id[pid] for pid in referenced_products if pid in prod_by_id]
    dropped_products = sum(
        1
        for pid in referenced_products
        if pid not in prod_by_id or not (prod_by_id[pid].get("category") or "").strip()
    )
    kept_products = [p for p in kept_products if (p.get("category") or "").strip()]

    stats = {
        "crm_rows_total": len(crm),
        "crm_rows_picked": len(picked),
        "name_conflict_groups_available": len(name_conflict),
        "name_conflict_groups_used": min(10, len(name_conflict)),
        "exact_dup_groups_available": len(exact_dup),
        "exact_dup_groups_used": min(10, len(exact_dup)),
        "case_variant_groups_available": len(casevariant),
        "case_variant_groups_used": min(10, len(casevariant)),
        "customers_picked": len(picked_ids),
        "orders_kept": sum(len(v) for v in cust_orders.values()),
        "orders_dropped_missing_date": dropped_orders,
        "products_referenced": len(referenced_products),
        "products_kept": len(kept_products),
        "products_dropped_missing_category": dropped_products,
    }
    print(json.dumps(stats, indent=2))
    if args.stats_only:
        return 0

    # Event time: single import-batch timestamp for every fact (see module
    # docstring for why the source dates are not spread out).
    default_event = ANCHOR_MAX.isoformat()
    order_seq = 0
    documents = []
    for row_idx, row in picked:
        signup_iso = default_event
        name = ((row["first_name"] or "") + " " + (row["last_name"] or "")).strip()
        identity_fields = {}
        if name:
            identity_fields["display_name"] = field(name, signup_iso)
        if (row["email"] or "").strip():
            identity_fields["email"] = field(row["email"].strip(), signup_iso)
        if (row["phone_number"] or "").strip():
            identity_fields["phone"] = field(row["phone_number"].strip(), signup_iso)
        if (row["address"] or "").strip():
            identity_fields["address"] = field(row["address"].strip(), signup_iso)
        customer_orders = cust_orders.get(row["customer_id"], [])
        if not customer_orders:
            if identity_fields:
                documents.append(
                    {
                        "document_id": f"kag-crm-{row_idx:06d}",
                        "entity_type": "user",
                        "source": "crm",
                        "synthetic": True,
                        "fields": identity_fields,
                    }
                )
            continue
        for order in customer_orders:
            order_seq += 1
            order_iso = default_event
            order_fields = dict(identity_fields)
            order_fields["order_id"] = field(order["order_id"], order_iso)
            # B-doc: identity + this order (join key), source crm.
            b_fields = {
                k: dict(v, event_time=order_iso) if k in identity_fields else v
                for k, v in order_fields.items()
            }
            documents.append(
                {
                    "document_id": f"kag-crm-{row_idx:06d}-ord-{order_seq:06d}",
                    "entity_type": "user",
                    "source": "crm",
                    "synthetic": True,
                    "fields": b_fields,
                }
            )
            # O-doc: bare order record, source orders — shares order_id with its B-doc.
            o_fields = {"order_id": field(order["order_id"], order_iso)}
            if (order.get("product_id") or "").strip():
                o_fields["product_ref"] = {
                    "value": order["product_id"].strip(),
                    "event_time": order_iso,
                    "verified": True,
                    "ttl_seconds": TTL_SECONDS,
                }
            documents.append(
                {
                    "document_id": f"kag-ord-{order_seq:06d}",
                    "entity_type": "user",
                    "source": "orders",
                    "synthetic": True,
                    "fields": o_fields,
                }
            )

    for j, product in enumerate(sorted(kept_products, key=lambda p: p["product_id"])):
        documents.append(
            {
                "document_id": f"kag-prod-{j:06d}",
                "entity_type": "product",
                "source": "catalog",
                "synthetic": True,
                "fields": {"category": field(product["category"].strip(), default_event)},
            }
        )

    meta = {
        "source": "Kaggle vinaykandimalla/customer-360 (synthetic per dataset card), frozen sample",
        "note": "Deterministic selection: 10 name-conflict + 10 exact-dup + 10 whitespace/case-variant customer_id groups, unique fill by file order. B-docs repeat their order's order_id as the join key (+ product_ref on O-docs); contact values come from the CRM row only. Tickets excluded: no v1-mappable field. All event_times are one import-batch timestamp (real date spread exceeds the engine mangle bound); TTL 3600s = synthetic fixtures; verified=true: CRM/catalog/orders are each the owning system's own record (sap-like role in fixtures), unverified reserved for unattested claims.",
        "policy": "trust-engine policies pol-kaggle-2026-10-28-v1 (user) + pol-product-kaggle-2026-10-28-v1 (product); staged under data/kaggle/policies/, composed at run time, never auto-loaded",
        "counts": {**stats, "documents": len(documents)},
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"documents": documents, "meta": meta}, indent=1, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {out} ({len(documents)} documents)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
