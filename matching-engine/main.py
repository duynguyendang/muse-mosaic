"""Splink matching engine (CSD 3.3, 12.7b) — standalone service.

Proposer only, never a decider. DuckDB backend in v1: flatten fragments, block
on exact identifiers (blocking comes from the policy file, passed per
request), propose candidate clusters via deterministic exact-identifier
linkage, and attach EM match weights/probabilities best-effort as candidate
signals only.

A high score is a candidate signal; a low score is not a rejection either:
pairs below the policy threshold are not reported as scored candidates, but
exact-identifier linkage still proposes the cluster. Stateless: no ledger, no
graph writes, no golden writes. mosaic-api merges the returned candidates with
Muse claims; the trust-engine still verifies every hop (L1).
"""

from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from splink_engine import (
    BLOCKING_COLUMNS,
    STATUS_UNAVAILABLE,
    SUPPORTED_COMPARISONS,
    run_match,
)

SPLINK_ENABLED = os.environ.get("SPLINK_ENABLED", "true").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)

try:
    from importlib import metadata as _metadata

    SPLINK_VERSION = _metadata.version("splink")
except Exception:
    SPLINK_VERSION = "unknown"

try:
    import duckdb as _duckdb

    DUCKDB_VERSION = _duckdb.__version__
except Exception:
    DUCKDB_VERSION = "unknown"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MatchFragment(StrictModel):
    """One flattened fragment row. Missing fields become None (never "")."""

    fragment_id: str = Field(min_length=1)
    entity_type: str = "user"
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    order_id: str | None = None
    category: str | None = None


class MatchRequest(StrictModel):
    """Match contract. Blocking/threshold come from the policy file."""

    fragments: list[MatchFragment] = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    known_policy_versions: list[str] = Field(default_factory=list)
    blocking: list[str] = Field(default_factory=lambda: ["email", "phone", "order_id"])
    comparisons: list[str] = Field(default_factory=lambda: ["exact", "jaro_winkler"])
    threshold: float = Field(default=0.9, ge=0.0, le=1.0)
    work_id: str = ""


class MatchPair(StrictModel):
    left: str
    right: str
    match_weight: float | None = None
    match_probability: float | None = None
    meets_threshold: bool = True
    blocking_rule: str = ""
    proposer: str = "Splink"


class MatchCluster(StrictModel):
    cluster_id: str
    member_ids: list[str]


class MatchResponse(StrictModel):
    status: str
    proposer: str = "Splink"
    work_id: str = ""
    policy_version: str = "unknown"
    splink_version: str = SPLINK_VERSION
    duckdb_version: str = DUCKDB_VERSION
    clusters: list[MatchCluster] = Field(default_factory=list)
    pairs: list[MatchPair] = Field(default_factory=list)
    detail: str = ""
    comparisons_applied: list[str] = Field(default_factory=list)


class HealthResponse(StrictModel):
    status: str
    service: str = "matching-engine"
    splink_service: str = "ok"
    splink_version: str = SPLINK_VERSION
    duckdb_version: str = DUCKDB_VERSION
    splink_enabled: bool = True


app = FastAPI(
    title="Matching Engine",
    version="0.1.0",
    description=(
        "Splink matching engine (proposer only). Stateless: no ledger, "
        "no graph writes, no golden writes. Internal only."
    ),
)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(
        status="ok" if SPLINK_ENABLED else "unavailable",
        splink_service="ok" if SPLINK_ENABLED else "unavailable",
        splink_enabled=SPLINK_ENABLED,
    )


@app.post("/match", response_model=MatchResponse)
def match(request: MatchRequest) -> MatchResponse:
    if not SPLINK_ENABLED:
        raise HTTPException(
            status_code=503,
            detail="SPLINK_UNAVAILABLE — matching-engine disabled",
        )
    for column in request.blocking:
        if column not in BLOCKING_COLUMNS:
            raise HTTPException(
                status_code=422,
                detail=f"unknown blocking column {column!r}; must be one of {list(BLOCKING_COLUMNS)}",
            )
    for comparison in request.comparisons:
        if comparison not in SUPPORTED_COMPARISONS:
            raise HTTPException(
                status_code=422,
                detail=f"unsupported comparison {comparison!r}; must be one of {list(SUPPORTED_COMPARISONS)}",
            )
    if request.known_policy_versions and (
        request.policy_version not in request.known_policy_versions
    ):
        raise HTTPException(
            status_code=422,
            detail=f"unknown policy_version {request.policy_version!r}; fail closed",
        )
    if SPLINK_VERSION == "unknown" or DUCKDB_VERSION == "unknown":
        return MatchResponse(
            status=STATUS_UNAVAILABLE,
            clusters=[],
            pairs=[],
            detail="SPLINK_UNAVAILABLE — Splink/DuckDB not installed; fallback to Muse + fuzzy",
            comparisons_applied=[],
        )
    rows = [fragment.model_dump() for fragment in request.fragments]
    out = run_match(rows, threshold=request.threshold, blocking=request.blocking)
    return MatchResponse(
        status=out["status"],
        work_id=request.work_id,
        policy_version=request.policy_version,
        clusters=out["clusters"],
        pairs=out["pairs"],
        detail=out["detail"],
        comparisons_applied=list(request.comparisons),
    )
