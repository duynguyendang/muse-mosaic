"""POST /ingest/webhook — authenticated unattended intake.

HMAC-SHA256 over the raw body (header X-Mosaic-Signature) with the configured
INGEST_WEBHOOK_SECRET. Missing secret = endpoint disabled (503, fail closed);
bad signature = 401. A valid body is a BuildRequest; it is enqueued as a job
(202) and processed by the worker — never derived inline, so a slow caller
cannot hold the request open and a crash loses nothing.
"""

from __future__ import annotations

from typing import Any

from config import Settings
from fastapi import APIRouter, HTTPException, Request
from jobs import JobConflictError, submit_build_job
from notify import SIGNATURE_HEADER
from schemas import BuildRequest
from worker import verify_ingest_signature

router = APIRouter()


@router.post("/ingest/webhook", status_code=202)
async def ingest_webhook(request: Request) -> dict[str, Any]:
    current: Settings = request.app.state.settings
    raw = await request.body()
    try:
        body = raw.decode("utf-8")
        import json as _json

        payload = _json.loads(body)
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="invalid JSON body") from exc
    verify_ingest_signature(current, raw, request.headers.get(SIGNATURE_HEADER, ""))
    try:
        build_request = BuildRequest.model_validate(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        job = submit_build_job(current.database_path, build_request)
    except JobConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"job_id": job["job_id"], "status": job["status"]}
