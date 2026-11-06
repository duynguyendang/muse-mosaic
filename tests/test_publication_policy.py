"""Publication policy ships and runs end-to-end: the newest entity type is
a data drop (policy files), never a code change."""

from __future__ import annotations

API_DOCS = [
    {
        "document_id": "pub-dblp-1",
        "entity_type": "publication",
        "source": "dblp",
        "synthetic": True,
        "fields": {
            "title": {
                "value": "DeepMatcher: a deep matching framework",
                "authority_rank": 1,
                "event_time": "2026-10-27T09:00:00+07:00",
                "verified": True,
                "ttl_seconds": 3600,
            },
            "venue": {
                "value": "SIGMOD",
                "authority_rank": 1,
                "event_time": "2026-10-27T09:00:00+07:00",
                "verified": True,
                "ttl_seconds": 3600,
            },
            "authors": {
                "value": "Mudgal, Li, Rekatsinas",
                "authority_rank": 1,
                "event_time": "2026-10-27T09:00:00+07:00",
                "verified": True,
                "ttl_seconds": 3600,
            },
            "year": {
                "value": "2018",
                "authority_rank": 1,
                "event_time": "2026-10-27T09:00:00+07:00",
                "verified": True,
                "ttl_seconds": 3600,
            },
        },
    },
    {
        "document_id": "pub-acm-1",
        "entity_type": "publication",
        "source": "acm",
        "synthetic": True,
        "fields": {
            "title": {
                "value": "DeepMatcher: A Deep Matching Framework to End-to-end Entity Resolution",
                "authority_rank": 1,
                "event_time": "2026-10-27T09:05:00+07:00",
                "verified": True,
                "ttl_seconds": 3600,
            },
            "venue": {
                "value": "SIGMOD",
                "authority_rank": 1,
                "event_time": "2026-10-27T09:05:00+07:00",
                "verified": True,
                "ttl_seconds": 3600,
            },
            "authors": {
                "value": "Mudgal, Li, Rekatsinas",
                "authority_rank": 1,
                "event_time": "2026-10-27T09:05:00+07:00",
                "verified": True,
                "ttl_seconds": 3600,
            },
            "year": {
                "value": "2018",
                "authority_rank": 1,
                "event_time": "2026-10-27T09:05:00+07:00",
                "verified": True,
                "ttl_seconds": 3600,
            },
        },
    },
]


def test_publication_policy_is_shipped_and_valid() -> None:
    from policies import load_policies

    policies = load_policies()
    assert "publication" in policies
    policy = policies["publication"]
    assert policy.golden_attributes == ["venue"]
    assert set(policy.authority_matrix) >= {"venue", "title"}


def test_publication_build_end_to_end(api_client):
    response = api_client.post(
        "/build",
        json={
            "work_id": "work-pub-e2e",
            "idempotency_key": "pub-e2e-1",
            "dataset": {"documents": API_DOCS},
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["verdict"] in ("published", "escalated")
    assert body["policy_versions"]["publication"] == "pol-publication-2026-11-06-v1"
    for golden in body["goldens"]:
        if golden.get("golden_id"):
            assert set(golden["attributes"]) <= {"venue"}
            assert "title" not in golden["attributes"]
