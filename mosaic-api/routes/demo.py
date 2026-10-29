"""Demo-only engine kill switch (Act 5 / Fail-Closed Test).

POST /demo/engine/stop sets an in-memory flag that makes mosaic-api treat
the trust-engine as down: /health reports down and every derive attempt
fails closed through the exact same path as a real outage (escalate, no
golden, no decision row). POST /demo/engine/start clears it.

The flag is process-local and defaults to off; a restart always recovers.
It never touches the trust-engine process itself — the point being proved
is that mosaic-api fails closed when derivation is unavailable, however
that happens. Always presented in the UI with a DEMO ONLY badge.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

router = APIRouter()


def _set_killed(app, killed: bool) -> None:
    app.state.engine_killed = killed


@router.post("/demo/engine/stop")
def engine_stop(request: Request) -> dict[str, Any]:
    _set_killed(request.app, True)
    return {
        "engine": "down",
        "demo_only": True,
        "note": "Trust-engine treated as stopped (demo simulation). "
        "New derives escalate, no golden is published.",
    }


@router.post("/demo/engine/start")
def engine_start(request: Request) -> dict[str, Any]:
    _set_killed(request.app, False)
    return {
        "engine": "up",
        "demo_only": True,
        "note": "Trust-engine treated as reachable again. Retry with the same "
        "idempotency key to prove the same decision id returns.",
    }
