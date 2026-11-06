"""Autonomy controller v1 (M3): modes, freeze, breakers, audit sampling."""

from __future__ import annotations

import asyncio
from pathlib import Path

import autonomy
import pytest
from config import Settings


def _settings(**overrides) -> Settings:
    base = Settings(
        use_mock_muse=True,
        meta_model_api_key="",
        muse_model_id="",
        embedding_model="",
        embedding_version="",
        embedding_dim=None,
        trust_engine_url="http://127.0.0.1:1/derive",
        manglekit_bin="",
        database_path="/tmp/onboarding-test.db",
        enable_background_tasks=False,
    )
    for key, value in overrides.items():
        base = __import__("dataclasses").replace(base, **{key: value})
    return base


def _product_docs():
    common = {
        "authority_rank": 1,
        "event_time": "2026-10-27T09:00:00+07:00",
        "verified": True,
        "ttl_seconds": 3600,
    }

    def doc(doc_id: str, source: str, name: str, maker: str):
        return {
            "document_id": doc_id,
            "entity_type": "product",
            "source": source,
            "synthetic": True,
            "fields": {
                "name": {"value": name, **common},
                "manufacturer": {"value": maker, **common},
                "category": {"value": "accounting", **common},
            },
        }

    return [
        doc("p1", "pim", "QuickBooks 2007", "Intuit"),
        doc("p2", "marketplace", "quickbooks 2007", "Intuit"),
    ]


def test_resolve_mode_defaults_off() -> None:
    assert autonomy.resolve_mode(_settings(), "user") == "off"
    assert autonomy.resolve_mode(_settings(), "anything") == "off"


def test_resolve_mode_build_wide_and_override() -> None:
    wide = _settings(autonomy_mode="auto")
    assert autonomy.resolve_mode(wide, "product") == "auto"
    with_pref = _settings(autonomy_mode="auto", autonomy_modes={"product": "shadow"})
    assert autonomy.resolve_mode(with_pref, "product") == "shadow"
    # an override for another entity does not bleed across
    assert autonomy.resolve_mode(with_pref, "user") == "auto"


def test_legacy_modes_normalise() -> None:
    assert autonomy.normalize_mode("manual") == "off"
    assert autonomy.normalize_mode("supervised") == "suggest"
    assert autonomy.normalize_mode(None) == "off"
    with pytest.raises(ValueError):
        autonomy.normalize_mode("full_auto")


def test_effective_mode_freezes_auto_to_shadow() -> None:
    settings = _settings(autonomy_mode="auto")
    assert autonomy.effective_mode(settings, "user", frozen=True) == "shadow"
    settings = _settings(autonomy_mode="suggest")
    assert autonomy.effective_mode(settings, "user", frozen=True) == "suggest"
    settings = _settings(autonomy_mode="auto", autonomy_kill_switch=True)
    assert autonomy.is_frozen(settings)

    class _State:
        autonomy_frozen = True

    assert autonomy.is_frozen(settings, _State())


def test_audit_sample_zero_rate_collects_nothing() -> None:
    ids = [f"d{i:04d}" for i in range(100)]
    assert autonomy.audit_sample(ids, rate=0.0) == []


def test_audit_sample_deterministic_and_contains_withheld() -> None:
    ids = [f"d{i:04d}" for i in range(200)]
    first = autonomy.audit_sample(ids, rate=0.1)
    second = autonomy.audit_sample(list(reversed(ids)), rate=0.1)
    assert first == second
    sample = autonomy.audit_sample(ids, rate=0.0, always=["w1", "w2"])
    assert {s["decision_id"] for s in sample} == {"w1", "w2"}


def test_audit_sample_rejects_out_of_range() -> None:
    with pytest.raises(ValueError):
        autonomy.audit_sample(["x"], rate=1.5)


def test_breaker_zero_candidates_at_scale() -> None:
    by_entity = {"user": {"clusters_in": 500, "candidate_pairs": 0}}
    breakers = autonomy.evaluate_breakers(by_entity)
    assert [b["breaker"] for b in breakers] == ["zero_candidates_at_scale"]


def test_breaker_judge_failure_rate() -> None:
    by_entity = {
        "product": {
            "clusters_in": 50,
            "candidate_pairs": 10,
            "judge_calls": 10,
            "judge_failures": 2,
        },
    }
    breakers = autonomy.evaluate_breakers(by_entity)
    assert [b["breaker"] for b in breakers] == ["judge_failure_rate"]


def test_breaker_entity_run_failed_wins() -> None:
    by_entity = {
        "product": {
            "clusters_in": 500,
            "candidate_pairs": 0,
            "skipped_reason": "Muse unavailable: TimeoutError: boom",
        },
    }
    breakers = autonomy.evaluate_breakers(by_entity)
    assert [b["breaker"] for b in breakers] == ["entity_run_failed"]


def test_breaker_below_thresholds_does_not_trigger() -> None:
    by_entity = {
        "user": {"clusters_in": 50, "candidate_pairs": 0, "judge_calls": 9, "judge_failures": 0},
    }
    assert autonomy.evaluate_breakers(by_entity) == []


def _clusters():
    docs = _product_docs()
    return [
        {"cluster_id": "c1", "entity_type": "product", "member_docs": [docs[0]]},
        {"cluster_id": "c2", "entity_type": "product", "member_docs": [docs[1]]},
    ]


def test_shadow_scan_judges_but_never_merges() -> None:
    clusters = _clusters()
    before = [list(c["member_docs"]) for c in clusters]
    out_clusters, info = asyncio.run(
        autonomy.shadow_scan(
            _settings(autonomy_mode="shadow"), clusters, mode="shadow", frozen=False
        )
    )
    assert info["autonomy"] == {"mode": "shadow", "frozen": False}
    assert info["merged"] == 0
    assert info["would_merge"] >= 1  # the pipeline would have merged these
    assert [c["member_docs"] for c in out_clusters] == before


def test_off_scan_applies_nothing_and_says_so() -> None:
    clusters = _clusters()
    _, info = autonomy.off_scan(clusters, mode="off", frozen=False)
    assert info["enabled"] is False
    assert "autonomy off" in info["skipped_reason"]


def test_api_freeze_unfreeze_status_roundtrip(api_client) -> None:
    assert api_client.get("/api/v1/autonomy/status").json()["frozen"] is False
    assert api_client.post("/api/v1/autonomy/freeze").json()["frozen"] is True
    status = api_client.get("/api/v1/autonomy/status")
    assert status.json()["frozen"] is True
    assert api_client.post("/api/v1/autonomy/unfreeze").json()["frozen"] is False


def test_report_audit_includes_withheld_decisions() -> None:
    goldens = [{"decision_id": f"auto-{i:03d}"} for i in range(50)]
    stewards = [{"decision_id": "withheld-1"}, {"decision_id": "withheld-2"}]
    sample = autonomy.report_audit(goldens, stewards, rate=0.0)
    assert {s["decision_id"] for s in sample} == {"withheld-1", "withheld-2"}
    assert all(s["reason"] == "withheld" for s in sample)


# --- integration: /build honours off/shadow/frozen -------------------------


def _product_build_body():
    base = {
        "authority_rank": 1,
        "event_time": "2026-10-27T09:00:00+07:00",
        "verified": True,
        "ttl_seconds": 3600,
    }

    def product(doc_id, source, label, maker):
        return {
            "synthetic": True,
            "document_id": doc_id,
            "entity_type": "product",
            "source": source,
            "fields": {
                "name": {"value": label, **base},
                "manufacturer": {"value": maker, **base},
                "category": {"value": "accounting", **base},
            },
        }

    return {
        "work_id": "w-autonomy",
        "idempotency_key": "autonomy-1",
        "dataset": {
            "documents": [
                product("a", "amazon", "QuickBooks 2007", "Intuit"),
                product("b", "google", "quickbooks 2007", "Intuit"),
            ]
        },
    }


def _witness(tmp_path: Path) -> str:
    from conftest import write_witness

    return write_witness(tmp_path)


def _client(te_url, matching_engine, tmp_path, **overrides):
    from config import Settings
    from fastapi.testclient import TestClient
    from main import create_app

    settings = Settings(
        use_mock_muse=True,
        trust_engine_url=te_url,
        matching_service_url=matching_engine,
        database_path=str(Path(tmp_path) / "m.db"),
        enable_background_tasks=False,
        muse_candidate_matching=True,
        **overrides,
    )
    return TestClient(create_app(settings))


def test_default_mode_is_fail_closed(te_url, matching_engine, tmp_path):
    with _client(te_url, matching_engine, tmp_path) as client:
        response = client.post("/build", json=_product_build_body())
    assert response.status_code == 200, response.text
    out = response.json()
    assert out["autonomy"]["modes"]["product"] == "off"
    assert out["muse_candidates"]["enabled"] is False
    assert out["muse_candidates"]["merged"] == 0


def test_shadow_mode_judges_but_never_merges(te_url, matching_engine, tmp_path):
    with _client(te_url, matching_engine, tmp_path, autonomy_mode="shadow") as client:
        response = client.post("/build", json=_product_build_body())
    assert response.status_code == 200, response.text
    out = response.json()
    assert out["autonomy"]["modes"]["product"] == "shadow"
    info = out["muse_candidates"]
    assert info["merged"] == 0
    assert info["enabled"] is True
    assert info["autonomy"] == {"mode": "shadow", "frozen": False}


def test_freeze_demotes_auto_to_shadow(te_url, matching_engine, tmp_path):
    with _client(
        te_url,
        matching_engine,
        tmp_path,
        autonomy_mode="auto",
        muse_auto_witness=_witness(tmp_path),
    ) as client:
        assert client.post("/api/v1/autonomy/freeze").json()["frozen"] is True
        response = client.post("/build", json=_product_build_body())
        assert client.post("/api/v1/autonomy/unfreeze").json()["frozen"] is False
    assert response.status_code == 200, response.text
    out = response.json()
    assert out["autonomy"]["frozen"] is True
    info = out["muse_candidates"]
    assert info["merged"] == 0
    assert info["autonomy"] == {"mode": "shadow", "frozen": True}


def test_deliberate_off_is_configuration_not_a_breaker() -> None:
    by_entity = {
        "product": {
            "skipped_reason": "autonomy off: Muse not applied for this entity",
            "clusters_in": 10,
            "candidate_pairs": 0,
        },
    }
    assert autonomy.evaluate_breakers(by_entity) == []


def test_enforce_breakers_raises_with_breaker_detail() -> None:
    by_entity = {"user": {"clusters_in": 500, "candidate_pairs": 0}}
    with pytest.raises(autonomy.BreakerTripped) as excinfo:
        autonomy.enforce_breakers(by_entity)
    assert [b["breaker"] for b in excinfo.value.breakers] == ["zero_candidates_at_scale"]
    assert excinfo.value.breakers[0]["entity_type"] == "user"
    assert "500" in str(excinfo.value)


def test_enforce_breakers_passes_when_quiet() -> None:
    assert autonomy.enforce_breakers(None) == []
    assert (
        autonomy.enforce_breakers(
            {
                "user": {
                    "clusters_in": 50,
                    "candidate_pairs": 0,
                    "judge_calls": 9,
                    "judge_failures": 0,
                }
            }
        )
        == []
    )
    # Deliberate off/shadow labels are configuration, never an enforcement.
    assert (
        autonomy.enforce_breakers(
            {
                "user": {
                    "skipped_reason": "autonomy off: Muse not applied for this entity",
                    "clusters_in": 10**6,
                    "candidate_pairs": 0,
                }
            }
        )
        == []
    )
