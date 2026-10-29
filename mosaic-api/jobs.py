"""Background jobs: durable, idempotent build runs (autonomy Phase 0/1).

A job wraps one build payload. Per-cluster decisions keep their own
idempotency keys, so re-running a job replays finished clusters and only
derives the rest — crash-resume is a property of the existing gate, not new
logic. The publisher still writes nothing except derived + validated values;
this module only tracks execution, never trust.

Token budgets are enforced between clusters from Meta-reported usage stored
in agent_traces (mock mode reports 0, so budgets never trip in tests).
Exceeding a budget stops the job as failed (terminal, not retried: raising
the budget and re-queueing is the operator's explicit act).
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import ledger
from config import Settings
from schemas import BuildRequest, JobStatus

logger = logging.getLogger("mosaic-api")

JOB_NAMESPACE = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c9")
JOB_KIND_BUILD = "build"

RETRYABLE_HTTP = {408, 429, 500, 502, 503, 504}

BACKOFF_BASE_SECONDS = 5
BACKOFF_MAX_SECONDS = 300


class JobConflictError(RuntimeError):
    """Same idempotency key, different payload. Nothing was enqueued."""


def job_payload_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def job_id_for(idempotency_key: str) -> str:
    return str(uuid.uuid5(JOB_NAMESPACE, f"mosaic:job:{idempotency_key}"))


def submit_build_job(database_path: str, request: BuildRequest) -> dict[str, Any]:
    """Enqueue one build job. Same key + same payload replays the same job."""
    payload = request.model_dump(mode="json")
    payload_hash = job_payload_hash(payload)
    now = datetime.now(UTC).isoformat()
    existing = ledger.fetch_job_by_key(database_path, request.idempotency_key)
    if existing is not None:
        if existing["payload_hash"] != payload_hash:
            raise JobConflictError(
                f"idempotency key {request.idempotency_key!r} was already used "
                "with a different payload; refusing to enqueue"
            )
        return existing
    return ledger.create_job(
        database_path,
        job_id=job_id_for(request.idempotency_key),
        kind=JOB_KIND_BUILD,
        idempotency_key=request.idempotency_key,
        payload_hash=payload_hash,
        payload=payload,
        created_at=now,
    )


def cluster_usage_tokens(database_path: str, decision_ids: list[str]) -> int:
    """Meta-reported total_tokens for freshly derived decisions.

    Reads the subagents trace the publisher stored. Replayed decisions carry
    no new usage (mock mode reports 0), so budgets only bite in real mode.
    """
    total = 0
    for decision_id in decision_ids:
        for trace in ledger.fetch_agent_traces(database_path, decision_id):
            if trace.get("kind") != "subagents":
                continue
            usage = (trace.get("payload") or {}).get("usage") or {}
            try:
                total += int(usage.get("total_tokens", 0) or 0)
            except (TypeError, ValueError):
                continue
    return total


def daily_tokens_used(database_path: str) -> int:
    """Tokens charged by all jobs created since UTC midnight."""
    midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return ledger.sum_job_tokens_since(database_path, midnight.isoformat())


def _backoff_seconds(attempts: int) -> int:
    return min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * (2 ** max(0, attempts)))


def _budget_stop_reason(settings: Settings, database_path: str, tokens_used: int) -> str | None:
    if settings.muse_job_token_budget > 0 and tokens_used > settings.muse_job_token_budget:
        return (
            f"MUSE_JOB_TOKEN_BUDGET_EXHAUSTED (used={tokens_used} "
            f"budget={settings.muse_job_token_budget})"
        )
    if settings.muse_daily_token_budget > 0:
        daily = daily_tokens_used(database_path)
        if daily > settings.muse_daily_token_budget:
            return (
                f"MUSE_DAILY_TOKEN_BUDGET_EXHAUSTED (used={daily} "
                f"budget={settings.muse_daily_token_budget})"
            )
    return None


def _is_retryable(error: BaseException) -> bool:
    """Transient failures may retry; contract violations fail immediately."""
    from fastapi import HTTPException

    if isinstance(error, HTTPException):
        return error.status_code in RETRYABLE_HTTP
    from publisher import EngineUnavailableError

    return isinstance(error, (EngineUnavailableError, TimeoutError, ConnectionError))


async def run_job_once(
    settings: Settings,
    policies: dict,
    taxonomies: dict | None,
    expected_structures: dict,
    *,
    now_iso: str | None = None,
) -> str | None:
    """Claim one due job and run it to a terminal state. Returns the job id."""
    from datetime import datetime as _datetime

    now = now_iso or _datetime.now(UTC).isoformat()
    job = ledger.claim_next_job(settings.database_path, now)
    if job is None:
        return None
    logger.info("job %s claimed (kind=%s attempt=%d)", job["job_id"], job["kind"], job["attempts"])
    try:
        summary, tokens_used = await _run_build_job(
            settings, policies, taxonomies, expected_structures, job
        )
    except _JobCancelled:
        current = ledger.fetch_job(settings.database_path, job["job_id"])
        ledger.update_job(
            settings.database_path,
            job["job_id"],
            datetime.now(UTC).isoformat(),
            tokens_used=int((current or job).get("tokens_used", 0) or 0),
            result={"cancelled": True},
            error="cancelled by operator",
        )
        logger.info("job %s cancelled", job["job_id"])
        return job["job_id"]
    except Exception as exc:  # noqa: BLE001 — job must always land somewhere
        return _record_job_failure(settings, job, exc)
    verdict = str((summary or {}).get("verdict", "denied"))
    status = {
        "published": JobStatus.PUBLISHED,
        "escalated": JobStatus.ESCALATED,
        "denied": JobStatus.DENIED,
    }.get(verdict, JobStatus.FAILED)
    ledger.update_job(
        settings.database_path,
        job["job_id"],
        datetime.now(UTC).isoformat(),
        status=status,
        tokens_used=tokens_used,
        result=summary,
        error=None if status == JobStatus.PUBLISHED else f"build verdict: {verdict}",
    )
    if status != JobStatus.PUBLISHED:
        _notify_terminal(settings, job, status, summary, None)
    logger.info("job %s finished: %s", job["job_id"], status)
    return job["job_id"]


def _record_job_failure(settings: Settings, job: dict, error: BaseException) -> str:
    attempts = int(job.get("attempts", 0) or 0) + 1
    if _is_retryable(error) and attempts < settings.job_max_attempts:
        run_at = (datetime.now(UTC) + timedelta(seconds=_backoff_seconds(attempts))).isoformat()
        ledger.update_job(
            settings.database_path,
            job["job_id"],
            datetime.now(UTC).isoformat(),
            status=JobStatus.QUEUED,
            attempts=attempts,
            error=f"{type(error).__name__}: {error}",
            next_run_at=run_at,
        )
        logger.warning(
            "job %s failed transiently (attempt %d/%d), retry at %s: %s",
            job["job_id"],
            attempts,
            settings.job_max_attempts,
            run_at,
            error,
        )
    else:
        ledger.update_job(
            settings.database_path,
            job["job_id"],
            datetime.now(UTC).isoformat(),
            status=JobStatus.FAILED,
            attempts=attempts,
            error=f"{type(error).__name__}: {error}",
        )
        logger.error("job %s failed permanently: %s", job["job_id"], error)
        _notify_terminal(settings, job, JobStatus.FAILED, None, str(error))
    return job["job_id"]


def _notify_terminal(
    settings: Settings,
    job: dict,
    status: str,
    summary: dict[str, Any] | None,
    error: str | None,
) -> None:
    try:
        from notify import notify_job_terminal

        notify_job_terminal(settings, job, status, summary or {}, error)
    except Exception as exc:  # noqa: BLE001 — notification never fails a job
        logger.warning("job %s notify failed (non-fatal): %s", job["job_id"], exc)


class _JobHooks:
    """Per-cluster intermission for jobs: cancel, budget, token charge."""

    def __init__(self, settings: Settings, job_id: str, tokens_used: int):
        self.settings = settings
        self.job_id = job_id
        self.tokens_used = tokens_used

    def before_cluster(self) -> None:
        _raise_if_cancelled(self.settings, self.job_id)
        reason = _budget_stop_reason(self.settings, self.settings.database_path, self.tokens_used)
        if reason is not None:
            raise RuntimeError(reason)

    def after_cluster(self, goldens: list[dict[str, Any]]) -> None:
        decision_ids = [
            golden["decision_id"]
            for golden in goldens
            if isinstance(golden, dict) and golden.get("decision_id")
        ]
        fresh = cluster_usage_tokens(self.settings.database_path, decision_ids)
        if fresh:
            logger.debug("job %s charged %d tokens", self.job_id, fresh)
        self.tokens_used += fresh
        ledger.update_job(
            self.settings.database_path,
            self.job_id,
            datetime.now(UTC).isoformat(),
            tokens_used=self.tokens_used,
        )


async def execute_build(ctx, clusters: list[dict[str, Any]], *, hooks=None) -> dict[str, Any]:
    """Run every cluster and aggregate the build summary (shared sync/async path).

    hooks.before_cluster() may raise to stop (_JobCancelled, RuntimeError);
    hooks.after_cluster(goldens) observes freshly persisted decisions.
    """
    from routes.build import _build_one_cluster, _build_summary

    goldens: list[dict[str, Any]] = []
    conflicts: list = []
    stewards: list = []
    verdicts: list[str] = []
    for index, cluster in enumerate(clusters):
        if hooks is not None:
            hooks.before_cluster()
        goldens_i, conflicts_i, stewards_i, verdict = await _build_one_cluster(ctx, cluster, index)
        goldens.extend(goldens_i)
        conflicts.extend(conflicts_i)
        stewards.extend(stewards_i)
        verdicts.append(verdict)
        if hooks is not None:
            hooks.after_cluster(goldens_i)
    return _build_summary(ctx, goldens, conflicts, stewards, verdicts)


async def execute_full_build(
    settings: Settings,
    policies: dict,
    taxonomies: dict | None,
    expected_structures: dict,
    request: BuildRequest,
    *,
    hooks=None,
) -> dict[str, Any]:
    """Full pipeline shared by POST /build and jobs: match, cluster, derive."""
    from routes.build import _build_clusters, _BuildContext, _match_documents

    ctx = _BuildContext(
        settings=settings,
        policies=policies,
        taxonomies=taxonomies,
        expected_structures=expected_structures,
        request=request,
    )
    documents = [doc.model_dump() for doc in request.dataset.documents]
    for document in documents:
        document["content_hash"] = document.get("content_hash") or document["document_id"]
    splink_out, splink_status = await _match_documents(
        settings, policies, documents, request.work_id
    )
    ctx.splink_out = splink_out
    ctx.splink_status = splink_status
    clusters = await _build_clusters(
        settings,
        policies=policies,
        taxonomies=taxonomies,
        documents=documents,
        splink_out=splink_out,
    )
    return await execute_build(ctx, clusters, hooks=hooks)


async def _run_build_job(
    settings: Settings,
    policies: dict,
    taxonomies: dict | None,
    expected_structures: dict,
    job: dict,
) -> tuple[dict[str, Any], int]:
    """Execute one build job cluster-by-cluster with budget/cancel checks."""
    request = BuildRequest.model_validate(job["payload"])
    if settings.muse_daily_token_budget > 0:
        reason = _budget_stop_reason(settings, settings.database_path, 0)
        if reason is not None:
            raise RuntimeError(reason)
    hooks = _JobHooks(settings, job["job_id"], int(job.get("tokens_used", 0) or 0))
    summary = await execute_full_build(
        settings, policies, taxonomies, expected_structures, request, hooks=hooks
    )
    return summary, hooks.tokens_used


def _raise_if_cancelled(settings: Settings, job_id: str) -> None:
    job = ledger.fetch_job(settings.database_path, job_id)
    if job is not None and job["status"] == JobStatus.CANCELLED:
        raise _JobCancelled(f"job {job_id} was cancelled")


class _JobCancelled(RuntimeError):
    """Internal control flow: the operator cancelled a running job."""
