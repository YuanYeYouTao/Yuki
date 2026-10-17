"""Shared secret redaction for model-facing tool results and evidence."""

from __future__ import annotations

import re
from typing import Any

_CAMEL_CASE_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_IDENTIFIER = re.compile(r"[^a-z0-9]+")
_SENSITIVE_TEXT = re.compile(
    r"""(?ix)
    (?P<prefix>
        ["']?
        (?:
            api[_-]?key|authorization|bearer[_-]?token|client[_-]?secret|
            access[_-]?token|refresh[_-]?token|id[_-]?token|session[_-]?token|
            password|passwd|passphrase|private[_-]?key|secret|set[_-]?cookie|
            cookies?|tokens?
        )
        ["']?\s*[:=]\s*
    )
    (?P<value>
        "(?:\\.|[^"\\])*"|
        '(?:\\.|[^'\\])*'|
        (?:Bearer|Basic)\s+[^\s,;}]+|
        [^\s,;}]+
    )
    """
)

_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "bearer_token",
        "client_secret",
        "cookie",
        "cookies",
        "credential",
        "credentials",
        "access_token",
        "refresh_token",
        "id_token",
        "session_token",
        "password",
        "passwd",
        "passphrase",
        "private_key",
        "secret",
        "set_cookie",
        "token",
    }
)
_SENSITIVE_SUFFIXES = (
    "_api_key",
    "_authorization",
    "_cookie",
    "_password",
    "_private_key",
    "_secret",
    "_token",
)


def redact_sensitive_data(value: Any) -> Any:
    """Return a JSON-compatible copy with credential-bearing fields redacted."""

    if isinstance(value, dict):
        return {
            str(key): ("[redacted]" if is_sensitive_key(str(key)) else redact_sensitive_data(child))
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive_data(child) for child in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive_data(child) for child in value)
    return value


def redact_sensitive_text(value: str) -> str:
    """Redact common inline credential assignments in otherwise opaque text."""

    return _SENSITIVE_TEXT.sub(
        lambda match: f'{match.group("prefix")}"[redacted]"',
        value,
    )


def is_sensitive_key(value: str) -> bool:
    normalized = _normalize_key(value)
    return normalized in _SENSITIVE_KEYS or normalized.endswith(_SENSITIVE_SUFFIXES)


def _normalize_key(value: str) -> str:
    separated = _CAMEL_CASE_BOUNDARY.sub("_", value.strip())
    return _NON_IDENTIFIER.sub("_", separated.casefold()).strip("_")
