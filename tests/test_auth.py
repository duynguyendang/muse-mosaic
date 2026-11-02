"""MOSAIC_API_TOKEN gates every state-changing request; reads stay open."""

from __future__ import annotations

from config import Settings
from fastapi.testclient import TestClient
from main import create_app


def _client(tmp_path, token):
    return TestClient(create_app(Settings(database_path=tmp_path / "a.db", api_token=token)))


def test_mutation_without_token_is_401(tmp_path):
    c = _client(tmp_path, "s3cret")
    r = c.post("/api/v1/pipeline/copilot/draft", json={"instruction": "treat crm as tier 3"})
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


def test_wrong_token_is_401_and_right_token_passes(tmp_path):
    c = _client(tmp_path, "s3cret")
    body = {"instruction": "hello"}
    bad = c.post(
        "/api/v1/pipeline/copilot/draft", json=body, headers={"Authorization": "Bearer nope"}
    )
    assert bad.status_code == 401
    ok = c.post(
        "/api/v1/pipeline/copilot/draft", json=body, headers={"Authorization": "Bearer s3cret"}
    )
    assert ok.status_code == 200


def test_reads_stay_open_when_token_set(tmp_path):
    assert _client(tmp_path, "s3cret").get("/health").status_code == 200


def test_no_token_configured_keeps_dev_behaviour(tmp_path):
    c = _client(tmp_path, "")
    assert (
        c.post("/api/v1/pipeline/copilot/draft", json={"instruction": "hello"}).status_code == 200
    )
