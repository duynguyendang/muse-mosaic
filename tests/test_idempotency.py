"""Idempotency tests (CSD 12.5): same key+hash replays, same key+hash differs."""

from __future__ import annotations


PHONE_FRAGMENTS = [
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
]


def resolve_body(fragments, key="idem-001", entity_id="P-001"):
    return {
        "work_id": "work-001",
        "entity_type": "user",
        "entity_id": entity_id,
        "idempotency_key": key,
        "fragments": fragments,
    }


def test_same_key_same_hash_returns_same_decision_without_double_publish(
    api_client,
):
    first = api_client.post("/resolve", json=resolve_body(PHONE_FRAGMENTS))
    assert first.status_code == 200, first.text
    first_json = first.json()

    second = api_client.post("/resolve", json=resolve_body(PHONE_FRAGMENTS))
    assert second.status_code == 200, second.text
    second_json = second.json()

    assert second_json["decision_id"] == first_json["decision_id"]
    assert second_json["golden"]["version"] == 1
    assert second_json["golden"]["version"] == first_json["golden"]["version"]

    golden = api_client.get("/golden/user/P-001").json()
    assert golden["version"] == 1


def test_same_key_different_hash_is_rejected_without_publish(api_client):
    first = api_client.post("/resolve", json=resolve_body(PHONE_FRAGMENTS))
    assert first.status_code == 200, first.text

    altered = [dict(PHONE_FRAGMENTS[0], value="+84 900 000 009")]
    conflict = api_client.post("/resolve", json=resolve_body(altered))
    assert conflict.status_code == 409

    golden = api_client.get("/golden/user/P-001").json()
    assert golden["version"] == 1
    assert golden["decision_id"] == first.json()["decision_id"]
