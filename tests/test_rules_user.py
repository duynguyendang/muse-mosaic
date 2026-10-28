"""User survivorship parity tests (G-01..G-20) through the real derive path.

Each case POSTs fact JSON to a live trust-engine (manglekit subprocess) and
compares winner fact id, loser fact ids, rule, per-attribute verdict and
policy version. The .mg rules are the system under test; these expectations
are the spec.
"""

from __future__ import annotations

import httpx

USER_POLICY = "pol-2026-10-27-v1"
USER_TAXONOMY = "tax-user-2026-10-27-v1"

T_SAP = "2026-10-26T09:00:00+07:00"
T_SF = "2026-10-27T09:00:00+07:00"
T_CIAM = "2026-10-27T08:30:00+07:00"
T_NEW = "2026-10-27T10:00:00+07:00"


def fact(
    fact_id,
    attribute,
    value,
    source,
    rank,
    event_time,
    verified,
    ttl=3600,
    entity_id="P-001",
):
    return {
        "fact_id": fact_id,
        "entity_type": "user",
        "entity_id": entity_id,
        "attribute": attribute,
        "value": value,
        "source": source,
        "source_id": f"{fact_id}-src",
        "authority_rank": rank,
        "event_time": event_time,
        "verified": verified,
        "ttl_seconds": ttl,
    }


def derive(te_url, facts, key="k", policy=USER_POLICY, entity_id="P-001", taxonomy=USER_TAXONOMY):
    response = httpx.post(
        te_url,
        json={
            "decision_id": f"dec-{key}",
            "entity_type": "user",
            "entity_id": entity_id,
            "idempotency_key": f"idem-{key}",
            "payload_hash": f"hash-{key}",
            "policy_version": policy,
            "taxonomy_version": taxonomy,
            "candidate_facts": facts,
        },
        timeout=30,
    )
    return response.status_code, response.json()


def by_attribute(payload, key="golden_facts"):
    return {row["attribute"]: row for row in payload.get(key, [])}


def test_G01_authority_beats_recency(te_url):
    status, out = derive(
        te_url,
        [
            fact("fA", "phone", "+84-A", "salesforce-like", 2, T_SF, False),
            fact("fB", "phone", "+84-B", "sap-like", 1, T_SAP, True, ttl=172800),
        ],
        key="g01",
    )
    assert status == 200
    assert out["verdict"] == "published"
    golden = by_attribute(out)["phone"]
    assert golden["winner_fact_id"] == "fB"
    assert golden["loser_fact_ids"] == ["fA"]
    assert golden["rule_fired"] == "R1"
    assert golden["policy_version"] == USER_POLICY


def test_G02_email_ciam_authority_wins(te_url):
    status, out = derive(
        te_url,
        [
            fact("fSF", "email", "e@x.invalid", "salesforce-like", 2, T_SF, True),
            fact("fCIAM", "email", "e@x.invalid", "ciam-like", 1, T_CIAM, True),
            fact("fSAP", "email", "e@x.invalid", "sap-like", 3, T_SAP, True),
        ],
        key="g02",
    )
    assert status == 200
    golden = by_attribute(out)["email"]
    assert golden["winner_fact_id"] == "fCIAM"
    assert golden["loser_fact_ids"] == []
    assert golden["rule_fired"] == "R1"


def test_G03_legitimate_verified_ticket_wins_address(te_url):
    status, out = derive(
        te_url,
        [
            fact("fSF", "address", "12 Synthetic Street", "salesforce-like", 2, T_SF, True),
            fact("fT", "address", "22 Synthetic Avenue", "ticket-verified", 1, T_NEW, True),
        ],
        key="g03",
    )
    assert status == 200
    golden = by_attribute(out)["address"]
    assert golden["winner_fact_id"] == "fT"
    assert golden["loser_fact_ids"] == ["fSF"]


def test_G04_address_without_ticket_sf_wins(te_url):
    status, out = derive(
        te_url,
        [
            fact("fSF", "address", "12 Synthetic Street", "salesforce-like", 2, T_SF, True),
            fact("fSAP", "address", "14 Synthetic Street", "sap-like", 3, T_SAP, True, ttl=172800),
            fact("fC", "address", "PO Box 99", "ciam-like", 3, T_CIAM, False),
        ],
        key="g04",
    )
    assert status == 200
    golden = by_attribute(out)["address"]
    assert golden["winner_fact_id"] == "fSF"
    assert sorted(golden["loser_fact_ids"]) == ["fC", "fSAP"]


def test_G05_single_verified_fact_publishes(te_url):
    status, out = derive(
        te_url,
        [fact("fB", "phone", "+84-B", "sap-like", 1, T_SAP, True)],
        key="g05",
    )
    assert status == 200
    assert out["verdict"] == "published"
    assert by_attribute(out)["phone"]["winner_fact_id"] == "fB"


def test_G06_expired_authoritative_fact_is_missing(te_url):
    status, out = derive(
        te_url,
        [
            fact("fB", "phone", "+84-B", "sap-like", 1, T_SAP, True, ttl=60),
            fact("fA", "phone", "+84-A", "salesforce-like", 2, T_SF, False),
        ],
        key="g06",
    )
    assert status == 200
    assert out["verdict"] == "escalated"
    assert out["golden_facts"] == []
    conflict = by_attribute(out, "conflicts")["phone"]
    assert conflict["reason_code"] == "CRITICAL_LOW_CONFIDENCE"
    assert conflict["rule_fired"] == "R2"


def test_G07_all_expired_relative_to_newer_evidence(te_url):
    status, out = derive(
        te_url,
        [
            fact("fB", "phone", "+84-B", "sap-like", 1, T_SAP, True, ttl=3600),
            fact("fE", "email", "e@x.invalid", "ciam-like", 1, "2026-10-28T09:00:00+07:00", True),
        ],
        key="g07",
    )
    assert status == 200
    assert out["golden_facts"] and all(row["attribute"] == "email" for row in out["golden_facts"])
    conflict = by_attribute(out, "conflicts")["phone"]
    assert conflict["reason_code"] == "MISSING_OR_EXPIRED"
    assert conflict["rule_fired"] == "R3"


def test_G08_unverified_only_critical_escalates(te_url):
    status, out = derive(
        te_url,
        [fact("fX", "phone", "+84-X", "ticket", 3, T_NEW, False)],
        key="g08",
    )
    assert status == 200
    assert out["verdict"] == "escalated"
    assert out["golden_facts"] == []
    assert by_attribute(out, "conflicts")["phone"]["reason_code"] == ("CRITICAL_LOW_CONFIDENCE")


def test_G09_critical_conflict_winner_unverified_escalates(te_url):
    status, out = derive(
        te_url,
        [
            fact("fA", "phone", "+84-A", "salesforce-like", 2, T_SF, False),
            fact("fX", "phone", "+84-X", "ticket", 3, T_NEW, False),
        ],
        key="g09",
    )
    assert status == 200
    assert out["verdict"] == "escalated"
    assert by_attribute(out, "conflicts")["phone"]["rule_fired"] == "R2"


def test_G10_exact_tie_is_ambiguous_not_guessed(te_url):
    status, out = derive(
        te_url,
        [
            fact("f1", "phone", "+84-1", "salesforce-like", 2, T_SF, False),
            fact("f2", "phone", "+84-2", "salesforce-like", 2, T_SF, False),
        ],
        key="g10",
    )
    assert status == 200
    assert out["verdict"] == "escalated"
    conflict = by_attribute(out, "conflicts")["phone"]
    assert conflict["reason_code"] in (
        "CRITICAL_LOW_CONFIDENCE",
        "WINNER_TIE_AMBIGUOUS",
    )


def test_G11_rank_mismatch_with_matrix_is_rejected(te_url):
    status, out = derive(
        te_url,
        [fact("fA", "phone", "+84-A", "salesforce-like", 1, T_SF, True)],
        key="g11",
    )
    assert status == 400
    assert out["reason_code"] == "INVALID_RANK_MISMATCH"


def test_G12_unknown_entity_type_is_rejected(te_url):
    response = httpx.post(
        te_url,
        json={
            "decision_id": "dec-g12",
            "entity_type": "agent",
            "entity_id": "A-1",
            "idempotency_key": "idem-g12",
            "payload_hash": "hash-g12",
            "policy_version": "pol-x",
            "taxonomy_version": "tax-user-2026-10-27-v1",
            "candidate_facts": [
                {
                    "fact_id": "f1",
                    "entity_type": "agent",
                    "entity_id": "A-1",
                    "attribute": "team",
                    "value": "t1",
                    "source": "x",
                    "source_id": "x1",
                    "authority_rank": 1,
                    "event_time": T_SF,
                    "verified": True,
                    "ttl_seconds": 3600,
                }
            ],
        },
        timeout=30,
    )
    assert response.status_code == 400
    assert response.json()["reason_code"] == "UNKNOWN_ENTITY_TYPE"


def test_G13_unknown_policy_version_is_rejected(te_url):
    status, out = derive(
        te_url,
        [fact("fB", "phone", "+84-B", "sap-like", 1, T_SAP, True)],
        key="g13",
        policy="pol-1999-01-01-v0",
    )
    assert status == 400
    assert out["reason_code"] == "UNKNOWN_POLICY_VERSION"


def test_G14_disallowed_attribute_is_rejected(te_url):
    status, out = derive(
        te_url,
        [fact("fS", "ssn", "123", "sap-like", 1, T_SAP, True)],
        key="g14",
    )
    assert status == 400
    assert out["reason_code"] == "ATTRIBUTE_NOT_ALLOWED"


def test_G15_duplicate_fact_ids_are_rejected(te_url):
    status, out = derive(
        te_url,
        [
            fact("fB", "phone", "+84-B", "sap-like", 1, T_SAP, True),
            fact("fB", "email", "e@x.invalid", "ciam-like", 1, T_CIAM, True),
        ],
        key="g15",
    )
    assert status == 400


def test_G16_mixed_verdicts_per_attribute(te_url):
    status, out = derive(
        te_url,
        [
            fact("fB", "phone", "+84-B", "sap-like", 1, T_SAP, True, ttl=172800),
            fact("fA", "phone", "+84-A", "salesforce-like", 2, T_SF, False),
            fact("fN", "display_name", "Synthetic Person", "sap-like", 1, T_SAP, True, ttl=172800),
        ],
        key="g16",
    )
    assert status == 200
    assert out["verdict"] == "escalated"
    assert by_attribute(out)["phone"]["winner_fact_id"] == "fB"
    conflict = by_attribute(out, "conflicts")["display_name"]
    assert conflict["reason_code"] == "NO_AUTHORITY_RANKS"


def test_G17_display_name_without_matrix_escalates(te_url):
    status, out = derive(
        te_url,
        [fact("fN", "display_name", "Synthetic Person", "sap-like", 1, T_SAP, True)],
        key="g17",
    )
    assert status == 200
    assert out["verdict"] == "escalated"
    assert out["golden_facts"] == []
    assert by_attribute(out, "conflicts")["display_name"]["rule_fired"] == "R4"


def test_G18_naive_datetime_is_rejected(te_url):
    status, out = derive(
        te_url,
        [fact("fB", "phone", "+84-B", "sap-like", 1, "2026-10-26T09:00:00", True)],
        key="g18",
    )
    assert status == 400


def test_G19_unlisted_source_accepted_with_provided_rank(te_url):
    status, out = derive(
        te_url,
        [
            {
                "fact_id": "fSt",
                "entity_type": "user",
                "entity_id": "P-001",
                "attribute": "phone",
                "value": "+84-S",
                "source": "steward-approved",
                "source_id": "steward-st-1",
                "authority_rank": 1,
                "event_time": T_NEW,
                "verified": True,
                "ttl_seconds": 3600,
            }
        ],
        key="g19",
    )
    assert status == 200
    assert out["verdict"] == "published"
    assert by_attribute(out)["phone"]["winner_fact_id"] == "fSt"


def test_G20_sub_minute_ttl_is_rejected_not_coarsened(te_url):
    status, out = derive(
        te_url,
        [fact("fB", "phone", "+84-B", "sap-like", 1, T_SAP, True, ttl=30)],
        key="g20",
    )
    assert status == 400
    assert out["reason_code"] == "INVALID_TTL_RESOLUTION"
