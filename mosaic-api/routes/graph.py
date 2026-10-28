"""GET /graph — persisted Data Graph + pathfinder + blast radius (canvas reads this)."""

from __future__ import annotations

from typing import Any

from config import Settings
from fastapi import APIRouter, Request
from graph import fetch_graph, pathfinder
from graph import impact as blast_radius
from schemas import GraphResponse

from routes.deps import _check_entity

router = APIRouter()


@router.get("/graph/{entity_type}/{entity_id}")
def graph(
    request: Request,
    entity_type: str,
    entity_id: str,
    path_from: str | None = None,
    path_to: str | None = None,
    impact: str | None = None,
) -> dict[str, Any]:
    """Render the persisted Data Graph (canvas reads this).

    Optional pathfinder (?path_from=&path_to=) and blast radius
    (?impact=<field>) run on the same graph.
    """
    _check_entity(entity_type)
    current: Settings = request.app.state.settings
    nodes, edges = fetch_graph(current.database_path, entity_type, entity_id)
    path: list[str] = []
    impact_result: dict[str, Any] = {}
    if path_from and path_to:
        path = pathfinder(nodes, edges, path_from, path_to)
    if impact:
        impact_result = blast_radius(nodes, edges, impact)
    return GraphResponse(
        entity_type=entity_type,
        entity_id=entity_id,
        nodes=nodes,  # type: ignore[arg-type]
        edges=edges,  # type: ignore[arg-type]
        path=path,
        impact=impact_result,
    ).model_dump(mode="json")
