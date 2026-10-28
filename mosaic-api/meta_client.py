"""Meta Model API caller with strict contracts (CSD 2.9, cookbook synthesis).

One Chat Completions call per sub-agent step:
- strict ``json_schema`` (not ``json_object``): constrains decoding itself.
  Code-side Pydantic validation still applies — schema guarantees shape,
  never facts.
- bounded transient retry (429/500/503/timeout, exp backoff + jitter,
  honor Retry-After, max 3); 400/401/404/413 never retried.
- circuit breaker: 3 consecutive failures open the breaker for 30s; a trial
  half-open call that succeeds closes it.
- usage captured per call (prompt/completion/total, cached/reasoning tokens)
  for the README usage log; never sent anywhere.

Any final failure raises ``MetaCallError``. The caller escalates, never
publishes. No Meta tool is ever called in tests.
"""

from __future__ import annotations

import asyncio
import json
import random
import threading
import time
from typing import Any

import httpx

from config import Settings

MUSE_TIMEOUT_SECONDS = 120.0
MUSE_CHAT_COMPLETIONS_PATH = "/chat/completions"
MAX_ATTEMPTS = 3
BREAKER_THRESHOLD = 3
BREAKER_COOLDOWN_SECONDS = 30.0

RETRYABLE_STATUS = {429, 500, 502, 503}


class MetaCallError(RuntimeError):
    """A Meta Model API call failed or its output was unusable."""

    def __init__(self, kind: str, message: str, usage: dict[str, Any] | None = None):
        super().__init__(message)
        self.kind = kind
        self.usage = usage or {}


class _Breaker:
    """Tiny circuit breaker shared by all real Muse calls in the process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._consecutive_failures = 0
        self._opened_at: float | None = None

    def check(self) -> None:
        with self._lock:
            if self._opened_at is None:
                return
            if time.monotonic() - self._opened_at >= BREAKER_COOLDOWN_SECONDS:
                self._opened_at = None
                return
            raise MetaCallError(
                "breaker-open",
                "Muse breaker is open after consecutive failures; escalating",
            )

    def record(self, ok: bool) -> None:
        with self._lock:
            if ok:
                self._consecutive_failures = 0
                self._opened_at = None
            else:
                self._consecutive_failures += 1
                if self._consecutive_failures >= BREAKER_THRESHOLD:
                    self._opened_at = time.monotonic()


_BREAKER = _Breaker()


def json_schema_response(name: str, schema: dict[str, Any]) -> dict[str, Any]:
    """Strict json_schema response_format for Chat Completions."""
    return {
        "type": "json_schema",
        "json_schema": {"name": name, "strict": True, "schema": schema},
    }


def strip_fences(text: str) -> str:
    """Remove markdown fences; then the caller extracts the first balanced {}."""
    lines = text.strip().splitlines()
    lines = [line for line in lines if not line.strip().startswith("```")]
    return "\n".join(lines).strip()


def extract_json_object(text: str) -> Any:
    """Parse the first balanced JSON object in text (fence-tolerant)."""
    cleaned = strip_fences(text)
    try:
        return json.loads(cleaned)
    except ValueError:
        pass
    start = cleaned.find("{")
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(cleaned)):
        if start < 0:
            break
        char = cleaned[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return json.loads(cleaned[start : index + 1])
    raise ValueError("no balanced JSON object found")


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


async def chat_json(
    settings: Settings,
    *,
    model: str,
    system: str,
    user: str,
    schema_name: str,
    schema: dict[str, Any],
    reasoning_effort: str | None = None,
    prompt_cache_key: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """POST one strict-schema Chat Completions call; return (parsed, usage).

    Raises MetaCallError on transport failure (after bounded retries),
    refusal, or unusable output. Usage follows the Chat Completions shape:
    prompt/completion/total tokens plus cached/reasoning details when present.
    """
    settings.require_real_muse_config()
    _BREAKER.check()
    url = settings.meta_model_api_base_url.rstrip("/") + MUSE_CHAT_COMPLETIONS_PATH
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "response_format": json_schema_response(schema_name, schema),
    }
    if reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort
    if prompt_cache_key:
        payload["prompt_cache_key"] = prompt_cache_key

    last_error: MetaCallError | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            async with httpx.AsyncClient(timeout=MUSE_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {settings.meta_model_api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
            last_error = MetaCallError("unreachable", f"Muse unreachable: {exc}")
        except httpx.HTTPError as exc:
            last_error = MetaCallError("transport", f"Muse transport error: {exc}")
            break
        else:
            if response.status_code == 200:
                return _parse_success(response, model)
            if response.status_code in RETRYABLE_STATUS:
                wait = _retry_after_seconds(response)
                if wait is None:
                    wait = (2**attempt) + random.uniform(0, 1.0)
                last_error = MetaCallError(
                    "refused-retryable",
                    f"Muse refused (HTTP {response.status_code}); will retry",
                )
                if attempt < MAX_ATTEMPTS - 1:
                    await asyncio.sleep(min(wait, 30.0))
                continue
            raise MetaCallError(
                "refused",
                f"Muse refused the call (HTTP {response.status_code}): "
                f"{response.text[:300]}",
            )
        if attempt < MAX_ATTEMPTS - 1:
            await asyncio.sleep((2**attempt) + random.uniform(0, 1.0))
    _BREAKER.record(False)
    raise last_error or MetaCallError("unknown", "Muse call failed")


def _parse_success(
    response: httpx.Response, model: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        body = response.json()
        content = body["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        _BREAKER.record(False)
        raise MetaCallError(
            "invalid-envelope", f"Muse output envelope unusable: {exc}"
        ) from exc
    try:
        parsed = extract_json_object(content)
    except ValueError as exc:
        _BREAKER.record(False)
        raise MetaCallError(
            "invalid-json", f"Muse output is not JSON: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        _BREAKER.record(False)
        raise MetaCallError("invalid-shape", "Muse output is not a JSON object")
    usage = _usage_of(body)
    usage["model"] = model
    _BREAKER.record(True)
    return parsed, usage


def _usage_of(body: dict[str, Any]) -> dict[str, Any]:
    raw = body.get("usage", {}) if isinstance(body, dict) else {}
    details = raw.get("prompt_tokens_details", {}) or {}
    completion_details = raw.get("completion_tokens_details", {}) or {}
    return {
        "prompt_tokens": raw.get("prompt_tokens", 0),
        "completion_tokens": raw.get("completion_tokens", 0),
        "total_tokens": raw.get("total_tokens", 0),
        "cached_tokens": details.get("cached_tokens", 0),
        "reasoning_tokens": completion_details.get("reasoning_tokens", 0),
    }
