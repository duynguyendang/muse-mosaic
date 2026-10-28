"""Real Muse proposer tests — no network, no credit, no model.

Covers fail-fast configuration and strict output validation. Live API calls
are never made here; mock mode carries the demo test suite.
"""

from __future__ import annotations

import pytest
from config import ConfigurationError, Settings
from proposer import (
    ProposerError,
    parse_proposal_content,
    real_proposal,
)
from schemas import CandidateFact


def settings(**overrides):
    base = dict(
        use_mock_muse=False,
        meta_model_api_key="test-key",
        meta_model_api_base_url="https://api.meta.ai/v1",
        muse_model_id="muse-spark-1.3",
    )
    base.update(overrides)
    return Settings(**base)


def fact(fact_id, attribute, value, rank=1):
    return CandidateFact(
        fact_id=fact_id,
        entity_type="user",
        entity_id="P-001",
        attribute=attribute,
        value=value,
        source="sap-like",
        source_id=f"{fact_id}-src",
        authority_rank=rank,
        event_time="2026-10-26T09:00:00+07:00",
        verified=True,
        ttl_seconds=3600,
    )


def test_real_mode_fails_fast_without_key_or_model():
    with pytest.raises(ConfigurationError):
        settings(meta_model_api_key="").validate_startup()
    with pytest.raises(ConfigurationError):
        settings(muse_model_id="").validate_startup()
    with pytest.raises(ConfigurationError):
        settings(meta_model_api_base_url="http://insecure.example").validate_startup()


def test_real_mode_ok_when_pinned():
    settings().validate_startup()


def test_parse_valid_proposal_with_fences():
    facts = [fact("f1", "phone", "+84-B"), fact("f2", "phone", "+84-A", rank=2)]
    content = """```json
    {"attributes": {"phone": {"proposed_value": "+84-B", "confidence": "high",
      "explanation": "Authoritative verified.", "evidence_fact_ids": ["f1"],
      "injection_detected": false}}}
    ```"""
    out = parse_proposal_content(content, facts, "muse-spark-1.3")
    assert out["mode"] == "real"
    assert out["model"] == "muse-spark-1.3"
    assert out["attributes"]["phone"]["proposed_value"] == "+84-B"
    assert out["attributes"]["phone"]["confidence"] == "high"


def test_parse_rejects_invented_value():
    facts = [fact("f1", "phone", "+84-B")]
    content = (
        '{"attributes": {"phone": {"proposed_value": "+84-X", '
        '"confidence": "high", "explanation": "x", '
        '"evidence_fact_ids": ["f1"], "injection_detected": false}}}'
    )
    with pytest.raises(ProposerError) as exc:
        parse_proposal_content(content, facts, "muse-spark-1.3")
    assert exc.value.kind == "invented-value"


def test_parse_rejects_unknown_attribute():
    facts = [fact("f1", "phone", "+84-B")]
    content = (
        '{"attributes": {"ssn": {"proposed_value": "1", "confidence": "high", '
        '"explanation": "x", "evidence_fact_ids": [], "injection_detected": false}}}'
    )
    with pytest.raises(ProposerError) as exc:
        parse_proposal_content(content, facts, "muse-spark-1.3")
    assert exc.value.kind == "hallucinated-attribute"


def test_parse_rejects_unknown_evidence_id():
    facts = [fact("f1", "phone", "+84-B")]
    content = (
        '{"attributes": {"phone": {"proposed_value": "+84-B", "confidence": "high", '
        '"explanation": "x", "evidence_fact_ids": ["f9"], "injection_detected": false}}}'
    )
    with pytest.raises(ProposerError) as exc:
        parse_proposal_content(content, facts, "muse-spark-1.3")
    assert exc.value.kind == "invented-evidence"


def test_parse_rejects_non_json():
    with pytest.raises(ProposerError) as exc:
        parse_proposal_content("not json at all", [fact("f1", "phone", "B")], "m")
    assert exc.value.kind == "invalid-json"


def test_parse_fills_omitted_attribute_with_abstain():
    facts = [fact("f1", "phone", "+84-B"), fact("f2", "email", "e@x.invalid")]
    content = (
        '{"attributes": {"phone": {"proposed_value": "+84-B", "confidence": "high", '
        '"explanation": "x", "evidence_fact_ids": ["f1"], "injection_detected": false}}}'
    )
    out = parse_proposal_content(content, facts, "muse-spark-1.3")
    assert out["attributes"]["email"]["confidence"] == "abstain"
    assert out["attributes"]["email"]["proposed_value"] is None


def test_real_proposal_transport_failure_fails_closed():
    import asyncio

    facts = [fact("f1", "phone", "+84-B")]
    bad = settings(meta_model_api_base_url="https://127.0.0.1:9")

    async def run():
        with pytest.raises(ProposerError) as exc:
            await real_proposal(facts, bad)
        assert exc.value.kind in ("unreachable", "transport")

    asyncio.run(run())
