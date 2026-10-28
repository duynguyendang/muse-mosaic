"""Structure validation tests (CSD 3.1 [STRUCTURE] gate).

Assembled nested goldens validate against the expected whole-document JSON
Schema. Field failures escalate that field (STRUCTURE_MISMATCH + steward);
a missing required field withholds the whole golden (no silent partial).
"""

from __future__ import annotations


def _structure(name="expected_user_golden.json"):
    import json
    from pathlib import Path

    return json.loads(
        (Path(__file__).resolve().parents[1] / "data" / "mock" / "templates" / name).read_text(
            encoding="utf-8"
        )
    )


def test_structure_valid_golden_passes():
    from publisher import assemble_nested_golden, validate_structure

    nested = assemble_nested_golden(
        {"display_name": {"value": "An"}, "email": {"value": "e@x.invalid"}},
        [],
    )
    assert validate_structure(nested, _structure()) == []


def test_structure_missing_required_field_blocks_golden():
    from publisher import assemble_and_validate, utcnow_iso
    from schemas import ResolveRequest

    request = ResolveRequest(
        work_id="w",
        entity_type="user",
        entity_id="P-001",
        idempotency_key="k",
        fragments=[
            {
                "source": "sap-like",
                "source_id": "s",
                "attribute": "email",
                "value": "e@x.invalid",
                "authority_rank": 1,
                "event_time": "2026-10-27T08:30:00+07:00",
                "verified": True,
                "ttl_seconds": 3600,
            }
        ],
    )
    import schemas

    golden = [
        schemas.GoldenFact(
            entity_type="user",
            entity_id="P-001",
            attribute="phone",
            value="B",
            winner_fact_id="f",
            loser_fact_ids=[],
            rule_fired="R1",
            policy_version="pol",
            taxonomy_version="tax",
            lineage="x",
        )
    ]
    assembled, _purchased, _edges, conflicts, stewards, ok = assemble_and_validate(
        request=request,
        facts=[],
        golden=golden,
        policy_version="pol",
        taxonomy_version="tax",
        golden_attrs={"phone"},
        expected_structure=_structure(),
        documents=[],
        decision_id="dec",
        created_at=utcnow_iso(),
        database_path=":memory:",
    )
    assert ok is False
    assert assembled == []
    assert any(c.reason_code == "STRUCTURE_MISMATCH" for c in conflicts)
    assert any(s.attribute == "golden" for s in stewards)


def test_structure_nested_failure_escalates_element_only():
    from publisher import assemble_and_validate, utcnow_iso
    from schemas import ResolveRequest

    request = ResolveRequest(
        work_id="w",
        entity_type="user",
        entity_id="P-001",
        idempotency_key="k",
        fragments=[
            {
                "source": "sap-like",
                "source_id": "s",
                "attribute": "email",
                "value": "e@x.invalid",
                "authority_rank": 1,
                "event_time": "2026-10-27T08:30:00+07:00",
                "verified": True,
                "ttl_seconds": 3600,
            }
        ],
    )
    import schemas

    golden = [
        schemas.GoldenFact(
            entity_type="user",
            entity_id="P-001",
            attribute="email",
            value="e@x.invalid",
            winner_fact_id="f",
            loser_fact_ids=[],
            rule_fired="R1",
            policy_version="pol",
            taxonomy_version="tax",
            lineage="x",
        )
    ]
    assembled, purchased, _edges, conflicts, stewards, ok = assemble_and_validate(
        request=request,
        facts=[],
        golden=golden,
        policy_version="pol",
        taxonomy_version="tax",
        golden_attrs={"email"},
        expected_structure=_structure(),
        documents=[],
        decision_id="dec",
        created_at=utcnow_iso(),
        database_path=":memory:",
        purchased_override=[{"name": "prod-001"}],
    )
    assert ok is True
    assert [fact.attribute for fact in assembled] == ["email"]
    assert purchased == []
    assert any(c.reason_code == "STRUCTURE_MISMATCH_NESTED" for c in conflicts)
    assert any(s.attribute == "purchased_products" for s in stewards)
