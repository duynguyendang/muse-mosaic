"""Review queue for suggested merges (M4 / G4 rung of the autonomy ladder).

shadow/suggest/canary builds persist the merges they would have applied but
held back. A human labels them here; labels are recorded, never applied to
goldens. Mutating calls go through the same bearer-token middleware as every
other state-changing route.

  GET  /api/v1/suggestions          list proposals (?status=, ?entity_type=, ?limit=)
  GET  /api/v1/suggestions/{id}     one proposal
  POST /api/v1/suggestions/{id}/accept   record an accepted label
  POST /api/v1/suggestions/{id}/reject   record a rejected label
"""

from __future__ import annotations

from typing import Any

import ledger
from config import Settings
from fastapi import APIRouter, HTTPException, Request

router = APIRouter()


def _settings(request: Request) -> Settings:
    return request.app.state.settings


@router.get("/api/v1/suggestions")
def list_suggestions(
    request: Request,
    status: str | None = None,
    entity_type: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    if status is not None and status not in ("pending", "accepted", "rejected"):
        raise HTTPException(status_code=422, detail="status must be pending, accepted or rejected")
    limit = max(1, min(200, int(limit or 50)))
    current = _settings(request)
    return {
        "suggestions": ledger.fetch_merge_proposals(
            current.database_path,
            status=status,
            entity_type=entity_type,
            limit=limit,
        ),
        "filters": {"status": status, "entity_type": entity_type, "limit": limit},
    }


@router.get("/api/v1/suggestions/{proposal_id}")
def get_suggestion(proposal_id: str, request: Request) -> dict[str, Any]:
    current = _settings(request)
    proposal = ledger.fetch_merge_proposal(current.database_path, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail=f"merge proposal {proposal_id} not found")
    return proposal


def _decide(
    proposal_id: str,
    request: Request,
    *,
    decision: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    by = str((payload or {}).get("by") or "").strip()
    note = str((payload or {}).get("note") or "").strip()
    if not by:
        raise HTTPException(
            status_code=422, detail="'by' must name the person labelling the proposal"
        )
    if note and len(note) > 500:
        raise HTTPException(status_code=422, detail="note must be at most 500 characters")
    current = _settings(request)
    try:
        proposal = ledger.fetch_merge_proposal(current.database_path, proposal_id)
        if proposal is None:
            raise HTTPException(status_code=404, detail=f"merge proposal {proposal_id} not found")
        updated = ledger.decide_merge_proposal(
            current.database_path,
            proposal_id,
            decision=decision,
            resolved_by=by,
            note=note or None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"proposal": updated}


@router.post("/api/v1/suggestions/{proposal_id}/accept")
async def accept_suggestion(proposal_id: str, request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except ValueError:
        payload = {}
    return _decide(proposal_id, request, decision="accepted", payload=payload)


@router.post("/api/v1/suggestions/{proposal_id}/reject")
async def reject_suggestion(proposal_id: str, request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except ValueError:
        payload = {}
    return _decide(proposal_id, request, decision="rejected", payload=payload)
