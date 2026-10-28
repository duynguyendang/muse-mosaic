"""Steward queue — the only human path (approve/reject/split/Accept).

Approve records a new verified rank-1 fact and re-derives; split restores a
prior golden version as a new version; Accept writes a new taxonomy version
file (single-use, bound to one suggested action) and re-derives under it.
"""

from __future__ import annotations

import json as jsonlib
from datetime import UTC, datetime
from typing import Any

import ledger
from config import Settings
from fastapi import APIRouter, HTTPException, Request
from ingest import normalize_value
from policies import PolicyError, active_version
from publisher import (
    EngineUnavailableError,
    IdempotencyConflictError,
    decision_id_for,
    resolve_flow,
    utcnow_iso,
)
from schemas import (
    Fragment,
    ResolveRequest,
    StewardAcceptRequest,
    StewardApproveRequest,
    StewardSplitRequest,
)
from taxonomies import default_version, load_taxonomies

from routes.deps import (
    _row_to_fact,
    _write_taxonomy_version,
    active_taxonomy,
    refresh_taxonomies,
)

router = APIRouter()


@router.get("/steward/pending")
def steward_pending(request: Request) -> dict[str, Any]:
    current: Settings = request.app.state.settings
    return {
        "pending": ledger.fetch_pending_stewards(current.database_path),
    }


@router.post("/steward/{steward_id}/approve")
async def steward_approve(steward_id: str, request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid JSON body") from exc
    try:
        approval = StewardApproveRequest.model_validate(body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    current: Settings = request.app.state.settings
    item = ledger.fetch_steward(current.database_path, steward_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"steward item {steward_id} not found")
    if item["status"] != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"steward item {steward_id} is {item['status']}, not pending",
        )
    return await _steward_derive(
        request,
        item,
        approval.value,
        "approved",
        source="steward-approved",
        source_id=f"steward-{steward_id}",
    )


@router.post("/steward/{steward_id}/reject")
def steward_reject(steward_id: str, request: Request) -> dict[str, Any]:
    current: Settings = request.app.state.settings
    item = ledger.fetch_steward(current.database_path, steward_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"steward item {steward_id} not found")
    if item["status"] != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"steward item {steward_id} is {item['status']}, not pending",
        )
    ledger.update_steward_status(current.database_path, steward_id, "rejected")
    return {"steward_id": steward_id, "status": "rejected"}


@router.post("/steward/{steward_id}/split")
async def steward_split(steward_id: str, request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="invalid JSON body")
    return _do_split(steward_id, request, body)


@router.post("/steward/{steward_id}/accept")
async def steward_accept(steward_id: str, request: Request) -> dict[str, Any]:
    """Accept a suggested taxonomy action (steward-only, single-use).

    Bound to one exact suggested action and one taxonomy version; creates
    the new taxonomy version and triggers an explicit re-derive. AI may
    propose but never accepts its own suggestion.
    """
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid JSON body") from exc
    try:
        acceptance = StewardAcceptRequest.model_validate(body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return await _steward_accept_taxonomy(steward_id, acceptance, request)


async def _steward_accept_taxonomy(steward_id: str, acceptance, request: Request) -> dict[str, Any]:
    """Accept a suggested taxonomy term (steward-only, single-use).

    Bound to one exact suggested action and one taxonomy version. Creates the
    new taxonomy version file, then runs an explicit re-derive under it.
    AI may propose but never accepts its own suggestion; a missing or
    mismatched binding fails closed.
    """
    current: Settings = request.app.state.settings
    item = ledger.fetch_steward(current.database_path, steward_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"steward item {steward_id} not found")
    if item["status"] != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"steward item {steward_id} is {item['status']}, not pending",
        )
    if not item.get("suggested_action") or item["suggested_action"] != acceptance.action:
        raise HTTPException(
            status_code=422,
            detail="acceptance is not bound to this item's exact suggested action",
        )

    taxonomies = getattr(request.app.state, "taxonomies", None) or load_taxonomies()
    base = taxonomies.get(default_version(taxonomies, item["entity_type"]))
    if base is None:
        raise HTTPException(status_code=422, detail="no taxonomy to extend")
    try:
        new_version = _write_taxonomy_version(
            taxonomies, base, acceptance.action, acceptance.taxonomy_version
        )
    except (ValueError, OSError) as exc:
        raise HTTPException(
            status_code=422, detail=f"cannot create taxonomy version: {exc}"
        ) from exc
    # Refresh the in-memory store so the re-derive sees the new version.
    refresh_taxonomies(request)

    # Explicit re-derive under the new taxonomy version (same facts, new terms).

    stored_facts = ledger.fetch_candidate_facts(
        current.database_path, item["entity_type"], item["entity_id"]
    )
    fragments = [Fragment.model_validate(_row_to_fact(row)) for row in stored_facts]

    entity_type, entity_id = item["entity_type"], item["entity_id"]
    re_request = ResolveRequest(
        work_id=f"steward-{steward_id}-accept",
        entity_type=entity_type,
        entity_id=entity_id,
        idempotency_key=f"{item['decision_id']}-accept-{steward_id}",
        fragments=fragments,
    )

    try:
        response, _proposal = await resolve_flow(
            re_request,
            current,
            request.app.state.policies,
            request.app.state.taxonomies,
            taxonomy_version=new_version,
            expected_structure=request.app.state.expected_structures.get(entity_type),
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"re-derive failed: {exc}") from exc
    ledger.update_steward_status(current.database_path, steward_id, "accepted")
    payload = response.model_dump(mode="json")
    payload["taxonomy_version"] = new_version
    payload["accepted_action"] = acceptance.action
    return payload


async def _steward_derive(
    request: Request,
    item: dict[str, Any],
    value: str,
    new_status: str,
    *,
    source: str,
    source_id: str,
) -> dict[str, Any]:
    """A steward decision is a new fact, triggering re-derivation."""

    current: Settings = request.app.state.settings
    entity_type, entity_id = item["entity_type"], item["entity_id"]
    stored_facts = ledger.fetch_candidate_facts(current.database_path, entity_type, entity_id)
    fragments = [Fragment.model_validate(_row_to_fact(row)) for row in stored_facts]
    fragments.append(
        Fragment(
            source=source,
            source_id=source_id,
            attribute=item["attribute"],
            value=normalize_value(item["attribute"], value),
            authority_rank=1,
            event_time=datetime.now(UTC).isoformat(),
            verified=True,
            ttl_seconds=86400,
        )
    )
    steward_request = ResolveRequest(
        work_id=f"steward-{item['steward_id']}",
        entity_type=entity_type,
        entity_id=entity_id,
        idempotency_key=f"{item['decision_id']}-{new_status}-{item['steward_id']}",
        fragments=fragments,
    )
    try:
        # Flat re-derive like /resolve (no expected structure): the approve
        # path republishes what the engine derives; whole-document validation
        # lives on /build, which supplies an explicit structure.
        response, proposal = await resolve_flow(
            steward_request,
            current,
            request.app.state.policies,
            getattr(request.app.state, "taxonomies", None),
        )
    except PolicyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except EngineUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "verdict": "escalated",
                "decision_id": exc.decision_id,
                "reason_code": exc.reason_code,
                "message": str(exc),
                "fail_closed": True,
            },
        ) from exc
    ledger.update_steward_status(current.database_path, item["steward_id"], new_status)
    payload = response.model_dump(mode="json")
    payload["proposal"] = proposal.model_dump(mode="json")
    payload["steward_item"] = item["steward_id"]
    return payload


def _fetch_split_source(
    current: Settings, steward_id: str, body: dict[str, Any]
) -> tuple[dict[str, Any], Any, dict[str, Any], str, dict[str, Any], dict[str, Any]]:
    """Validate the split request and load the steward item + golden versions.

    Returns (item, split_request, entity ids..., target golden, restored_attrs).
    Anything missing or in the wrong state fails closed (404/409).
    """
    try:
        split_request = StewardSplitRequest.model_validate(body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    item = ledger.fetch_steward(current.database_path, steward_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"steward item {steward_id} not found")
    if item["status"] not in ("pending", "approved"):
        raise HTTPException(
            status_code=409,
            detail=f"steward item {steward_id} is {item['status']}; "
            "split requires a pending or approved item",
        )
    entity_type, entity_id = item["entity_type"], item["entity_id"]
    target = ledger.fetch_latest_golden(current.database_path, entity_type, entity_id)
    versions: list[dict[str, Any]] = []
    connection = ledger.connect(current.database_path)
    try:
        rows = connection.execute(
            """
            SELECT * FROM golden_records
            WHERE entity_type = ? AND entity_id = ? ORDER BY version
            """,
            (entity_type, entity_id),
        ).fetchall()
        versions = [dict(row) for row in rows]
    finally:
        connection.close()
    wanted = [row for row in versions if row["version"] == split_request.restore_version]
    if not wanted:
        raise HTTPException(
            status_code=404,
            detail=f"version {split_request.restore_version} not found for "
            f"{entity_type}/{entity_id}",
        )
    restored_attrs = wanted[0]["attributes"]
    if isinstance(restored_attrs, str):
        restored_attrs = jsonlib.loads(restored_attrs)
    target_attrs = target["attributes"] if target else {}
    if isinstance(target_attrs, str):
        target_attrs = jsonlib.loads(target_attrs)
    return (
        item,
        split_request,
        entity_type,
        entity_id,
        target or {},
        {"restored": restored_attrs, "latest": target_attrs},
    )


def _build_split_restored(
    steward_id: str,
    split_request,
    policy,
    taxonomy_version: str,
    restored_attrs: dict[str, Any],
) -> dict[str, Any]:
    """Re-stamp one restored version as new winner facts (rule SPLIT)."""
    restored: dict[str, Any] = {}
    for attribute, value in restored_attrs.items():
        restored[attribute] = {
            "value": value["value"],
            "winner_fact_id": f"split-{steward_id}",
            "loser_fact_ids": [],
            "rule_fired": "SPLIT",
            "policy_version": policy.policy_version,
            "taxonomy_version": taxonomy_version,
            "lineage": (f"split restore v{split_request.restore_version} " f"via {steward_id}"),
        }
    return restored


def _insert_split_golden(
    current: Settings,
    entity_type: str,
    entity_id: str,
    steward_id: str,
    decision_id: str,
    policy_version: str,
    taxonomy_version: str | None,
    created_at: str,
    restored: dict[str, Any],
    latest_attrs: dict[str, Any],
    restore_version: int,
) -> tuple[str, int]:
    """INSERT the restored golden version + superseded-prev tombstones."""
    withdrawn = [attr for attr in latest_attrs if attr not in restored]
    connection = ledger.connect(current.database_path)
    try:
        with connection:
            latest_version = latest_version_of(current, entity_type, entity_id)
            golden_id = f"golden-{decision_id}"
            connection.execute(
                """
                INSERT INTO golden_records
                  (golden_id, entity_type, entity_id, version, attributes,
                   decision_id, policy_version, taxonomy_version,
                   created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    golden_id,
                    entity_type,
                    entity_id,
                    latest_version + 1,
                    jsonlib.dumps(restored, sort_keys=True, ensure_ascii=False),
                    decision_id,
                    policy_version,
                    taxonomy_version or None,
                    created_at,
                    created_at,
                ),
            )
            for attribute, value in latest_attrs.items():
                prev_winner = (value or {}).get("winner_fact_id")
                if not prev_winner:
                    continue
                current_winner = (restored.get(attribute) or {}).get("winner_fact_id")
                if current_winner != prev_winner or attribute in withdrawn:
                    ledger.insert_tombstone(
                        connection,
                        f"tb-{decision_id}-{prev_winner}-split",
                        prev_winner,
                        golden_id,
                        decision_id,
                        "superseded-by-split",
                    )
            return golden_id, latest_version + 1
    finally:
        connection.close()


def latest_version_of(current: Settings, entity_type: str, entity_id: str) -> int:
    """Latest golden version number, 0 when no golden exists yet."""
    target = ledger.fetch_latest_golden(current.database_path, entity_type, entity_id)
    return target["version"] if target else 0


def _do_split(steward_id: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
    current: Settings = request.app.state.settings
    item, split_request, entity_type, entity_id, target, attrs = _fetch_split_source(
        current, steward_id, body
    )
    created_at = utcnow_iso()
    decision_seq = f"split-{steward_id}-v{split_request.restore_version}"
    decision_id = decision_id_for(decision_seq, decision_seq)
    policy = active_version(request.app.state.policies, entity_type)
    taxonomy_version = target.get("taxonomy_version") or active_taxonomy(request, entity_type)[0]
    restored = _build_split_restored(
        steward_id, split_request, policy, taxonomy_version, attrs["restored"]
    )
    golden_id, golden_version = _insert_split_golden(
        current,
        entity_type,
        entity_id,
        steward_id,
        decision_id,
        policy.policy_version,
        taxonomy_version,
        created_at,
        restored,
        attrs["latest"],
        split_request.restore_version,
    )
    ledger.update_steward_status(current.database_path, steward_id, "split")
    return {
        "decision_id": decision_id,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "verdict": "published",
        "golden_version": golden_version,
        "restored_version": split_request.restore_version,
        "steward_id": steward_id,
        "policy_version": policy.policy_version,
        "taxonomy_version": taxonomy_version,
    }
