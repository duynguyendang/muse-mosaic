"""Kill-service / fail-closed test (CSD Flow C).

Stop the trust-engine, resolve again, and confirm no new golden record is
published while an escalation is recorded. Restart, retry with the same
idempotency key, and confirm the same decision id now publishes — no double
derivation, no lost request.
"""

from __future__ import annotations

from conftest import TrustEngine, make_api_client


PHONE_FRAGMENTS = [
    {
        "source": "sap-like",
        "source_id": "sap-001-phone",
        "attribute": "phone",
        "value": "+84 900 000 001",
        "authority_rank": 1,
        "event_time": "2026-10-26T09:00:00+07:00",
        "verified": True,
        "ttl_seconds": 172800,
    },
]


def body(fragments, key):
    return {
        "work_id": "work-kill",
        "entity_type": "user",
        "entity_id": "P-001",
        "idempotency_key": key,
        "fragments": fragments,
    }


def test_no_golden_published_while_trust_engine_is_stopped(
    trust_engine_binary, mkit_bin, tmp_path
):
    engine = TrustEngine(trust_engine_binary, mkit_bin).start()
    try:
        with make_api_client(engine.url, tmp_path) as client:
            ok = client.post("/resolve", json=body(PHONE_FRAGMENTS, "idem-ok"))
            assert ok.status_code == 200, ok.text
            assert api_version(client) == 1

            engine.stop()
            down = client.post(
                "/resolve", json=body(PHONE_FRAGMENTS, "idem-down")
            )
            assert down.status_code == 503
            detail = down.json()["detail"]
            assert detail["verdict"] == "escalated"
            assert detail["reason_code"] == "TRUST_ENGINE_UNAVAILABLE"
            assert detail["fail_closed"] is True

            # No new golden: the ledger still holds version 1 only.
            assert api_version(client) == 1
            # The escalation is recorded in the steward queue.
            pending = client.get("/steward/pending").json()["pending"]
            assert any(
                item["decision_id"] == detail["decision_id"] for item in pending
            )

            engine.start()
            retry = client.post(
                "/resolve", json=body(PHONE_FRAGMENTS, "idem-down")
            )
            assert retry.status_code == 200, retry.text
            retried = retry.json()
            assert retried["decision_id"] == detail["decision_id"]
            assert retried["verdict"] == "published"
            assert api_version(client) == 2
    finally:
        engine.stop()


def api_version(client) -> int:
    response = client.get("/golden/user/P-001")
    assert response.status_code == 200
    return response.json()["version"]
