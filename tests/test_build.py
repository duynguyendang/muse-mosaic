"""End-to-end v2.3 flow tests: /build, /upload, /graph, /downstream, Accept.

Uses the hidden-cluster fixtures through the real pipeline (Splink +
sub-agents + manglekit + structure gate). Mock Muse mode: no model calls.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("te_url")


def hidden_documents():
    import json
    from pathlib import Path

    base = Path(__file__).resolve().parents[1] / "data" / "mock" / "hidden"
    documents = []
    for path in sorted(base.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        documents.append({k: v for k, v in raw.items() if k != "notice"})
    return documents


def build(client, documents, key, expected_structure=None, entity_note=""):
    body: dict = {
        "work_id": f"work-{key}",
        "idempotency_key": key,
        "dataset": {"documents": documents},
    }
    if expected_structure is not None:
        body["expected_structure"] = expected_structure
    return client.post("/build", json=body)


def test_build_hidden_cluster_publishes_golden(api_client):
    response = build(api_client, hidden_documents(), "build-h01")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["taxonomy_version"] == "tax-user-2026-10-27-v1"
    assert payload["splink_status"].startswith("ok")
    goldens = [g for g in payload["goldens"] if g.get("golden_id")]
    assert goldens, "expected at least one published golden"
    by_entity_id = {g["entity_id"]: g for g in goldens}
    # Cluster-1 (F1..F5) publishes the hidden person's golden...
    user = by_entity_id["cluster-1"]
    assert user["attributes"]["phone"]["value"] == "0901234567"
    assert user["attributes"]["phone"]["rule_fired"] == "R1"
    assert user["attributes"]["email"]["value"] == "an.van@example.com"
    # ...while the trap gets its own separate golden (never merged in).
    assert by_entity_id["cluster-2"]["attributes"]["phone"]["value"] == "0901234568"
    for golden in goldens:
        if golden["entity_id"] != "cluster-1":
            continue
        for _name, field in golden["attributes"].items():
            if isinstance(field, dict):
                assert field.get("winner_fact_id", "").find("F6-") == -1, \
                    "trap fact must never win in the main cluster"


def test_build_is_idempotent(api_client):
    first = build(api_client, hidden_documents(), "build-idem")
    assert first.status_code == 200, first.text
    second = build(api_client, hidden_documents(), "build-idem")
    assert second.status_code == 200, second.text
    assert second.json()["decision_id"] == first.json()["decision_id"]


def test_upload_template_and_csv_rejected(api_client):
    import json
    from pathlib import Path

    template = Path(__file__).resolve().parents[1] / "data" / "mock" / "templates" / "user_profile.json"
    data = json.loads(template.read_text(encoding="utf-8"))
    response = api_client.post(
        "/upload",
        files={"file": ("user_profile.json", json.dumps(data), "application/json")},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["detected_entity_type"] in ("user_profile", "user")
    assert payload["fragments_parsed"] >= 1

    bad = api_client.post(
        "/upload",
        files={"file": ("data.csv", "a,b\n1,2\n", "text/csv")},
    )
    assert bad.status_code == 422
    assert "CSV" in bad.json()["detail"]


def test_graph_canvas_and_pathfinder(api_client):
    build(api_client, hidden_documents(), "build-graph")
    response = api_client.get("/graph/user/cluster-1")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["nodes"], "expected graph nodes"
    assert payload["edges"], "expected graph edges"
    types = {edge["edge_type"] for edge in payload["edges"]}
    assert "has_attribute" in types
    assert "verified_link" in types or "proposed_link" in types


def test_downstream_two_answers(api_client):
    # Golden path first: a plain user resolve publishes phone.
    from test_flows import USER_FRAGMENTS, resolve

    resolve(api_client, USER_FRAGMENTS, "flow-ds-1")
    ok = api_client.get("/downstream/otp?entity_type=user&entity_id=P-001")
    assert ok.status_code == 200
    assert ok.json()["answer"] == "golden"
    assert ok.json()["value"] == "+84900000001"
    # Escalated phone (ticket claim) -> downstream must stop, not guess.
    from test_flows import TICKET_CLAIM

    resolve(api_client, USER_FRAGMENTS + [TICKET_CLAIM], "flow-ds-2")
    stopped = api_client.get("/downstream/otp?entity_type=user&entity_id=P-001")
    assert stopped.status_code == 200
    payload = stopped.json()
    assert payload["answer"] == "no_trusted_value"
    assert payload["value"] is None
    assert "fallback" in payload and payload["fallback"]
