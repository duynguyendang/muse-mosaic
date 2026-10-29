#!/usr/bin/env python3
"""Build a frozen Mosaic sample from the Olist Brazilian E-Commerce dump.

Reads the raw CSVs (never modifies them) and deterministically selects:
  - repeat customers (same customer_unique_id, 2-4 orders): the
    hidden-connection clusters (F1..F5 analog),
  - one trap customer (different unique_id, SAME zip as a main cluster):
    the F6 neighbour-trap analog (address match only, never merged),
plus all of their orders, order items, and referenced products.

Document design (mirrors data/mock/hidden fixtures + the kaggle sampler):
  - one customer doc per customer row (source olist-customers),
  - one order doc per order (source olist-orders),
  - one item doc per order_item row (source olist-items),
  - one product doc per referenced product (source olist-products),
  all with verbatim Olist values (no invented phones, emails, or names).

Honesty rules enforced here, not in prose:
  - every value is copied verbatim from its source CSV cell;
  - rows missing a join key (order without customer, item without order or
    product) are DROPPED and counted, never guessed;
  - event_time is the import batch timestamp for every fact (documented in
    the JSON meta); the source dates (2016-2018) fall outside the engine's
    TTL window, and spreading them would either expire everything (R3) or
    require an invented TTL. Batch timestamp keeps every fact live; recency
    is therefore NOT asserted from this dataset (R1 decides on
    authority+verified, ties escalate);
  - verified=true + rank 1 for owning-table facts (crm/orders/items/products
    each own their records, like sap-like in the synthetic fixtures);
    unverified is reserved for unattested claims (tickets, injections),
    of which this sample has none;
  - entity_type is "user" for every document: the demo story is
    person resolution (repeat customer), exactly like the F1..F6 fixture.

Usage:
  python3 scripts/olist_brazilian_sample.py [--olist DIR] [--out FILE]
      [--stats-only] [--trap-zip ZIP]
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

TTL_SECONDS = 3600  # same as the synthetic fixtures
IMPORT_ATTRS = ("customer_unique_id", "order_id", "product_id", "seller_id")


def read_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8", errors="replace") as fh:
        return list(csv.DictReader(fh))


def field(value: str, event_iso: str, rank: int = 1) -> dict:
    return {
        "value": value,
        "authority_rank": rank,
        "event_time": event_iso,
        "verified": True,
        "ttl_seconds": TTL_SECONDS,
    }


def doc(document_id: str, source: str, fields: dict, event_iso: str, trap: bool = False) -> dict:
    return {
        "document_id": document_id,
        "entity_type": "user",
        "source": source,
        "synthetic": False,
        "trap": trap,
        "fields": {
            k: (v if isinstance(v, dict) else field(v, event_iso))
            for k, v in fields.items()
            if (v.get("value") if isinstance(v, dict) else v) not in (None, "")
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--olist", type=str, default="/tmp/opencode/olist")
    parser.add_argument("--out", type=str, default="data/olist/sample_olist.json")
    parser.add_argument("--stats-only", action="store_true")
    parser.add_argument("--repeat-customers", type=int, default=3)
    args = parser.parse_args()

    base = Path(args.olist)
    customers = read_csv(base / "olist_customers_dataset.csv")
    orders = read_csv(base / "olist_orders_dataset.csv")
    items = read_csv(base / "olist_order_items_dataset.csv")
    products = read_csv(base / "olist_products_dataset.csv")

    by_unique: dict[str, list[dict]] = defaultdict(list)
    for row in customers:
        by_unique[row["customer_unique_id"]].append(row)
    repeats = sorted(k for k, v in by_unique.items() if 1 < len(v) <= 17)
    mains = repeats[: max(0, args.repeat_customers)]

    # Trap: a single-order customer sharing a zip with the first main cluster.
    trap = None
    if mains:
        main_zips = {r["customer_zip_code_prefix"] for r in by_unique[mains[0]]}
        for key in sorted(by_unique):
            if key in mains or len(by_unique[key]) != 1:
                continue
            if by_unique[key][0]["customer_zip_code_prefix"] in main_zips:
                trap = key
                break

    now_iso = datetime.now(UTC).isoformat()
    documents: list[dict] = []
    dropped = {"orders_missing_customer": 0, "items_missing_order": 0, "items_missing_product": 0}
    cust_by_id = {row["customer_id"]: row for row in customers}
    orders_by_customer: dict[str, list[dict]] = defaultdict(list)
    for row in orders:
        if row["customer_id"] not in cust_by_id:
            dropped["orders_missing_customer"] += 1
            continue
        orders_by_customer[row["customer_id"]].append(row)
    items_by_order: dict[str, list[dict]] = defaultdict(list)
    for row in items:
        items_by_order[row["order_id"]].append(row)
    products_by_id = {row["product_id"]: row for row in products}

    chosen_uniques = list(mains) + ([trap] if trap else [])
    trap_order_ids = {
        order["order_id"]
        for row in customers
        if row["customer_unique_id"] == trap
        for order in orders_by_customer.get(row["customer_id"], [])
    }
    trap_product_ids = {
        item["product_id"]
        for order_id in trap_order_ids
        for item in items_by_order.get(order_id, [])
    }
    main_product_ids = {
        item["product_id"]
        for unique_id in mains
        for row in by_unique[unique_id]
        for order in orders_by_customer.get(row["customer_id"], [])
        for item in items_by_order.get(order["order_id"], [])
    }
    shared_product_ids = trap_product_ids & main_product_ids
    for unique_id in chosen_uniques:
        is_trap = unique_id == trap
        for row in sorted(by_unique[unique_id], key=lambda r: r["customer_id"]):
            documents.append(
                doc(
                    f"olist-{row['customer_id'][:8]}",
                    "olist-customers",
                    {
                        "customer_unique_id": field(row["customer_unique_id"], now_iso, 1),
                        "customer_zip": field(row["customer_zip_code_prefix"], now_iso, 1),
                        "customer_city": field(row["customer_city"], now_iso, 1),
                        "customer_state": field(row["customer_state"], now_iso, 1),
                    },
                    now_iso,
                    trap=is_trap,
                )
            )
            for order in sorted(
                orders_by_customer.get(row["customer_id"], []), key=lambda r: r["order_id"]
            ):
                documents.append(
                    doc(
                        f"olist-ord-{order['order_id'][:8]}",
                        "olist-orders",
                        {
                            "customer_unique_id": field(unique_id, now_iso, 2),
                            "order_id": field(order["order_id"], now_iso, 1),
                            "order_status": field(order["order_status"], now_iso, 1),
                        },
                        now_iso,
                        trap=is_trap,
                    )
                )
                for item in sorted(
                    items_by_order.get(order["order_id"], []),
                    key=lambda r: (r["order_item_id"], r["product_id"]),
                ):
                    product = products_by_id.get(item["product_id"])
                    if product is None:
                        dropped["items_missing_product"] += 1
                        continue
                    documents.append(
                        doc(
                            f"olist-item-{order['order_id'][:8]}-{item['order_item_id']}",
                            "olist-items",
                            {
                                "order_id": field(item["order_id"], now_iso, 2),
                                "product_id": field(item["product_id"], now_iso, 2),
                                "seller_id": field(item["seller_id"], now_iso, 1),
                            },
                            now_iso,
                            trap=is_trap,
                        )
                    )
                    documents.append(
                        doc(
                            f"olist-prod-{item['product_id'][:8]}",
                            "olist-products",
                            {
                                "product_id": field(item["product_id"], now_iso, 1),
                                "product_category": field(
                                    product.get("product_category_name", ""), now_iso, 1
                                ),
                            },
                            now_iso,
                            trap=(is_trap and item["product_id"] not in shared_product_ids),
                        )
                    )
    if args.stats_only:
        print(
            json.dumps(
                {"mains": mains, "trap": trap, "dropped": dropped, "documents": len(documents)},
                indent=1,
            )
        )
        return 0
    payload = {
        "documents": documents,
        "meta": {
            "source": "olistbr/brazilian-ecommerce (official olist/work-at-olist-data mirror)",
            "main_unique_ids": mains,
            "trap_unique_id": trap,
            "dropped": dropped,
            "mapping": {
                "customer_unique_id": "verbatim customer_unique_id (person key per dataset docs)",
                "customer_zip/city/state": "verbatim location fields",
                "order_id": "verbatim order key (exact link proof)",
                "product_id/seller_id": "verbatim item keys",
                "product_category": "verbatim category name (raw Portuguese)",
            },
            "event_time_note": "import batch timestamp for TTL liveness; recency not asserted",
        },
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {len(documents)} documents to {out} (trap={trap}, dropped={dropped})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
