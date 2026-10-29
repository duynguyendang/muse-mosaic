"""Unattended intake: inbox files and signed webhooks enqueue jobs, never derive.

Covers: valid file -> queued + moved to processed/; invalid JSON -> failed/;
duplicate content -> no second job; conflicting payload same key -> failed/;
webhook without secret -> 503; bad signature -> 401; good signature -> 202.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

pytestmark = pytest.mark.usefixtures("te_url")


@pytest.fixture()
def job_client(te_url, matching_engine, tmp_path):
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


def test_inbox_valid_file_enqueues_and_moves(job_client, tmp_path):
    import worker

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    settings = job_client.app.state.settings
    settings = _replace(settings, inbox_dir=str(inbox))

    (inbox / "a.json").write_text(json.dumps(hidden_body("inbox-1")), encoding="utf-8")
    counts = worker.inbox_poll_once(settings)
    assert counts == {"queued": 1, "skipped": 0, "failed": 0}
    assert (inbox / "processed" / "a.json").exists()
    assert not (inbox / "a.json").exists()

    queued = job_client.get("/jobs").json()["jobs"]
    assert [j["idempotency_key"] for j in queued] == ["inbox-1"]

    # Second poll: file is gone, nothing new enqueued.
    counts = worker.inbox_poll_once(settings)
    assert counts == {"queued": 0, "skipped": 0, "failed": 0}
    assert len(job_client.get("/jobs").json()["jobs"]) == 1


def test_inbox_invalid_json_moves_to_failed(job_client, tmp_path):
    import worker

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    settings = _replace(job_client.app.state.settings, inbox_dir=str(inbox))

    (inbox / "bad.json").write_text("{not json", encoding="utf-8")
    counts = worker.inbox_poll_once(settings)
    assert counts == {"failed": 1, "queued": 0, "skipped": 0}
    assert (inbox / "failed" / "bad.json").exists()
    assert job_client.get("/jobs").json()["jobs"] == []


def test_inbox_duplicate_content_does_not_double_enqueue(job_client, tmp_path):
    import worker

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    settings = _replace(job_client.app.state.settings, inbox_dir=str(inbox))

    (inbox / "a.json").write_text(json.dumps(hidden_body("inbox-dup")), encoding="utf-8")
    (inbox / "b.json").write_text(json.dumps(hidden_body("inbox-dup")), encoding="utf-8")
    counts = worker.inbox_poll_once(settings)
    assert counts["queued"] == 2  # both files accepted...
    jobs = job_client.get("/jobs").json()["jobs"]
    assert len(jobs) == 1  # ...but map to the same job (same key + same hash)
    assert (inbox / "processed" / "a.json").exists()
    assert (inbox / "processed" / "b.json").exists()


def test_inbox_conflicting_key_moves_to_failed(job_client, tmp_path):
    import worker

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    settings = _replace(job_client.app.state.settings, inbox_dir=str(inbox))

    first = dict(hidden_body("inbox-clash"))
    (inbox / "a.json").write_text(json.dumps(first), encoding="utf-8")
    worker.inbox_poll_once(settings)

    second = dict(hidden_body("inbox-clash"))
    second["dataset"] = {"documents": second["dataset"]["documents"][:1]}
    (inbox / "b.json").write_text(json.dumps(second), encoding="utf-8")
    counts = worker.inbox_poll_once(settings)
    assert counts["failed"] == 1
    assert (inbox / "failed" / "b.json").exists()
    assert len(job_client.get("/jobs").json()["jobs"]) == 1


def test_inbox_disabled_when_no_dir(job_client):
    import worker

    counts = worker.inbox_poll_once(job_client.app.state.settings)
    assert counts == {"queued": 0, "skipped": 0, "failed": 0}


def _replace(settings, **kwargs):
    import dataclasses

    return dataclasses.replace(settings, **kwargs)


def _webhook_client(tmp_path, secret):
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
        ingest_webhook_secret=secret,
    )
    return TestClient(create_app(settings))


def _sign(secret, body):
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_webhook_disabled_without_secret(tmp_path):
    with _webhook_client(tmp_path, "") as client:
        response = client.post(
            "/ingest/webhook",
            json=hidden_body("wh-1"),
            headers={"X-Mosaic-Signature": "anything"},
        )
        assert response.status_code == 503, response.text


def test_webhook_rejects_bad_signature(tmp_path):
    with _webhook_client(tmp_path, "s3cret") as client:
        response = client.post("/ingest/webhook", json=hidden_body("wh-1"))
        assert response.status_code == 401, response.text
        response = client.post(
            "/ingest/webhook",
            json=hidden_body("wh-1"),
            headers={"X-Mosaic-Signature": "wrong"},
        )
        assert response.status_code == 401, response.text
        assert client.get("/jobs").json()["jobs"] == []


def test_webhook_accepts_signed_body(tmp_path):
    with _webhook_client(tmp_path, "s3cret") as client:
        raw = json.dumps(hidden_body("wh-2")).encode()
        response = client.post(
            "/ingest/webhook",
            content=raw,
            headers={
                "Content-Type": "application/json",
                "X-Mosaic-Signature": _sign("s3cret", raw),
            },
        )
        assert response.status_code == 202, response.text
        payload = response.json()
        assert payload["status"] == "queued"
        assert len(client.get("/jobs").json()["jobs"]) == 1


def test_notify_posts_signed_payload_and_survives_outage(tmp_path):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from notify import SIGNATURE_HEADER, notify_job_terminal

    received = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            received["body"] = self.rfile.read(length)
            received["signature"] = self.headers.get(SIGNATURE_HEADER)
            received["path"] = self.path
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        from config import Settings

        settings = Settings(
            use_mock_muse=True,
            steward_webhook_url=f"http://127.0.0.1:{server.server_port}/hook",
            steward_webhook_secret="shh",
        )
        job = {"job_id": "j1", "kind": "build"}
        summary = {
            "verdict": "escalated",
            "goldens": [{"golden_id": "g1"}],
            "steward_queue": [
                {
                    "steward_id": "st-1",
                    "entity_type": "user",
                    "entity_id": "P-001",
                    "attribute": "phone",
                    "deadline": "tomorrow",
                    "fallback": "do not use",
                    "suggested_action": None,
                }
            ],
        }
        assert notify_job_terminal(settings, job, "escalated", summary, None) is True
        event = json.loads(received["body"])
        assert event["event"] == "job_terminal"
        assert event["job_id"] == "j1"
        assert event["steward_items"][0]["steward_id"] == "st-1"
        assert received["signature"] == _sign("shh", received["body"])
    finally:
        server.shutdown()
        thread.join(timeout=5)

    # Unreachable webhook: False, no raise.
    from config import Settings as _Settings

    down = _Settings(
        use_mock_muse=True,
        steward_webhook_url="http://127.0.0.1:9/hook",
    )
    assert notify_job_terminal(down, {"job_id": "j2"}, "failed", {}, "boom") is False
    # No webhook configured: False, no raise, no network.
    plain = _Settings(use_mock_muse=True)
    assert notify_job_terminal(plain, {"job_id": "j3"}, "denied", {}, None) is False
