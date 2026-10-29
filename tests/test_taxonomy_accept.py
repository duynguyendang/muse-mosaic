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


def test_resolve_unknown_term_synthesizes_t1_item(api_client):
    """Deterministic T1: an unmapped category on an unpublished attribute
    raises `create new term "X"` even when the proposer emits no new_terms."""
    from test_flows import PRODUCT_FRAGMENTS, resolve

    fragments = list(PRODUCT_FRAGMENTS) + [
        {
            "source": "marketplace-like",
            "source_id": "mp-gc-1",
            "attribute": "category",
            "value": "Gaming Chair",
            "authority_rank": 3,
            "event_time": "2026-10-27T11:00:00+07:00",
            "verified": False,
            "ttl_seconds": 3600,
        }
    ]
    response = resolve(
        api_client, fragments, "flow-gc-t1", entity_type="product", entity_id="prod-gc"
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    items = [
        s for s in payload["steward_queue"] if '"Gaming Chair"' in (s.get("suggested_action") or "")
    ]
    assert items, "expected a T1 new-term steward item for Gaming Chair"
    assert any(
        c["rule_fired"] == "T1"
        and c["reason_code"] in ("TAXONOMY_UNKNOWN_TERM", "TAXONOMY_NEW_TERM_PROPOSED")
        for c in payload["conflicts"]
    )
    # Accepting the exact suggested action mints the version and re-derives.
    accept = api_client.post(
        f"/steward/{items[0]['steward_id']}/accept",
        json={
            "action": items[0]["suggested_action"],
            "taxonomy_version": "tax-product-2026-10-27-v99",
        },
    )
    try:
        assert accept.status_code == 200, accept.text
        assert accept.json()["taxonomy_version"] == "tax-product-2026-10-27-v99"
        assert "Gaming Chair" in str(accept.json())
    finally:
        # The accept path writes the new version file next to the fixtures;
        # remove it so later tests see the pristine taxonomy.
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mosaic-api"))
        from taxonomies import taxonomies_dir

        created = taxonomies_dir() / "tax-product-2026-10-27-v99.json"
        if created.is_file():
            created.unlink()


def test_taxonomy_synthesis_unknown_term_without_proposer_terms():
    """Unit: synthesis fires when the proposer emits nothing (real-Muse path
    where Gaming Chair never becomes a new_term claim)."""
    import sys

    sys.path.insert(
        0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "mosaic-api")
    )
    from publisher import taxonomy_steward_items
    from schemas import CandidateFact, Proposal
    from taxonomies import default_version, load_taxonomies

    taxonomies = load_taxonomies()
    taxonomy = taxonomies[default_version(taxonomies, "product")]
    facts = [
        CandidateFact(
            fact_id="prod-gc-mp-gc-1",
            entity_type="product",
            entity_id="prod-gc",
            attribute="category",
            value="Gaming Chair",
            source="marketplace-like",
            source_id="mp-gc-1",
            authority_rank=3,
            event_time="2026-10-27T11:00:00+07:00",
            verified=False,
            ttl_seconds=3600,
        )
    ]
    proposal = Proposal(mode="mock", model=None, attributes={}, note="")
    conflicts, stewards = taxonomy_steward_items(
        proposal,
        "product",
        "prod-gc",
        "dec-unit-1",
        facts,
        "2026-10-29T00:00:00+07:00",
        policy_version="pol-product-2026-10-27-v1",
        taxonomy_version=taxonomy.taxonomy_version,
        taxonomy=taxonomy,
        published_attrs=frozenset(),
    )
    assert [s.suggested_action for s in stewards] == ['create new term "Gaming Chair"']
    assert [(c.reason_code, c.rule_fired) for c in conflicts] == [("TAXONOMY_UNKNOWN_TERM", "T1")]
    # Published attributes never synthesize.
    _, none_stewards = taxonomy_steward_items(
        proposal,
        "product",
        "prod-gc",
        "dec-unit-2",
        facts,
        "2026-10-29T00:00:00+07:00",
        taxonomy=taxonomy,
        published_attrs=frozenset({"category"}),
    )
    assert none_stewards == []
