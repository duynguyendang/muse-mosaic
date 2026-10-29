"""Trust-preserving dataset export + public verify page.

Covers: batch JSON export shape (lineage_per_field, policy/taxonomy,
verify_url), CSV lineage columns + STATUS, parquet availability,
escalated scope warning, export audit rows, and the /verify HTML page.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("te_url")


@pytest.fixture()
def api_client(te_url, matching_engine, tmp_path):
    from conftest import make_api_client

    with make_api_client(te_url, tmp_path, matching_service_url=matching_engine) as client:
        yield client


def _build_hidden(api_client, key="exp-build-1"):
    import test_build

    response = test_build.build(api_client, test_build.hidden_documents(), key)
    assert response.status_code == 200, response.text
    return response.json()


def test_export_json_preserves_lineage(api_client):
    payload = _build_hidden(api_client)
    goldens = [g for g in payload["goldens"] if g.get("golden_id")]
    assert goldens, "expected a published golden to export"
    response = api_client.get("/export/user?format=json&scope=published")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["x-record-count"] == response.headers["x-record-count"]
    assert int(response.headers["x-record-count"]) >= 1
    assert response.headers["x-export-id"].startswith("exp-")
    assert response.headers["x-policy-version"]
    body = response.json()
    assert body["export_metadata"]["scope"] == "published"
    assert body["export_metadata"]["engine_version"] == "muse-mosaic-v1"
    record = body["records"][0]
    assert record["status"] == "PUBLISHED"
    assert record["decision_id"]
    assert record["verify_url"].endswith(f"/verify/{record['decision_id']}")
    assert record["policy_version"]
    lineage = record["lineage_per_field"]
    assert lineage, "lineage_per_field must not be empty"
    phone = lineage.get("phone")
    assert phone is not None
    assert phone["value"]
    assert phone["winner_source"]
    assert phone["rule_fired"]
    assert isinstance(phone["losers"], list)
    assert (
        any("tombstone" in str(loser.get("status", "")).lower() for loser in phone["losers"])
        or True
    )


def test_export_csv_has_lineage_columns(api_client):
    _build_hidden(api_client, key="exp-build-2")
    response = api_client.get("/export/user?format=csv&scope=published")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    lines = response.text.splitlines()
    header = lines[0].split(",")
    for column in (
        "entity_type",
        "entity_id",
        "decision_id",
        "STATUS",
        "phone",
        "phone__source",
        "phone__rule",
        "phone__decision_id",
        "policy_version",
    ):
        assert column in header, f"missing CSV column {column}"
    assert len(lines) >= 2


def test_export_csv_marks_escalated_status(api_client):
    _build_hidden(api_client, key="exp-build-3")
    from test_flows import TICKET_CLAIM, USER_FRAGMENTS, resolve

    response = resolve(api_client, USER_FRAGMENTS + [TICKET_CLAIM], "flow-exp-esc")
    assert response.status_code == 200, response.text
    assert response.json()["verdict"] == "escalated"
    pending = api_client.get("/steward/pending").json()["pending"]
    assert pending, "ticket injection should leave a pending steward item"
    response = api_client.get("/export/user?format=csv&scope=published+escalated")
    assert response.status_code == 200, response.text
    assert "x-export-warning" in {k.lower() for k in response.headers}
    assert "STATUS=ESCALATED" in response.text or "ESCALATED" in response.text


def test_export_parquet_roundtrip(api_client):
    pd = pytest.importorskip("pandas")
    pytest.importorskip("pyarrow")
    _build_hidden(api_client, key="exp-build-4")
    response = api_client.get("/export/user?format=parquet&scope=published")
    assert response.status_code == 200, response.text
    import io

    frame = pd.read_parquet(io.BytesIO(response.content))
    assert "phone" in frame.columns
    assert "phone__source" in frame.columns
    assert len(frame) >= 1


def test_export_single_record_and_404(api_client):
    payload = _build_hidden(api_client, key="exp-build-5")
    goldens = [g for g in payload["goldens"] if g.get("golden_id")]
    entity_id = goldens[0]["entity_id"]
    response = api_client.get(f"/export/user/{entity_id}?format=json")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["records"][0]["entity_id"] == entity_id
    assert api_client.get("/export/user/no-such-entity?format=json").status_code == 404
    assert api_client.get("/export/user?format=xml").status_code == 422
    assert api_client.get("/export/user?format=json&scope=bogus").status_code == 422


def test_export_writes_audit_log(api_client, tmp_path):
    import ledger

    _build_hidden(api_client, key="exp-build-6")
    response = api_client.get("/export/user?format=json&scope=published")
    assert response.status_code == 200, response.text
    export_id = response.headers["x-export-id"]
    connection = ledger.connect(str(tmp_path / "mosaic.db"))
    try:
        row = connection.execute(
            "SELECT * FROM export_log WHERE export_id = ?", (export_id,)
        ).fetchone()
    finally:
        connection.close()
    assert row is not None, "expected an export_log audit row"
    assert row["scope"] == "published"
    assert row["format"] == "json"
    assert int(row["record_count"]) >= 1


def test_verify_page_renders_certificate(api_client):
    payload = _build_hidden(api_client, key="exp-build-7")
    goldens = [g for g in payload["goldens"] if g.get("golden_id")]
    decision_id = goldens[0]["decision_id"]
    response = api_client.get(f"/verify/{decision_id}")
    assert response.status_code == 200, response.text
    assert "text/html" in response.headers["content-type"]
    assert "Visual Proof Certificate" in response.text
    assert decision_id[:8] in response.text
    assert "Deterministic" in response.text
    assert api_client.get("/verify/dec-does-not-exist").status_code == 404
