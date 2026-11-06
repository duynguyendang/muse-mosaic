"""Release-gate trap suite: the failures that matter in production.

Each test is an attack, not a feature check. All of them must hold on every
release; a regression here means the trust boundary broke, not merely that a
feature changed.

Traps:
  1. direct handler bypass — a golden handed to the write layer around the
     pipeline is refused and nothing is written (no golden, no decision)
  2. tampered stored field — an out-of-band edit to a ledger golden fails
     the integrity check at read time (409); it is never served
  3. expired approval — an expired authoritative fact cannot back a publish
     (the engine never sees it as available evidence)
  4. replayed decision id — the same idempotency key with a different body is
     a 409 with the golden untouched; the same key replays, never re-derives
  5. policy version mismatch — a decision derived under a policy version the
     active one no longer matches is refused at the write gate; an unknown
     policy version never reaches the engine (see test_rules_user G-13)
  6. near-duplicate neighbour with a different house number — same name, same
     street family, different house number: never merged, even at high
     confidence
"""

from __future__ import annotations

import json
import sqlite3

import httpx
import muse_candidates as mc
import pytest

USER_FRAGMENTS = [
    {
        "source": "salesforce-like",
        "source_id": "trap-sf-001",
        "attribute": "phone",
        "value": "+84 900 000 002",
        "authority_rank": 2,
        "event_time": "2026-10-27T09:00:00+07:00",
        "verified": False,
        "ttl_seconds": 3600,
    },
    {
        "source": "sap-like",
        "source_id": "trap-sap-001",
        "attribute": "phone",
        "value": "+84 900 000 001",
        "authority_rank": 1,
        "event_time": "2026-10-26T09:00:00+07:00",
        "verified": True,
        "ttl_seconds": 172800,
    },
]


def _resolve(client, fragments, key, entity_type="user", entity_id="trap-P-001"):
    return client.post(
        "/resolve",
        json={
            "work_id": f"work-{key}",
            "entity_type": entity_type,
            "entity_id": entity_id,
            "idempotency_key": key,
            "fragments": fragments,
        },
    )


# --- 1. direct handler bypass ----------------------------------------------


def test_handler_bypass_with_off_policy_golden_attribute_writes_nothing(api_client):
    """A caller that reaches the write layer with a golden outside the
    policy's golden_attributes is refused: no golden, no decision row."""
    import publisher
    from policies import load_policies
    from schemas import GoldenFact, ResolveRequest

    policies = load_policies()
    product_policy_version = next(
        p.policy_version for p in policies.values() if p.entity_type == "product"
    )
    request = ResolveRequest(
        work_id="work-trap-bypass",
        entity_type="product",
        entity_id="trap-prod-001",
        idempotency_key="trap-bypass-1",
        fragments=[
            {
                "source": "pim-like",
                "source_id": "trap-bypass-1",
                "attribute": "category",
                "value": "accounting",
                "authority_rank": 1,
                "event_time": "2026-10-27T09:00:00+07:00",
                "verified": True,
                "ttl_seconds": 3600,
            }
        ],
    )
    rogue = [
        GoldenFact(
            entity_type="product",
            entity_id="trap-prod-001",
            attribute="name",  # product golden_attributes == ["category"]
            value="Not In Policy",
            winner_fact_id="trap-fake-fact",
            loser_fact_ids=[],
            rule_fired="BYPASS",
            policy_version=product_policy_version,
            taxonomy_version="tax-product-2026-10-27-v1",
            lineage="attacker-constructed",
        )
    ]
    with pytest.raises(publisher.GoldenGateError):
        publisher.persist_decision(
            settings=api_client.app.state.settings,
            request=request,
            facts=[],
            payload_hash="trap-hash-bypass-1",
            decision_id="trap-decision-bypass-1",
            policy_version=product_policy_version,
            taxonomy_version="tax-product-2026-10-27-v1",
            rule_fired="BYPASS",
            verdict="published",
            reason_code=None,
            golden=rogue,
            conflicts=[],
            stewards=[],
            created_at="2026-11-06T00:00:00+00:00",
            publish_golden=True,
            policies=policies,
        )
    db = api_client.app.state.settings.database_path
    connection = sqlite3.connect(db)
    try:
        golden_rows = connection.execute(
            "SELECT COUNT(*) FROM golden_records WHERE entity_id = 'trap-prod-001'"
        ).fetchone()[0]
        decision_rows = connection.execute(
            "SELECT COUNT(*) FROM decisions WHERE idempotency_key = 'trap-bypass-1'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert golden_rows == 0
    assert decision_rows == 0


def test_handler_bypass_with_stale_policy_version_writes_nothing(api_client):
    """A golden derived under a policy version that is no longer active is
    refused at the write gate — version skew never silently publishes."""
    import publisher
    from policies import load_policies
    from schemas import GoldenFact, ResolveRequest

    policies = load_policies()
    request = ResolveRequest(
        work_id="work-trap-stale",
        entity_type="product",
        entity_id="trap-prod-002",
        idempotency_key="trap-stale-1",
        fragments=[
            {
                "source": "pim-like",
                "source_id": "trap-stale-1",
                "attribute": "category",
                "value": "accounting",
                "authority_rank": 1,
                "event_time": "2026-10-27T09:00:00+07:00",
                "verified": True,
                "ttl_seconds": 3600,
            }
        ],
    )
    fake = [
        GoldenFact(
            entity_type="product",
            entity_id="trap-prod-002",
            attribute="category",
            value="accounting",
            winner_fact_id="trap-fake-fact",
            loser_fact_ids=[],
            rule_fired="R1",
            policy_version="pol-product-1999-01-01-v0",
            taxonomy_version="tax-product-2026-10-27-v1",
            lineage="test",
        )
    ]
    with pytest.raises(publisher.GoldenGateError, match="derived under"):
        publisher.persist_decision(
            settings=api_client.app.state.settings,
            request=request,
            facts=[],
            payload_hash="trap-hash-stale-1",
            decision_id="trap-decision-stale-1",
            policy_version="pol-product-1999-01-01-v0",
            taxonomy_version="tax-product-2026-10-27-v1",
            rule_fired="R1",
            verdict="published",
            reason_code=None,
            golden=fake,
            conflicts=[],
            stewards=[],
            created_at="2026-11-06T00:00:00+00:00",
            publish_golden=True,
            policies=policies,
        )
    db = api_client.app.state.settings.database_path
    connection = sqlite3.connect(db)
    try:
        golden_rows = connection.execute(
            "SELECT COUNT(*) FROM golden_records WHERE entity_id = 'trap-prod-002'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert golden_rows == 0


# --- 2. tampered stored field -----------------------------------------------


def test_tampered_golden_is_never_served(api_client):
    """An out-of-band edit to a stored golden fails the ledger integrity
    check at read time. The endpoint refuses (409), it never serves it."""
    response = _resolve(api_client, USER_FRAGMENTS, "trap-tamper")
    assert response.status_code == 200, response.text
    assert response.json()["verdict"] == "published"

    good = api_client.get("/golden/user/trap-P-001")
    assert good.status_code == 200
    assert good.json()["content_sha256"]

    db = api_client.app.state.settings.database_path
    connection = sqlite3.connect(db)
    try:
        row = connection.execute(
            "SELECT attributes FROM golden_records WHERE entity_id = 'trap-P-001'"
        ).fetchone()
        attributes = json.loads(row[0])
        attributes["phone"]["value"] = "+84 900 000 999"  # silent edit
        connection.execute(
            "UPDATE golden_records SET attributes = ? WHERE entity_id = 'trap-P-001'",
            (json.dumps(attributes, sort_keys=True, ensure_ascii=False),),
        )
        connection.commit()
    finally:
        connection.close()

    tampered = api_client.get("/golden/user/trap-P-001")
    assert tampered.status_code == 409
    assert "integrity" in tampered.json()["detail"].lower()


# --- 3. expired approval -----------------------------------------------------


def test_expired_approval_cannot_back_a_publish(te_url):
    """A steward-grade authoritative fact whose TTL has elapsed cannot back
    a golden: fresh evidence loses to expiry only when no fresher fact exists,
    and then the attribute escalates. (Engine semantics: see test_rules_user.)
    """
    t_old = "2026-10-26T09:00:00+07:00"
    t_new = "2026-10-27T09:00:00+07:00"

    def fact(fact_id, attribute, value, source, rank, event_time, verified, ttl):
        return {
            "fact_id": fact_id,
            "entity_type": "user",
            "entity_id": "trap-exp-001",
            "attribute": attribute,
            "value": value,
            "source": source,
            "source_id": f"{fact_id}-src",
            "authority_rank": rank,
            "event_time": event_time,
            "verified": verified,
            "ttl_seconds": ttl,
        }

    response = httpx.post(
        te_url,
        json={
            "decision_id": "dec-trap-exp",
            "entity_type": "user",
            "entity_id": "trap-exp-001",
            "idempotency_key": "idem-trap-exp",
            "payload_hash": "hash-trap-exp",
            "policy_version": "pol-2026-11-02-v1",
            "taxonomy_version": "tax-user-2026-10-27-v1",
            "candidate_facts": [
                fact("trap-exp-old", "phone", "+84-old", "sap-like", 1, t_old, True, 60),
                fact(
                    "trap-exp-fresh",
                    "email",
                    "fresh@example.invalid",
                    "ciam-like",
                    1,
                    t_new,
                    True,
                    3600,
                ),
            ],
        },
        timeout=30,
    )
    assert response.status_code == 200, response.text
    out = response.json()
    # the expired phone is not a winning fact; only the fresh email may publish
    assert out["golden_facts"] and all(row["attribute"] == "email" for row in out["golden_facts"])


# --- 4. replayed decision id --------------------------------------------------


def test_replayed_decision_id_conflict_and_no_duplicate(api_client):
    """Same key, different body: 409 and the golden is untouched. Same key,
    same body: the identical decision replays (no second version)."""
    first = _resolve(api_client, USER_FRAGMENTS, "trap-replay")
    assert first.status_code == 200, first.text
    decision_id = first.json()["decision_id"]
    version = first.json()["golden"]["version"]

    replay = _resolve(api_client, USER_FRAGMENTS, "trap-replay")
    assert replay.status_code == 200
    assert replay.json()["decision_id"] == decision_id
    assert api_client.get("/golden/user/trap-P-001").json()["version"] == version

    tampered = [dict(f, value="+84 000 000 000") for f in USER_FRAGMENTS]
    conflict = _resolve(api_client, tampered, "trap-replay")
    assert conflict.status_code == 409
    golden = api_client.get("/golden/user/trap-P-001")
    assert golden.json()["version"] == version
    assert "+84900000001" in json.dumps(golden.json()["attributes"])  # SAP rank-1 value wins


# --- 5. policy version mismatch ----------------------------------------------
# (the unknown-version case is enforced by the engine as UNKNOWN_POLICY_VERSION,
# covered in test_rules_user G-13; this pins the write-gate side instead.)


# --- 6. near-duplicate neighbour, different house number ------------------------


def _mc_pair(left_addr, right_addr, evidence=None, pair_id="trap-p1"):
    return mc.CandidatePair(
        pair_id, 0, 1, 0.9, 0.9,
        {"names": ["nguyen van a"], "addresses": left_addr},
        {"names": ["nguyen van a"], "addresses": right_addr},
        [], evidence or [],
    )  # fmt: skip


def test_near_duplicate_neighbour_never_merged():
    """Same blocking neighbourhood, same name, different house number: never
    merged — even when the judge says 'same' with high confidence."""
    pairs = [
        _mc_pair(["116 tran hung dao"], ["110 tran hung dao"], pair_id="trap-hn-1"),
        _mc_pair(["35 nguyen trai"], ["135 nguyen trai"], pair_id="trap-hn-2"),
    ]
    verdicts = [
        mc.Verdict(pair_id=pair.pair_id, verdict="same", confidence="high", reason="typo")
        for pair in pairs
    ]
    assert mc.select_merges(pairs, verdicts, "high") == []
    # sanity: the same neighbour under a typo is mergeable when judged same
    real = _mc_pair(["12 elm street"], ["12 elm st"], pair_id="trap-hn-3")
    verdict = mc.Verdict(pair_id="trap-hn-3", verdict="same", confidence="high", reason="typo")
    assert mc.select_merges([real], [verdict], "high")
