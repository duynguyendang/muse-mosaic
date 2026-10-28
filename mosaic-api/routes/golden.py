"""GET /golden — latest golden record per entity (or 404)."""

from __future__ import annotations

import ledger
from config import Settings
from fastapi import APIRouter, HTTPException, Request
from schemas import GoldenRecord

from routes.deps import _check_entity

router = APIRouter()


@router.get("/golden/{entity_type}/{entity_id}", response_model=GoldenRecord)
def get_golden(entity_type: str, entity_id: str, request: Request) -> GoldenRecord:
    _check_entity(entity_type)
    current: Settings = request.app.state.settings
    record = ledger.fetch_latest_golden(current.database_path, entity_type, entity_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"no golden record for {entity_type}/{entity_id}",
        )
    try:
        return GoldenRecord.model_validate(record)
    except ValueError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"ledger record failed the golden contract: {exc}",
        ) from exc
