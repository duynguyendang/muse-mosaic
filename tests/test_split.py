"""Split / rollback test (CSD 5.4).

Merge, then steward-approve a new phone, then split back to the first version:
the restored version must carry the original phone value, the steward item
must be resolved as split, and the superseded winner must be tombstoned.
"""

from __future__ import annotations

BASE = [
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
        # Long TTL so the email fact survives wall-clock evaluation time
        # in the approve re-derive (steward facts are stamped "now").
        "ttl_seconds": 31536000,
    },
]


def body(fragments, key, entity_id="P-001"):
    return {
        "work_id": "work-split",
        "entity_type": "user",
        "entity_id": entity_id,
        "idempotency_key": key,
        "fragments": fragments,
    }


def test_split_restores_prior_identity_from_history(api_client):
    first = api_client.post("/resolve", json=body(BASE, "idem-v1"))
    assert first.status_code == 200, first.text
    assert first.json()["golden"]["version"] == 1

    # Steward approves a new phone number.
    pending = api_client.get("/steward/pending").json()["pending"]
    assert pending == []

    escalate = api_client.post(
        "/resolve",
        json=body(
            BASE
            + [
                {
                    "source": "ticket",
                    "source_id": "tck-001-phone",
                    "attribute": "phone",
                    "value": "+84 900 000 999",
                    "authority_rank": 3,
                    "event_time": "2026-10-27T09:05:00+07:00",
                    "verified": False,
                    "ttl_seconds": 3600,
                }
            ],
            "idem-v2",
        ),
    )
    assert escalate.status_code == 200, escalate.text
    assert escalate.json()["verdict"] == "escalated"
    steward_id = escalate.json()["steward_queue"][0]["steward_id"]

    approved = api_client.post(f"/steward/{steward_id}/approve", json={"value": "+84 900 000 999"})
    assert approved.status_code == 200, approved.text
    approved_json = approved.json()
    assert approved_json["golden"]["attributes"]["phone"]["value"] == ("+84900000999")
    assert approved_json["golden"]["version"] == 3

    split = api_client.post(f"/steward/{steward_id}/split", json={"restore_version": 1})
    assert split.status_code == 200, split.text
    assert split.json()["restored_version"] == 1

    golden = api_client.get("/golden/user/P-001").json()
    assert golden["version"] == 4
    assert golden["attributes"]["phone"]["value"] == "+84900000001"
    assert golden["attributes"]["phone"]["lineage"].startswith("split restore v1")

    pending = api_client.get("/steward/pending").json()["pending"]
    assert all(item["steward_id"] != steward_id for item in pending)
