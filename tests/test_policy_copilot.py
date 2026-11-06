"""Policy Copilot: closed vocabulary, deterministic compile, engine verdict."""

from __future__ import annotations

import asyncio
import dataclasses

import pytest
from config import Settings
from fastapi.testclient import TestClient
from main import create_app
from policy_copilot import PolicySpec, compile_spec, line_diff, mock_propose
from policy_profiles import load_profiles


def _retail_horn() -> str:
    return load_profiles()["retail_balanced"].policies["user"].horn_clauses


def test_mock_parser_understands_tier_and_multivalued():
    spec = mock_propose(
        "Treat marketplace-like as tier 3 and order_id is multivalued", "retail_balanced"
    )
    assert spec.source_tiers == {"marketplace-like": 3}
    assert spec.multivalued_attributes == ["order_id"]
    assert spec.unsupported is None


@pytest.mark.parametrize(
    "text",
    ["Allow marketplace orders if verified within 30 days", "delete rule C1", "hello"],
)
def test_out_of_vocabulary_instructions_are_refused_not_guessed(text):
    spec = mock_propose(text, "retail_balanced")
    assert spec.unsupported and spec.is_empty


def test_spec_rejects_unknown_fields_and_bad_tiers():
    with pytest.raises(ValueError):
        PolicySpec.model_validate({"base_profile": "retail_balanced", "raw_datalog": "x."})
    spec = PolicySpec(base_profile="retail_balanced", source_tiers={"crm": 9})
    assert any("must be 1, 2 or 3" in p for p in spec.problems())
    spec = PolicySpec(base_profile="retail_balanced", source_tiers={'x"); evil(': 2})
    assert any("not an identifier" in p for p in spec.problems())


def test_compile_replaces_existing_tier_and_diff_is_minimal():
    horn = _retail_horn()
    spec = PolicySpec(base_profile="retail_balanced", source_tiers={"crm": 3})
    new, reasons = compile_spec(horn, spec)
    assert not reasons
    assert line_diff(horn, new) == ['-source_tier("crm", 1).', '+source_tier("crm", 3).']


def test_compile_adds_new_source_and_multivalued():
    horn = _retail_horn()
    spec = PolicySpec(
        base_profile="retail_balanced",
        source_tiers={"newfeed": 2},
        multivalued_attributes=["ticket_id"],
    )
    new, _ = compile_spec(horn, spec)
    assert 'source_tier("newfeed", 2).' in new
    assert 'multivalued("ticket_id").' in new


def test_tier_override_refused_when_profile_has_no_hierarchy():
    horn = load_profiles()["analytical_mesh"].policies["user"].horn_clauses
    spec = PolicySpec(base_profile="analytical_mesh", source_tiers={"crm": 3})
    _, reasons = compile_spec(horn, spec)
    assert reasons and "no source hierarchy" in reasons[0]


@pytest.fixture
def client(tmp_path, monkeypatch, mkit_bin):
    monkeypatch.setenv("MANGLEKIT_BIN", str(mkit_bin))
    settings = Settings(database_path=tmp_path / "t.db", use_mock_muse=True)
    return TestClient(create_app(settings))


def test_endpoint_compiled_draft_is_verified_by_real_engine_and_not_applied(client):
    r = client.post(
        "/api/v1/pipeline/copilot/draft",
        json={"instruction": "treat crm as tier 3", "base_profile": "retail_balanced"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "compiled", body
    assert body["checks"]["engine_available"] and body["checks"]["compiles"] is True
    assert body["applied"] is False


def test_endpoint_refuses_unsupported(client):
    r = client.post(
        "/api/v1/pipeline/copilot/draft",
        json={"instruction": "allow marketplace orders if verified within 30 days"},
    )
    assert r.json()["status"] == "unsupported"
    assert r.json()["diff"] == []


def test_engine_rejects_a_broken_program(monkeypatch, mkit_bin):
    """The engine, not our code, decides validity: garbage must not 'compile'."""
    from policy_copilot import verify_with_engine

    monkeypatch.setenv("MANGLEKIT_BIN", str(mkit_bin))
    result = verify_with_engine("p(X) :- q(X), !p(X).\nbroken(((")
    assert result["compiles"] is False
    assert result["error"]


def test_no_engine_reports_unverified_never_compiled(monkeypatch):
    import policy_copilot

    monkeypatch.setattr(policy_copilot, "find_mkit", lambda: None)
    result = policy_copilot.verify_with_engine("x.")
    assert result["compiles"] is None


def test_live_schema_is_strict_mode_compatible():
    """Meta's strict json_schema needs additionalProperties:false on EVERY object
    (found against the real API: free-form maps are rejected with HTTP 400)."""
    from policy_copilot import SPEC_JSON_SCHEMA

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False, node
                assert set(node["properties"]) == set(node["required"]), node
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(SPEC_JSON_SCHEMA)


def test_noop_change_is_reported_not_called_compiled(client):
    r = client.post(
        "/api/v1/pipeline/copilot/draft",
        json={"instruction": "treat crm as tier 1", "base_profile": "retail_balanced"},
    )
    assert r.json()["status"] == "no_change"


# --- multi-model channel: Wonder maps when pinned, Muse falls back ----------


def _live_settings(**overrides) -> Settings:
    base = Settings(
        use_mock_muse=False,
        meta_model_api_key="test-key",
        muse_model_id="muse-test-1",
        wonder_model_id="",
        database_path="/tmp/copilot-test.db",
        enable_background_tasks=False,
    )
    return dataclasses.replace(base, **overrides) if overrides else base


def test_propose_channel_splits_wonder_and_muse():
    from policy_copilot import propose_channel

    assert propose_channel(Settings()) == ("", "mock")
    assert propose_channel(_live_settings()) == ("muse-test-1", "live")
    assert propose_channel(_live_settings(wonder_model_id="wonder-2")) == ("wonder-2", "wonder")


def test_live_propose_routes_to_the_pinned_wonder(monkeypatch):
    import meta_client

    seen: list[dict] = []

    async def fake_chat_json(settings, **kwargs):
        seen.append(kwargs)
        return (
            {
                "base_profile": "retail_balanced",
                "source_tiers": [{"source": "crm", "tier": 3}],
                "multivalued_attributes": [],
                "unsupported": None,
            },
            {"total_tokens": 11},
        )

    monkeypatch.setattr(meta_client, "chat_json", fake_chat_json)
    from policy_copilot import live_propose

    spec, usage = asyncio.run(
        live_propose(
            _live_settings(wonder_model_id="wonder-2"), "trust crm most", "retail_balanced"
        )
    )
    assert spec.source_tiers == {"crm": 3}
    assert [call["model"] for call in seen] == ["wonder-2"]
    assert seen[0]["reasoning_effort"] == "low"
    assert usage == {"total_tokens": 11}  # usage flows to the run log


def test_live_propose_falls_back_to_muse_without_wonder(monkeypatch):
    import meta_client

    seen: list[dict] = []

    async def fake_chat_json(settings, **kwargs):
        seen.append(kwargs)
        return (
            {
                "base_profile": "retail_balanced",
                "source_tiers": [],
                "multivalued_attributes": ["order_id"],
                "unsupported": None,
            },
            {},
        )

    monkeypatch.setattr(meta_client, "chat_json", fake_chat_json)
    from policy_copilot import live_propose

    spec, _usage = asyncio.run(
        live_propose(_live_settings(), "order_id multivalued", "retail_balanced")
    )
    assert spec.multivalued_attributes == ["order_id"]
    assert [call["model"] for call in seen] == ["muse-test-1"]


def test_draft_labels_the_wonder_channel(monkeypatch):
    import policy_copilot

    async def fake_propose(settings, instruction, base_profile):
        return (
            PolicySpec(
                base_profile=base_profile,  # type: ignore[arg-type]
                source_tiers={"crm": 3},
            ),
            {"total_tokens": 5},
        )

    monkeypatch.setattr(policy_copilot, "live_propose", fake_propose)
    monkeypatch.setattr(
        policy_copilot,
        "verify_with_engine",
        lambda horn: {"engine_available": True, "compiles": True, "error": None},
    )
    out = asyncio.run(
        policy_copilot.draft(
            _live_settings(wonder_model_id="wonder-2"),
            "trust crm most",
            "retail_balanced",
        )
    )
    assert out["proposer"] == "wonder"
    assert out["status"] == "compiled"
    assert out["usage"] == {"total_tokens": 5}  # cost reaches the report
    assert out["applied"] is False
