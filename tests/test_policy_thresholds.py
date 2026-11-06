"""M3 breakers ride versioned policy data; thresholds fail closed at load."""

from __future__ import annotations

import pytest
from autonomy import breaker_overrides, evaluate_breakers
from policies import load_policies
from schemas import Policy


def test_shipped_policies_carry_breaker_thresholds() -> None:
    policies = load_policies()
    for entity in ("user", "product", "publication"):
        assert entity in policies, f"publication policy expected alongside; got {sorted(policies)}"
        thresholds = policies[entity].thresholds
        assert thresholds["m3_breakers"] == {
            "zero_candidates_clusters": 200,
            "judge_failure_limit": 0.1,
        }
        assert thresholds["borderline_band"] == {"low": 0.6, "high": 0.85}


def test_breaker_overrides_from_policy() -> None:
    policies = load_policies()
    for _entity, policy in policies.items():
        assert breaker_overrides(policy) == {
            "zero_warn": 200,
            "judge_failure_limit": 0.1,
        }


def test_breaker_overrides_tolerate_partial_policy() -> None:
    class _P:
        thresholds = {"m3_breakers": {"zero_candidates_clusters": 42, "judge_failure_limit": "bad"}}

    assert breaker_overrides(_P()) == {"zero_warn": 42}
    assert breaker_overrides(type("NoThresholds", (), {})()) == {}


def test_evaluate_breakers_honours_overrides() -> None:
    by_entity = {"user": {"clusters_in": 10, "candidate_pairs": 0}}
    assert evaluate_breakers(by_entity) == []
    breakers = evaluate_breakers(by_entity, overrides={"user": {"zero_warn": 10}})
    assert [b["breaker"] for b in breakers] == ["zero_candidates_at_scale"]

    by_entity = {
        "user": {"clusters_in": 5, "candidate_pairs": 3, "judge_calls": 10, "judge_failures": 3}
    }
    assert evaluate_breakers(by_entity) != []  # 30% > default 10%
    relaxed = evaluate_breakers(by_entity, overrides={"user": {"judge_failure_limit": 0.5}})
    assert relaxed == []


def _policy_kwargs(**thresholds) -> dict:
    return {
        "policy_version": "pol-test-v1",
        "entity_type": "product",
        "allowed_attributes": ["category"],
        "authority_matrix": {"category": {"pim": 1}},
        "critical_attributes": [],
        "rules": ["R1"],
        "thresholds": thresholds,
        "ttls_seconds": {},
        "splink": {"threshold": 0.9, "blocking": ["category"]},
    }


def test_borderline_band_validated_fail_closed() -> None:
    good = Policy(**_policy_kwargs(borderline_band={"low": 0.5, "high": 0.8}))
    assert good.thresholds["borderline_band"] == {"low": 0.5, "high": 0.8}

    with pytest.raises(ValueError, match="low must be < high"):
        Policy(**_policy_kwargs(borderline_band={"low": 0.9, "high": 0.8}))
    with pytest.raises(ValueError, match="low.*must lie in"):
        Policy(**_policy_kwargs(borderline_band={"low": -0.1, "high": 0.8}))
    with pytest.raises(ValueError, match="high.*number"):
        Policy(**_policy_kwargs(borderline_band={"low": 0.5, "high": "always"}))


def test_m3_breaker_thresholds_validated_fail_closed() -> None:
    with pytest.raises(ValueError, match="zero_candidates_clusters"):
        Policy(
            **_policy_kwargs(
                m3_breakers={"zero_candidates_clusters": 0, "judge_failure_limit": 0.1}
            )
        )
    with pytest.raises(ValueError, match="judge_failure_limit.*lie in"):
        Policy(
            **_policy_kwargs(
                m3_breakers={"zero_candidates_clusters": 200, "judge_failure_limit": 1.5}
            )
        )
    with pytest.raises(ValueError, match="judge_failure_limit.*number"):
        Policy(
            **_policy_kwargs(
                m3_breakers={"zero_candidates_clusters": 200, "judge_failure_limit": "high"}
            )
        )
    good = Policy(
        **_policy_kwargs(m3_breakers={"zero_candidates_clusters": 500, "judge_failure_limit": 0.25})
    )
    assert good.thresholds["m3_breakers"]["zero_candidates_clusters"] == 500


def test_unknown_threshold_keys_are_forward_compatible() -> None:
    policy = Policy(**_policy_kwargs(future_threshold={"anything": 1}))
    assert policy.thresholds["future_threshold"] == {"anything": 1}
