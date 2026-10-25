"""Character counts for the JSON encoding already used by tool results."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any


def escaped_character_length(character: str) -> int:
    if character in '\\"\b\f\n\r\t':
        return 2
    return 6 if ord(character) < 0x20 else 1


def json_string_prefix(text: str, available: int) -> int:
    """Longest original-character prefix, excluding its two JSON quotes."""
    used = 0
    for index, character in enumerate(text):
        used += escaped_character_length(character)
        if used > available:
            return index
    return len(text)


def json_character_count(
    value: object,
    *,
    limit: int | None = None,
    compact: bool = False,
    cache: dict[int, tuple[object, int, bool]] | None = None,
) -> int | None:
    """Exact size, or limit+1 when that is sufficient to prove overflow.

    Only visited immutable JSON containers are memoized by the caller. Saturated
    counts are lower bounds, never estimates of the complete original value.
    """
    separator = 1 if compact else 2
    active: set[int] = set()

    def key_text(key: Any) -> str:
        if isinstance(key, str):
            return key
        if key is True:
            return "true"
        if key is False:
            return "false"
        if key is None:
            return "null"
        if isinstance(key, (int, float)):
            return json.dumps(key)
        raise TypeError(f"keys must be str, int, float, bool or None, not {type(key).__name__}")

    def count(item: object, remaining: int | None) -> int:
        if remaining is not None and remaining < 0:
            return 0
        if isinstance(item, str):
            if remaining is not None and len(item) + 2 > remaining:
                return remaining + 1
            size = 2
            for start in range(0, len(item), 65536):
                size += len(json.encoder.encode_basestring(item[start : start + 65536])) - 2
                if remaining is not None and size > remaining:
                    return remaining + 1
            return size
        if not isinstance(item, (dict, list, tuple)):
            if item is None:
                return min(4, remaining + 1) if remaining is not None else 4
            if isinstance(item, bool):
                size = 4 if item else 5
            elif isinstance(item, int):
                size = len(str(item))
            elif isinstance(item, float):
                size = len(json.dumps(item))
            else:
                return count(str(item), remaining)
            return min(size, remaining + 1) if remaining is not None else size

        identity = id(item)
        if cache is not None and (saved := cache.get(identity)) is not None:
            original, size, exact = saved
            if original is item and (exact or (remaining is not None and size > remaining)):
                return min(size, remaining + 1) if remaining is not None else size
        if remaining is not None and remaining < 2:
            return remaining + 1
        if identity in active:
            raise ValueError("Circular reference detected")
        active.add(identity)
        size = 2
        exact = True
        try:
            children: Iterator[tuple[object | None, object]] = (
                iter(item.items()) if isinstance(item, dict) else ((None, child) for child in item)
            )
            for index, (key, child) in enumerate(children):
                if index:
                    size += separator
                if isinstance(item, dict):
                    size += count(key_text(key), None if remaining is None else remaining - size)
                    size += separator
                size += count(child, None if remaining is None else remaining - size)
                if remaining is not None and size > remaining:
                    size, exact = remaining + 1, False
                    break
        finally:
            active.remove(identity)
        if cache is not None:
            cache[identity] = (item, size, exact)
        return size

    try:
        return count(value, limit)
    except (RecursionError, ValueError):
        return None
