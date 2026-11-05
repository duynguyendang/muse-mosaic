"""Autonomy controller endpoints (M3).

GET  /api/v1/autonomy/status   — current freeze state, per-entity modes, breaker config
POST /api/v1/autonomy/freeze   — freeze every auto-apply without stopping reads
POST /api/v1/autonomy/unfreeze — release the freeze

Process-local, defaults to off; a restart always recovers. Non-GET calls are
gated by the same token middleware as every other mutating route when
MOSAIC_API_TOKEN is set.
"""

from __future__ import annotations

from typing import Any

from config import Settings
from fastapi import APIRouter, Request

router = APIRouter()


def _status(settings: Settings, app_state: Any) -> dict[str, Any]:
    return {
        "frozen": bool(getattr(app_state, "autonomy_frozen", False)),
        "env_kill_switch": bool(getattr(settings, "autonomy_kill_switch", False)),
        "modes": dict(getattr(settings, "autonomy_modes", {}) or {}),
        "default_mode": getattr(settings, "autonomy_mode", "off"),
        "breakers": [
            "zero_candidates_at_scale (>=200 clusters, 0 candidates)",
            "judge_failure_rate (>10% of judge calls)",
            "entity_run_failed (outage / output rejected)",
        ],
        "audit_rate": getattr(settings, "autonomy_audit_rate", 0.01),
        "note": "freeze demotes auto -> shadow; a restart recovers",
    }


@router.get("/api/v1/autonomy/status")
def autonomy_status(request: Request) -> dict[str, Any]:
    current: Settings = request.app.state.settings
    return _status(current, request.app.state)


@router.post("/api/v1/autonomy/freeze")
def autonomy_freeze(request: Request) -> dict[str, Any]:
    request.app.state.autonomy_frozen = True
    return {**_status(request.app.state.settings, request.app.state), "frozen": True}


@router.post("/api/v1/autonomy/unfreeze")
def autonomy_unfreeze(request: Request) -> dict[str, Any]:
    request.app.state.autonomy_frozen = False
    return {**_status(request.app.state.settings, request.app.state), "frozen": False}
