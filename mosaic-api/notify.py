"""Steward notifications: tell a human when unattended work needs them.

Fires on terminal job outcomes that publish nothing actionable on their own
(escalated / denied / failed) and on the overdue-steward digest. Published
goldens need no notification — downstream reads the ledger.

Delivery is a signed webhook POST when STEWARD_WEBHOOK_URL is set
(HMAC-SHA256 over the raw body, header X-Mosaic-Signature), plus a structured
log line always. Delivery failure never fails the job: it logs and returns.
Payloads carry steward routing (ids, deadlines, fallbacks, suggested
actions) — never golden values; the steward reads those from the ledger.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any

import httpx
from config import Settings

logger = logging.getLogger("mosaic-api")

SIGNATURE_HEADER = "X-Mosaic-Signature"
NOTIFY_TIMEOUT_SECONDS = 10.0


def sign_payload(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_signature(secret: str, body: bytes, signature: str) -> bool:
    """Constant-time HMAC check for inbound webhooks."""
    if not secret or not signature:
        return False
    expected = sign_payload(secret, body)
    return hmac.compare_digest(expected, signature)


def notify_job_terminal(
    settings: Settings,
    job: dict[str, Any],
    status: str,
    summary: dict[str, Any],
    error: str | None,
) -> bool:
    """Notify that a job ended without an actionable publish. Returns sent."""
    stewards = summary.get("steward_queue", []) if isinstance(summary, dict) else []
    event = {
        "event": "job_terminal",
        "job_id": job.get("job_id"),
        "kind": job.get("kind"),
        "status": status,
        "verdict": (summary.get("verdict") if isinstance(summary, dict) else None),
        "goldens_published": sum(
            1
            for golden in (summary.get("goldens", []) if isinstance(summary, dict) else [])
            if isinstance(golden, dict) and golden.get("golden_id")
        ),
        "steward_items": [
            {
                "steward_id": item.get("steward_id"),
                "entity_type": item.get("entity_type"),
                "entity_id": item.get("entity_id"),
                "attribute": item.get("attribute"),
                "deadline": item.get("deadline"),
                "fallback": item.get("fallback"),
                "suggested_action": item.get("suggested_action"),
            }
            for item in stewards
            if isinstance(item, dict)
        ],
        "error": error,
    }
    return _send(settings, event, f"job {job.get('job_id')} ended as {status}")


def notify_overdue_digest(settings: Settings, overdue: list[dict[str, Any]]) -> bool:
    """Notify deputies about steward items past deadline. Returns sent."""
    if not overdue:
        return False
    event = {
        "event": "steward_overdue",
        "items": [
            {
                "steward_id": item.get("steward_id"),
                "entity_type": item.get("entity_type"),
                "entity_id": item.get("entity_id"),
                "attribute": item.get("attribute"),
                "owner": item.get("owner"),
                "deputy": item.get("deputy"),
                "deadline": item.get("deadline"),
                "status": item.get("status"),
            }
            for item in overdue
        ],
    }
    return _send(settings, event, f"{len(overdue)} steward item(s) overdue")


def _send(settings: Settings, event: dict[str, Any], log_summary: str) -> bool:
    body = json.dumps(event, ensure_ascii=False, sort_keys=True).encode("utf-8")
    if not settings.steward_webhook_url:
        logger.info("steward notify (no webhook configured): %s", log_summary)
        return False
    headers = {"Content-Type": "application/json"}
    if settings.steward_webhook_secret:
        headers[SIGNATURE_HEADER] = sign_payload(settings.steward_webhook_secret, body)
    try:
        with httpx.Client(timeout=NOTIFY_TIMEOUT_SECONDS) as client:
            response = client.post(settings.steward_webhook_url, content=body, headers=headers)
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001 — notification never raises
        logger.warning("steward notify failed (non-fatal) for %s: %s", log_summary, exc)
        return False
    logger.info("steward notify sent: %s", log_summary)
    return True
