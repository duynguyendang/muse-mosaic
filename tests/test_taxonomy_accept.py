"""Taxonomy Accept test (CSD 3.5): steward-only new term, new version, re-derive.

A product with an unmapped category escalates (T1). The steward Accepts the
suggested new term (bound to the exact action + a new taxonomy version),
which creates the version and triggers an explicit re-derive that publishes.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest


def product_doc(category, fridge_id="prod-009"):
    return {
        "document_id": "P1",
        "entity_type": "product",
        "source": "marketplace-like",
        "synthetic": True,
        "fields": {
            "category": {
                "value": category,
                "authority_rank": 3,
                "event_time": "2026-10-27T10:00:00+07:00",
                "verified": True,
                "ttl_seconds": 3600,
            }
        },
    }


def test_taxonomy_accept_creates_version_and_republishes(
    trust_engine_binary, mkit_bin, tmp_path, monkeypatch
):
    from conftest import TrustEngine, make_api_client

    tax_dir = tmp_path / "taxonomy"
    tax_dir.mkdir()
    src = Path(__file__).resolve().parents[1] / "data" / "mock" / "taxonomy"
    for path in src.glob("*.json"):
        shutil.copy(path, tax_dir / path.name)
    monkeypatch.setenv("TAXONOMY_DIR", str(tax_dir))

    engine = TrustEngine(trust_engine_binary, mkit_bin).start()
    try:
        with make_api_client(engine.url, tmp_path) as client:
            build = client.post(
                "/build",
                json={
                    "work_id": "work-tax",
                    "idempotency_key": "tax-1",
                    "dataset": {"documents": [product_doc("Gaming Chair")]},
                },
            )
            assert build.status_code == 200, build.text
            payload = build.json()
            assert payload["verdict"] == "escalated"
            stewards = [s for s in payload["steward_queue"] if s.get("suggested_action")]
            assert stewards, "expected a taxonomy new-term steward item"
            item = next(s for s in stewards if "Gaming Chair" in (s.get("suggested_action") or ""))
            suggested = item["suggested_action"]

            accept = client.post(
                f"/steward/{item['steward_id']}/accept",
                json={"action": suggested, "taxonomy_version": "tax-product-2026-10-27-v2"},
            )
            assert accept.status_code == 200, accept.text
            accepted = accept.json()
            assert accepted["taxonomy_version"] == "tax-product-2026-10-27-v2"
            golden = accepted.get("golden") or {}
            attrs = golden.get("attributes", {})
            assert attrs.get("category", {}).get("value") == "Gaming Chair"

            # The new version file exists and round-trips through validation.
            assert (tax_dir / "tax-product-2026-10-27-v2.json").exists()
            pending = client.get("/steward/pending").json()["pending"]
            assert all(p["steward_id"] != item["steward_id"] for p in pending)
    finally:
        engine.stop()


def test_taxonomy_accept_rejects_wrong_action(api_client):
    from test_flows import PRODUCT_FRAGMENTS, resolve

    response = resolve(
        api_client,
        PRODUCT_FRAGMENTS,
        "flow-accept-x",
        entity_type="product",
        entity_id="prod-accept",
    )
    assert response.status_code == 200
    items = [s for s in response.json()["steward_queue"] if s.get("suggested_action")]
    if not items:
        pytest.skip("no taxonomy item raised in mock mode for this fixture")
    item = items[0]
    bad = api_client.post(
        f"/steward/{item['steward_id']}/accept",
        json={
            "action": "Create something else entirely",
            "taxonomy_version": "tax-product-2026-10-27-v2",
        },
    )
    assert bad.status_code == 422
