"""GET /similar — structured similar search (vector recall explicitly skipped)."""

from __future__ import annotations

from config import Settings
from fastapi import APIRouter, Query, Request
from schemas import SimilarResponse
from similar_search import similar as run_similar

from routes.deps import _check_entity

router = APIRouter()


@router.get("/similar", response_model=SimilarResponse)
def similar(
    request: Request,
    entity_type: str = Query(min_length=1),
    entity_id: str = Query(min_length=1),
    limit: int = Query(default=20, ge=1, le=100),
) -> SimilarResponse:
    _check_entity(entity_type)
    current: Settings = request.app.state.settings
    result = run_similar(
        current.database_path,
        entity_type=entity_type,
        entity_id=entity_id,
        limit=limit,
    )
    return SimilarResponse.model_validate(result)
