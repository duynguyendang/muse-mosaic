"""Background loops: job worker, inbox poller, hygiene analyser.

All three are cooperative asyncio tasks started from the app lifespan when
ENABLE_BACKGROUND_TASKS is true (tests disable them and drive the one-shot
helpers directly). Every loop is fail-closed: unexpected errors are logged,
never crash the API, and no loop ever publishes — only run_job_once writes,
and only through the publisher gate.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
from pathlib import Path

from config import Settings
from schemas import BuildRequest

logger = logging.getLogger("mosaic-api")

WORKER_IDLE_SECONDS = 2.0


def _app_deps(app) -> tuple[Settings, dict, dict | None, dict]:
    settings: Settings = app.state.settings
    return (
        settings,
        app.state.policies,
        getattr(app.state, "taxonomies", None),
        getattr(app.state, "expected_structures", {}) or {},
    )


async def run_jobs_loop(app) -> None:
    """Claim and run queued jobs until cancelled."""
    from jobs import run_job_once

    settings = app.state.settings
    logger.info("job worker started (database=%s)", settings.database_path)
    try:
        while True:
            try:
                claimed = await run_job_once(*_app_deps(app))
            except Exception as exc:  # noqa: BLE001 — the loop must survive
                logger.error("job worker iteration failed (non-fatal): %s", exc)
                claimed = None
            await asyncio.sleep(0 if claimed else WORKER_IDLE_SECONDS)
    except asyncio.CancelledError:
        logger.info("job worker stopped")
        raise


async def inbox_loop(app) -> None:
    """Poll INBOX_DIR for build JSON; each file becomes one job, then moves."""
    settings: Settings = app.state.settings
    inbox = Path(settings.inbox_dir)
    logger.info("inbox poller started (dir=%s every=%ds)", inbox, settings.inbox_poll_seconds)
    try:
        while True:
            try:
                inbox_poll_once(settings)
            except Exception as exc:  # noqa: BLE001 — the loop must survive
                logger.error("inbox poll failed (non-fatal): %s", exc)
            await asyncio.sleep(max(1, settings.inbox_poll_seconds))
    except asyncio.CancelledError:
        logger.info("inbox poller stopped")
        raise


def inbox_poll_once(settings: Settings) -> dict[str, int]:
    """One inbox scan. Returns counts; never raises on a bad file."""
    inbox = Path(settings.inbox_dir)
    if not settings.inbox_dir or not inbox.is_dir():
        return {"queued": 0, "skipped": 0, "failed": 0}
    counts = {"queued": 0, "skipped": 0, "failed": 0}
    for path in sorted(inbox.glob("*.json")):
        try:
            counts["queued" if _ingest_inbox_file(settings, path) else "skipped"] += 1
        except Exception as exc:  # noqa: BLE001 — one bad file never stops the scan
            logger.warning("inbox file %s rejected: %s", path.name, exc)
            _move_inbox_file(path, inbox / "failed")
            counts["failed"] += 1
    return counts


def _ingest_inbox_file(settings: Settings, path: Path) -> bool:
    """Validate one inbox file and enqueue it. True = new job queued."""
    from jobs import JobConflictError, submit_build_job

    raw = json.loads(path.read_text(encoding="utf-8"))
    request = BuildRequest.model_validate(raw)
    try:
        job = submit_build_job(settings.database_path, request)
    except JobConflictError:
        # Same key, different payload: a real problem. Let the caller move
        # the file to failed/ and count it; nothing was enqueued.
        raise
    inbox = path.parent
    _move_inbox_file(path, inbox / "processed")
    logger.info(
        "inbox file %s enqueued as job %s (key=%s)",
        path.name,
        job["job_id"],
        request.idempotency_key,
    )
    return True


def _move_inbox_file(path: Path, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / path.name
    if target.exists():
        stem = f"{path.stem}-{hashlib.sha256(path.read_bytes()).hexdigest()[:8]}"
        target = directory / f"{stem}{path.suffix}"
    shutil.move(str(path), str(target))


async def analyser_loop(app) -> None:
    """Periodic hygiene run. Disabled when ANALYSER_INTERVAL_SECONDS is 0."""
    from analyser import run_analyser_once

    settings: Settings = app.state.settings
    interval = max(1, settings.analyser_interval_seconds)
    logger.info("analyser loop started (every=%ds)", interval)
    try:
        while True:
            try:
                summary = run_analyser_once(settings)
                logger.info(
                    "analyser finished: %d entities, %d new items, %d overdue",
                    summary["entities"],
                    summary["inserted"],
                    len(summary["overdue"]),
                )
            except Exception as exc:  # noqa: BLE001 — the loop must survive
                logger.error("analyser run failed (non-fatal): %s", exc)
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.info("analyser loop stopped")
        raise


def verify_ingest_signature(settings: Settings, body: bytes, signature: str) -> None:
    """Fail closed on unauthenticated or misconfigured ingest webhooks."""
    from fastapi import HTTPException
    from notify import verify_signature

    if not settings.ingest_webhook_secret:
        raise HTTPException(
            status_code=503, detail="ingest webhook is disabled (no secret configured)"
        )
    if not verify_signature(settings.ingest_webhook_secret, body, signature or ""):
        raise HTTPException(status_code=401, detail="invalid ingest webhook signature")
