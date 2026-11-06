"""The G4 review queue: suggest persists would-merges; a human labels them.

Labels are recorded, never applied to goldens. The queue is also the input
of future rule induction — the labels are the dataset.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import write_witness


def _client(te_url, matching_engine, tmp_path, **overrides):
    from config import Settings
    from fastapi.testclient import TestClient
    from main import create_app

    base = {
        "use_mock_muse": True,
        "trust_engine_url": te_url,
        "matching_service_url": matching_engine,
        "database_path": str(Path(tmp_path) / "m.db"),
        "enable_background_tasks": False,
        "muse_candidate_matching": True,
        "autonomy_mode": "suggest",
        "muse_auto_witness": write_witness(tmp_path),
    }
    base.update(overrides)
    return TestClient(create_app(Settings(**base)))


PRODUCT_DOCS = [
    {
        "synthetic": True,
        "document_id": "sg-a",
        "entity_type": "product",
        "source": "amazon",
        "fields": {
            "name": {
                "value": "QuickBooks 2007",
                "authority_rank": 1,
                "event_time": "2026-10-27T09:00:00+07:00",
                "verified": True,
                "ttl_seconds": 3600,
            },
            "manufacturer": {
                "value": "Intuit",
                "authority_rank": 1,
                "event_time": "2026-10-27T09:00:00+07:00",
                "verified": True,
                "ttl_seconds": 3600,
            },
            "category": {
                "value": "accounting",
                "authority_rank": 1,
                "event_time": "2026-10-27T09:00:00+07:00",
                "verified": True,
                "ttl_seconds": 3600,
            },
        },
    },
    {
        "synthetic": True,
        "document_id": "sg-b",
        "entity_type": "product",
        "source": "google",
        "fields": {
            "name": {
                "value": "quickbooks 2007",
                "authority_rank": 2,
                "event_time": "2026-10-27T09:00:00+07:00",
                "verified": True,
                "ttl_seconds": 3600,
            },
            "manufacturer": {
                "value": "Intuit",
                "authority_rank": 2,
                "event_time": "2026-10-27T09:00:00+07:00",
                "verified": True,
                "ttl_seconds": 3600,
            },
            "category": {
                "value": "education",
                "authority_rank": 2,
                "event_time": "2026-10-27T09:00:00+07:00",
                "verified": True,
                "ttl_seconds": 3600,
            },
        },
    },
]


def _body(key: str) -> dict:
    return {
        "work_id": f"work-{key}",
        "idempotency_key": key,
        "dataset": {"documents": PRODUCT_DOCS},
    }


@pytest.fixture()
def suggest_client(te_url, matching_engine, tmp_path):
    with _client(te_url, matching_engine, tmp_path) as client:
        yield client


def test_suggest_build_persists_proposals_and_is_stable(suggest_client, tmp_path):
    first = suggest_client.post("/build", json=_body("sg-1"))
    assert first.status_code == 200, first.text

    listing = suggest_client.get("/api/v1/suggestions", params={"status": "pending"}).json()
    proposals = listing["suggestions"]
    assert proposals, "suggest mode must persist the would-merge proposals"
    assert all(p["status"] == "pending" for p in proposals)
    assert any(set(p["left_docs"]) & set(PRODUCT_DOCS[0]) or p["left_docs"] for p in proposals)
    assert all(p["left_docs"] and p["right_docs"] for p in proposals)
    assert all(p["policy_version"] == "pol-product-2026-10-27-v1" for p in proposals)

    # replay under the same key: no new proposals
    count_after_first = len(proposals)
    suggest_client.post("/build", json=_body("sg-1"))
    again = suggest_client.get("/api/v1/suggestions", params={"status": "pending"}).json()
    assert len(again["suggestions"]) == count_after_first

    # a fresh build with different key: same pairs, still no duplicates (unique key)
    suggest_client.post("/build", json=_body("sg-2"))
    third = suggest_client.get("/api/v1/suggestions", params={"status": "pending"}).json()
    assert len(third["suggestions"]) == count_after_first


def test_accept_and_reject_are_recorded_once(suggest_client):
    suggest_client.post("/build", json=_body("sg-accept-1"))
    proposals = suggest_client.get("/api/v1/suggestions", params={"status": "pending"}).json()[
        "suggestions"
    ]
    assert proposals

    proposal_id = proposals[0]["proposal_id"]
    check = suggest_client.get(f"/api/v1/suggestions/{proposal_id}")
    assert check.status_code == 200
    assert check.json()["status"] == "pending"

    accepted = suggest_client.post(
        f"/api/v1/suggestions/{proposal_id}/accept",
        json={"by": "steward-jane", "note": "same product, correct merge"},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["proposal"]["status"] == "accepted"
    assert accepted.json()["proposal"]["resolved_by"] == "steward-jane"

    again = suggest_client.post(
        f"/api/v1/suggestions/{proposal_id}/accept",
        json={"by": "steward-jane"},
    )
    assert again.status_code == 409

    if len(proposals) >= 2:
        rejected = suggest_client.post(
            f"/api/v1/suggestions/{proposals[1]['proposal_id']}/reject",
            json={"by": "steward-jane", "note": "different licence tiers"},
        )
        assert rejected.status_code == 200
        assert rejected.json()["proposal"]["status"] == "rejected"

    # decisions are reflected in the filtered listings
    accepted_list = suggest_client.get("/api/v1/suggestions", params={"status": "accepted"}).json()[
        "suggestions"
    ]
    assert [p["proposal_id"] for p in accepted_list] == [proposal_id]


def test_accept_requires_by(suggest_client):
    suggest_client.post("/build", json=_body("sg-need-by"))
    proposals = suggest_client.get("/api/v1/suggestions", params={"status": "pending"}).json()[
        "suggestions"
    ]
    assert proposals
    response = suggest_client.post(
        f"/api/v1/suggestions/{proposals[0]['proposal_id']}/accept",
        json={"note": "no name given"},
    )
    assert response.status_code == 422


def test_unknown_proposal_is_404(suggest_client):
    assert suggest_client.get("/api/v1/suggestions/mp-nope").status_code == 404
    assert (
        suggest_client.post("/api/v1/suggestions/mp-nope/accept", json={"by": "x"}).status_code
        == 404
    )


def test_suggestion_labels_never_apply_to_goldens(suggest_client):
    """Accepting a merge records the label; the goldens stay as derived."""
    from test_flows import USER_FRAGMENTS

    resolved = suggest_client.post(
        "/resolve",
        json={
            "work_id": "work-no-apply",
            "entity_type": "user",
            "entity_id": "no-apply-P-001",
            "idempotency_key": "no-apply-1",
            "fragments": USER_FRAGMENTS,
        },
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["verdict"] == "published"

    suggest_client.post("/build", json=_body("sg-no-apply"))
    proposals = suggest_client.get("/api/v1/suggestions", params={"status": "pending"}).json()[
        "suggestions"
    ]
    assert proposals
    golden_before = suggest_client.get("/golden/user/no-apply-P-001").json()
    import sqlite3

    db = suggest_client.app.state.settings.database_path
    connection = sqlite3.connect(db)
    try:
        count_before = connection.execute("SELECT COUNT(*) FROM golden_records").fetchone()[0]
    finally:
        connection.close()

    for proposal in proposals:
        suggest_client.post(
            f"/api/v1/suggestions/{proposal['proposal_id']}/accept",
            json={"by": "steward-jane"},
        )

    golden_after = suggest_client.get("/golden/user/no-apply-P-001").json()
    assert golden_after == golden_before
    connection = sqlite3.connect(db)
    try:
        count_after = connection.execute("SELECT COUNT(*) FROM golden_records").fetchone()[0]
    finally:
        connection.close()
    assert count_after == count_before


def test_canary_holds_merges_for_the_queue(te_url, matching_engine, tmp_path):
    with _client(
        te_url, matching_engine, tmp_path, autonomy_mode="auto", autonomy_canary_rate=0.0001
    ) as client:
        response = client.post("/build", json=_body("sg-canary-1"))
        assert response.status_code == 200, response.text
        info = response.json()["muse_candidates"]
        # rate ~0: essentially every merge is held, never applied
        assert info["merged"] == 0
        assert info["would_merge"] >= 1
        assert info["autonomy"]["canary_held"] >= 1
        queued = client.get("/api/v1/suggestions", params={"status": "pending"}).json()
        assert queued["suggestions"], "held canary merges must reach the queue"


def test_canary_applies_when_rate_is_one(te_url, matching_engine, tmp_path):
    with _client(
        te_url, matching_engine, tmp_path, autonomy_mode="auto", autonomy_canary_rate=1.0
    ) as client:
        response = client.post("/build", json=_body("sg-canary-2"))
        assert response.status_code == 200, response.text
        info = response.json()["muse_candidates"]
        assert info["merged"] >= 1
        assert info["would_merge"] == 0


def test_canary_predicate_is_deterministic():
    from routes.build import _canary_predicate

    class _Pair:
        def __init__(self, left, right):
            self.left = left
            self.right = right

    low = _canary_predicate("product", 0.5)
    pair = _Pair(3, 17)
    assert low(pair) == low(pair)
    assert _canary_predicate("product", 0.0) is None
    always = _canary_predicate("product", 1.0)
    assert always(pair) is True
