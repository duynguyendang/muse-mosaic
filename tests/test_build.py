"""End-to-end v2.3 flow tests: /build, /upload, /graph, /downstream, Accept.

Uses the hidden-cluster fixtures through the real pipeline (Splink +
sub-agents + manglekit + structure gate). Mock Muse mode: no model calls.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("te_url")


@pytest.fixture()
def api_client(te_url, matching_engine, tmp_path):
    """API client wired to the live trust-engine AND the live matching-engine,
    so /build exercises the real Match stage end to end."""
    from conftest import make_api_client

    with make_api_client(te_url, tmp_path, matching_service_url=matching_engine) as client:
        yield client


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
                assert (
                    field.get("winner_fact_id", "").find("F6-") == -1
                ), "trap fact must never win in the main cluster"


def test_build_is_idempotent(api_client):
    first = build(api_client, hidden_documents(), "build-idem")
    assert first.status_code == 200, first.text
    second = build(api_client, hidden_documents(), "build-idem")
    assert second.status_code == 200, second.text
    assert second.json()["decision_id"] == first.json()["decision_id"]


def test_build_nests_verified_purchase_and_survives_rebuild(api_client):
    """Nested purchased_products end-to-end on synthetic fixtures only.

    A verified purchase (exact order_id shared by two docs + published product
    golden) nests into the user golden with L1 lineage; rebuilding over a
    golden that already contains nested products must not crash the
    tombstone path (list-valued previous attributes have no single winner).
    """
    ts = "2026-10-27T09:00:00+07:00"

    def frag(source, sid, attribute, value, rank=1, verified=True):
        return {
            "source": source,
            "source_id": sid,
            "attribute": attribute,
            "value": value,
            "authority_rank": rank,
            "event_time": ts,
            "verified": verified,
            "ttl_seconds": 3600,
        }

    def doc_field(value, rank=1, verified=True):
        return {
            "value": value,
            "authority_rank": rank,
            "event_time": ts,
            "verified": verified,
            "ttl_seconds": 3600,
        }

    resolve = api_client.post(
        "/resolve",
        json={
            "work_id": "work-nest-prod",
            "idempotency_key": "nest-prod",
            "entity_type": "product",
            "entity_id": "prod-nest-1",
            "fragments": [frag("pim-like", "nest-cat", "category", "Smartphone")],
        },
    )
    assert resolve.status_code == 200, resolve.text
    assert resolve.json()["verdict"] == "published", resolve.json()

    order = "ORD-NEST-1"
    docs = [
        {
            "document_id": "nest-b",
            "entity_type": "user",
            "source": "crm",
            "synthetic": True,
            "fields": {
                "email": doc_field("nest@example.com"),
                "phone": doc_field("0900000001"),
                "order_id": doc_field(order),
            },
        },
        {
            "document_id": "nest-o",
            "entity_type": "user",
            "source": "orders",
            "synthetic": True,
            "fields": {
                "order_id": doc_field(order),
                "product_ref": doc_field("prod-nest-1"),
            },
        },
    ]
    first = build(api_client, docs, "build-nest-1")
    assert first.status_code == 200, first.text
    goldens = [g for g in first.json()["goldens"] if g.get("golden_id")]
    assert len(goldens) == 1, first.json()
    purchased = goldens[0]["attributes"].get("purchased_products")
    assert purchased, "verified purchase must nest into the golden"
    assert purchased[0]["name"] == "prod-nest-1"
    assert purchased[0]["category"] == "Smartphone"
    assert purchased[0]["_lineage"]["proof"] == "exact_order"
    assert purchased[0]["_lineage"]["rule_fired"] == "L1"

    # Rebuild over the nested golden (new key): must not crash on the
    # list-valued previous attribute, must keep the nested purchase, and must
    # persist the verified purchased edge in the Data Graph (not only JSON).
    second = build(api_client, docs, "build-nest-2")
    assert second.status_code == 200, second.text
    goldens2 = [g for g in second.json()["goldens"] if g.get("golden_id")]
    assert len(goldens2) == 1, second.json()
    assert goldens2[0]["attributes"].get("purchased_products"), second.json()
    assert goldens2[0]["version"] == 2, second.json()
    graph = api_client.get("/graph/user/cluster-1")
    assert graph.status_code == 200, graph.text
    purchased = [e for e in graph.json()["edges"] if e["edge_type"] == "purchased"]
    assert len(purchased) == 1, graph.json()
    assert purchased[0]["proof"] == "exact_order", purchased
    assert purchased[0]["proposer"] == "graph-analyser", purchased


def test_upload_template_and_csv_rejected(api_client):
    import json
    from pathlib import Path

    template = (
        Path(__file__).resolve().parents[1] / "data" / "mock" / "templates" / "user_profile.json"
    )
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


def test_build_replay_reattaches_subagent_trace(api_client):
    """Idempotent replay returns the original sub-agent trace.

    Regression: replayed builds omitted the proposer trace, so the agent
    ribbon and inspector showed an empty run for replayed decisions.
    """
    first = build(api_client, hidden_documents(), "build-replay-trace")
    assert first.status_code == 200, first.text
    first_proposals = first.json().get("proposals", [])
    assert first_proposals, "expected proposer bundles on first build"
    first_trace = first_proposals[0].get("subagent_trace") or first_proposals[0].get("trace") or []
    assert first_trace, "expected a non-empty sub-agent trace on first build"

    second = build(api_client, hidden_documents(), "build-replay-trace")
    assert second.status_code == 200, second.text
    second_proposals = second.json().get("proposals", [])
    assert any(p.get("replayed") for p in second_proposals), "expected a replayed bundle"
    replayed = [p for p in second_proposals if p.get("replayed")]
    replay_trace = replayed[0].get("subagent_trace") or replayed[0].get("trace") or []
    assert replay_trace, "replayed build must carry the original sub-agent trace"
    assert {s.get("agent") for s in replay_trace} == {s.get("agent") for s in first_trace}
