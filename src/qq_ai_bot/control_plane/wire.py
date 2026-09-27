"""Pure transport conversion for the reviewed control DTOs, never ORM objects."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import Enum

from qq_ai_bot.control_plane.command_types import ControlCommandError
from qq_ai_bot.control_plane.commands import ControlCommand, ControlResult
from qq_ai_bot.control_plane.json_types import (
    JsonObject,
    JsonValue,
    freeze_json_object,
    freeze_json_value,
)
from qq_ai_bot.control_plane.operations import OperationRef
from qq_ai_bot.control_plane.paging import Cursor, Page, PageRequest
from qq_ai_bot.control_plane.problems import Problem, ProblemCode
from qq_ai_bot.control_plane.query_types import ControlQueryError
from qq_ai_bot.control_plane.surface import ControlMethodView, ControlSurfaceView
from qq_ai_bot.domain.identity import (
    ConversationGeneration,
    RouteGeneration,
    _CanonicalUuid4,
)

_OUTPUT_TYPES = (ControlResult, OperationRef, Page, Problem, ControlMethodView, ControlSurfaceView)


def _wire(value: object) -> JsonValue:
    if isinstance(value, _CanonicalUuid4):
        return value.text
    if isinstance(value, (ConversationGeneration, RouteGeneration)):
        return value.value
    if isinstance(value, Enum):
        return _wire(value.value)
    if type(value) is datetime:
        if value.tzinfo is None:
            raise ValueError("control timestamps must be timezone aware")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if type(value) is Cursor:
        return value.value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        if (
            type(value) not in _OUTPUT_TYPES
            and type(value).__module__ != "qq_ai_bot.control_plane.query_types"
        ):
            raise TypeError("only reviewed control output DTOs may be serialized")
        return {
            field.name: _wire(getattr(value, field.name)) for field in dataclasses.fields(value)
        }
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise TypeError("control object keys must be strings")
        return {key: _wire(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return tuple(_wire(item) for item in value)
    if value is None or type(value) in {str, bool, int, float}:
        return freeze_json_value(value)
    raise TypeError("unsupported control output")


def control_response(request_id: object, data: object) -> JsonObject:
    from qq_ai_bot.domain.identity import RequestId

    if type(request_id) is not RequestId:
        raise TypeError("response requires original request id")
    # A raw dict, ORM row, request payload, Settings, Principal or exception is not an output DTO.
    if not dataclasses.is_dataclass(data) or isinstance(data, type):
        raise TypeError("response requires a control output DTO")
    problem = data if type(data) is Problem else None
    envelope: dict[str, JsonValue] = {
        "protocol_version": "control.v1",
        "request_id": request_id.text,
        "data": None if problem is not None else _wire(data),
        "problem": _wire(problem),
    }
    freeze_json_object(envelope)
    return envelope


def decode_command(body: object) -> ControlCommand:
    from qq_ai_bot.domain.identity import RequestId

    try:
        if not isinstance(body, Mapping) or set(body) != {
            "request_id",
            "expected_revision",
            "payload",
        }:
            raise ValueError("invalid command envelope")
        return ControlCommand(
            request_id=RequestId.parse(body["request_id"]),
            expected_revision=body["expected_revision"],
            payload=body["payload"],
        )
    except (TypeError, ValueError) as exc:
        raise ControlCommandError(Problem(ProblemCode.VALIDATION_ERROR)) from exc


def decode_page(body: object) -> PageRequest:
    try:
        if not isinstance(body, Mapping) or set(body) - {"limit", "cursor"}:
            raise ValueError("invalid page envelope")
        raw_cursor = body.get("cursor")
        return PageRequest(
            limit=body.get("limit", 20), cursor=None if raw_cursor is None else Cursor(raw_cursor)
        )
    except (TypeError, ValueError) as exc:
        raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR)) from exc
