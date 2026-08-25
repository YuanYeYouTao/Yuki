"""Opaque public-protocol tokens. Not user-authored free text."""

from __future__ import annotations

import unicodedata
from datetime import datetime
from typing import Final

MAX_CURSOR_LENGTH: Final[int] = 256
MAX_RESOURCE_TOKEN_LENGTH: Final[int] = 128
MAX_CAPABILITY_ID_LENGTH: Final[int] = 128


def require_opaque_token(value: object, *, name: str, max_length: int) -> str:
    """Reject empty, padded, overlong, whitespace, and control-bearing tokens."""

    if type(value) is not str:
        raise TypeError(f"{name} must be a str")
    if not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty opaque token")
    if len(value) > max_length:
        raise ValueError(f"{name} exceeds max length")
    if any(ch.isspace() or unicodedata.category(ch) == "Cc" for ch in value):
        raise ValueError(f"{name} must not contain whitespace or control characters")
    if any(ord(ch) < 33 or ord(ch) > 126 for ch in value):
        raise ValueError(f"{name} must be an opaque graphic token")
    return value


def require_aware_datetime(value: object, *, name: str) -> datetime:
    """Reject naive datetimes, including tzinfo that cannot produce an offset."""

    if type(value) is not datetime:
        raise TypeError(f"{name} must be datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value
