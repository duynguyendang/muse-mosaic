"""Semantic evidence rules (S1 semantic_phone, S2 semantic_email), live engine.

Muse may CLAIM that two facts are the same identifier written differently. The
engine must verify that against mechanical features, not trust the claim:

  S1  phones share their last 9 digits (country-code / format variants)
  S2  emails share a deletion key: same domain, edit distance <= 1

A semantic hop grounds the chain like an exact hop. A claim without evidence,
or a names-only `fuzzy_name_address` link, never verifies.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
POLICY = "pol-2026-11-02-v1"
TAXONOMY = "tax-user-2026-10-27-v1"
ENTITY = "P-SEM"
T = "2026-10-27T09:00:00+07:00"

pytestmark = pytest.mark.usefixtures("te_url")


def fact(doc, attribute, value, source, rank, t=T):
    return {
        "fact_id": f"{ENTITY}-{doc}-{attribute}",
        "entity_type": "user",
        "entity_id": ENTITY,
        "attribute": attribute,
        "value": value,
        "source": source,
        "source_id": f"{doc}-{attribute}",
        "authority_rank": rank,
        "event_time": t,
        "verified": True,
        "ttl_seconds": 3600,
    }


def lnk(left, right, proof):
    return {
        "from_fact_id": f"{ENTITY}-{left}",
        "to_fact_id": f"{ENTITY}-{right}",
        "proof": proof,
        "proposer": "Muse",
    }


def facts(phone_b="+1 4499771729", email_b="maria.dqy@example.com"):
    """Doc A (sap-like) and doc B (salesforce-like): same person, no exact id."""
    return [
        fact("A", "display_name", "Maria Day", "sap-like", 1),
        fact("A", "phone", "4499771729", "sap-like", 1),
        fact("A", "email", "maria.day@example.com", "sap-like", 3),
        fact("B", "display_name", "maria day", "salesforce-like", 2),
        fact("B", "phone", phone_b, "salesforce-like", 2),
        fact("B", "email", email_b, "salesforce-like", 2),
    ]


# Within each document the Linker chains facts (same_fragment), as in /build.
CHAIN = [
    lnk("A-display_name", "A-phone", "same_fragment"),
    lnk("A-phone", "A-email", "same_fragment"),
    lnk("B-display_name", "B-phone", "same_fragment"),
    lnk("B-phone", "B-email", "same_fragment"),
]


def derive(te_url, fact_list, links, key, policy=POLICY):
    response = httpx.post(
        te_url,
        json={
            "decision_id": f"dec-{key}",
            "entity_type": "user",
            "entity_id": ENTITY,
            "idempotency_key": f"idem-{key}",
            "payload_hash": f"hash-{key}",
            "policy_version": policy,
            "taxonomy_version": TAXONOMY,
            "candidate_facts": fact_list,
            "links": links,
        },
        timeout=30,
    )
    return response.status_code, response.json()


def proofs(out):
    return {row["proof"] for row in out.get("verified_links", [])}


def reasons(out):
    return {c["reason_code"] for c in out.get("conflicts", [])}


def test_S1_phone_with_country_code_is_verified_by_the_engine(te_url):
    status, out = derive(
        te_url, facts(), CHAIN + [lnk("A-phone", "B-phone", "semantic_phone")], "s1"
    )
    assert status == 200, out
    assert "semantic_phone" in proofs(out)
    assert "CHAIN_BROKEN" not in reasons(out), "a verified hop must ground doc B"


def test_S1_claim_without_matching_digits_is_not_verified(te_url):
    other = facts(phone_b="5551234567")
    status, out = derive(
        te_url, other, CHAIN + [lnk("A-phone", "B-phone", "semantic_phone")], "s1x"
    )
    assert status == 200, out
    assert "semantic_phone" not in proofs(out)
    assert "CHAIN_BROKEN" in reasons(out), "no evidence -> B stays cut off and is held"


def test_S1_claim_on_the_wrong_attribute_is_not_verified(te_url):
    # phone_key of an email fact is empty; a semantic_phone claim over emails fails
    status, out = derive(
        te_url, facts(), CHAIN + [lnk("A-email", "B-email", "semantic_phone")], "s1y"
    )
    assert status == 200, out
    assert "semantic_phone" not in proofs(out)


def test_S2_email_one_typo_is_verified(te_url):
    status, out = derive(
        te_url, facts(), CHAIN + [lnk("A-email", "B-email", "semantic_email")], "s2"
    )
    assert status == 200, out
    assert "semantic_email" in proofs(out)
    assert "CHAIN_BROKEN" not in reasons(out)


def test_S2_two_edits_is_not_verified(te_url):
    far = facts(email_b="maria.dqq@example.com")
    status, out = derive(te_url, far, CHAIN + [lnk("A-email", "B-email", "semantic_email")], "s2x")
    assert status == 200, out
    assert "semantic_email" not in proofs(out)


def test_S2_short_local_part_gives_no_evidence(te_url):
    short = [
        fact("A", "email", "ab.cd@example.com", "sap-like", 3),
        fact("B", "email", "ab.ce@example.com", "salesforce-like", 2),
    ]
    status, out = derive(te_url, short, [lnk("A-email", "B-email", "semantic_email")], "s2s")
    assert status == 200, out
    assert "semantic_email" not in proofs(out)


def test_names_only_link_never_verifies(te_url):
    status, out = derive(
        te_url,
        facts(),
        CHAIN + [lnk("A-display_name", "B-display_name", "fuzzy_name_address")],
        "fz",
    )
    assert status == 200, out
    assert not proofs(out) & {"semantic_phone", "semantic_email", "fuzzy_name_address"}
    assert "CHAIN_BROKEN" in reasons(out)


def test_unknown_semantic_proof_is_rejected_fail_closed(te_url):
    status, out = derive(te_url, facts(), [lnk("A-phone", "B-phone", "semantic_name")], "bad")
    assert status == 400, out


def test_older_policy_ignores_semantic_claims(te_url):
    """pol-2026-10-28-v1 is immutable and has no S1/S2: the claim verifies nothing."""
    status, out = derive(
        te_url,
        facts(),
        CHAIN + [lnk("A-phone", "B-phone", "semantic_phone")],
        "old",
        policy="pol-2026-10-28-v1",
    )
    assert status == 200, out
    assert "semantic_phone" not in proofs(out)


def test_every_semantic_policy_carries_the_canonical_block():
    """Copies must not drift from trust-engine/policies/semantic/semantic-v1.mg."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "apply_semantic_block.py"), "--check"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_new_policy_version_is_additive_over_v2():
    old = json.loads((ROOT / "trust-engine/policies/user/pol-2026-10-28-v1.json").read_text())
    new = json.loads((ROOT / "trust-engine/policies/user/pol-2026-11-02-v1.json").read_text())
    assert new["authority_matrix"] == old["authority_matrix"]
    assert new["policy_version"] == "pol-2026-11-02-v1"
    assert {"S1", "S2"} <= set(new["rules"])


# --- S3: a names-only merge never certifies -------------------------------

RETAIL = "pol-retail-balanced-v1"
MESH = "pol-analytical-mesh-v1"
LATER = "2026-10-27T09:05:00+07:00"


def newer_b(phone_b="+1 4499771729"):
    """Both docs from crm at rank 1; doc B is five minutes NEWER, so B's values win."""
    return [
        fact("A", "display_name", "Maria Day", "crm", 1),
        fact("A", "phone", "4499771729", "crm", 1),
        fact("A", "email", "maria.day@example.com", "crm", 1),
        fact("B", "display_name", "maria day", "crm", 1, LATER),
        fact("B", "phone", phone_b, "crm", 1, LATER),
        fact("B", "email", "maria.dqy@example.com", "crm", 1, LATER),
    ]


NAMES_ONLY = CHAIN + [lnk("A-display_name", "B-display_name", "fuzzy_name_address")]


def outcomes(out):
    return {g["attribute"]: g["value"] for g in out.get("golden_facts", [])}


def test_S3_names_only_merge_does_not_certify_the_new_values(te_url):
    status, out = derive(te_url, newer_b(), NAMES_ONLY, "s3", policy=RETAIL)
    assert status == 200, out
    assert "SEMANTIC_MERGE_UNVERIFIED" in reasons(out)
    assert "phone" not in outcomes(out), "B's unverified-merge phone must not be certified"


def test_S3_verified_semantic_hop_lets_the_newer_value_publish(te_url):
    links = NAMES_ONLY + [lnk("A-phone", "B-phone", "semantic_phone")]
    status, out = derive(te_url, newer_b(), links, "s3ok", policy=RETAIL)
    assert status == 200, out
    assert "SEMANTIC_MERGE_UNVERIFIED" not in reasons(out)
    assert outcomes(out)["phone"] == "+1 4499771729"  # freshness decides, as for an exact hop
    assert "semantic_phone" in proofs(out)


def test_S3_is_silent_when_nobody_claimed_a_merge(te_url):
    status, out = derive(te_url, newer_b(), CHAIN, "s3none", policy=RETAIL)
    assert status == 200, out
    assert "SEMANTIC_MERGE_UNVERIFIED" not in reasons(out)


def test_S3_is_not_installed_on_analytical_mesh(te_url):
    """That profile has no escalation rules by construction; S3 must not add one."""
    status, out = derive(te_url, newer_b(), NAMES_ONLY, "s3mesh", policy=MESH)
    assert status == 200, out
    assert not reasons(out)
