"""Pure JSON aliases for control payloads. No serialization I/O."""

from __future__ import annotations

import math
from collections.abc import Mapping
from types import MappingProxyType

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | tuple[JsonValue, ...] | Mapping[str, JsonValue]
type JsonObject = Mapping[str, JsonValue]

_MAX_JSON_DEPTH = 32


def freeze_json_value(value: object, *, depth: int = 0) -> JsonValue:
    """Copy a JSON-compatible value into an immutable form."""

    if depth > _MAX_JSON_DEPTH:
        raise ValueError("json payload exceeded max depth")
    if value is None or type(value) is str or type(value) is bool:
        return value
    if type(value) is int:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("json numbers must be finite")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, JsonValue] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError("json object keys must be str")
            frozen[key] = freeze_json_value(item, depth=depth + 1)
        return MappingProxyType(frozen)
    if type(value) is list or type(value) is tuple:
        return tuple(freeze_json_value(item, depth=depth + 1) for item in value)
    raise TypeError(f"unsupported json type: {type(value).__name__}")


def freeze_json_object(value: object) -> JsonObject:
    frozen = freeze_json_value(value)
    if not isinstance(frozen, Mapping):
        raise TypeError("json object is required")
    return frozen
