"""Splink adapter tests (S-01..S-04, CSD 12.7b).

S-01: Splink finds the hidden chain cluster F1-F5 as candidates, F6 separate.
S-02: a Splink cluster that wrongly includes F6 is split by the gate (C1).
S-03: a semantic link Splink misses is added by Muse + taxonomy and publishes.
S-04: Splink unavailable falls back, marks the run, no crash, no silent publish.
"""

from __future__ import annotations

import pytest

splink = pytest.importorskip("splink", reason="splink not installed")


def hidden_documents():
    import json
    from pathlib import Path

    base = Path(__file__).resolve().parents[1] / "data" / "mock" / "hidden"
    documents = []
    for path in sorted(base.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        documents.append({
            "document_id": raw["document_id"],
            "entity_type": raw.get("entity_type", "user"),
            "fields": {
                name: {"value": field["value"]}
                for name, field in raw.get("fields", {}).items()
            },
        })
    return documents


def test_S01_splink_finds_hidden_cluster(api_client):
    from splink_adapter import flatten_documents, run_splink

    rows = flatten_documents(hidden_documents())
    out = run_splink(rows, enabled=True, threshold=0.9,
                     blocking=["email", "phone", "order_id"])
    assert out["status"] == "ok", out
    by_member = {}
    for cluster in out["clusters"]:
        for member in cluster["member_ids"]:
            by_member[member] = cluster["cluster_id"]
    main = by_member["F1"]
    assert all(by_member[f"F{i}"] == main for i in (2, 3, 4, 5))
    assert by_member["F6"] != main
    # Pairs carry candidate weights/probabilities (signals only, never verdicts).
    assert out["pairs"], "expected candidate pairs with weights"
    for pair in out["pairs"]:
        assert "match_weight" in pair and "match_probability" in pair


def test_S02_gate_splits_overmerged_trap(te_url):
    """Simulate a Splink over-merge (F6 in the cluster): C1 fires, F6 never wins."""
    import httpx

    from test_hidden import base_facts, fact, linker_links, T1, T3

    facts = base_facts() + [
        fact("F6", "display_name", "Nguyen Van Binh", "sap-like", 1, T1, True),
        fact("F6", "email", "binh@example.com", "sap-like", 3, T1, True),
        fact("F6", "phone", "0901234568", "sap-like", 1, T3, True),
        fact("F6", "address", "123 Le Loi, HCM", "sap-like", 3, T1, True),
    ]
    links = linker_links(base_facts())
    response = httpx.post(
        te_url,
        json={
            "decision_id": "dec-s02",
            "entity_type": "user",
            "entity_id": "P-HIDDEN",
            "idempotency_key": "idem-s02",
            "payload_hash": "hash-s02",
            "policy_version": "pol-2026-10-27-v1",
            "taxonomy_version": "tax-user-2026-10-27-v1",
            "candidate_facts": facts,
            "links": links,
        },
        timeout=30,
    )
    assert response.status_code == 200, response.text
    out = response.json()
    assert out["verdict"] == "escalated"
    conflicts = {row["attribute"]: row for row in out["conflicts"]}
    assert conflicts["phone"]["reason_code"] == "CLUSTER_CONFLICT"
    golden = {row["attribute"]: row for row in out.get("golden_facts", [])}
    # display_name has no matrix ranks in v1: escalates, never publishes.
    assert "display_name" not in golden
    assert conflicts["display_name"]["reason_code"] == "NO_AUTHORITY_RANKS"
    assert golden["email"]["winner_fact_id"].endswith("F2-email")
    assert golden["address"]["winner_fact_id"].endswith("F3-address")
    assert golden["order_id"]["winner_fact_id"].endswith("F2-order_id")
    won = {row["value"] for row in out.get("golden_facts", [])}
    assert "0901234568" not in won and "Nguyen Van Binh" not in won
    assert not any("F6" in hop for row in out["verified_links"] for hop in (row["from_fact_id"], row["to_fact_id"]))


def test_S03_semantic_link_splink_misses():
    """A pair sharing no blocking key (F1/F3) gets a Muse-proposed link, while
    Splink proposes nothing: the semantic recall is Muse's, never Splink's."""
    from datetime import datetime

    from schemas import CandidateFact
    from subagents import mock_linker

    def fact_obj(fid, attr, value):
        return CandidateFact(
            fact_id=fid, entity_type="user", entity_id="P-HIDDEN",
            attribute=attr, value=value, source="x", source_id=f"{fid}-s",
            authority_rank=3, event_time=datetime.fromisoformat("2026-10-27T09:00:00+07:00"),
            verified=False, ttl_seconds=3600,
        )

    facts = [
        fact_obj("P-HIDDEN-F1-display_name", "display_name", "Nguyen Van An"),
        fact_obj("P-HIDDEN-F3-display_name", "display_name", "An Nguyen"),
    ]
    links, _trace = mock_linker(
        facts,
        {"P-HIDDEN-F1-display_name": "F1", "P-HIDDEN-F3-display_name": "F3"},
    )
    assert any(link.proposer == "Muse" for link in links)
    # Splink on this pair alone finds nothing to block on together.
    from splink_adapter import flatten_documents, run_splink

    rows = flatten_documents([
        {"document_id": "F1", "entity_type": "user",
         "fields": {"display_name": {"value": "Nguyen Van An"}}},
        {"document_id": "F3", "entity_type": "user",
         "fields": {"display_name": {"value": "An Nguyen"}}},
    ])
    out = run_splink(rows, enabled=True, threshold=0.9,
                     blocking=["email", "phone", "order_id"])
    assert out["status"] == "ok"
    assert out["pairs"] == []


def test_S04_splink_unavailable_falls_back():
    """Splink disabled/failed: fallback marked, no crash, no silent publish."""
    from splink_adapter import flatten_documents, run_splink

    rows = flatten_documents([{
        "document_id": "F1", "entity_type": "user",
        "fields": {"email": {"value": "an.van@example.com"}},
    }])
    out = run_splink(rows, enabled=False, threshold=0.9, blocking=["email"])
    assert out["status"] == "unavailable"
    assert "fallback" in out["detail"].lower()
    assert out["clusters"] == [] and out["pairs"] == []
