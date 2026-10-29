"""Read-only reference data: hidden dataset, taxonomy, policies, history.

Nothing here writes, derives, or publishes. These endpoints expose the same
versioned inputs the engine derives from, so the UI can show config, policy,
and history without inventing any of it.
"""

from __future__ import annotations

import re
from typing import Any

import ledger
from fastapi import APIRouter, HTTPException, Query, Request
from ingest import load_hidden_documents
from policies import PolicyError, policies_dir
from taxonomies import TaxonomyError, default_version

from routes.deps import _check_entity

router = APIRouter()

_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@router.get("/datasets/hidden")
def hidden_dataset(request: Request) -> dict[str, Any]:
    """Hidden-connection fixture set (F1..F5 + F6 trap), one click for Act 2."""
    from config import Settings

    current: Settings = request.app.state.settings
    try:
        documents = load_hidden_documents(current.mock_data_dir)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=f"hidden dataset unavailable: {exc}") from exc
    return {
        "synthetic": True,
        "notice": "SYNTHETIC DEMO DATA. Hidden Connection scene: F1..F5 plus the F6 neighbour trap. Never merged on similarity alone.",
        "documents": [doc.model_dump(mode="json") for doc in documents],
    }


@router.get("/taxonomy/{entity_type}")
def taxonomy_view(request: Request, entity_type: str) -> dict[str, Any]:
    """Read-only taxonomy for an entity type (terms + aliases, never edited)."""
    _check_entity(entity_type)
    taxonomies = getattr(request.app.state, "taxonomies", None) or {}
    try:
        version = default_version(taxonomies, entity_type)
        taxonomy = taxonomies[version]
    except (TaxonomyError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=404, detail=f"no taxonomy for {entity_type!r}") from exc
    return {
        "taxonomy_version": taxonomy.taxonomy_version,
        "entity_type": taxonomy.entity_type,
        "attribute_aliases": dict(taxonomy.attribute_aliases),
        "terms": [{"term": term.term, "synonyms": list(term.synonyms)} for term in taxonomy.terms],
    }


@router.get("/policies/{entity_type}/{version}")
def policy_view(request: Request, entity_type: str, version: str) -> dict[str, Any]:
    """Read-only policy JSON plus its runnable .mg source (for the drawer)."""
    from schemas import Policy

    _check_entity(entity_type)
    if not _VERSION_RE.fullmatch(version):
        raise HTTPException(status_code=422, detail=f"invalid policy version {version!r}")
    try:
        entity_dir = policies_dir() / entity_type
        policy_path = entity_dir / f"{version}.json"
        if not entity_dir.is_dir() or not policy_path.is_file():
            raise HTTPException(status_code=404, detail=f"no policy {entity_type}/{version}")
        policy = Policy.model_validate_json(policy_path.read_text(encoding="utf-8"))
        mg_path = entity_dir / f"{version}.mg"
        rules_datalog = mg_path.read_text(encoding="utf-8") if mg_path.is_file() else None
    except PolicyError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {
        "policy_version": policy.policy_version,
        "entity_type": policy.entity_type,
        "policy": policy.model_dump(mode="json"),
        "rules_datalog": rules_datalog,
    }


@router.get("/golden/{entity_type}/{entity_id}/history")
def golden_history(
    request: Request,
    entity_type: str,
    entity_id: str,
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    """Golden versions for one entity, newest first, each linking to Explain."""
    from config import Settings

    _check_entity(entity_type)
    current: Settings = request.app.state.settings
    entries = ledger.fetch_golden_history(current.database_path, entity_type, entity_id)[:limit]
    if not entries:
        raise HTTPException(
            status_code=404, detail=f"no golden history for {entity_type}/{entity_id}"
        )
    return {"entity_type": entity_type, "entity_id": entity_id, "versions": entries}
