"""Fact/golden contract, policy, mock-data and DDL tests.

These pass in the scaffold. Rule parity tests are separate and skipped until
the manglekit adapter exists (CSD 12.1 step 5 / 12.7).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import ledger
import pytest
from config import Settings
from fastapi.testclient import TestClient
from main import create_app
from pydantic import ValidationError
from schemas import (
    CandidateFact,
    DeriveRequest,
    Fragment,
    GoldenFact,
    Policy,
    ResolveRequest,
)

ROOT = Path(__file__).resolve().parents[1]
MOCK_DIR = ROOT / "data" / "mock"
POLICY_DIR = ROOT / "trust-engine" / "policies"
USER_POLICY_PATH = POLICY_DIR / "user" / "pol-2026-10-27-v1.json"
PRODUCT_POLICY_PATH = POLICY_DIR / "product" / "pol-product-2026-10-27-v1.json"


def candidate_data(**overrides):
    data = {
        "fact_id": "fact-001",
        "entity_type": "user",
        "entity_id": "P-001",
        "attribute": "phone",
        "value": "+84 900 000 001",
        "source": "sap-like",
        "source_id": "sap-001-phone",
        "authority_rank": 1,
        "event_time": "2026-10-26T09:00:00+07:00",
        "verified": True,
        "ttl_seconds": 3600,
    }
    data.update(overrides)
    return data


def golden_data(**overrides):
    data = {
        "entity_type": "user",
        "entity_id": "P-001",
        "attribute": "phone",
        "value": "+84 900 000 001",
        "winner_fact_id": "fact-sap-phone",
        "loser_fact_ids": ["fact-sf-phone"],
        "rule_fired": "R1",
        "policy_version": "pol-2026-10-27-v1",
        "lineage": "sap-like/sap-001-phone -> R1 -> published",
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# Fact contract
# ---------------------------------------------------------------------------


def test_candidate_fact_accepts_complete_provenance():
    fact = CandidateFact(**candidate_data())
    assert fact.authority_rank == 1
    assert fact.verified is True
    assert fact.event_time.utcoffset() is not None


@pytest.mark.parametrize("missing", ["source", "event_time", "authority_rank"])
def test_candidate_fact_rejects_missing_provenance(missing):
    data = candidate_data()
    data.pop(missing)
    with pytest.raises(ValidationError):
        CandidateFact(**data)


def test_candidate_fact_rejects_embedding_as_derivation_input():
    data = candidate_data()
    data["embedding"] = [0.1, 0.2, 0.3]
    with pytest.raises(ValidationError):
        CandidateFact(**data)


def test_candidate_fact_requires_timezone_aware_event_time():
    with pytest.raises(ValidationError):
        CandidateFact(**candidate_data(event_time="2026-10-26T09:00:00"))


@pytest.mark.parametrize(
    "overrides",
    [
        {"authority_rank": 0},
        {"ttl_seconds": 0},
        {"verified": "maybe"},
        {"entity_type": "User"},
    ],
)
def test_candidate_fact_rejects_invalid_gate_values(overrides):
    with pytest.raises(ValidationError):
        CandidateFact(**candidate_data(**overrides))


def test_fragment_rejects_missing_ttl():
    data = candidate_data()
    data.pop("fact_id")
    data.pop("ttl_seconds")
    with pytest.raises(ValidationError):
        Fragment(**data)


def test_golden_fact_accepts_complete_proof():
    fact = GoldenFact(**golden_data())
    assert fact.rule_fired == "R1"
    assert fact.policy_version == "pol-2026-10-27-v1"


@pytest.mark.parametrize("missing", ["winner_fact_id", "rule_fired", "policy_version", "lineage"])
def test_golden_fact_rejects_missing_proof(missing):
    data = golden_data()
    data.pop(missing)
    with pytest.raises(ValidationError):
        GoldenFact(**data)


def test_golden_fact_winner_cannot_be_a_loser():
    with pytest.raises(ValidationError):
        GoldenFact(
            **golden_data(
                winner_fact_id="fact-sap-phone",
                loser_fact_ids=["fact-sap-phone", "fact-sf-phone"],
            )
        )


# ---------------------------------------------------------------------------
# API request examples from CSD 12.4
# ---------------------------------------------------------------------------


def test_user_resolve_example_validates():
    request = ResolveRequest.model_validate(
        {
            "work_id": "work-001",
            "entity_type": "user",
            "entity_id": "P-001",
            "idempotency_key": "idem-001",
            "fragments": [
                {
                    "source": "salesforce-like",
                    "source_id": "sf-001",
                    "attribute": "phone",
                    "value": "A",
                    "authority_rank": 2,
                    "event_time": "2026-10-27T09:00:00+07:00",
                    "verified": False,
                    "ttl_seconds": 3600,
                },
                {
                    "source": "sap-like",
                    "source_id": "sap-001",
                    "attribute": "phone",
                    "value": "B",
                    "authority_rank": 1,
                    "event_time": "2026-10-26T09:00:00+07:00",
                    "verified": True,
                    "ttl_seconds": 3600,
                },
            ],
        }
    )
    assert len(request.fragments) == 2


def test_product_resolve_example_validates():
    request = ResolveRequest.model_validate(
        {
            "work_id": "work-101",
            "entity_type": "product",
            "entity_id": "prod-001",
            "idempotency_key": "idem-101",
            "fragments": [
                {
                    "source": "pim-like",
                    "source_id": "pim-001",
                    "attribute": "category",
                    "value": "Smartphone",
                    "authority_rank": 1,
                    "event_time": "2026-10-27T09:00:00+07:00",
                    "verified": True,
                    "ttl_seconds": 3600,
                },
                {
                    "source": "marketplace-like",
                    "source_id": "mp-001",
                    "attribute": "category",
                    "value": "Phụ kiện điện thoại",
                    "authority_rank": 3,
                    "event_time": "2026-10-27T10:00:00+07:00",
                    "verified": False,
                    "ttl_seconds": 3600,
                },
            ],
        }
    )
    assert request.entity_type == "product"


def test_derive_request_rejects_envelope_mismatch():
    fact = candidate_data()
    with pytest.raises(ValidationError):
        DeriveRequest.model_validate(
            {
                "decision_id": "dec-001",
                "entity_type": "user",
                "entity_id": "P-002",
                "idempotency_key": "idem-001",
                "payload_hash": "hash-001",
                "policy_version": "pol-2026-10-27-v1",
                "candidate_facts": [fact],
            }
        )


# ---------------------------------------------------------------------------
# Policies (externalized, versioned; no invented ranks)
# ---------------------------------------------------------------------------


def test_user_policy_matches_csd_v1_matrix():
    policy = Policy.model_validate_json(USER_POLICY_PATH.read_text(encoding="utf-8"))
    assert policy.policy_version == "pol-2026-10-27-v1"
    assert policy.entity_type == "user"
    assert policy.critical_attributes == ["phone"]
    assert policy.authority_matrix["phone"] == {
        "sap-like": 1,
        "salesforce-like": 2,
        "ciam-like": 3,
        "ticket": 3,
    }
    assert policy.authority_matrix["email"] == {
        "ciam-like": 1,
        "salesforce-like": 2,
        "sap-like": 3,
        "ticket": 3,
    }
    assert policy.authority_matrix["address"] == {
        "ticket-verified": 1,
        "salesforce-like": 2,
        "sap-like": 3,
        "ciam-like": 3,
    }
    # v1 gap (kept for history): display_name is allowed but has no
    # authority ranks. The engine must escalate/deny it, not invent ranks.
    assert "display_name" in policy.allowed_attributes
    assert "display_name" not in policy.authority_matrix


def test_user_policy_v2_adds_display_name_ranks():
    # Policy fix for the P-001 steward-queue pile-up: v2 assigns display_name
    # authority ranks (mirroring the fixture ranks), so an agreeing
    # display_name publishes R1 instead of escalating R4 NO_AUTHORITY_RANKS.
    # v1 is untouched above; the engine resolves explicit versions (G17/G21).
    path = POLICY_DIR / "user" / "pol-2026-10-28-v1.json"
    policy = Policy.model_validate_json(path.read_text(encoding="utf-8"))
    assert policy.policy_version == "pol-2026-10-28-v1"
    assert policy.authority_matrix["display_name"] == {
        "sap-like": 1,
        "salesforce-like": 2,
        "ciam-like": 3,
        "ticket": 3,
    }


def test_product_policy_matches_csd_v1_matrix():
    policy = Policy.model_validate_json(PRODUCT_POLICY_PATH.read_text(encoding="utf-8"))
    assert policy.policy_version == "pol-product-2026-10-27-v1"
    assert policy.entity_type == "product"
    assert policy.allowed_attributes == ["category"]
    assert policy.authority_matrix["category"] == {
        "pim-like": 1,
        "erp-like": 2,
        "marketplace-like": 3,
    }
    # Not specified by the CSD; left empty rather than invented.
    assert policy.critical_attributes == []


def test_policy_rejects_authority_for_disallowed_attribute():
    data = json.loads(USER_POLICY_PATH.read_text(encoding="utf-8"))
    data["authority_matrix"]["ssn"] = {"sap-like": 1}
    with pytest.raises(ValidationError):
        Policy.model_validate(data)


def test_policy_rejects_critical_attribute_not_allowed():
    data = json.loads(USER_POLICY_PATH.read_text(encoding="utf-8"))
    data["critical_attributes"] = ["ssn"]
    with pytest.raises(ValidationError):
        Policy.model_validate(data)


# ---------------------------------------------------------------------------
# Synthetic mock fixtures
# ---------------------------------------------------------------------------


def test_user_mock_fixture_is_labelled_and_contains_required_conflict():
    data = json.loads((MOCK_DIR / "users.json").read_text(encoding="utf-8"))
    assert data["synthetic"] is True
    assert "SYNTHETIC" in data["notice"]

    entity = data["entities"][0]
    assert entity["entity_type"] == "user"
    assert entity["entity_id"] == "P-001"
    fragments = [Fragment.model_validate(item) for item in entity["fragments"]]

    sources = {fragment.source for fragment in fragments}
    assert {"salesforce-like", "sap-like", "ciam-like"} <= sources

    phones = [fragment for fragment in fragments if fragment.attribute == "phone"]
    assert len(phones) == 2
    newer_unverified = max(phones, key=lambda item: item.event_time)
    older_verified = min(phones, key=lambda item: item.event_time)
    assert newer_unverified.verified is False
    assert older_verified.verified is True
    assert older_verified.authority_rank < newer_unverified.authority_rank

    emails = {fragment.value for fragment in fragments if fragment.attribute == "email"}
    assert len(emails) == 1

    addresses = {fragment.value for fragment in fragments if fragment.attribute == "address"}
    assert len(addresses) > 1


def test_product_mock_fixture_has_conflict_and_clean_case():
    data = json.loads((MOCK_DIR / "products.json").read_text(encoding="utf-8"))
    assert data["synthetic"] is True
    entities = {entity["entity_id"]: entity for entity in data["entities"]}
    assert {"prod-001", "prod-002"} <= set(entities)

    conflicted = {
        fragment["value"]
        for fragment in entities["prod-001"]["fragments"]
        if fragment["attribute"] == "category"
    }
    assert len(conflicted) == 3

    clean = {
        fragment["value"]
        for fragment in entities["prod-002"]["fragments"]
        if fragment["attribute"] == "category"
    }
    assert clean == {"Wireless Earbuds"}


def test_ticket_fixtures_are_synthetic_and_injection_is_unverified():
    data = json.loads((MOCK_DIR / "tickets.json").read_text(encoding="utf-8"))
    assert data["synthetic"] is True
    tickets = {ticket["ticket_id"]: ticket for ticket in data["tickets"]}

    injection = tickets["TCK-001"]
    assert injection["kind"] == "injection-bearing"
    assert injection["synthetic"] is True
    for item in injection["proposed_fragments"]:
        fragment = Fragment.model_validate(item)
        assert fragment.attribute == "phone"
        assert fragment.verified is False

    legitimate = tickets["TCK-002"]
    assert legitimate["kind"] == "legitimate"
    for item in legitimate["proposed_fragments"]:
        fragment = Fragment.model_validate(item)
        assert fragment.verified is True
        assert fragment.source == "ticket-verified"


# ---------------------------------------------------------------------------
# SQLite DDL and ledger reads
# ---------------------------------------------------------------------------


def test_ddl_creates_tables_and_gate_columns(tmp_path):
    database = tmp_path / "ledger.db"
    ledger.init_db(database)

    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {
            "golden_records",
            "candidate_facts",
            "decisions",
            "steward_queue",
            "tombstones",
            "conflicts",
            "graph_nodes",
            "graph_edges",
            "agent_traces",
        } <= tables

        golden_columns = {row[1] for row in connection.execute("PRAGMA table_info(golden_records)")}
        assert {
            "golden_id",
            "entity_type",
            "entity_id",
            "version",
            "attributes",
            "profile_embedding",
            "embedding_model",
            "embedding_version",
            "embedding_dim",
            "decision_id",
            "policy_version",
            "taxonomy_version",
        } <= golden_columns

        candidate_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(candidate_facts)")
        }
        assert {
            "fact_id",
            "entity_type",
            "entity_id",
            "attribute",
            "value",
            "source",
            "source_id",
            "authority_rank",
            "event_time",
            "verified",
            "ttl_seconds",
            "embedding",
            "embedding_model",
            "embedding_version",
            "embedding_dim",
            "splink_match_weight",
            "splink_match_probability",
        } <= candidate_columns

        node_columns = {row[1] for row in connection.execute("PRAGMA table_info(graph_nodes)")}
        assert {"node_id", "node_type", "entity_type", "entity_id", "decision_id"} <= node_columns
        edge_columns = {row[1] for row in connection.execute("PRAGMA table_info(graph_edges)")}
        assert {
            "edge_id",
            "edge_type",
            "from_node_id",
            "to_node_id",
            "proof",
            "proposer",
            "rule_fired",
            "taxonomy_version",
        } <= edge_columns
        steward_columns = {row[1] for row in connection.execute("PRAGMA table_info(steward_queue)")}
        assert "suggested_action" in steward_columns
        decision_columns = {row[1] for row in connection.execute("PRAGMA table_info(decisions)")}
        assert "taxonomy_version" in decision_columns


def test_decisions_idempotency_key_is_unique(tmp_path):
    database = tmp_path / "ledger.db"
    ledger.init_db(database)
    row = (
        "dec-001",
        "work-001",
        "idem-001",
        "hash-001",
        "R1",
        "published",
        None,
        "pol-2026-10-27-v1",
        "tax-user-2026-10-27-v1",
        "2026-10-27T10:00:00+07:00",
    )
    columns = (
        "decision_id, work_id, idempotency_key, payload_hash, rule_fired, "
        "verdict, reason_code, policy_version, taxonomy_version, created_at"
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            f"INSERT INTO decisions ({columns}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", row
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f"INSERT INTO decisions ({columns}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("dec-002", *row[1:]),
            )


def test_fetch_latest_golden_returns_highest_version(tmp_path):
    database = tmp_path / "ledger.db"
    ledger.init_db(database)
    with sqlite3.connect(database) as connection:
        for version, decision in ((1, "dec-001"), (2, "dec-002")):
            connection.execute(
                """
                INSERT INTO golden_records
                  (golden_id, entity_type, entity_id, version, attributes,
                   decision_id, policy_version, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"golden-{version}",
                    "user",
                    "P-001",
                    version,
                    json.dumps({"phone": {"value": "B"}}),
                    decision,
                    "pol-2026-10-27-v1",
                    "2026-10-27T10:00:00+07:00",
                    "2026-10-27T10:00:00+07:00",
                ),
            )

    record = ledger.fetch_latest_golden(database, "user", "P-001")
    assert record is not None
    assert record["version"] == 2
    assert record["decision_id"] == "dec-002"
    assert record["attributes"]["phone"]["value"] == "B"


# ---------------------------------------------------------------------------
# API behaviour (implemented paths + fail-closed paths)
# ---------------------------------------------------------------------------


def test_api_reads_empty_ledger_and_reports_engine_down(tmp_path):
    settings = Settings(
        use_mock_muse=True,
        meta_model_api_key="",
        muse_model_id="",
        embedding_model="",
        embedding_version="",
        embedding_dim=None,
        trust_engine_url="http://127.0.0.1:9/derive",
        manglekit_bin="",
        database_path=str(tmp_path / "ledger.db"),
    )
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/golden/user/P-001").status_code == 404
        assert client.get("/golden/!bad/P-001").status_code == 422
        assert client.get("/explain/dec-001").status_code == 404
        assert client.get("/fragments/user/unknown").status_code == 404
        assert client.get("/similar?entity_type=user&entity_id=P-001").status_code == 200
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["mosaic_api"] == "ok"
        assert health.json()["trust_engine"] == "down"
        assert health.json()["splink"] in ("ok", "unavailable")
        # Engine unreachable escalates fail-closed: 503, steward recorded,
        # no golden, no decision row that could be mistaken for a derivation.
        down = client.post(
            "/resolve",
            json={
                "work_id": "work-001",
                "entity_type": "user",
                "entity_id": "P-001",
                "idempotency_key": "idem-001",
                "fragments": [
                    {
                        "source": "sap-like",
                        "source_id": "sap-001",
                        "attribute": "phone",
                        "value": "B",
                        "authority_rank": 1,
                        "event_time": "2026-10-26T09:00:00+07:00",
                        "verified": True,
                        "ttl_seconds": 3600,
                    }
                ],
            },
        )
        assert down.status_code == 503
        detail = down.json()["detail"]
        assert detail["verdict"] == "escalated"
        assert detail["reason_code"] == "TRUST_ENGINE_UNAVAILABLE"
        assert detail["fail_closed"] is True
        assert client.get("/golden/user/P-001").status_code == 404
        pending = client.get("/steward/pending").json()["pending"]
        assert len(pending) == 1


def test_api_rejects_disallowed_attribute_before_derive(tmp_path):
    settings = Settings(
        use_mock_muse=True,
        meta_model_api_key="",
        muse_model_id="",
        embedding_model="",
        embedding_version="",
        embedding_dim=None,
        trust_engine_url="http://127.0.0.1:9/derive",
        manglekit_bin="",
        database_path=str(tmp_path / "ledger.db"),
    )
    app = create_app(settings)
    with TestClient(app) as client:
        rejected = client.post(
            "/resolve",
            json={
                "work_id": "work-001",
                "entity_type": "user",
                "entity_id": "P-001",
                "idempotency_key": "idem-001",
                "fragments": [
                    {
                        "source": "sap-like",
                        "source_id": "sap-001",
                        "attribute": "ssn",
                        "value": "123",
                        "authority_rank": 1,
                        "event_time": "2026-10-26T09:00:00+07:00",
                        "verified": True,
                        "ttl_seconds": 3600,
                    }
                ],
            },
        )
        assert rejected.status_code == 422


def test_policy_mg_matrix_matches_policy_json():
    import re

    for _entity_type, policy_path in (
        ("user", USER_POLICY_PATH),
        ("product", PRODUCT_POLICY_PATH),
    ):
        policy = Policy.model_validate_json(policy_path.read_text(encoding="utf-8"))
        mg_path = policy_path.with_suffix(".mg")
        assert mg_path.exists(), f"missing runnable policy {mg_path}"
        mg = mg_path.read_text(encoding="utf-8")
        assert 'json_link("input", "facts", L)' in mg
        mg_ranks: dict[str, dict[str, int]] = {}
        for match in re.finditer(r'auth_rank\("([^"]+)", "([^"]+)", (\d+)\)\.', mg):
            mg_ranks.setdefault(match.group(1), {})[match.group(2)] = int(match.group(3))
        assert mg_ranks == policy.authority_matrix
        for critical in policy.critical_attributes:
            assert f'critical("{critical}").' in mg
        assert f"policy_version: {policy.policy_version}" in mg
        # v2.3 rule tiers present and wired to the same outcome contract.
        # L1 surfaces as verified links (verified_hop/vlink); C1/C2/T1 as
        # escalate reasons; I1 is determinism-by-construction (documented).
        for rule in ("C1", "C2", "T1"):
            assert f'"{rule}"' in mg, f"{policy_path} missing rule {rule}"
        assert "verified_hop" in mg
        assert "vlink(F, T, P)" in mg
        assert set(policy.rules) >= {"R1", "R2", "R3", "R4", "L1", "C1", "C2", "T1", "I1"}
        assert set(policy.golden_attributes) <= set(policy.allowed_attributes)


def test_manglekit_version_pin_is_coherent():
    import re

    adapter_go = (ROOT / "trust-engine" / "adapter.go").read_text(encoding="utf-8")
    (go_version,) = re.findall(r'ManglekitVersion\s*=\s*"([^"]+)"', adapter_go)
    dockerfile = (ROOT / "trust-engine" / "Dockerfile").read_text(encoding="utf-8")
    (docker_version,) = re.findall(r"ARG MANGLEKIT_VERSION=(v\S+)", dockerfile)
    conftest = (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    (test_version,) = re.findall(r'MANGLEKIT_VERSION = "([^"]+)"', conftest)
    assert go_version == docker_version == test_version
    assert go_version.startswith("v")
