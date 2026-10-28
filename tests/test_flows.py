"""End-to-end demo flows (CSD 1b + 12.8 scenes 1-3, minus the video).

Flow A: correct merge publishes (authority beats recency).
Flow B: injection-bearing ticket claim escalates the critical phone.
Product: same engine, product policy, PIM wins.
Steward reject: pending item rejected without side effects.
"""

from __future__ import annotations


USER_FRAGMENTS = [
    {
        "source": "salesforce-like",
        "source_id": "sf-001-phone",
        "attribute": "phone",
        "value": "+84 900 000 002",
        "authority_rank": 2,
        "event_time": "2026-10-27T09:00:00+07:00",
        "verified": False,
        "ttl_seconds": 3600,
    },
    {
        "source": "sap-like",
        "source_id": "sap-001-phone",
        "attribute": "phone",
        "value": "+84 900 000 001",
        "authority_rank": 1,
        "event_time": "2026-10-26T09:00:00+07:00",
        "verified": True,
        "ttl_seconds": 172800,
    },
    {
        "source": "ciam-like",
        "source_id": "ciam-001-email",
        "attribute": "email",
        "value": "synthetic.person.p001@example.invalid",
        "authority_rank": 1,
        "event_time": "2026-10-27T08:30:00+07:00",
        "verified": True,
        "ttl_seconds": 7200,
    },
    {
        "source": "salesforce-like",
        "source_id": "sf-001-address",
        "attribute": "address",
        "value": "12 Synthetic Street",
        "authority_rank": 2,
        "event_time": "2026-10-27T09:00:00+07:00",
        "verified": True,
        "ttl_seconds": 3600,
    },
]

TICKET_CLAIM = {
    "source": "ticket",
    "source_id": "tck-001-phone",
    "attribute": "phone",
    "value": "+84 900 000 999",
    "authority_rank": 3,
    "event_time": "2026-10-27T09:05:00+07:00",
    "verified": False,
    "ttl_seconds": 3600,
}

PRODUCT_FRAGMENTS = [
    {
        "source": "pim-like",
        "source_id": "pim-001-category",
        "attribute": "category",
        "value": "Smartphone",
        "authority_rank": 1,
        "event_time": "2026-10-27T09:00:00+07:00",
        "verified": True,
        "ttl_seconds": 3600,
    },
    {
        "source": "erp-like",
        "source_id": "erp-001-category",
        "attribute": "category",
        "value": "Điện thoại",
        "authority_rank": 2,
        "event_time": "2026-10-27T08:00:00+07:00",
        "verified": True,
        "ttl_seconds": 10800,
    },
    {
        "source": "marketplace-like",
        "source_id": "mp-001-category",
        "attribute": "category",
        "value": "Phụ kiện điện thoại",
        "authority_rank": 3,
        "event_time": "2026-10-27T10:00:00+07:00",
        "verified": False,
        "ttl_seconds": 3600,
    },
]


def resolve(client, fragments, key, entity_type="user", entity_id="P-001"):
    return client.post(
        "/resolve",
        json={
            "work_id": f"work-{key}",
            "entity_type": entity_type,
            "entity_id": entity_id,
            "idempotency_key": key,
            "fragments": fragments,
        },
    )


def test_flow_A_correct_merge_publishes(api_client):
    response = resolve(api_client, USER_FRAGMENTS, "flow-a")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["verdict"] == "published"
    assert payload["policy_version"] == "pol-2026-10-27-v1"

    phone = payload["golden"]["attributes"]["phone"]
    assert phone["winner_fact_id"] == "P-001-sap-001-phone"
    assert phone["loser_fact_ids"] == ["P-001-sf-001-phone"]
    assert phone["rule_fired"] == "R1"

    # The mock proposer proposed recency; the engine overruled it.
    assert (
        payload["proposal"]["attributes"]["phone"]["proposed_value"]
        == "+84900000002"
    )
    assert payload["golden"]["attributes"]["phone"]["value"] == "+84900000001"


def test_flow_B_injection_claim_escalates_critical_phone(api_client):
    first = resolve(api_client, USER_FRAGMENTS, "flow-b-1")
    assert first.status_code == 200, first.text

    response = resolve(
        api_client, USER_FRAGMENTS + [TICKET_CLAIM], "flow-b-2"
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["verdict"] == "escalated"

    # The unverified ticket value is never published.
    assert "phone" not in payload["golden"]["attributes"]
    values = [
        value["value"] for value in payload["golden"]["attributes"].values()
    ]
    assert "+84900000999" not in values

    # Other attributes still publish.
    assert "email" in payload["golden"]["attributes"]
    assert "address" in payload["golden"]["attributes"]

    steward = payload["steward_queue"][0]
    assert steward["attribute"] == "phone"
    assert steward["owner"] == "steward-001"
    assert steward["fallback"] == (
        "do not use the new phone for OTP or payment notification"
    )
    reasons = {
        conflict["attribute"]: conflict["reason_code"]
        for conflict in payload["conflicts"]
    }
    assert reasons["phone"] == "UNVERIFIED_CRITICAL_CLAIM"

    # The ledger golden for phone is withdrawn: no trusted value downstream.
    golden = api_client.get("/golden/user/P-001").json()
    assert "phone" not in golden["attributes"]


def test_product_generic_gate_pim_wins(api_client):
    response = resolve(
        api_client, PRODUCT_FRAGMENTS, "flow-p",
        entity_type="product", entity_id="prod-001",
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    # The unmapped marketplace label becomes a steward-only taxonomy proposal,
    # so the verdict is escalated — while the R1 golden still publishes.
    assert payload["verdict"] == "escalated"
    assert payload["policy_version"] == "pol-product-2026-10-27-v1"
    assert payload["taxonomy_version"] == "tax-product-2026-10-27-v1"
    category = payload["golden"]["attributes"]["category"]
    assert category["value"] == "Smartphone"
    assert category["rule_fired"] == "R1"
    reasons = {conflict["reason_code"] for conflict in payload["conflicts"]}
    assert "TAXONOMY_NEW_TERM_PROPOSED" in reasons


def test_steward_reject_leaves_golden_untouched(api_client):
    first = resolve(api_client, USER_FRAGMENTS, "flow-r-1")
    assert first.status_code == 200, first.text
    version_before = first.json()["golden"]["version"]

    escalated = resolve(
        api_client, USER_FRAGMENTS + [TICKET_CLAIM], "flow-r-2"
    )
    steward_id = escalated.json()["steward_queue"][0]["steward_id"]

    rejected = api_client.post(f"/steward/{steward_id}/reject")
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"

    golden = api_client.get("/golden/user/P-001").json()
    assert golden["version"] == version_before + 1
    assert "+84900000999" not in str(golden["attributes"])

    pending = api_client.get("/steward/pending").json()["pending"]
    assert all(item["steward_id"] != steward_id for item in pending)


def test_fragments_endpoint_labels_synthetic_data(api_client):
    response = api_client.get("/fragments/user/P-001")
    assert response.status_code == 200
    payload = response.json()
    assert payload["synthetic"] is True
    assert "SYNTHETIC" in payload["notice"]
    assert len(payload["fragments"]) >= 4


def test_similar_returns_structured_evidence_without_vectors(api_client):
    resolve(
        api_client, PRODUCT_FRAGMENTS, "flow-s-1",
        entity_type="product", entity_id="prod-001",
    )
    resolve(
        api_client,
        [
            dict(PRODUCT_FRAGMENTS[0], source_id="pim-002-category",
                 value="Wireless Earbuds"),
        ],
        "flow-s-2",
        entity_type="product",
        entity_id="prod-002",
    )
    response = api_client.get(
        "/similar?entity_type=product&entity_id=prod-001"
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["vector_recall"].startswith("skipped")
    assert payload["results"], "expected prod-002 facts as candidates"
    for hit in payload["results"]:
        assert hit["vector_score"] is None
        assert "shared_tokens" in hit["evidence"]
