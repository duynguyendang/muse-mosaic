"""Product category parity tests (P-01..P-06) through the real derive path.

Same engine, different policy: PIM rank 1 beats ERP rank 2 beats
marketplace rank 3, and frequency never beats authority.
"""

from __future__ import annotations

import pytest

PRODUCT_POLICY = "pol-product-2026-10-27-v1"

T_PIM = "2026-10-27T09:00:00+07:00"
T_ERP = "2026-10-27T08:00:00+07:00"
T_MP = "2026-10-27T10:00:00+07:00"


def fact(
    fact_id, value, source, rank, event_time, verified, ttl=3600,
    entity_id="prod-001",
):
    return {
        "fact_id": fact_id,
        "entity_type": "product",
        "entity_id": entity_id,
        "attribute": "category",
        "value": value,
        "source": source,
        "source_id": f"{fact_id}-src",
        "authority_rank": rank,
        "event_time": event_time,
        "verified": verified,
        "ttl_seconds": ttl,
    }


PRODUCT_TAXONOMY = "tax-product-2026-10-27-v1"


def derive(te_url, facts, key, policy=PRODUCT_POLICY, entity_id="prod-001",
           taxonomy=PRODUCT_TAXONOMY):
    import httpx

    response = httpx.post(
        te_url,
        json={
            "decision_id": f"dec-{key}",
            "entity_type": "product",
            "entity_id": entity_id,
            "idempotency_key": f"idem-{key}",
            "payload_hash": f"hash-{key}",
            "policy_version": policy,
            "taxonomy_version": taxonomy,
            "candidate_facts": facts,
        },
        timeout=30,
    )
    return response.status_code, response.json()


def category(out):
    rows = {row["attribute"]: row for row in out.get("golden_facts", [])}
    return rows["category"]


def test_P01_pim_beats_erp_and_marketplace(te_url):
    status, out = derive(
        te_url,
        [
            fact("fPIM", "Smartphone", "pim-like", 1, T_PIM, True),
            fact("fERP", "Điện thoại", "erp-like", 2, T_ERP, True, ttl=10800),
            fact("fMP", "Phụ kiện điện thoại", "marketplace-like", 3, T_MP, False),
        ],
        key="p01",
    )
    assert status == 200
    assert out["verdict"] == "published"
    golden = category(out)
    assert golden["winner_fact_id"] == "fPIM"
    assert golden["value"] == "Smartphone"
    assert sorted(golden["loser_fact_ids"]) == ["fERP", "fMP"]
    assert golden["rule_fired"] == "R1"


def test_P02_frequency_never_beats_authority(te_url):
    status, out = derive(
        te_url,
        [
            fact("fPIM", "Smartphone", "pim-like", 1, T_PIM, True),
            fact("fE1", "Tai nghe", "erp-like", 2, T_ERP, True),
            fact("fM1", "Tai nghe", "marketplace-like", 3, T_MP, False),
            fact("fM2", "Tai nghe", "marketplace-like", 3, T_MP, False),
        ],
        key="p02",
    )
    assert status == 200
    golden = category(out)
    assert golden["winner_fact_id"] == "fPIM"
    assert golden["value"] == "Smartphone"


def test_P03_clean_product_publishes(te_url):
    status, out = derive(
        te_url,
        [
            fact("fPIM", "Wireless Earbuds", "pim-like", 1, T_PIM, True, entity_id="prod-002"),
            fact("fERP", "Wireless Earbuds", "erp-like", 2, T_ERP, True, ttl=10800, entity_id="prod-002"),
            fact("fMP", "Wireless Earbuds", "marketplace-like", 3, T_MP, True, entity_id="prod-002"),
        ],
        key="p03",
        entity_id="prod-002",
    )
    assert status == 200
    assert out["verdict"] == "published"
    assert category(out)["winner_fact_id"] == "fPIM"


def test_P04_weak_marketplace_claim_is_denied(te_url):
    status, out = derive(
        te_url,
        [fact("fMP", "Phụ kiện điện thoại", "marketplace-like", 3, T_MP, False)],
        key="p04",
    )
    assert status == 200
    assert out["verdict"] == "denied"
    assert out["golden_facts"] == []
    conflicts = {row["attribute"]: row for row in out["conflicts"]}
    assert conflicts["category"]["reason_code"] == "WEAK_PROVENANCE"
    assert conflicts["category"]["rule_fired"] == "R4"


def test_P05_expired_pim_falls_back_to_erp(te_url):
    # Note: mosaic-api normalizes exact synonyms pre-derive (Taxonomist
    # claim), so the engine sees the canonical term here.
    status, out = derive(
        te_url,
        [
            fact("fPIM", "Smartphone", "pim-like", 1, T_PIM, True, ttl=60),
            fact("fERP", "Smartphone", "erp-like", 2, "2026-10-27T09:30:00+07:00", True),
        ],
        key="p05",
    )
    assert status == 200
    golden = category(out)
    assert golden["winner_fact_id"] == "fERP"
    assert golden["rule_fired"] == "R1"


def test_product_taxonomy_unknown_term_escalates(te_url):
    status, out = derive(
        te_url,
        [fact("fMP", "Gaming Chair", "marketplace-like", 3, T_MP, True)],
        key="p07",
    )
    assert status == 200
    assert out["verdict"] == "escalated"
    conflicts = {row["attribute"]: row for row in out["conflicts"]}
    assert conflicts["category"]["reason_code"] == "TAXONOMY_UNKNOWN_TERM"
    assert conflicts["category"]["rule_fired"] == "T1"


def test_P06_unknown_product_attribute_is_rejected(te_url):
    status, out = derive(
        te_url,
        [
            {
                "fact_id": "fX",
                "entity_type": "product",
                "entity_id": "prod-001",
                "attribute": "price",
                "value": "99",
                "source": "pim-like",
                "source_id": "x",
                "authority_rank": 1,
                "event_time": T_PIM,
                "verified": True,
                "ttl_seconds": 3600,
            }
        ],
        key="p06",
    )
    assert status == 400
    assert out["reason_code"] == "ATTRIBUTE_NOT_ALLOWED"
