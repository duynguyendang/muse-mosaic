"""POST /upload — JSON fragments in, link proposals out. Never publishes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from ingest import detect_entity_type, parse_upload, to_candidate_facts
from schemas import Fragment, UploadResponse
from subagents import mock_linker

from routes.deps import logger

router = APIRouter()


@router.post("/upload")
async def upload(request: Request) -> dict[str, Any]:
    """Upload JSON fragments (CSD 12.4). Upload provides fragments only;
    every fragment goes through the same validation, MATCH, graph, derive,
    publisher gate and ledger write as fixed fragments. Nothing uploads
    directly to a golden."""

    form = await request.form()
    file = form.get("file")
    if file is None or not hasattr(file, "filename") or not hasattr(file, "read"):
        raise HTTPException(status_code=422, detail="multipart field 'file' is required")
    raw = (await file.read()).decode("utf-8", errors="replace")
    try:
        fragments, rejected = parse_upload(file.filename or "upload.json", raw)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    entity_type, confidence = detect_entity_type({"fragments": fragments} if fragments else {})
    if entity_type == "unknown":
        entity_type = "user_profile"
    # the detector's organic names map onto shipped contracts; anything that is
    # neither product-like nor user-like previews as a person (fail-safe demo
    # path — never a publish, since nothing here reaches the ledger)
    pipeline_entity = (
        entity_type
        if entity_type in ("user", "product")
        else ("user" if entity_type.startswith("user") else "product")
    )
    link_proposals: list[dict[str, Any]] = []
    try:
        facts = to_candidate_facts(
            pipeline_entity,
            "upload-preview",
            [Fragment.model_validate(item) for item in fragments],
            work_id="upload-preview",
        )
        by_fact = {fact.fact_id: fact for fact in facts}
        _ = by_fact
        links, _trace = mock_linker(facts, None)
        link_proposals = [link.model_dump() for link in links]
    except Exception as exc:
        logger.warning("upload: link proposals failed, preview continues without: %s", exc)
        link_proposals = []
    return UploadResponse(
        file_name=file.filename or "upload.json",
        detected_entity_type=entity_type,
        detected_confidence=confidence,  # type: ignore[arg-type]
        fragments_parsed=len(fragments),
        fragments=fragments,
        fragments_rejected=rejected,
        link_proposals=link_proposals,  # type: ignore[arg-type]
    ).model_dump(mode="json")
