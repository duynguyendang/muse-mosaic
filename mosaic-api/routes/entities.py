"""GET /entities + GET /fragments — definable synthetic entities.

Entities are defined by data/mock fixtures (see ingest.list_mock_entities);
defining a new entity is adding an entry to users.json/products.json.
"""

from __future__ import annotations

from typing import Any

from config import Settings
from fastapi import APIRouter, HTTPException, Request
from ingest import list_mock_entities, load_mock_fragments, load_mock_tickets
from schemas import EntitiesResponse, FragmentsResponse

from routes.deps import _check_entity

router = APIRouter()


@router.get("/entities", response_model=EntitiesResponse)
def get_entities(request: Request) -> dict[str, Any]:
    current: Settings = request.app.state.settings
    return {
        "synthetic": True,
        "notice": (
            "SYNTHETIC DEMO DATA. Entities are defined by data/mock "
            "fixtures: add an entry to users.json/products.json to "
            "define a new one."
        ),
        "entities": list_mock_entities(current.mock_data_dir),
    }


@router.get("/fragments/{entity_type}/{entity_id}", response_model=FragmentsResponse)
def get_fragments(entity_type: str, entity_id: str, request: Request) -> FragmentsResponse:
    _check_entity(entity_type)
    current: Settings = request.app.state.settings
    try:
        fragments = load_mock_fragments(current.mock_data_dir, entity_type, entity_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    tickets = load_mock_tickets(current.mock_data_dir, entity_type, entity_id)
    return FragmentsResponse(
        synthetic=True,
        notice=(
            "SYNTHETIC DEMO DATA. No real people, systems or integrations. "
            "All sources are labelled '-like'."
        ),
        entity_type=entity_type,
        entity_id=entity_id,
        fragments=fragments,
        tickets=tickets,
    )
