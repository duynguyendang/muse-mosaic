"""matching-engine contract tests (standalone matching engine).

Health reports pinned versions; /match validates its contract, proposes
candidates with per-pair blocking evidence, and never publishes anything.
"""

from __future__ import annotations

import pytest

pytest.importorskip("splink", reason="splink not installed")

SPLINK_PIN = "5.0.0"
DUCKDB_PIN = "2.0.0"


def test_health_reports_pinned_versions(matching_engine):
    import httpx

    response = httpx.get(f"{matching_engine}/health", timeout=10)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["splink_service"] == "ok"
    assert body["service"] == "matching-engine"
    assert body["splink_enabled"] is True
    assert body["splink_version"] == SPLINK_PIN, body
    assert body["duckdb_version"] == DUCKDB_PIN, body


def _match(matching_engine, **overrides):
    import httpx

    payload = {
        "fragments": [
            {
                "fragment_id": "F1",
                "entity_type": "user",
                "email": "an.van@example.com",
                "phone": "0901234567",
            },
            {
                "fragment_id": "F2",
                "entity_type": "user",
                "email": "an.van@example.com",
                "phone": "0901234567",
            },
            {"fragment_id": "F9", "entity_type": "user", "email": "other@example.com"},
        ],
        "policy_version": "pol-2026-10-27-v1",
        "blocking": ["email", "phone", "order_id"],
        "threshold": 0.9,
        "work_id": "work-test-1",
    }
    payload.update(overrides)
    return httpx.post(f"{matching_engine}/match", json=payload, timeout=120)


def test_match_proposes_candidates_with_evidence(matching_engine):
    response = _match(matching_engine)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["proposer"] == "Splink"
    assert body["work_id"] == "work-test-1"
    assert body["policy_version"] == "pol-2026-10-27-v1"
    assert body["splink_version"] == SPLINK_PIN
    assert body["comparisons_applied"] == ["exact", "jaro_winkler"]
    by_member = {}
    for cluster in body["clusters"]:
        for member in cluster["member_ids"]:
            by_member[member] = cluster["cluster_id"]
    assert by_member["F1"] == by_member["F2"]
    assert by_member["F9"] != by_member["F1"]
    assert body["pairs"], "expected at least the F1/F2 candidate pair"
    for pair in body["pairs"]:
        assert pair["proposer"] == "Splink"
        assert pair["blocking_rule"], "each pair must carry blocking evidence"
        assert "meets_threshold" in pair


def test_match_rejects_unknown_blocking_and_comparisons(matching_engine):
    import httpx

    bad_blocking = httpx.post(
        f"{matching_engine}/match",
        json={
            "fragments": [{"fragment_id": "F1"}],
            "policy_version": "pol-2026-10-27-v1",
            "blocking": ["face_shape"],
        },
        timeout=30,
    )
    assert bad_blocking.status_code == 422, bad_blocking.text

    bad_comparison = httpx.post(
        f"{matching_engine}/match",
        json={
            "fragments": [{"fragment_id": "F1"}],
            "policy_version": "pol-2026-10-27-v1",
            "comparisons": ["neural_rerank"],
        },
        timeout=30,
    )
    assert bad_comparison.status_code == 422, bad_comparison.text

    empty = httpx.post(
        f"{matching_engine}/match",
        json={"fragments": [], "policy_version": "pol-2026-10-27-v1"},
        timeout=30,
    )
    assert empty.status_code == 422, empty.text

    unknown_policy = httpx.post(
        f"{matching_engine}/match",
        json={
            "fragments": [{"fragment_id": "F1"}],
            "policy_version": "pol-does-not-exist",
            "known_policy_versions": ["pol-2026-10-27-v1"],
        },
        timeout=30,
    )
    assert unknown_policy.status_code == 422, unknown_policy.text

    category_block = httpx.post(
        f"{matching_engine}/match",
        json={
            "fragments": [
                {"fragment_id": "P1", "entity_type": "product", "category": "ELECTRONICS"},
                {"fragment_id": "P2", "entity_type": "product", "category": "ELECTRONICS"},
                {"fragment_id": "P3", "entity_type": "product", "category": "BOOKS"},
            ],
            "policy_version": "pol-product-kaggle-2026-10-28-v1",
            "blocking": ["category"],
            "threshold": 0.9,
        },
        timeout=60,
    )
    assert category_block.status_code == 200, category_block.text
    cat_body = category_block.json()
    assert cat_body["status"] == "ok", cat_body
    cat_by_member = {}
    for cluster in cat_body["clusters"]:
        for member in cluster["member_ids"]:
            cat_by_member[member] = cluster["cluster_id"]
    assert cat_by_member["P1"] == cat_by_member["P2"]
    assert cat_by_member["P3"] != cat_by_member["P1"]
