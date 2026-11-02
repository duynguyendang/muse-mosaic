"""Muse candidate matching: near-matches the exact identifiers miss."""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path

import muse_candidates as mc
import pytest


def doc(doc_id, name, address=None, email=None, phone=None, source="crm"):
    fields = {"display_name": {"value": name}}
    if address:
        fields["address"] = {"value": address}
    if email:
        fields["email"] = {"value": email}
    if phone:
        fields["phone"] = {"value": phone}
    return {"document_id": doc_id, "entity_type": "user", "source": source, "fields": fields}


def cluster(index, *docs):
    return {"cluster_id": f"cluster-{index}", "entity_type": "user", "member_docs": list(docs)}


@pytest.fixture
def clusters():
    return [
        cluster(1, doc("a1", "Maria Day", "12 Elm Street, Hanoi")),
        cluster(2, doc("a2", "maria  DAY", "12 elm st hanoi")),  # same person, no shared id
        cluster(3, doc("b1", "Brian Jones", "5 Oak Road, Hue")),
        cluster(4, doc("b2", "Brad Jones", "90 Pine Lane, Da Nang")),  # different person
        cluster(5, doc("c1", "Linh Tran", "1 Main St")),
    ]


def test_shortlist_is_bounded_and_skips_unrelated(clusters):
    pairs = mc.shortlist(clusters)
    keys = {(p.left, p.right) for p in pairs}
    assert (0, 1) in keys  # Maria Day pair found
    assert not any(4 in k for k in keys)  # Linh Tran matches nobody
    assert len(mc.shortlist(clusters, max_pairs=1)) == 1


def test_shortlist_ignores_overly_common_tokens():
    many = [
        cluster(i, doc(f"d{i}", f"Nguyen Person{i}")) for i in range(mc.COMMON_TOKEN_CLUSTERS + 5)
    ]
    assert mc.shortlist(many) == []  # "nguyen" blocks nothing, so no n^2 blowup


def test_profile_sends_only_name_and_address():
    p = mc.cluster_profile(cluster(1, doc("x", "A B", "addr", email="e@x.com", phone="0900")))
    assert set(p) == {"names", "addresses"}
    assert "e@x.com" not in str(p) and "0900" not in str(p)


def test_mock_judge_merges_only_when_name_and_address_agree(clusters):
    pairs = mc.shortlist(clusters)
    merges = mc.select_merges(pairs, mc.mock_judge(pairs))
    assert [(m[0].left, m[0].right) for m in merges] == [(0, 1)]


def test_only_high_confidence_same_merges(clusters):
    pairs = mc.shortlist(clusters)
    weak = [
        mc.Verdict(pair_id=p.pair_id, verdict="same", confidence="medium", reason="x")
        for p in pairs
    ]
    unsure = [
        mc.Verdict(pair_id=p.pair_id, verdict="unsure", confidence="high", reason="x")
        for p in pairs
    ]
    assert mc.select_merges(pairs, weak) == []
    assert mc.select_merges(pairs, unsure) == []


def test_apply_merges_unions_and_links(clusters):
    pairs = mc.shortlist(clusters)
    merged, applied = mc.apply_merges(clusters, mc.select_merges(pairs, mc.mock_judge(pairs)))
    assert applied == 1 and len(merged) == 4
    big = next(c for c in merged if len(c["member_docs"]) == 2)
    assert big["muse_links"][0]["reason"]
    assert [c["cluster_id"] for c in merged] == [f"cluster-{i}" for i in range(1, 5)]


def test_giant_chains_are_refused():
    chain = [
        cluster(i, doc(f"d{i}", "Same Name", "1 Same St")) for i in range(mc.MAX_MERGED_DOCS + 5)
    ]
    pairs = [mc.CandidatePair(f"p{i}", i, i + 1, 1.0, 1.0) for i in range(len(chain) - 1)]
    verdicts = [
        mc.Verdict(pair_id=p.pair_id, verdict="same", confidence="high", reason="r") for p in pairs
    ]
    merged, _ = mc.apply_merges(chain, mc.select_merges(pairs, verdicts))
    assert max(len(c["member_docs"]) for c in merged) <= mc.MAX_MERGED_DOCS


def test_live_schema_is_strict_mode_compatible():
    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False
                assert set(node["properties"]) == set(node["required"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(mc.JUDGE_SCHEMA)


def test_model_outage_means_no_merges_and_a_label(clusters, monkeypatch):
    from config import Settings

    async def boom(settings, pairs):
        raise RuntimeError("down")

    monkeypatch.setattr(mc, "live_judge", boom)
    settings = dataclasses.replace(Settings(), use_mock_muse=False)
    out, info = asyncio.run(mc.propose_and_merge(settings, clusters))
    assert out == clusters and info["merged"] == 0 and "unavailable" in info["skipped_reason"]


def test_unknown_pair_in_model_output_is_rejected(clusters, monkeypatch):
    from config import Settings

    async def bad(settings, pairs):
        return mc.Verdict.model_validate  # not a list: forces a TypeError path

    async def evil(settings, pairs):
        raise ValueError("verdict for unknown pair 'p99'")

    monkeypatch.setattr(mc, "live_judge", evil)
    settings = dataclasses.replace(Settings(), use_mock_muse=False)
    out, info = asyncio.run(mc.propose_and_merge(settings, clusters))
    assert out == clusters and "rejected" in info["skipped_reason"]


# --- end to end through /build -------------------------------------------

pytestmark_e2e = pytest.mark.usefixtures("te_url")


def _client(te_url, matching_engine, tmp_path, enabled):
    from config import Settings
    from fastapi.testclient import TestClient
    from main import create_app

    settings = Settings(
        use_mock_muse=True,
        trust_engine_url=te_url,
        matching_service_url=matching_engine,
        database_path=str(Path(tmp_path) / "m.db"),
        enable_background_tasks=False,
        muse_candidate_matching=enabled,
    )
    return TestClient(create_app(settings))


def _fragmented_docs():
    base = {
        "authority_rank": 1,
        "event_time": "2026-10-27T09:00:00+07:00",
        "verified": True,
        "ttl_seconds": 3600,
    }

    def d(doc_id, source, **fields):
        return {
            "synthetic": True,
            "document_id": doc_id,
            "entity_type": "user",
            "source": source,
            "fields": {k: {"value": v, **base} for k, v in fields.items()},
        }

    return [
        d(
            "m1",
            "crm",
            display_name="Maria Day",
            address="12 Elm Street Hanoi",
            email="maria.day@old.example",
            phone="0901111111",
        ),
        # same person after a phone AND email change: shares no exact identifier
        d(
            "m2",
            "orders",
            display_name="maria  DAY",
            address="12 elm st hanoi",
            email="mday@new.example",
            phone="0902222222",
        ),
        d(
            "o1",
            "crm",
            display_name="Linh Tran",
            address="1 Main St",
            email="linh@x.example",
            phone="0903333333",
        ),
    ]


def _run(client, key):
    body = {
        "work_id": f"w-{key}",
        "idempotency_key": key,
        "dataset": {"documents": _fragmented_docs()},
    }
    response = client.post("/build", json=body)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.usefixtures("te_url")
def test_off_by_default_leaves_the_person_split(te_url, matching_engine, tmp_path):
    with _client(te_url, matching_engine, tmp_path, False) as client:
        out = _run(client, "off")
    entities = {g["entity_id"] for g in out["goldens"] if g.get("golden_id")}
    assert len(entities) == 3  # Maria twice (two "certified" goldens) + Linh
    assert out["muse_candidates"] is None


@pytest.mark.usefixtures("te_url")
def test_on_merges_the_person_and_the_engine_will_not_certify_it(te_url, matching_engine, tmp_path):
    with _client(te_url, matching_engine, tmp_path, True) as client:
        out = _run(client, "on")
    info = out["muse_candidates"]
    assert info["enabled"] and info["merged"] == 1 and info["clusters_out"] == 2
    # The merged cluster's phone/email disagree and the fuzzy link is no hop:
    # the engine must hold it, not certify one of the two values.
    held = {c["attribute"] for c in out["conflicts"]}
    assert held & {"phone", "email"}, out["conflicts"]
    linh = [g for g in out["goldens"] if g.get("golden_id")]
    assert any(
        g["attributes"].get("email", {}).get("value") == "linh@x.example" for g in linh
    ), "the unrelated customer must still be certified"


# --- identifier hints, tiers and normalised blocking -----------------------


def test_identifier_hints_are_computed_by_code_and_ignore_exact_matches():
    a = {"emails": {"maria.day@x.com"}, "phones": {"4499771729"}}
    typo = {"emails": {"maria.dqy@x.com"}, "phones": {"14499771729"}}  # +1 prefix, 1-char typo
    assert mc.identifier_hints(a, typo) == ["email_near_match", "phone_suffix_match"]
    assert mc.identifier_hints(a, a) == []  # exact matches are not "hints"
    assert mc.identifier_hints(a, {"emails": {"other@y.org"}, "phones": {"5551234567"}}) == []


def test_near_identifier_shortlists_a_pair_even_when_names_differ_but_judge_decides():
    left = cluster(1, doc("a", "Maria Day", email="m@x.com", phone="449-977-1729"))
    right = cluster(2, doc("b", "Totally Different", email="q@y.org", phone="+1 4499771729"))
    pairs = mc.shortlist([left, right])
    assert len(pairs) == 1 and pairs[0].hints == ["phone_suffix_match"]
    # names disagree, so the (mock) judge must not merge on the hint alone
    assert mc.select_merges(pairs, mc.mock_judge(pairs)) == []


def test_merge_tier_records_whether_code_found_identifier_evidence():
    a = cluster(1, doc("a", "Maria Day", "12 Elm St", email="m@x.com", phone="4499771729"))
    b = cluster(2, doc("b", "maria day", None, email="m@x.co", phone="+1 4499771729"))
    pairs = mc.shortlist([a, b])
    merged, _ = mc.apply_merges([a, b], mc.select_merges(pairs, mc.mock_judge(pairs)))
    assert merged[0]["muse_links"][0]["tier"] == "semantic_verified"
    c = cluster(3, doc("c", "Maria Day", "12 Elm Street, Hanoi"))
    d = cluster(4, doc("d", "maria day", "12 elm st hanoi"))
    pairs = mc.shortlist([c, d])
    merged, _ = mc.apply_merges([c, d], mc.select_merges(pairs, mc.mock_judge(pairs)))
    assert merged[0]["muse_links"][0]["tier"] == "semantic_only"


def test_blocking_normalises_identifiers_like_the_facts_the_engine_verifies():
    from routes.build import _exact_identifier_groups

    docs = [
        {
            "document_id": "x",
            "fields": {"phone": {"value": "(449) 977-1729"}, "email": {"value": "A@X.COM"}},
        },
        {
            "document_id": "y",
            "fields": {"phone": {"value": "449.977.1729"}, "email": {"value": "a@x.com"}},
        },
    ]
    groups = _exact_identifier_groups(docs, ["email", "phone"])
    assert sorted(groups[("phone", "4499771729")]) == ["x", "y"]
    assert sorted(groups[("email", "a@x.com")]) == ["x", "y"]
