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

_NAME_ATTRIBUTES = ("display_name", "name", "full_name")


def _display_name(facts: list[dict[str, Any]]) -> str | None:
    for attr in _NAME_ATTRIBUTES:
        for fact in facts:
            if fact.get("attribute") == attr and fact.get("value"):
                return str(fact["value"])
    return None


@router.get("/datasets/summary")
def dataset_summary(request: Request) -> dict[str, Any]:
    """Dataset health: per-record clean/needs-review status from ledger rows.

    Read-only aggregation over candidate facts, goldens, tombstones, and the
    pending steward queue. Record counts, piece counts and system names are
    computed, never invented; names come from display facts on record.
    """
    from config import Settings

    current: Settings = request.app.state.settings
    database_path = current.database_path
    records: list[dict[str, Any]] = []
    total_pieces = 0
    sources: list[str] = []
    for entity_type, entity_id in ledger.fetch_entities(database_path):
        facts = ledger.fetch_candidate_facts(database_path, entity_type, entity_id)
        total_pieces += len(facts)
        for fact in facts:
            source = fact.get("source")
            if source and source not in sources:
                sources.append(source)
        golden = ledger.fetch_latest_golden(database_path, entity_type, entity_id)
        pending = ledger.fetch_pending_stewards(database_path, entity_type, entity_id)
        issues = [
            {
                "steward_id": item.get("steward_id"),
                "attribute": item.get("attribute"),
                "suggested_action": item.get("suggested_action"),
                "owner": item.get("owner"),
                "deadline": item.get("deadline"),
                "fallback": item.get("fallback"),
                "status": item.get("status"),
            }
            for item in pending
        ]
        records.append(
            {
                "entity_type": entity_type,
                "entity_id": entity_id,
                "display_name": _display_name(facts),
                "pieces": len(facts),
                "clean": golden is not None and not pending,
                "golden_id": (golden or {}).get("golden_id"),
                "decision_id": (golden or {}).get("decision_id"),
                "policy_version": (golden or {}).get("policy_version"),
                "taxonomy_version": (golden or {}).get("taxonomy_version"),
                "issues": issues,
            }
        )
    needs_review = [record for record in records if record["issues"]]
    clean = [record for record in records if record["clean"]]
    total = len(records)
    return {
        "synthetic": True,
        "total_records": total,
        "total_pieces": total_pieces,
        "systems": sorted(sources),
        "clean_records": len(clean),
        "clean_pct": round(100 * len(clean) / total) if total else 0,
        "needs_review": needs_review,
        "clean_list": [
            {
                "entity_type": record["entity_type"],
                "entity_id": record["entity_id"],
                "display_name": record["display_name"],
                "golden_id": record["golden_id"],
                "decision_id": record["decision_id"],
            }
            for record in clean
        ],
    }


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
