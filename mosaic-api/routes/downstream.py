"""GET /downstream — two-answer mock: a golden, or No trusted value.

Agents: otp, refund, search. No fragment fallback, no score fallback —
a downstream that cannot accept a stop answer must not consume goldens.
"""

from __future__ import annotations

from typing import Any

import ledger
from config import Settings
from fastapi import APIRouter, HTTPException, Query, Request
from schemas import DownstreamAnswer

from routes.deps import _check_entity

router = APIRouter()


@router.get("/downstream/{agent}")
def downstream(
    agent: str,
    request: Request,
    entity_type: str = Query(min_length=1),
    entity_id: str = Query(min_length=1),
    field: str | None = Query(default=None),
) -> dict[str, Any]:
    """Downstream two-answer mock: a golden, or No trusted value.

    Agents: otp, refund, search. No fragment fallback, no score fallback —
    a downstream that cannot accept a stop answer must not consume goldens.
    """
    if agent not in ("otp", "refund", "search"):
        raise HTTPException(status_code=404, detail=f"unknown downstream agent {agent!r}")
    _check_entity(entity_type)
    current: Settings = request.app.state.settings
    # no field named: the entity's first golden attribute from its policy
    # (product -> category, user -> phone), never a hardcoded per-type literal
    policy = (getattr(request.app.state, "policies", None) or {}).get(entity_type)
    default_field = "phone"
    if policy is not None:
        default_field = next(iter(policy.golden_attributes or policy.allowed_attributes), "phone")
    wanted = field or default_field
    golden = ledger.fetch_latest_golden(current.database_path, entity_type, entity_id)
    if golden is None:
        return DownstreamAnswer(
            answer="no_trusted_value",
            agent=agent,
            entity_type=entity_type,
            entity_id=entity_id,
            field=wanted,
            reason="MISSING_GOLDEN",
            fallback=f"do not act on {wanted}: no golden published",
        ).model_dump(mode="json")
    value = golden["attributes"].get(wanted)
    if value is None:
        return DownstreamAnswer(
            answer="no_trusted_value",
            agent=agent,
            entity_type=entity_type,
            entity_id=entity_id,
            field=wanted,
            reason="FIELD_WITHHELD",
            fallback=f"do not act on {wanted}: escalated or denied, no trusted value",
        ).model_dump(mode="json")
    return DownstreamAnswer(
        answer="golden",
        agent=agent,
        entity_type=entity_type,
        entity_id=entity_id,
        field=wanted,
        value=value.get("value") if isinstance(value, dict) else str(value),
        decision_id=golden.get("decision_id"),
        policy_version=golden.get("policy_version"),
        taxonomy_version=golden.get("taxonomy_version"),
        lineage=value.get("lineage") if isinstance(value, dict) else None,
    ).model_dump(mode="json")
