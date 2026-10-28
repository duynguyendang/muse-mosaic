"""Splink adapter tests (S-01..S-04, CSD 12.7b).

S-01: Splink finds the hidden chain cluster F1-F5 as candidates, F6 separate.
S-02: a Splink cluster that wrongly includes F6 is split by the gate (C1).
S-03: a semantic link Splink misses is added by Muse + taxonomy and publishes.
S-04: Splink unavailable falls back, marks the run, no crash, no silent publish.
"""

from __future__ import annotations

import pytest

splink = pytest.importorskip("splink", reason="splink not installed")


def hidden_documents():
    import json
    from pathlib import Path

    base = Path(__file__).resolve().parents[1] / "data" / "mock" / "hidden"
    documents = []
    for path in sorted(base.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        documents.append(
            {
                "document_id": raw["document_id"],
                "entity_type": raw.get("entity_type", "user"),
                "fields": {
                    name: {"value": field["value"]} for name, field in raw.get("fields", {}).items()
                },
            }
        )
    return documents


def test_S01_splink_finds_hidden_cluster(matching_engine):
    from splink_adapter import flatten_documents, run_splink

    rows = flatten_documents(hidden_documents())
    out = run_splink(
        rows,
        enabled=True,
        threshold=0.9,
        blocking=["email", "phone", "order_id"],
        service_url=matching_engine,
    )
    assert out["status"] == "ok", out
    by_member = {}
    for cluster in out["clusters"]:
        for member in cluster["member_ids"]:
            by_member[member] = cluster["cluster_id"]
    main = by_member["F1"]
    assert all(by_member[f"F{i}"] == main for i in (2, 3, 4, 5))
    assert by_member["F6"] != main
    # Pairs carry candidate weights/probabilities (signals only, never verdicts).
    assert out["pairs"], "expected candidate pairs with weights"
    for pair in out["pairs"]:
        assert "match_weight" in pair and "match_probability" in pair
        assert pair["proposer"] == "Splink"
        assert pair["blocking_rule"], "each pair must carry its blocking evidence"


def test_S02_gate_splits_overmerged_trap(te_url):
    """Simulate a Splink over-merge (F6 in the cluster): C1 fires, F6 never wins."""
    import httpx
    from test_hidden import T1, T3, base_facts, fact, linker_links

    facts = base_facts() + [
        fact("F6", "display_name", "Nguyen Van Binh", "sap-like", 1, T1, True),
        fact("F6", "email", "binh@example.com", "sap-like", 3, T1, True),
        fact("F6", "phone", "0901234568", "sap-like", 1, T3, True),
        fact("F6", "address", "123 Le Loi, HCM", "sap-like", 3, T1, True),
    ]
    links = linker_links(base_facts())
    response = httpx.post(
        te_url,
        json={
            "decision_id": "dec-s02",
            "entity_type": "user",
            "entity_id": "P-HIDDEN",
            "idempotency_key": "idem-s02",
            "payload_hash": "hash-s02",
            "policy_version": "pol-2026-10-27-v1",
            "taxonomy_version": "tax-user-2026-10-27-v1",
            "candidate_facts": facts,
            "links": links,
        },
        timeout=30,
    )
    assert response.status_code == 200, response.text
    out = response.json()
    assert out["verdict"] == "escalated"
    conflicts = {row["attribute"]: row for row in out["conflicts"]}
    assert conflicts["phone"]["reason_code"] == "CLUSTER_CONFLICT"
    golden = {row["attribute"]: row for row in out.get("golden_facts", [])}
    # display_name has no matrix ranks in v1: escalates, never publishes.
    assert "display_name" not in golden
    assert conflicts["display_name"]["reason_code"] == "NO_AUTHORITY_RANKS"
    assert golden["email"]["winner_fact_id"].endswith("F2-email")
    assert golden["address"]["winner_fact_id"].endswith("F3-address")
    assert golden["order_id"]["winner_fact_id"].endswith("F2-order_id")
    won = {row["value"] for row in out.get("golden_facts", [])}
    assert "0901234568" not in won and "Nguyen Van Binh" not in won
    assert not any(
        "F6" in hop
        for row in out["verified_links"]
        for hop in (row["from_fact_id"], row["to_fact_id"])
    )


def test_S03_semantic_link_splink_misses(matching_engine):
    """A pair sharing no blocking key (F1/F3) gets a Muse-proposed link, while
    Splink proposes nothing: the semantic recall is Muse's, never Splink's."""
    from datetime import datetime

    from schemas import CandidateFact
    from subagents import mock_linker

    def fact_obj(fid, attr, value):
        return CandidateFact(
            fact_id=fid,
            entity_type="user",
            entity_id="P-HIDDEN",
            attribute=attr,
            value=value,
            source="x",
            source_id=f"{fid}-s",
            authority_rank=3,
            event_time=datetime.fromisoformat("2026-10-27T09:00:00+07:00"),
            verified=False,
            ttl_seconds=3600,
        )

    facts = [
        fact_obj("P-HIDDEN-F1-display_name", "display_name", "Nguyen Van An"),
        fact_obj("P-HIDDEN-F3-display_name", "display_name", "An Nguyen"),
    ]
    links, _trace = mock_linker(
        facts,
        {"P-HIDDEN-F1-display_name": "F1", "P-HIDDEN-F3-display_name": "F3"},
    )
    assert any(link.proposer == "Muse" for link in links)
    # Splink on this pair alone finds nothing to block on together.
    from splink_adapter import flatten_documents, run_splink

    rows = flatten_documents(
        [
            {
                "document_id": "F1",
                "entity_type": "user",
                "fields": {"display_name": {"value": "Nguyen Van An"}},
            },
            {
                "document_id": "F3",
                "entity_type": "user",
                "fields": {"display_name": {"value": "An Nguyen"}},
            },
        ]
    )
    out = run_splink(
        rows,
        enabled=True,
        threshold=0.9,
        blocking=["email", "phone", "order_id"],
        service_url=matching_engine,
    )
    assert out["status"] == "ok"
    assert out["pairs"] == []


def test_S04_splink_unavailable_falls_back(matching_engine):
    """Splink disabled/failed: fallback marked, no crash, no silent publish."""
    from splink_adapter import flatten_documents, run_splink

    rows = flatten_documents(
        [
            {
                "document_id": "F1",
                "entity_type": "user",
                "fields": {"email": {"value": "an.van@example.com"}},
            }
        ]
    )
    out = run_splink(
        rows, enabled=False, threshold=0.9, blocking=["email"], service_url=matching_engine
    )
    assert out["status"] == "unavailable"
    assert "fallback" in out["detail"].lower()
    assert out["clusters"] == [] and out["pairs"] == []


def test_S04_matching_engine_down_falls_back():
    """The real service is unreachable: client marks SPLINK_UNAVAILABLE,
    returns no candidates, never raises — the UI/API fall back to Muse + fuzzy."""
    from splink_adapter import flatten_documents, run_splink

    rows = flatten_documents(
        [
            {
                "document_id": "F1",
                "entity_type": "user",
                "fields": {"email": {"value": "an.van@example.com"}},
            }
        ]
    )
    out = run_splink(
        rows,
        enabled=True,
        threshold=0.9,
        blocking=["email"],
        service_url="http://127.0.0.1:9",
        timeout_seconds=5.0,
    )
    assert out["status"] == "unavailable"
    assert "SPLINK_UNAVAILABLE" in out["detail"]
    assert "fallback" in out["detail"].lower()
    assert out["clusters"] == [] and out["pairs"] == []


def _stub_matching_engine(*, match_body: bytes, match_status: int = 200):
    """A stub matching-engine: garbage or doctored /match, healthy /health."""
    import json as _json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    health = _json.dumps(
        {
            "status": "ok",
            "service": "matching-engine",
            "splink_service": "ok",
            "splink_version": "5.0.0",
            "duckdb_version": "2.0.0",
            "splink_enabled": True,
        }
    ).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = health if self.path == "/health" else b"{}"
            code = 200 if self.path == "/health" else 404
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            self.send_response(match_status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(match_body)))
            self.end_headers()
            self.wfile.write(match_body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}"


def test_S05_malformed_match_response_fails_closed(te_url, tmp_path):
    """S-05: malformed service output -> SPLINK_UNAVAILABLE, no golden from
    the malformed output, GET /health reports splink unavailable."""

    from conftest import make_api_client

    server, stub_url = _stub_matching_engine(match_body=b"this is not json{{{")
    try:
        import json as _json2
        from pathlib import Path as _Path

        base = _Path(__file__).resolve().parents[1] / "data" / "mock" / "hidden"
        documents = [
            {
                k: v
                for k, v in _json2.loads(path.read_text(encoding="utf-8")).items()
                if k != "notice"
            }
            for path in sorted(base.glob("*.json"))
        ]
        with make_api_client(te_url, tmp_path, matching_service_url=stub_url) as client:
            build = client.post(
                "/build",
                json={
                    "work_id": "work-s05",
                    "idempotency_key": "build-s05",
                    "dataset": {"documents": documents},
                },
            )
            assert build.status_code == 200, build.text
            payload = build.json()
            splink = payload["splink"]
            assert splink["status"] == "unavailable", splink
            assert "SPLINK_UNAVAILABLE" in splink["detail"]
            assert splink["pairs"] == [] and splink["clusters"] == []
            # No golden publishes FROM the malformed output: no Splink pairs
            # means no verified links and no Splink-proposed golden lineage.
            goldens = [g for g in payload["goldens"] if g.get("golden_id")]
            for golden in goldens:
                for _name, field in golden["attributes"].items():
                    if isinstance(field, dict):
                        assert "splink" not in str(field.get("lineage", "")).lower()
            health = client.get("/health")
            assert health.status_code == 200, health.text
            assert health.json()["splink"] == "unavailable", health.json()
            # No decision is recorded as a Splink success anywhere.
            assert payload["splink_status"].startswith("unavailable")
    finally:
        server.shutdown()
        server.server_close()


def test_splink_version_mismatch_fails_closed():
    """A service reporting non-pinned versions is not silently used."""
    import json as _json

    from splink_adapter import flatten_documents, run_splink

    rows = flatten_documents(
        [
            {
                "document_id": "F1",
                "entity_type": "user",
                "fields": {"email": {"value": "an.van@example.com"}},
            }
        ]
    )
    body = _json.dumps(
        {
            "status": "ok",
            "proposer": "Splink",
            "splink_version": "9.9.9",
            "duckdb_version": "9.9.9",
            "clusters": [{"cluster_id": "splink-cluster-1", "member_ids": ["F1"]}],
            "pairs": [],
            "detail": "doctored",
        }
    ).encode()
    server, stub_url = _stub_matching_engine(match_body=body)
    try:
        out = run_splink(
            rows, enabled=True, threshold=0.9, blocking=["email"], service_url=stub_url
        )
    finally:
        server.shutdown()
        server.server_close()
    assert out["status"] == "unavailable"
    assert "version mismatch" in out["detail"]
    assert out["clusters"] == [] and out["pairs"] == []


def test_splink_liskov_fallback_matches_service(matching_engine):
    """Liskov: the in-process fallback is substitutable for the service —
    same clusters and pair endpoints on the hidden fixtures."""
    from splink_adapter import flatten_documents, run_splink

    rows = flatten_documents(hidden_documents())
    kwargs = dict(enabled=True, threshold=0.9, blocking=["email", "phone", "order_id"])
    live = run_splink(rows, service_url=matching_engine, **kwargs)
    local = run_splink(rows, service_url="", **kwargs)
    assert live["status"] == "ok", live
    assert local["status"] == "ok", local

    def members(out):
        return sorted(tuple(sorted(cluster["member_ids"])) for cluster in out["clusters"])

    def endpoints(out):
        return sorted((pair["left"], pair["right"]) for pair in out["pairs"])

    assert members(local) == members(live)
    assert endpoints(local) == endpoints(live)
