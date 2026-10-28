"""Hidden-connection tests (H-01..H-03) through the real derive path.

Proves the wow scene at the gate level: the transitive cluster is derived by
recursive Datalog rules over the whole cluster (never claimed by Muse), the
trap is not merged, and a broken hop escalates explicitly instead of
silently publishing a partial golden.

H-01: full hop set -> verified links + R1 publishes for phone/email/address/
      order_id; display_name escalates (no matrix ranks on users in v1).
H-02: F2-F3 phone-exact hop removed -> F3/F4 cut off from the anchor;
      their winners (display_name, address) escalate CHAIN_BROKEN (C2),
      grounded attributes still publish, verified links lose that hop.
H-03: trap F6 merged in (simulated over-merge) -> exact rank-1 verified
      phone tie -> CLUSTER_CONFLICT (C1), never merged; no F6-unique value
      ever wins; F6 touches no verified link.
"""

from __future__ import annotations

import httpx
import pytest

USER_POLICY = "pol-2026-10-27-v1"
USER_TAXONOMY = "tax-user-2026-10-27-v1"
ENTITY = "P-HIDDEN"

T1 = "2026-10-27T09:00:00+07:00"
T2 = "2026-10-27T09:05:00+07:00"
T3 = "2026-10-27T09:02:00+07:00"
T4 = "2026-10-27T09:10:00+07:00"
T5 = "2026-10-27T09:12:00+07:00"


def fact(doc, attribute, value, source, rank, event_time, verified, ttl=3600):
    return {
        "fact_id": f"{ENTITY}-{doc}-{attribute}",
        "entity_type": "user",
        "entity_id": ENTITY,
        "attribute": attribute,
        "value": value,
        "source": source,
        "source_id": f"{doc}-{attribute}",
        "authority_rank": rank,
        "event_time": event_time,
        "verified": verified,
        "ttl_seconds": ttl,
    }


def link(left, right, proof, proposer="Muse"):
    return {
        "from_fact_id": left,
        "to_fact_id": right,
        "proof": proof,
        "proposer": proposer,
    }


def to_candidate_facts(fact_dicts):
    from datetime import datetime

    from schemas import CandidateFact

    out = []
    for item in fact_dicts:
        out.append(CandidateFact(
            fact_id=item["fact_id"],
            entity_type=item["entity_type"],
            entity_id=item["entity_id"],
            attribute=item["attribute"],
            value=item["value"],
            source=item["source"],
            source_id=item["source_id"],
            authority_rank=item["authority_rank"],
            event_time=datetime.fromisoformat(item["event_time"]),
            verified=item["verified"],
            ttl_seconds=item["ttl_seconds"],
        ))
    return out


def linker_links(fact_dicts):
    """Links proposed by the real mock Linker (same_fragment chains, exact
    proofs, fuzzy and conflict claims) — the claims the engine verifies."""
    from subagents import mock_linker

    facts = to_candidate_facts(fact_dicts)
    # Document id = fact id minus the trailing attribute segment
    # ("P-HIDDEN-F1-display_name" -> "P-HIDDEN-F1").
    doc_of = {
        fact.fact_id: "-".join(fact.fact_id.split("-")[:-1]) for fact in facts
    }
    links, _trace = mock_linker(facts, doc_of)
    return [
        {"from_fact_id": link.from_fact_id, "to_fact_id": link.to_fact_id,
         "proof": link.proof, "proposer": link.proposer}
        for link in links
    ]


def base_facts():
    return [
        fact("F1", "display_name", "Nguyen Van An", "salesforce-like", 2, T1, True),
        fact("F1", "email", "an.van@example.com", "salesforce-like", 2, T1, True),
        fact("F2", "email", "an.van@example.com", "order-like", 1, T2, True),
        fact("F2", "phone", "0901234567", "order-like", 2, T2, True),
        fact("F2", "order_id", "ORD-101", "order-like", 1, T2, True),
        fact("F3", "display_name", "An Nguyen", "sap-like", 1, T3, True),
        fact("F3", "phone", "0901234567", "sap-like", 1, T3, True),
        fact("F3", "address", "123 Le Loi, HCM", "sap-like", 3, T3, True),
        fact("F4", "display_name", "An", "ticket", 3, T4, False),
        fact("F4", "phone", "0901234567", "ticket", 3, T4, False),
        fact("F4", "address", "123 Le Loi, HCM", "ticket", 3, T4, False),
        fact("F5", "display_name", "A. Nguyen", "marketplace-like", 3, T5, False),
        fact("F5", "address", "123 Le Loi, HCM", "marketplace-like", 3, T5, False),
        fact("F5", "order_id", "ORD-101", "marketplace-like", 2, T5, False),
    ]


def base_links():
    P = ENTITY + "-"
    return [
        link(P + "F1-email", P + "F2-email", "exact_email"),
        link(P + "F2-phone", P + "F3-phone", "exact_phone"),
        link(P + "F3-phone", P + "F4-phone", "exact_phone"),
        link(P + "F2-order_id", P + "F5-order_id", "exact_order"),
        link(P + "F4-address", P + "F5-address", "exact_address"),
    ]


def derive(te_url, facts, links, key, entity_id=ENTITY):
    response = httpx.post(
        te_url,
        json={
            "decision_id": f"dec-{key}",
            "entity_type": "user",
            "entity_id": entity_id,
            "idempotency_key": f"idem-{key}",
            "payload_hash": f"hash-{key}",
            "policy_version": USER_POLICY,
            "taxonomy_version": USER_TAXONOMY,
            "candidate_facts": facts,
            "links": links,
        },
        timeout=30,
    )
    return response.status_code, response.json()


def by_attribute(rows, key="attribute"):
    out = {}
    for row in rows:
        out.setdefault(row[key], []).append(row)
    return out


def vlink_set(out):
    return {
        (row["from_fact_id"], row["to_fact_id"], row["proof"])
        for row in out.get("verified_links", [])
    }


def test_H01_chain_publishes_with_verified_hops(te_url):
    status, out = derive(te_url, base_facts(), linker_links(base_facts()), key="h01")
    assert status == 200, out
    golden = by_attribute(out["golden_facts"])
    assert golden["phone"][0]["winner_fact_id"] == f"{ENTITY}-F3-phone"
    assert golden["phone"][0]["rule_fired"] == "R1"
    assert golden["email"][0]["winner_fact_id"] == f"{ENTITY}-F2-email"
    assert golden["address"][0]["winner_fact_id"] == f"{ENTITY}-F3-address"
    assert golden["order_id"][0]["winner_fact_id"] == f"{ENTITY}-F2-order_id"
    # display_name has no matrix ranks on users in v1: escalates, stays visible.
    conflicts = by_attribute(out["conflicts"])
    assert conflicts["display_name"][0]["reason_code"] == "NO_AUTHORITY_RANKS"
    # Every hop proved by the engine, not claimed by Muse.
    assert vlink_set(out) == {
        (f"{ENTITY}-F1-email", f"{ENTITY}-F2-email", "exact_email"),
        (f"{ENTITY}-F2-phone", f"{ENTITY}-F3-phone", "exact_phone"),
        (f"{ENTITY}-F3-phone", f"{ENTITY}-F4-phone", "exact_phone"),
        (f"{ENTITY}-F2-order_id", f"{ENTITY}-F5-order_id", "exact_order"),
    }


def test_H02_broken_hop_escalates_explicitly(te_url):
    links = linker_links(base_facts())
    # Remove the F2-F3 phone-exact hop: F3/F4 cut off from the anchor.
    links = [link for link in links
             if not (link["from_fact_id"] == f"{ENTITY}-F2-phone"
                     and link["to_fact_id"] == f"{ENTITY}-F3-phone"
                     and link["proof"] == "exact_phone")]
    assert len(links) < len(linker_links(base_facts()))
    status, out = derive(te_url, base_facts(), links, key="h02")
    assert status == 200, out
    assert out["verdict"] == "escalated"
    # Grounded attributes still publish with the same values (no silent change).
    golden = by_attribute(out["golden_facts"])
    assert golden["email"][0]["value"] == "an.van@example.com"
    assert golden["order_id"][0]["winner_fact_id"] == f"{ENTITY}-F2-order_id"
    # Detached winners escalate with the break named, not silently dropped.
    conflicts = by_attribute(out["conflicts"])
    phone_reasons = {row["reason_code"] for row in conflicts["phone"]}
    display_reasons = {row["reason_code"] for row in conflicts["display_name"]}
    address_reasons = {row["reason_code"] for row in conflicts["address"]}
    assert "CHAIN_BROKEN" in phone_reasons
    assert "CHAIN_BROKEN" in display_reasons
    assert "CHAIN_BROKEN" in address_reasons
    assert "email" not in conflicts and "order_id" not in conflicts
    # The removed hop is gone from verified links; the rest still verify.
    assert (f"{ENTITY}-F2-phone", f"{ENTITY}-F3-phone", "exact_phone") not in vlink_set(out)
    assert (f"{ENTITY}-F3-phone", f"{ENTITY}-F4-phone", "exact_phone") in vlink_set(out)


def test_H03_trap_is_not_merged(te_url):
    facts = base_facts() + [
        fact("F6", "display_name", "Nguyen Van Binh", "sap-like", 1, T1, True),
        fact("F6", "email", "binh@example.com", "sap-like", 3, T1, True),
        fact("F6", "phone", "0901234568", "sap-like", 1, T3, True),
        fact("F6", "address", "123 Le Loi, HCM", "sap-like", 3, T1, True),
    ]
    status, out = derive(te_url, facts, linker_links(facts), key="h03")
    assert status == 200, out
    assert out["verdict"] == "escalated"
    conflicts = by_attribute(out["conflicts"])
    phone_reasons = {row["reason_code"] for row in conflicts["phone"]}
    assert "CLUSTER_CONFLICT" in phone_reasons
    golden = by_attribute(out.get("golden_facts", []))
    assert "phone" not in golden
    # No trap-only value ever wins: the neighbour is not merged.
    won_values = {row["value"] for rows in golden.values() for row in rows}
    assert "0901234568" not in won_values
    assert "binh@example.com" not in won_values
    assert "Nguyen Van Binh" not in won_values
    # The trap touches no verified link.
    assert not any("F6" in end for hop in vlink_set(out) for end in hop[:2])
