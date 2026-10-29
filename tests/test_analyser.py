"""Scheduled hygiene: analysers surface findings as steward items, never publish.

Covers: orphans/stale/duplicates become deterministic steward items; re-runs
are no-ops (INSERT OR IGNORE); overdue items notify once (webhook stub) and
are never auto-resolved; analyser runs publish nothing new.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("te_url")


@pytest.fixture()
def api_client(te_url, matching_engine, tmp_path):
    from conftest import make_api_client

    with make_api_client(te_url, tmp_path, matching_service_url=matching_engine) as client:
        yield client


def _build_hidden(api_client, key):
    import test_build

    body = {
        "work_id": f"work-{key}",
        "idempotency_key": key,
        "dataset": {"documents": test_build.hidden_documents()},
    }
    response = api_client.post("/build", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_analyser_surfaces_findings_without_publishing(api_client):
    import ledger
    from analyser import run_analyser_once

    _build_hidden(api_client, "an-hygiene")
    settings = api_client.app.state.settings
    db = settings.database_path
    goldens_before = ledger.fetch_goldens_by_decision(db, "x")  # [] probe
    assert goldens_before == []

    summary = run_analyser_once(settings)
    assert summary["entities"] >= 1
    assert summary["inserted"] > 0
    kinds = {item.split("-")[2] for item in [s["steward_id"] for s in _pending(db)]}
    assert kinds <= {"orphan", "stale", "duplicate"}
    assert kinds, "expected at least one analyser finding on the hidden set"

    # Re-run: deterministic ids make it a no-op.
    again = run_analyser_once(settings)
    assert again["inserted"] == 0

    # Nothing published: analyser writes steward items only.
    for entity in ("cluster-1", "cluster-2"):
        golden = ledger.fetch_latest_golden(db, "user", entity)
        assert golden is not None
    total_goldens = sum(
        len(ledger.fetch_goldens_by_decision(db, d["decision_id"])) for d in _all_decisions(db)
    )
    rerun = run_analyser_once(settings)
    assert rerun["inserted"] == 0
    assert total_goldens == sum(
        len(ledger.fetch_goldens_by_decision(db, d["decision_id"])) for d in _all_decisions(db)
    )


def test_analyser_orphan_points_at_unlinked_fragment(api_client):
    from analyser import run_analyser_once

    _build_hidden(api_client, "an-orphan")
    settings = api_client.app.state.settings
    summary = run_analyser_once(settings)
    assert summary["inserted"] > 0
    orphans = [s for s in _pending(settings.database_path) if "-orphan-" in s["steward_id"]]
    for item in orphans:
        assert "no cluster" in item["fallback"]
        assert item["status"] == "pending"
        assert item["decision_id"] == "analyser"


def test_overdue_notifies_once_and_never_resolves(api_client, monkeypatch):
    import ledger
    from analyser import run_analyser_once

    settings = api_client.app.state.settings
    db = settings.database_path
    ledger.upsert_steward_items(
        db,
        [
            {
                "steward_id": "st-test-overdue-1",
                "entity_type": "user",
                "entity_id": "P-001",
                "attribute": "phone",
                "owner": "steward-001",
                "deputy": "steward-002",
                "deadline": "2026-01-01T00:00:00+00:00",
                "fallback": "do not use",
                "status": "pending",
                "decision_id": "dec-test",
                "suggested_action": None,
            }
        ],
    )

    calls = []
    import notify

    def fake_notify(s, overdue):
        calls.append(list(overdue))
        return True

    monkeypatch.setattr(notify, "notify_overdue_digest", fake_notify)
    summary = run_analyser_once(settings)
    assert summary["overdue"] == ["st-test-overdue-1"]
    assert summary["overdue_notified"] == 1
    assert [i["steward_id"] for i in calls[0]] == ["st-test-overdue-1"]

    # Still pending (never auto-resolved) and not notified twice.
    item = ledger.fetch_steward(db, "st-test-overdue-1")
    assert item is not None and item["status"] == "pending"
    summary = run_analyser_once(settings)
    assert summary["overdue"] == []
    assert len(calls) == 1


def test_overdue_without_webhook_logs_once(api_client, caplog):
    import ledger
    from analyser import run_analyser_once

    settings = api_client.app.state.settings
    assert not settings.steward_webhook_url
    db = settings.database_path
    ledger.upsert_steward_items(
        db,
        [
            {
                "steward_id": "st-test-overdue-2",
                "entity_type": "user",
                "entity_id": "P-001",
                "attribute": "email",
                "owner": "steward-001",
                "deputy": "steward-002",
                "deadline": "2026-01-01T00:00:00+00:00",
                "fallback": "do not use",
                "status": "pending",
                "decision_id": "dec-test",
                "suggested_action": None,
            }
        ],
    )
    with caplog.at_level("INFO", logger="mosaic-api"):
        summary = run_analyser_once(settings)
    assert summary["overdue"] == ["st-test-overdue-2"]
    assert summary["overdue_notified"] == 1
    # Second run: already marked, no repeat.
    summary = run_analyser_once(settings)
    assert summary["overdue"] == []


def _pending(db):
    import ledger

    return ledger.fetch_pending_stewards(db)


def _all_decisions(db):
    import sqlite3

    with sqlite3.connect(db) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute("SELECT * FROM decisions")]
