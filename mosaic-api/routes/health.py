"""GET /health — aggregate service health (mosaic-api + engines + versions)."""

from __future__ import annotations

from config import Settings
from fastapi import APIRouter, Request
from schemas import HealthResponse

from routes.deps import _matching_engine_health, _trust_engine_health

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    current: Settings = request.app.state.settings
    killed = bool(getattr(request.app.state, "engine_killed", False))
    trust_engine = None if killed else await _trust_engine_health(current.health_url())
    splink_status, splink_versions = await _matching_engine_health(current)
    policies = request.app.state.policies
    user_version = getattr(policies.get("user"), "policy_version", None)
    product_version = getattr(policies.get("product"), "policy_version", None)
    if trust_engine is not None:
        user_version = trust_engine.get("policy_version_user") or user_version
        product_version = trust_engine.get("policy_version_product") or product_version
    return HealthResponse(
        mosaic_api="ok",
        trust_engine="ok" if trust_engine is not None else "down",
        splink=splink_status,
        splink_version=splink_versions.get("splink_version"),
        duckdb_version=splink_versions.get("duckdb_version"),
        policy_version_user=user_version,
        policy_version_product=product_version,
        proposer_mode="mock" if current.use_mock_muse else "live",
        proposer_model=None if current.use_mock_muse else (current.muse_model_id or None),
    )
