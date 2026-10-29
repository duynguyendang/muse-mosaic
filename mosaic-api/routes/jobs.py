"""Background jobs: enqueue builds, poll status, retry/cancel.

POST /jobs accepts a BuildRequest and returns 202 with the job id; the worker
runs it cluster-by-cluster with crash-resume. Same idempotency key + same
payload replays the same job (202, no duplicate); same key + different payload
is 409. Nothing here publishes — jobs only execute the existing gated
pipeline through run_job_once.
"""

from __future__ import annotations

from typing import Any

import ledger
from config import Settings
from fastapi import APIRouter, HTTPException, Query, Request
from jobs import JobConflictError, submit_build_job
from schemas import BuildRequest, JobResponse, JobStatus

router = APIRouter()


def _job_payload(job: dict[str, Any]) -> dict[str, Any]:
    return {key: job.get(key) for key in JobResponse.model_fields}


@router.post("/jobs", status_code=202)
async def enqueue_job(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid JSON body") from exc
    try:
        build_request = BuildRequest.model_validate(body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    current: Settings = request.app.state.settings
    try:
        job = submit_build_job(current.database_path, build_request)
    except JobConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _job_payload(job)


@router.get("/jobs")
def list_jobs(
    request: Request,
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    current: Settings = request.app.state.settings
    if status is not None and status not in JobStatus.TERMINAL + ("queued", "running"):
        raise HTTPException(status_code=422, detail=f"unknown job status {status!r}")
    return {
        "jobs": [
            _job_payload(job) for job in ledger.list_jobs(current.database_path, status, limit)
        ]
    }


@router.get("/jobs/{job_id}")
def get_job(request: Request, job_id: str) -> dict[str, Any]:
    current: Settings = request.app.state.settings
    job = ledger.fetch_job(current.database_path, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    return _job_payload(job)


@router.post("/jobs/{job_id}/retry")
def retry_job(request: Request, job_id: str) -> dict[str, Any]:
    """Re-queue a terminal or failed job. Running/queued jobs are untouched."""
    from datetime import UTC, datetime

    current: Settings = request.app.state.settings
    job = ledger.fetch_job(current.database_path, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    if job["status"] not in JobStatus.TERMINAL:
        raise HTTPException(
            status_code=409, detail=f"job {job_id} is {job['status']}, not retryable"
        )
    ledger.update_job(
        current.database_path,
        job_id,
        datetime.now(UTC).isoformat(),
        status="queued",
        attempts=0,
        error=None,
        next_run_at=None,
        result=None,
    )
    updated = ledger.fetch_job(current.database_path, job_id)
    assert updated is not None
    return _job_payload(updated)


@router.delete("/jobs/{job_id}")
def cancel_job(request: Request, job_id: str) -> dict[str, Any]:
    """Cancel a queued job, or ask a running job to stop between clusters."""
    from datetime import UTC, datetime

    current: Settings = request.app.state.settings
    job = ledger.fetch_job(current.database_path, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    if job["status"] in JobStatus.TERMINAL:
        raise HTTPException(status_code=409, detail=f"job {job_id} is already {job['status']}")
    ledger.update_job(
        current.database_path, job_id, datetime.now(UTC).isoformat(), status="cancelled"
    )
    updated = ledger.fetch_job(current.database_path, job_id)
    assert updated is not None
    return _job_payload(updated)
