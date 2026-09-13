"""Protocol-independent HTTP failures with bounded, non-content diagnostics."""

from __future__ import annotations

import hashlib
import re

import httpx

from qq_ai_bot.llm.base import (
    LLMAuthenticationError,
    LLMError,
    LLMInvalidRequestError,
    LLMRateLimitError,
    RetryableProviderError,
)

_IDENTIFIER = re.compile(r"[A-Za-z0-9_.\[\]-]{1,120}\Z")


def check_provider_response(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    details: dict[str, object] = {
        "http_status": response.status_code,
        "body_sha256": hashlib.sha256(response.content).hexdigest(),
    }
    try:
        body = response.json()
    except ValueError:
        body = {}
    error = body.get("error", {}) if isinstance(body, dict) else {}
    if isinstance(error, dict):
        for key in ("code", "type", "param"):
            value = error.get(key)
            if isinstance(value, str) and _IDENTIFIER.fullmatch(value):
                details[key] = value
    request_id = response.headers.get("x-request-id", "")
    if _IDENTIFIER.fullmatch(request_id):
        details["request_id"] = request_id
    try:
        delay = float(response.headers.get("retry-after", "0"))
        if 0 < delay <= 86400:
            details["retry_after_seconds"] = delay
    except ValueError:
        pass
    category = (
        RetryableProviderError
        if response.status_code >= 500
        else LLMAuthenticationError
        if response.status_code in {401, 403}
        else LLMRateLimitError
        if response.status_code == 429
        else LLMInvalidRequestError
        if response.status_code == 400
        else LLMError
    )
    raise category(
        f"provider rejected request with HTTP {response.status_code}", diagnostics=details
    )
