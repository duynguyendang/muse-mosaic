"""CSV upload parsing (ingest-level + /upload endpoint)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("te_url")


@pytest.fixture()
def api_client(te_url, matching_engine, tmp_path):
    from conftest import make_api_client

    with make_api_client(te_url, tmp_path, matching_service_url=matching_engine) as client:
        yield client


def test_parse_csv_happy_path():
    from ingest import parse_upload

    raw = (
        "source,source_id,attribute,value,authority_rank,verified\n"
        "test-src,doc-1-phone,phone,+84000,1,true\n"
    )
    fragments, rejected = parse_upload("upload.csv", raw)
    assert rejected == []
    assert len(fragments) == 1
    assert fragments[0]["attribute"] == "phone"
    assert fragments[0]["value"] == "+84000"
    assert fragments[0]["verified"] is True
    assert fragments[0]["authority_rank"] == 1


def test_parse_csv_rejects_headerless_or_bad_rows():
    from ingest import parse_upload

    with pytest.raises(ValueError, match="CSV"):
        parse_upload("data.csv", "a,b\n1,2\n")
    fragments, rejected = parse_upload("messy.csv", "attribute,value\nphone,\n,email@x\n")
    assert fragments == []
    assert len(rejected) == 2


def test_upload_csv_end_to_end(api_client):
    raw = (
        "source,source_id,attribute,value,authority_rank,event_time,verified,ttl_seconds\n"
        "crm-like,crm-e1,email,a.user@example.com,1,2026-10-27T09:00:00+07:00,true,3600\n"
    )
    response = api_client.post(
        "/upload", files={"file": ("upload.csv", raw, "text/csv")}
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["fragments_parsed"] == 1
    assert payload["fragments"][0]["attribute"] == "email"
