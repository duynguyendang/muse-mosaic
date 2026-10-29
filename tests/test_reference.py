"""Read-only reference endpoints + demo kill switch.

Covers: hidden dataset shape, taxonomy/policy read endpoints and their
404/422 paths, golden history ordering, demo stop/start toggling health
and forcing the fail-closed path, and per-cluster proposals in /build.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("te_url")


@pytest.fixture()
def api_client(te_url, matching_engine, tmp_path):
    from conftest import make_api_client

    with make_api_client(te_url, tmp_path, matching_service_url=matching_engine) as client:
        yield client


def test_hidden_dataset_lists_f1_to_f6_trap(api_client):
    response = api_client.get("/datasets/hidden")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["synthetic"] is True
    ids = [doc["document_id"] for doc in payload["documents"]]
    assert ids == ["F1", "F2", "F3", "F4", "F5", "F6"]
    trap = next(doc for doc in payload["documents"] if doc["document_id"] == "F6")
    assert trap["trap"] is True
    main = [doc for doc in payload["documents"] if doc["document_id"] == "F1"][0]
    assert main["trap"] is False
    assert set(main["fields"]) >= {"email"}


def test_taxonomy_endpoint_serves_terms_and_aliases(api_client):
    response = api_client.get("/taxonomy/user")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["taxonomy_version"].startswith("tax-user-")
    assert payload["attribute_aliases"]["phone"]
    assert api_client.get("/taxonomy/nosuchtype").status_code == 404


def test_policy_endpoint_serves_json_and_datalog(api_client):
    response = api_client.get("/policies/user/pol-2026-10-27-v1")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["policy_version"] == "pol-2026-10-27-v1"
    assert "authority_matrix" in payload["policy"]
    assert payload["rules_datalog"] is not None
    assert "R1" in payload["rules_datalog"] or "auth_rank" in payload["rules_datalog"]
    assert api_client.get("/policies/user/nope").status_code in (404, 422)
    assert api_client.get("/policies/user/../../etc/passwd").status_code in (404, 422)


def test_golden_history_orders_newest_first(api_client):
    import test_build

    body = {
        "work_id": "work-hist",
        "idempotency_key": "hist-1",
        "dataset": {"documents": test_build.hidden_documents()},
    }
    first = api_client.post("/build", json=body)
    assert first.status_code == 200, first.text
    history = api_client.get("/golden/user/cluster-1/history")
    assert history.status_code == 200, history.text
    versions = history.json()["versions"]
    assert len(versions) >= 1
    assert versions == sorted(versions, key=lambda v: v["version"], reverse=True)
    assert all("decision_id" in entry for entry in versions)
    assert api_client.get("/golden/user/no-such-entity/history").status_code == 404


def test_demo_kill_switch_fails_closed_then_recovers(api_client):
    down = api_client.post("/demo/engine/stop")
    assert down.status_code == 200
    assert api_client.get("/health").json()["trust_engine"] == "down"

    import test_build

    body = {
        "work_id": "work-kill",
        "idempotency_key": "kill-demo-1",
        "dataset": {"documents": test_build.hidden_documents()},
    }
    failed = api_client.post("/build", json=body)
    assert failed.status_code == 503, failed.text
    detail = failed.json()["detail"]
    assert detail["fail_closed"] is True
    assert "steward" in str(detail.get("steward_queue", [])).lower() or detail.get("steward_queue")

    up = api_client.post("/demo/engine/start")
    assert up.status_code == 200
    assert api_client.get("/health").json()["trust_engine"] == "ok"
    first = api_client.post("/build", json=body)
    assert first.status_code == 200, first.text
    assert first.json()["goldens"], "expected a published golden after recovery"
    # Same payload re-derives the same per-cluster decisions (idempotent retry).
    again = api_client.post("/build", json=body)
    assert again.status_code == 200
    assert again.json()["decision_id"] == first.json()["decision_id"]


def test_build_response_carries_proposer_bundles(api_client):
    import test_build

    body = {
        "work_id": "work-prop",
        "idempotency_key": "prop-1",
        "dataset": {"documents": test_build.hidden_documents()},
    }
    response = api_client.post("/build", json=body)
    assert response.status_code == 200, response.text
    proposals = response.json().get("proposals", [])
    assert proposals, "expected per-cluster proposer bundles"
    by_cluster = {p["cluster_id"]: p for p in proposals if not p.get("replayed")}
    assert by_cluster, "expected at least one fresh (non-replayed) cluster"
    first = next(iter(by_cluster.values()))
    assert first["entity_type"] == "user"
    assert "attributes" in first and isinstance(first["attributes"], dict)
    assert isinstance(first.get("trace", []), list)
    assert isinstance(first.get("links", []), list)
