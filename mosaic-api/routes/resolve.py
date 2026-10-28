"""POST /resolve — single-entity flat path (no Splink, no structure gate)."""

from __future__ import annotations

from typing import Any

from config import Settings
from fastapi import APIRouter, HTTPException, Request
from policies import PolicyError
from publisher import (
    EngineUnavailableError,
    IdempotencyConflictError,
    resolve_flow,
)
from schemas import ResolveRequest

router = APIRouter()


@router.post("/resolve")
async def resolve(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid JSON body") from exc
    try:
        resolve_request = ResolveRequest.model_validate(body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    current: Settings = request.app.state.settings
    try:
        # /resolve is the single-entity flat path: no expected structure,
        # so no whole-document validation (that gate lives on /build).
        response, proposal = await resolve_flow(
            resolve_request,
            current,
            request.app.state.policies,
            request.app.state.taxonomies,
        )
    except PolicyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except EngineUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "verdict": "escalated",
                "decision_id": exc.decision_id,
                "reason_code": exc.reason_code,
                "message": str(exc),
                "steward_queue": exc.steward_items,
                "fail_closed": True,
            },
        ) from exc
    payload = response.model_dump(mode="json")
    payload["proposal"] = proposal.model_dump(mode="json")
    return payload
