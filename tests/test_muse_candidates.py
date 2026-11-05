"""Muse candidate matching: near-matches the exact identifiers miss."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path

import muse_candidates as mc
import pytest
from config import Settings


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

    async def boom(settings, pairs, stats=None):
        raise RuntimeError("down")

    monkeypatch.setattr(mc, "live_judge", boom)
    settings = dataclasses.replace(Settings(), use_mock_muse=False)
    out, info = asyncio.run(mc.propose_and_merge(settings, clusters))
    assert out == clusters and info["merged"] == 0 and "unavailable" in info["skipped_reason"]


def test_unknown_pair_in_model_output_is_rejected(clusters, monkeypatch):
    from config import Settings

    async def bad(settings, pairs):
        return mc.Verdict.model_validate  # not a list: forces a TypeError path

    async def evil(settings, pairs, stats=None):
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
        # the pipeline tests exercise the auto-apply path: the outcome of
        # MUSE_CANDIDATE_MATCHING with an explicit autonomy grant
        autonomy_mode="auto",
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


@pytest.mark.usefixtures("te_url")
def test_build_rejects_an_unknown_entity_type_loudly(te_url, matching_engine, tmp_path):
    """An unsupported type must fail closed, never be silently dropped."""
    base = {
        "authority_rank": 1,
        "event_time": "2026-10-27T09:00:00+07:00",
        "verified": True,
        "ttl_seconds": 3600,
    }
    docs = [
        {
            "synthetic": True,
            "document_id": "v1",
            "entity_type": "vehicle",
            "source": "src",
            "fields": {"name": {"value": "Nope", **base}},
        }
    ]
    with _client(te_url, matching_engine, tmp_path, True) as client:
        response = client.post(
            "/build",
            json={
                "work_id": "w-vehicle",
                "idempotency_key": "vehicle-1",
                "dataset": {"documents": docs},
            },
        )
    assert response.status_code == 422, response.text
    assert "vehicle" in response.text
    assert "user" in response.text and "product" in response.text  # lists supported types


@pytest.mark.usefixtures("te_url")
def test_merged_product_golden_carries_only_publishable_attributes(
    te_url, matching_engine, tmp_path
):
    """A merged product golden must never publish guessed identity.

    The product policy publishes ``category`` and nothing else -- not by
    benign omission but because a wrong product golden poisons every shop
    affiliation that reads it. Muse may merge the two records; the publisher
    may only emit fields the policy allows, never a guessed name or maker.
    """
    base = {
        "authority_rank": 1,
        "event_time": "2026-10-27T09:00:00+07:00",
        "verified": True,
        "ttl_seconds": 3600,
    }

    def product_doc(doc_id, source, **fields):
        return {
            "synthetic": True,
            "document_id": doc_id,
            "entity_type": "product",
            "source": source,
            "fields": {name: {"value": value, **base} for name, value in fields.items()},
        }

    docs = [
        product_doc(
            "p1", "pim", name="QuickBooks 2007", manufacturer="Intuit", category="accounting"
        ),
        product_doc(
            "p2",
            "marketplace",
            name="quickbooks 2007",
            manufacturer="Intuit",
            category="accounting",
        ),
    ]
    with _client(te_url, matching_engine, tmp_path, True) as client:
        response = client.post(
            "/build",
            json={
                "work_id": "w-product-win",
                "idempotency_key": "product-win",
                "dataset": {"documents": docs},
            },
        )
    assert response.status_code == 200, response.text
    for golden in response.json()["goldens"]:
        attrs = golden.get("attributes", {})
        # The guessed identity (a name or maker we merely merged) never publishes.
        assert "name" not in attrs, attrs
        assert "manufacturer" not in attrs, attrs


@pytest.mark.usefixtures("te_url")
def test_product_records_reach_muse_through_a_real_build(te_url, matching_engine, tmp_path):
    """The gate is gone at the HTTP boundary, not only inside the helper.

    Two products, two sources, no shared exact identifier -- the product
    policy blocks on ``category`` and these deliberately disagree on it, so
    only the judge can join them. This is the path the ``entity_type == "user"``
    test used to close: a full /build must still answer 200 with the product
    run reported, whether or not the engine later trusts the link.
    """
    base = {
        "authority_rank": 1,
        "event_time": "2026-10-27T09:00:00+07:00",
        "verified": True,
        "ttl_seconds": 3600,
    }

    def product_doc(doc_id, source, **fields):
        return {
            "synthetic": True,
            "document_id": doc_id,
            "entity_type": "product",
            "source": source,
            "fields": {name: {"value": value, **base} for name, value in fields.items()},
        }

    docs = [
        product_doc(
            "p1", "pim", name="QuickBooks 2007", manufacturer="Intuit", category="accounting"
        ),
        product_doc(
            "p2",
            "marketplace",
            name="quickbooks 2007",
            manufacturer="Intuit",
            category="business software",
        ),
    ]
    with _client(te_url, matching_engine, tmp_path, True) as client:
        response = client.post(
            "/build",
            json={
                "work_id": "w-product",
                "idempotency_key": "product-on",
                "dataset": {"documents": docs},
            },
        )
    assert response.status_code == 200, response.text
    info = response.json()["muse_candidates"]
    assert info["enabled"] and info["merged"] == 1, info
    assert info["clusters_out"] == 1
    assert set(info["by_entity"]) == {"product"}


# --- identifier hints, tiers and normalised blocking -----------------------


def test_identifier_hints_are_computed_by_code_and_ignore_exact_matches():
    a = {"emails": {"a": "maria.day@x.com"}, "phones": {"a": "4499771729"}}
    typo = {"emails": {"b": "maria.dqy@x.com"}, "phones": {"b": "14499771729"}}  # +1, 1-char typo
    assert mc.identifier_hints(a, typo) == ["email_near_match", "phone_suffix_match"]
    assert mc.identifier_hints(a, a) == []  # exact matches are not "hints"
    other = {"emails": {"c": "other.person@y.org"}, "phones": {"c": "5551234567"}}
    assert mc.identifier_hints(a, other) == []


def test_python_features_match_the_engine_definition():
    """Same cases as trust-engine/engine_test.go: Python must not claim evidence
    the engine would reject (it would only be wasted, but drift is a bug)."""
    assert mc.phone_key("+1 449-977-1729") == mc.phone_key("(449) 977-1729") == "499771729"
    assert mc.phone_key("449.977.1729x282") == "499771729"
    assert mc.phone_key("12345") == ""
    base = mc.email_deletion_keys("maria.day@example.com")
    assert base & mc.email_deletion_keys("maria.dqy@example.com")  # substitution
    assert base & mc.email_deletion_keys("maria.daay@example.com")  # insertion
    assert not base & mc.email_deletion_keys("maria.dqq@example.com")  # two edits
    assert not base & mc.email_deletion_keys("maria.day@example.org")  # other domain
    assert mc.email_deletion_keys("short@x.com") == set()  # local part too short


def test_evidence_names_the_documents_holding_the_near_identifiers():
    a = cluster(1, doc("a1", "Maria Day", email="maria.day@x.com", phone="4499771729"))
    b = cluster(2, doc("b1", "maria day", email="maria.dqy@x.com", phone="+1 4499771729"))
    pairs = mc.shortlist([a, b])
    assert {e["proof"] for e in pairs[0].evidence} == {"semantic_phone", "semantic_email"}
    assert all(e["doc_a"] == "a1" and e["doc_b"] == "b1" for e in pairs[0].evidence)


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


def _verifiable_docs():
    """Same person; phone has a country code and the email a typo. Exact blocking
    sees nothing in common, but both differences are checkable by the engine.
    Ranks follow the policy matrix (the engine rejects a rank that contradicts it):
    sap-like phone 1 / name 1 / email 3 / address 3, salesforce-like 2 throughout."""

    def d(doc_id, source, **fields):  # fields: name -> (value, rank)
        stamp = {"event_time": "2026-10-27T09:00:00+07:00", "verified": True, "ttl_seconds": 3600}
        return {
            "synthetic": True,
            "document_id": doc_id,
            "entity_type": "user",
            "source": source,
            "fields": {
                k: {"value": v, "authority_rank": r, **stamp} for k, (v, r) in fields.items()
            },
        }

    return [
        d(
            "m1",
            "sap-like",
            display_name=("Maria Day", 1),
            address=("12 Elm Street Hanoi", 3),
            email=("maria.day@old.example", 3),
            phone=("4499771729", 1),
        ),
        d(
            "m2",
            "salesforce-like",
            display_name=("maria  DAY", 2),
            address=("12 elm st hanoi", 2),
            email=("maria.dqy@old.example", 2),
            phone=("+1 4499771729", 2),
        ),
    ]


@pytest.mark.usefixtures("te_url")
def test_engine_verifies_semantic_evidence_so_the_merge_is_not_chain_broken(
    te_url, matching_engine, tmp_path
):
    with _client(te_url, matching_engine, tmp_path, True) as client:
        body = {
            "work_id": "w-sem",
            "idempotency_key": "sem",
            "dataset": {"documents": _verifiable_docs()},
        }
        response = client.post("/build", json=body)
    assert response.status_code == 200, response.text
    out = response.json()
    assert out["muse_candidates"]["merged"] == 1
    assert out["muse_candidates"]["tiers"] == {"semantic_verified": 1}
    reasons = {c["reason_code"] for c in out["conflicts"]}
    # The merged person is grounded by engine-verified semantic hops, so it is
    # no longer cut off (CHAIN_BROKEN) the way an unverifiable merge is.
    assert "CHAIN_BROKEN" not in reasons, out["conflicts"]
    # Authority now decides the conflicting values (R1); nothing is held.
    published = [g for g in out["goldens"] if g.get("golden_id")]
    assert published, out
    attrs = published[0]["attributes"]
    assert attrs["phone"]["value"] == "4499771729"  # sap-like (rank 1) beats salesforce-like
    assert attrs["phone"]["rule_fired"] == "R1"
    assert out["verdict"] == "published"


def test_co_location_chain_stops_at_the_parts_of_a_muse_merged_cluster():
    """Chaining every fact would ground a merge nobody verified (C2/S3 could never fire)."""
    from datetime import datetime

    from publisher import _batch_links
    from schemas import CandidateFact

    def f(fid):
        return CandidateFact(
            fact_id=fid,
            entity_type="user",
            entity_id="c",
            attribute="phone",
            value="1",
            source="crm",
            source_id=fid,
            authority_rank=1,
            event_time=datetime.fromisoformat("2026-10-27T09:00:00+07:00"),
            verified=True,
            ttl_seconds=3600,
        )

    facts = [f("a1"), f("a2"), f("b1"), f("b2")]
    parts = {"a1": 0, "a2": 0, "b1": 1, "b2": 1}
    chained = {
        (link["from_fact_id"], link["to_fact_id"]) for link in _batch_links(facts, None, [], parts)
    }
    assert chained == {("a1", "a2"), ("b1", "b2")}  # nothing crosses the merge
    # ordinary clusters keep the old behaviour: one chain across all facts
    legacy = {(link["from_fact_id"], link["to_fact_id"]) for link in _batch_links(facts, None, [])}
    assert ("a2", "b1") in legacy


@pytest.mark.usefixtures("te_url")
def test_names_only_merge_is_not_certified_through_build(te_url, matching_engine, tmp_path):
    """No identifier evidence, newest record wins -> the engine must hold it."""

    def d(doc_id, source, minute, **fields):
        stamp = {
            "event_time": f"2026-10-27T09:{minute:02d}:00+07:00",
            "verified": True,
            "ttl_seconds": 3600,
        }
        return {
            "synthetic": True,
            "document_id": doc_id,
            "entity_type": "user",
            "source": source,
            "fields": {
                k: {"value": v, "authority_rank": r, **stamp} for k, (v, r) in fields.items()
            },
        }

    docs = [
        d(
            "m1",
            "salesforce-like",
            0,
            display_name=("Maria Day", 2),
            address=("12 Elm Street Hanoi", 2),
            email=("maria.day@old.example", 2),
            phone=("0901111111", 2),
        ),
        d(
            "m2",
            "salesforce-like",
            5,
            display_name=("maria  DAY", 2),
            address=("12 elm st hanoi", 2),
            email=("mday@new.example", 2),
            phone=("0902222222", 2),
        ),
    ]
    with _client(te_url, matching_engine, tmp_path, True) as client:
        out = client.post(
            "/build", json={"work_id": "w", "idempotency_key": "k", "dataset": {"documents": docs}}
        ).json()
    assert out["muse_candidates"]["tiers"] == {"semantic_only": 1}
    grouping = out["muse_impact"]["grouping"]  # what the UI shows: who was joined, and why
    assert grouping["status"] == "mock" and grouping["model"] is None
    assert grouping["clusters_before"] == 2 and grouping["clusters_after"] == 1
    assert grouping["merges_total"] == 1 and grouping["records_regrouped"] == 2
    merge = grouping["merges"][0]
    assert {merge["a"]["name"], merge["b"]["name"]} == {"Maria Day", "maria  DAY"}
    assert merge["tier"] == "semantic_only" and merge["reason"]
    assert out["muse_impact"]["taxonomy"]["status"] == "off"
    assert out["muse_impact"]["grouping"]["declined"] == []  # the one pair it saw was joined
    reasons = {c["reason_code"] for c in out["conflicts"]}
    assert reasons & {"SEMANTIC_MERGE_UNVERIFIED", "CHAIN_BROKEN"}, out["conflicts"]
    assert not [g for g in out["goldens"] if g.get("golden_id")], "nothing may certify"


# --- configurable merge threshold -----------------------------------------


def _verdicts(pairs, confidence):
    return [
        mc.Verdict(pair_id=p.pair_id, verdict="same", confidence=confidence, reason="r")
        for p in pairs
    ]


def test_min_confidence_controls_what_merges(clusters):
    pairs = mc.shortlist(clusters)
    medium = _verdicts(pairs, "medium")
    assert mc.select_merges(pairs, medium) == []  # default is "high"
    allowed = [p for p in pairs if not mc.house_number_conflict(p)]
    assert len(mc.select_merges(pairs, medium, "medium")) == len(allowed) >= 1
    low = _verdicts(pairs, "low")
    assert mc.select_merges(pairs, low, "medium") == []  # "low" never merges


def test_min_confidence_never_lets_a_non_same_verdict_merge(clusters):
    pairs = mc.shortlist(clusters)
    for verdict in ("different", "unsure"):
        v = [
            mc.Verdict(pair_id=p.pair_id, verdict=verdict, confidence="high", reason="r")
            for p in pairs
        ]
        assert mc.select_merges(pairs, v, "medium") == []


def test_min_confidence_setting_is_validated():
    from config import ConfigurationError, Settings

    Settings(muse_candidate_min_confidence="medium").validate_startup()
    with pytest.raises(ConfigurationError):
        Settings(muse_candidate_min_confidence="low").validate_startup()


# --- scale: no pair cap, cache, parallel judging ---------------------------


def _many_clusters(groups=4, size=40):
    out = []
    for g in range(groups):
        for i in range(size):
            out.append(
                cluster(
                    g * size + i,
                    doc(
                        f"d{g}-{i}",
                        f"Group{chr(65 + g)} Person {i}",
                        f"{i} Street {g}",
                        phone=f"090{g}{i:05d}",
                    ),
                )
            )
    return out


def test_shortlist_has_no_cap_by_default_but_honours_an_explicit_one():
    clusters = _many_clusters()
    assert len(mc.shortlist(clusters)) > 200  # the old global cap hid these
    assert len(mc.shortlist(clusters, max_pairs=200)) == 200
    stats = {}
    pairs = mc.shortlist(clusters, stats=stats)
    assert stats["candidate_pairs"] >= stats["kept_pairs"] == len(pairs)
    assert stats["skipped_per_cluster"] > 0


def _fake_judge(calls, fail_on=None, in_flight=None):
    async def fake(settings, *, model, system, user, schema_name, schema, **kwargs):
        calls.append(1)
        if in_flight is not None:
            in_flight["now"] += 1
            in_flight["peak"] = max(in_flight["peak"], in_flight["now"])
            await asyncio.sleep(0.01)
            in_flight["now"] -= 1
        if fail_on is not None and len(calls) == fail_on:
            raise RuntimeError("batch down")
        payload = json.loads(user.split("\n", 1)[1])
        return (
            {
                "verdicts": [
                    {
                        "pair_id": p["pair_id"],
                        "verdict": "same",
                        "confidence": "high",
                        "reason": "r",
                    }
                    for p in payload
                ]
            },
            {"total_tokens": 10 * len(payload)},
        )

    return fake


def _pairs(n):
    out = []
    for i in range(n):
        out.append(
            mc.CandidatePair(
                pair_id=f"p{i + 1}",
                left=i,
                right=i + 1,
                name_score=0.9,
                address_score=0.9,
                left_profile={"names": [f"a{i}"], "addresses": ["x"]},
                right_profile={"names": [f"a{i + 1}"], "addresses": ["x"]},
            )
        )
    return out


def _live_settings(tmp_path, **kwargs):
    import dataclasses

    return dataclasses.replace(
        Settings(use_mock_muse=False, database_path=str(tmp_path / "m.db")), **kwargs
    )


def test_verdict_cache_reuses_identical_pairs(tmp_path, monkeypatch):
    import meta_client

    calls = []
    monkeypatch.setattr(meta_client, "chat_json", _fake_judge(calls))
    settings = _live_settings(tmp_path)
    pairs = _pairs(45)
    first_stats, second_stats = {}, {}
    first = asyncio.run(mc.live_judge(settings, pairs, stats=first_stats))
    second = asyncio.run(mc.live_judge(settings, pairs, stats=second_stats))
    assert len(first) == len(second) == 45
    assert first_stats["judge_calls"] == 3 and second_stats["judge_calls"] == 0
    assert second_stats["cache_hits"] == 45 and second_stats["tokens"] == 0
    assert len(calls) == 3  # no second round of calls


def test_cache_is_keyed_on_the_prompt_and_model(tmp_path, monkeypatch):
    import meta_client

    calls = []
    monkeypatch.setattr(meta_client, "chat_json", _fake_judge(calls))
    pairs = _pairs(2)
    asyncio.run(mc.live_judge(_live_settings(tmp_path), pairs, stats={}))
    # a different model may not reuse the verdicts
    asyncio.run(
        mc.live_judge(_live_settings(tmp_path, muse_model_id="other-model"), pairs, stats={})
    )
    assert len(calls) == 2


def test_judge_runs_batches_in_parallel_within_the_limit(tmp_path, monkeypatch):
    import meta_client

    in_flight = {"now": 0, "peak": 0}
    monkeypatch.setattr(meta_client, "chat_json", _fake_judge([], in_flight=in_flight))
    settings = _live_settings(tmp_path, muse_judge_concurrency=2)
    verdicts = asyncio.run(mc.live_judge(settings, _pairs(120), stats={}))
    assert len(verdicts) == 120
    assert 1 < in_flight["peak"] <= 2


def test_one_failed_batch_does_not_discard_the_others(tmp_path, monkeypatch):
    import meta_client

    monkeypatch.setattr(meta_client, "chat_json", _fake_judge([], fail_on=2))
    stats = {}
    verdicts = asyncio.run(mc.live_judge(_live_settings(tmp_path), _pairs(60), stats=stats))
    assert stats["judge_failures"] == 1
    assert len(verdicts) == 40  # the two healthy batches still count


def test_all_batches_failing_is_reported_as_unavailable(tmp_path, monkeypatch):
    import meta_client

    async def down(settings, **kwargs):
        raise RuntimeError("down")

    monkeypatch.setattr(meta_client, "chat_json", down)
    settings = dataclasses.replace(
        Settings(use_mock_muse=False, database_path=str(tmp_path / "m.db")),
        muse_verdict_cache=False,
    )
    out, info = asyncio.run(mc.propose_and_merge(settings, _many_clusters()))
    assert out and info["merged"] == 0 and "unavailable" in info["skipped_reason"]


def test_cache_lookup_handles_more_keys_than_sqlite_allows_in_one_query(tmp_path, monkeypatch):
    import meta_client

    calls = []
    monkeypatch.setattr(meta_client, "chat_json", _fake_judge(calls))
    settings = _live_settings(tmp_path)
    pairs = _pairs(1200)  # 1,200 keys > the 999 bound-parameter limit
    first_stats, second_stats = {}, {}
    assert len(asyncio.run(mc.live_judge(settings, pairs, stats=first_stats))) == 1200
    assert len(asyncio.run(mc.live_judge(settings, pairs, stats=second_stats))) == 1200
    assert first_stats["judge_calls"] == 60
    assert second_stats["cache_hits"] == 1200 and second_stats["judge_calls"] == 0


# --------------------------------------------------------------------------
# G1: field roles via taxonomy aliases. G2: blocking for common names.
# --------------------------------------------------------------------------


def _user_taxonomy():
    import json
    from pathlib import Path

    from schemas import Taxonomy

    path = (
        Path(__file__).resolve().parents[1] / "data/mock/taxonomy/taxonomy-user-2026-10-27-v1.json"
    )
    return Taxonomy.model_validate(json.loads(path.read_text()))


def _raw_doc(doc_id, name_key, name, addr_key, addr, phone_key="mobile", phone="0900111222"):
    return {
        "document_id": doc_id,
        "entity_type": "user",
        "source": "crm",
        "fields": {
            name_key: {"value": name},
            addr_key: {"value": addr},
            phone_key: {"value": phone},
        },
    }


def test_other_schema_yields_no_candidates_without_canonicalisation():
    a = cluster(1, _raw_doc("a", "full_name", "Maria Day", "address_line", "12 Elm St"))
    b = cluster(2, _raw_doc("b", "full_name", "maria day", "address_line", "12 elm st"))
    assert mc.shortlist([a, b]) == []  # the failure G1 fixes


def test_canonicalize_maps_aliases_so_other_schemas_link():
    tax = _user_taxonomy()
    docs = [
        _raw_doc("a", "full_name", "Maria Day", "address_line", "12 Elm St"),
        _raw_doc("b", "customer_name", "maria day", "street_address", "12 elm st"),
    ]
    mapped = mc.canonicalize_documents(docs, tax)
    assert set(mapped[0]["fields"]) == {"display_name", "address", "phone"}
    pairs = mc.shortlist([cluster(1, mapped[0]), cluster(2, mapped[1])])
    assert len(pairs) == 1
    assert docs[0]["fields"].keys() >= {"full_name"}  # input untouched


def test_canonicalize_never_overwrites_or_drops_a_value():
    tax = _user_taxonomy()
    d = {
        "document_id": "x",
        "fields": {"display_name": {"value": "Real Name"}, "full_name": {"value": "Other"}},
    }
    out = mc.canonicalize_documents([d], tax)[0]
    assert out["fields"]["display_name"]["value"] == "Real Name"
    assert out["fields"]["full_name"]["value"] == "Other"  # kept, not lost
    plain = {"document_id": "y", "fields": {"display_name": {"value": "A"}}}
    assert mc.canonicalize_documents([plain], tax)[0] is plain
    assert mc.canonicalize_documents([plain], None)[0] is plain


SURNAMES = ["Nguyen", "Tran", "Le", "Pham", "Hoang", "Phan", "Vu", "Dang"]
GIVENS = ["Lan", "Mai", "Linh", "Hoa", "Nam", "Hung", "Tam", "Binh"]


def _common_name_clusters():
    out = []
    for i in range(640):  # 8 x 8 name combos, each word in ~80 clusters: all "hot"
        surname, given = SURNAMES[i % 8], GIVENS[(i // 8) % 8]
        out.append(cluster(i, doc(f"v{i}", f"{surname} Thi {given}", f"{i} Le Loi")))
    out.append(
        cluster(900, doc("w900", "Mai Tran", "77 Hai Ba Trung"))
    )  # reorder of "Tran Thi Mai"
    return out


def test_common_names_block_nothing_by_default():
    clusters = _common_name_clusters()
    assert mc.shortlist(clusters) == []


def test_rarest_pair_blocking_recovers_common_names_and_reports_it():
    clusters = _common_name_clusters()
    target = next(i for i, c in enumerate(clusters) if c["member_docs"][0]["document_id"] == "w900")
    # the true counterpart lives at the same address as the reordered record
    twin = cluster(901, doc("t901", "Tran Thi Mai", "77 Hai Ba Trung"))
    clusters.append(twin)
    stats: dict = {}
    pairs = mc.shortlist(clusters, blocking="rarest_pair", stats=stats)
    keys = {(p.left, p.right) for p in pairs}
    assert (target, len(clusters) - 1) in keys
    assert stats["blocking"] == "rarest_pair" and stats["block_keys"] > 0


def test_zero_candidates_at_scale_is_reported_not_silent():
    clusters = _common_name_clusters()
    stats: dict = {}
    assert mc.shortlist(clusters, stats=stats) == []
    assert "rarest_pair" in stats["warning"] and stats["hot_keys_skipped"] > 0
    blank = [
        cluster(i, {"document_id": f"b{i}", "fields": {"full_name": {"value": "A B"}}})
        for i in range(300)
    ]
    stats = {}
    mc.shortlist(blank, stats=stats)
    assert "field mapping" in stats["warning"]


def test_cjk_two_character_names_can_block():
    a = cluster(1, doc("a", "张伟", "1 Road"))
    b = cluster(2, doc("b", "张伟", "1 Road"))
    assert len(mc.shortlist([a, b])) == 1


def test_unknown_blocking_mode_is_rejected():
    with pytest.raises(ValueError):
        mc.shortlist([], blocking="nope")
    with pytest.raises(Exception, match="MUSE_BLOCKING_MODE"):
        dataclasses.replace(Settings(), muse_blocking_mode="nope").validate_startup()


def test_build_clusters_links_another_schema_through_taxonomy_aliases():
    from policies import load_policies
    from routes.build import _build_clusters
    from taxonomies import load_taxonomies

    docs = [
        _raw_doc(
            "a", "full_name", "Maria Day", "address_line", "12 Elm St", "mobile", "0900111222"
        ),
        _raw_doc(
            "b", "customer_name", "maria day", "street_address", "12 elm st", "tel", "0911999888"
        ),
        _raw_doc("c", "full_name", "Zed Quux", "address_line", "5 Oak Rd", "mobile", "0900111222"),
    ]
    settings = dataclasses.replace(
        Settings(), use_mock_muse=True, muse_candidate_matching=True, autonomy_mode="auto"
    )
    out: dict = {}
    clusters = asyncio.run(_build_clusters(settings, load_policies(), load_taxonomies(), docs, out))
    # a+c share a phone under the alias `mobile` (exact blocking sees it only after
    # canonicalisation); b joins through Muse on name + address (aliases again).
    assert sorted(len(c["member_docs"]) for c in clusters) == [3]
    info = out["muse_candidates"]
    assert info["shortlisted"] >= 1 and info["merged"] == 1


def _pair(left_addr, right_addr, evidence=None):
    return mc.CandidatePair(
        "p1", 0, 1, 0.9, 0.9,
        {"names": ["a b"], "addresses": left_addr},
        {"names": ["a b"], "addresses": right_addr},
        [], evidence or [],
    )  # fmt: skip


def test_house_number_conflict_blocks_neighbours_a_model_calls_typos():
    assert mc.house_number_conflict(_pair(["116 tran hung dao"], ["110 tran hung dao"]))
    assert mc.house_number_conflict(_pair(["35 nguyen trai"], ["135 nguyen trai"]))


def test_house_number_conflict_leaves_real_matches_alone():
    assert not mc.house_number_conflict(_pair(["12 elm street"], ["12 elm st"]))
    assert not mc.house_number_conflict(_pair(["012 elm"], ["12 elm"]))  # leading zero
    assert not mc.house_number_conflict(_pair(["elm street"], ["12 elm st"]))  # one side no number
    assert not mc.house_number_conflict(_pair([], ["12 elm st"]))
    assert not mc.house_number_conflict(
        _pair(["12 elm", "9 oak"], ["9 oak rd"])
    )  # any shared number


def test_house_number_conflict_yields_to_identifier_evidence():
    evidence = [{"proof": "semantic_phone", "doc_a": "a", "doc_b": "b"}]
    assert not mc.house_number_conflict(_pair(["1 a st"], ["2 a st"], evidence))


def test_select_merges_refuses_a_house_number_conflict_even_at_high_confidence():
    pair = _pair(["116 tran hung dao"], ["110 tran hung dao"])
    verdict = mc.Verdict(pair_id="p1", verdict="same", confidence="high", reason="typo")
    assert mc.select_merges([pair], [verdict]) == []


def test_impact_says_off_rather_than_implying_the_model_helped():
    import muse_impact

    impact = muse_impact.build(Settings(), None, None)
    assert (
        impact["grouping"]["status"] == "off"
        and "MUSE_CANDIDATE_MATCHING" in impact["grouping"]["note"]
    )
    assert impact["taxonomy"]["status"] == "off"


def test_impact_labels_a_rules_only_taxonomy_run_as_not_the_model():
    import muse_impact

    ext = {"mode": "rules", "proposer": "rules", "accepted": 3, "rejected": 1, "terms": []}
    live = dataclasses.replace(Settings(), use_mock_muse=False, muse_model_id="muse-x")
    tax = muse_impact.build(live, None, ext)["taxonomy"]
    assert tax["status"] == "rules" and tax["model"] is None
    ext["proposer"] = "muse"
    tax = muse_impact.build(live, None, ext)["taxonomy"]
    assert tax["status"] == "live" and tax["model"] == "muse-x"


def test_impact_endpoint_without_a_build_is_unavailable(te_url, matching_engine, tmp_path):
    with _client(te_url, matching_engine, tmp_path, False) as client:
        assert client.get("/muse/impact").json() == {"available": False, "job_id": None}


def test_declined_report_explains_why_a_pair_was_not_joined():
    near = _pair(["116 tran hung dao"], ["110 tran hung dao"])
    near.pair_id = "p1"
    other = _pair(["1 a st"], ["1 a st"])
    other.pair_id = "p2"
    verdicts = [
        mc.Verdict(pair_id="p1", verdict="same", confidence="high", reason="typo"),
        mc.Verdict(pair_id="p2", verdict="same", confidence="medium", reason="close"),
    ]
    merges = mc.select_merges([near, other], verdicts, "high")
    rows = mc.declined_report([near, other], verdicts, merges)
    assert [r["pair"] if "pair" in r else r["why_not_joined"] for r in rows] == [
        "house numbers differ and no phone or email backs the link",
        "judged same, but below the confidence needed to join",
    ]


def test_impact_labels_a_build_where_only_one_entity_ran():
    """Muse now runs per entity type: one failure must not erase the others.

    The impact view answers "did the model help, and where". If the product
    run failed and the person run succeeded, calling the whole build "skipped"
    hides work that really happened -- and the reverse would claim help that
    never arrived.
    """
    import muse_impact

    outage = "Muse unavailable[rate_limit]: 429"
    info = {
        "mode": "mock",
        "clusters_in": 4,
        "candidate_pairs": 2,
        "judged": 2,
        "merged": 1,
        "skipped_reason": f"product: {outage}",
        "by_entity": {
            "product": {"mode": "mock", "skipped_reason": outage, "merged": 0},
            "user": {"mode": "mock", "skipped_reason": None, "merged": 1},
        },
    }
    grouping = muse_impact.build(Settings(), info, None)["grouping"]
    assert grouping["status"] == "mock"  # a person merge did happen in this build
    assert outage in grouping["skipped_reason"]  # and the failure is still visible

    # every entity skipped is still "skipped", not a model that helped
    all_skipped = {**info, "by_entity": {"product": info["by_entity"]["product"]}}
    assert muse_impact.build(Settings(), all_skipped, None)["grouping"]["status"] == "skipped"


def test_each_entity_reports_its_own_tiers_not_the_combined_total():
    """Regression: ``by_entity`` must carry each run's report, not the sums.

    ``_record_muse_run`` seeds the build-wide total from the first run and adds
    the rest on top. Seeding with the first run's objects let the addition
    rewrite that run's own ``tiers`` dict, so the product report ended up
    showing the product AND person counts and the caller's dict changed too.
    """
    from routes.build import _record_muse_run

    out: dict = {}
    run1 = {"mode": "mock", "merged": 2, "tiers": {"semantic_only": 2}, "skipped_reason": None}
    run2 = {
        "mode": "mock",
        "merged": 5,
        "tiers": {"semantic_only": 3, "semantic_verified": 2},
        "skipped_reason": None,
    }
    _record_muse_run(out, "product", run1)
    _record_muse_run(out, "user", run2)

    report = out["muse_candidates"]
    assert report["merged"] == 7
    assert report["tiers"] == {"semantic_only": 5, "semantic_verified": 2}
    assert report["by_entity"]["product"]["tiers"] == {"semantic_only": 2}
    assert report["by_entity"]["user"]["tiers"] == {"semantic_only": 3, "semantic_verified": 2}
    assert run1["tiers"] == {"semantic_only": 2}  # the caller's object is untouched


def test_skipped_reason_names_the_entity_even_when_only_one_ran():
    """The first-run path used to keep the bare reason: the entity that failed
    was only named once a second entity had also run."""
    from routes.build import _record_muse_run

    one: dict = {}
    reason = "Muse unavailable[rate_limit]: 429"
    _record_muse_run(one, "product", {"mode": "mock", "skipped_reason": reason, "merged": 0})
    assert one["muse_candidates"]["skipped_reason"] == "product: Muse unavailable[rate_limit]: 429"


def test_a_key_the_fold_does_not_know_survives_it():
    """Regression: keys outside the known scalar/list sets were silently
    dropped when a later run reported them (``blocking`` never made it into a
    mixed build). Unknown keys now carry through, last one wins."""
    from routes.build import _record_muse_run

    out: dict = {}
    _record_muse_run(out, "product", {"mode": "mock", "merged": 1, "skipped_reason": None})
    _record_muse_run(
        out,
        "user",
        {"mode": "mock", "merged": 1, "skipped_reason": None, "blocking": "rarest_pair"},
    )
    assert out["muse_candidates"]["blocking"] == "rarest_pair"
    # and a known scalar is still combined, not replaced
    assert out["muse_candidates"]["merged"] == 2
