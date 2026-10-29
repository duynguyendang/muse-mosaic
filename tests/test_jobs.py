"""Background jobs: enqueue, run, replay, resume, retry, cancel, budgets.

Driven through run_job_once with background tasks disabled (conftest), so
every transition is deterministic. Mock Muse mode: no model calls, usage 0.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("te_url")


@pytest.fixture()
def job_client(te_url, matching_engine, tmp_path):
    """API client with background tasks off; tests drive run_job_once."""
    from conftest import make_api_client

    with make_api_client(te_url, tmp_path, matching_service_url=matching_engine) as client:
        yield client


def hidden_body(key):
    import test_build

    return {
        "work_id": f"work-{key}",
        "idempotency_key": key,
        "dataset": {"documents": test_build.hidden_documents()},
    }


def drive(client, limit=20):
    """Run queued jobs to completion. Returns the first claimed job id."""
    import jobs

    app = client.app
    first = None
    for _ in range(limit):
        claimed = asyncio.run(
            jobs.run_job_once(
                app.state.settings,
                app.state.policies,
                app.state.taxonomies,
                app.state.expected_structures,
            )
        )
        if claimed is None:
            break
        first = first or claimed
    return first


def test_enqueue_and_run_publishes(job_client):
    first = job_client.post("/jobs", json=hidden_body("job-h01"))
    assert first.status_code == 202, first.text
    assert first.json()["status"] == "queued"
    job_id = first.json()["job_id"]

    assert drive(job_client) == job_id
    assert drive(job_client) is None  # queue drained

    got = job_client.get(f"/jobs/{job_id}").json()
    assert got["status"] in ("published", "escalated", "denied")
    assert got["result"]["verdict"] == got["status"]
    goldens = [g for g in got["result"]["goldens"] if g.get("golden_id")]
    assert goldens, "expected published goldens in the job result"
    by_entity = {g["entity_id"]: g for g in goldens}
    assert by_entity["cluster-1"]["attributes"]["phone"]["value"] == "0901234567"
    assert by_entity["cluster-2"]["attributes"]["phone"]["value"] == "0901234568"

    # Job result must match the synchronous /build verdict for the same input.
    sync = job_client.post("/build", json=hidden_body("job-h01-sync"))
    assert sync.status_code == 200, sync.text
    assert sync.json()["verdict"] == got["result"]["verdict"]
    for golden in goldens:
        if golden["entity_id"] != "cluster-1":
            continue
        for _name, field in golden["attributes"].items():
            if isinstance(field, dict):
                assert (
                    field.get("winner_fact_id", "").find("F6-") == -1
                ), "trap fact must never win in the main cluster"

    listed = job_client.get("/jobs").json()["jobs"]
    assert any(j["job_id"] == job_id for j in listed)
    assert job_client.get(f"/jobs?status={got['status']}").json()["jobs"]


def test_duplicate_submit_replays_same_job(job_client):
    first = job_client.post("/jobs", json=hidden_body("job-dup"))
    assert first.status_code == 202, first.text
    second = job_client.post("/jobs", json=hidden_body("job-dup"))
    assert second.status_code == 202, second.text
    assert second.json()["job_id"] == first.json()["job_id"]

    drive(job_client)
    drive(job_client)  # second drive finds nothing queued
    got = job_client.get(f"/jobs/{first.json()['job_id']}").json()
    assert got["status"] in ("published", "escalated", "denied")
    # No duplicate golden versions from the replayed run.
    versions = [
        g["version"]
        for g in got["result"]["goldens"]
        if g.get("golden_id") and g["entity_id"] == "cluster-1"
    ]
    assert versions == [1]


def test_conflicting_payload_rejected_without_enqueue(job_client):
    body = hidden_body("job-conflict")
    assert job_client.post("/jobs", json=body).status_code == 202
    body["work_id"] = "work-different-payload"
    body["dataset"]["documents"] = body["dataset"]["documents"][:1]
    conflict = job_client.post("/jobs", json=body)
    assert conflict.status_code == 409, conflict.text
    jobs = job_client.get("/jobs").json()["jobs"]
    assert [j["idempotency_key"] for j in jobs] == ["job-conflict"]


def test_resume_after_completion_replays_without_new_versions(job_client):
    import ledger

    response = job_client.post("/jobs", json=hidden_body("job-resume"))
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    drive(job_client)
    before = job_client.get(f"/jobs/{job_id}").json()
    assert before["status"] in ("published", "escalated", "denied")

    # Simulate a crash mid-build: force the finished job back to queued.
    db = job_client.app.state.settings.database_path
    ledger.update_job(db, job_id, "2026-10-29T00:00:00+00:00", status="queued", attempts=0)
    drive(job_client)
    after = job_client.get(f"/jobs/{job_id}").json()
    assert after["status"] == before["status"]
    # Same clusters replayed to the same decisions: no new golden versions.
    goldens = [g for g in after["result"]["goldens"] if g.get("golden_id")]
    assert sorted(g["decision_id"] for g in goldens) == sorted(
        g["decision_id"] for g in before["result"]["goldens"] if g.get("golden_id")
    )


def _down_client(tmp_path):
    """Client pointed at a dead trust-engine, one attempt then fail."""
    from pathlib import Path as _Path

    from config import Settings
    from fastapi.testclient import TestClient
    from main import create_app

    settings = Settings(
        use_mock_muse=True,
        meta_model_api_key="",
        muse_model_id="",
        embedding_model="",
        embedding_version="",
        embedding_dim=None,
        trust_engine_url="http://127.0.0.1:9/derive",
        manglekit_bin="",
        database_path=str(_Path(tmp_path) / "mosaic.db"),
        enable_background_tasks=False,
        job_max_attempts=1,
    )
    return TestClient(create_app(settings))


def test_engine_down_fails_without_publish(tmp_path):
    import ledger

    with _down_client(tmp_path) as client:
        response = client.post("/jobs", json=hidden_body("job-down"))
        assert response.status_code == 202
        drive(client)
        got = client.get(f"/jobs/{response.json()['job_id']}").json()
        assert got["status"] == "failed", got
        assert got["attempts"] == 1
        assert "503" in (got["error"] or "") or "TRUST_ENGINE" in (got["error"] or "")
        db = client.app.state.settings.database_path
        assert ledger.fetch_latest_golden(db, "user", "cluster-1") is None

        retried = client.post(f"/jobs/{got['job_id']}/retry")
        assert retried.status_code == 200
        assert retried.json()["status"] == "queued"
        assert retried.json()["attempts"] == 0


def test_cancel_queued_job_runs_nothing(job_client):
    import ledger

    response = job_client.post("/jobs", json=hidden_body("job-cancel"))
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    cancelled = job_client.delete(f"/jobs/{job_id}")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert drive(job_client) is None
    db = job_client.app.state.settings.database_path
    assert ledger.fetch_latest_golden(db, "user", "cluster-1") is None
    assert job_client.delete(f"/jobs/{job_id}").status_code == 409


def test_unknown_job_ids_404(job_client):
    assert job_client.get("/jobs/nope").status_code == 404
    assert job_client.post("/jobs/nope/retry").status_code == 404
    assert job_client.delete("/jobs/nope").status_code == 404


def test_budget_helpers_count_traces_and_stop(tmp_path):
    import ledger
    from config import Settings
    from jobs import (
        _budget_stop_reason,
        cluster_usage_tokens,
        daily_tokens_used,
    )

    db = str(Path(tmp_path) / "m.db")
    ledger.init_db(db)
    ledger.store_agent_trace(
        db,
        "dec-1",
        "subagents",
        {"usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
        "2026-10-29T00:00:00+00:00",
    )
    assert cluster_usage_tokens(db, ["dec-1", "dec-missing"]) == 15
    assert cluster_usage_tokens(db, []) == 0
    settings = Settings(
        use_mock_muse=True,
        muse_job_token_budget=10,
        muse_daily_token_budget=0,
        database_path=db,
    )
    assert _budget_stop_reason(settings, db, 15) is not None
    assert _budget_stop_reason(settings, db, 5) is None
    assert daily_tokens_used(db) == 0  # tokens_used lives on jobs, not traces


def test_daily_budget_counts_job_tokens(tmp_path):
    import ledger

    db = str(Path(tmp_path) / "m.db")
    ledger.init_db(db)
    now = "2026-10-29T12:00:00+00:00"
    ledger.create_job(
        db,
        job_id="j1",
        kind="build",
        idempotency_key="k1",
        payload_hash="h1",
        payload={"a": 1},
        created_at=now,
    )
    ledger.update_job(db, "j1", now, status="published", tokens_used=40)
    assert ledger.sum_job_tokens_since(db, "2026-10-29T00:00:00+00:00") == 40
    assert ledger.sum_job_tokens_since(db, "2026-10-30T00:00:00+00:00") == 0


def test_job_terminal_row_keeps_charged_tokens(tmp_path):
    """Regression: the terminal update must not clobber per-cluster charges."""
    import ledger
    from config import Settings
    from jobs import _JobHooks

    db = str(Path(tmp_path) / "m.db")
    ledger.init_db(db)
    now = "2026-10-29T12:00:00+00:00"
    ledger.create_job(
        db,
        job_id="j1",
        kind="build",
        idempotency_key="k1",
        payload_hash="h1",
        payload={"a": 1},
        created_at=now,
    )
    ledger.store_agent_trace(
        db,
        "dec-1",
        "subagents",
        {"usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}},
        now,
    )
    settings = Settings(use_mock_muse=True, database_path=db)
    hooks = _JobHooks(settings, "j1", 0)
    hooks.after_cluster([{"decision_id": "dec-1"}, {"replayed_decision_id": "dec-0"}])
    # Replayed decisions carry no new usage; fresh cluster charged 150.
    assert hooks.tokens_used == 150
    assert ledger.fetch_job(db, "j1")["tokens_used"] == 150
