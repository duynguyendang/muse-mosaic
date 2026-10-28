"""Trust-engine HTTP client (mosaic-api -> trust-engine, internal only).

Every failure mode here fails closed: unreachable engine, timeout, non-2xx, or
out-of-schema output means no golden facts may be published. The caller must
escalate, never publish and never retry blindly in a loop.
"""

from __future__ import annotations

import httpx

from config import Settings
from schemas import DeriveRequest, DeriveResponse


class TrustEngineError(RuntimeError):
    """The trust-engine cannot be used for this request. Fail closed."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


async def derive(
    settings: Settings, request: DeriveRequest, *, timeout_seconds: float = 20.0
) -> DeriveResponse:
    """POST /derive and validate the response against the derive contract."""
    payload = request.model_dump(mode="json")
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.post(settings.trust_engine_url, json=payload)
    except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
        raise TrustEngineError(
            "unreachable", f"trust-engine unreachable: {exc}"
        ) from exc
    except httpx.HTTPError as exc:
        raise TrustEngineError(
            "transport", f"trust-engine transport error: {exc}"
        ) from exc

    if response.status_code != 200:
        raise TrustEngineError(
            "derive-failed",
            f"trust-engine refused to derive (HTTP {response.status_code}): "
            f"{response.text[:300]}",
        )
    try:
        return DeriveResponse.model_validate(response.json())
    except (ValueError, TypeError) as exc:
        raise TrustEngineError(
            "invalid-output",
            f"trust-engine output failed the derive contract: {exc}",
        ) from exc
