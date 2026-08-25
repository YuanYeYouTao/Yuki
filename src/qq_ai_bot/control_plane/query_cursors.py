"""Internal keyset cursor codec. Public callers still see an opaque Cursor."""

from __future__ import annotations

from datetime import datetime
from typing import Final

from qq_ai_bot.control_plane.operations import StateEpoch
from qq_ai_bot.control_plane.paging import Cursor
from qq_ai_bot.control_plane.problems import Problem, ProblemCode
from qq_ai_bot.control_plane.query_types import (
    QUERY_CURSOR_VERSION,
    ControlQueryError,
    QueryCursorPhase,
    QueryResourceKind,
)
from qq_ai_bot.control_plane.tokens import require_aware_datetime, require_opaque_token

_SEPARATOR = "|"
TWO_PHASE_RESOURCE_KINDS: Final[frozenset[QueryResourceKind]] = frozenset(
    {
        QueryResourceKind.PERSON,
        QueryResourceKind.BINDING,
        QueryResourceKind.SPACE,
        QueryResourceKind.SPACE_BINDING,
        QueryResourceKind.CONVERSATION,
    }
)
CANONICAL_RESOURCE_KINDS: Final[frozenset[QueryResourceKind]] = frozenset(
    {
        QueryResourceKind.PRESENCE,
        QueryResourceKind.PERSON_ROUTE,
        QueryResourceKind.INGEST_ROUTE,
        QueryResourceKind.SPACE_ROUTE,
        QueryResourceKind.OPERATION,
        QueryResourceKind.CONFLICT,
        QueryResourceKind.CONFIG,
        QueryResourceKind.MEMORY_FACT,
        QueryResourceKind.MEMORY_JOB,
        QueryResourceKind.AUTOMATION,
        QueryResourceKind.PLUGIN,
        QueryResourceKind.MCP,
        QueryResourceKind.EMOJI,
        QueryResourceKind.SPEECH,
    }
)
TIME_ID_RESOURCE_KINDS: Final[frozenset[QueryResourceKind]] = frozenset({QueryResourceKind.AUDIT})
if TWO_PHASE_RESOURCE_KINDS | CANONICAL_RESOURCE_KINDS | TIME_ID_RESOURCE_KINDS != frozenset(
    QueryResourceKind
):
    raise ValueError("cursor phase table must cover every QueryResourceKind")


def allowed_cursor_phases(
    kind: QueryResourceKind,
    *,
    epoch: StateEpoch,
) -> frozenset[QueryCursorPhase]:
    if type(kind) is not QueryResourceKind:
        raise TypeError("kind must be QueryResourceKind")
    if type(epoch) is not StateEpoch:
        raise TypeError("epoch must be StateEpoch")
    if kind in TIME_ID_RESOURCE_KINDS:
        return frozenset({QueryCursorPhase.TIME_ID})
    if kind in CANONICAL_RESOURCE_KINDS:
        return frozenset({QueryCursorPhase.CANONICAL})
    if epoch is StateEpoch.V2:
        return frozenset({QueryCursorPhase.CANONICAL})
    return frozenset({QueryCursorPhase.CANONICAL, QueryCursorPhase.UNRESOLVED})


def encode_query_cursor(
    kind: QueryResourceKind,
    phase: QueryCursorPhase,
    key: str,
) -> Cursor:
    if type(kind) is not QueryResourceKind:
        raise TypeError("kind must be QueryResourceKind")
    if type(phase) is not QueryCursorPhase:
        raise TypeError("phase must be QueryCursorPhase")
    token = require_opaque_token(key, name="cursor_key", max_length=200)
    return Cursor(_SEPARATOR.join((QUERY_CURSOR_VERSION, kind.value, phase.value, token)))


def decode_query_cursor(
    cursor: Cursor,
    *,
    expected_kind: QueryResourceKind,
) -> tuple[QueryCursorPhase, str]:
    if type(cursor) is not Cursor:
        raise TypeError("cursor must be Cursor")
    if type(expected_kind) is not QueryResourceKind:
        raise TypeError("expected_kind must be QueryResourceKind")
    raw = cursor.value
    parts = raw.split(_SEPARATOR, 3)
    if len(parts) != 4:
        raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))
    version, kind_token, phase_token, key = parts
    if version != QUERY_CURSOR_VERSION:
        raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))
    try:
        kind = QueryResourceKind(kind_token)
        phase = QueryCursorPhase(phase_token)
        require_opaque_token(key, name="cursor_key", max_length=200)
    except (TypeError, ValueError) as exc:
        raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR)) from exc
    if kind is not expected_kind:
        raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))
    return phase, key


def decode_integer_cursor_key(key: str, *, minimum: int) -> int:
    """Strict decimal integer. Malformed or below minimum is validation_error."""

    if type(minimum) is not int or type(minimum) is bool:
        raise TypeError("minimum must be an int")
    try:
        token = require_opaque_token(key, name="cursor_key", max_length=200)
    except (TypeError, ValueError) as exc:
        raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR)) from exc
    if not token.isascii() or not token.isdigit():
        raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))
    try:
        value = int(token)
    except ValueError as exc:
        raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR)) from exc
    if token != str(value) or value < minimum:
        raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))
    return value


def encode_operation_cursor_key(created_at: datetime, kind: int, local_id: int) -> str:
    """Reversible keyset: full-precision created_at + kind + complete local id."""

    stamp = require_aware_datetime(created_at, name="created_at").isoformat()
    if type(kind) is bool or type(kind) is not int or kind < 1:
        raise ValueError("kind must be a positive int")
    if type(local_id) is bool or type(local_id) is not int or local_id < 1:
        raise ValueError("local_id must be a positive int")
    return f"{stamp}#{kind}#{local_id}"


def decode_operation_cursor_key(key: str) -> tuple[datetime, int, int]:
    try:
        token = require_opaque_token(key, name="cursor_key", max_length=200)
    except (TypeError, ValueError) as exc:
        raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR)) from exc
    stamp, first, rest = token.partition("#")
    kind_token, second, raw_id = rest.partition("#")
    if first != "#" or second != "#" or not stamp or not kind_token or not raw_id:
        raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))
    kind = decode_integer_cursor_key(kind_token, minimum=1)
    local_id = decode_integer_cursor_key(raw_id, minimum=1)
    try:
        created_at = datetime.fromisoformat(stamp)
        require_aware_datetime(created_at, name="created_at")
    except (TypeError, ValueError) as exc:
        raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR)) from exc
    return created_at, kind, local_id


def encode_time_id_key(created_at: datetime, row_id: int) -> str:
    stamp = require_aware_datetime(created_at, name="created_at").isoformat()
    if type(row_id) is not int or type(row_id) is bool or row_id < 1:
        raise ValueError("row_id must be a positive int")
    return f"{stamp}#{row_id}"


def decode_time_id_key(key: str) -> tuple[datetime, int]:
    try:
        token = require_opaque_token(key, name="cursor_key", max_length=200)
    except (TypeError, ValueError) as exc:
        raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR)) from exc
    stamp, separator, raw_id = token.rpartition("#")
    if separator != "#" or not stamp:
        raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))
    row_id = decode_integer_cursor_key(raw_id, minimum=1)
    try:
        created_at = datetime.fromisoformat(stamp)
        require_aware_datetime(created_at, name="created_at")
    except (TypeError, ValueError) as exc:
        raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR)) from exc
    return created_at, row_id


def decode_resource_cursor(
    cursor: Cursor,
    *,
    expected_kind: QueryResourceKind,
    epoch: StateEpoch,
) -> tuple[QueryCursorPhase, str]:
    phase, key = decode_query_cursor(cursor, expected_kind=expected_kind)
    if phase not in allowed_cursor_phases(expected_kind, epoch=epoch):
        raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))
    if expected_kind is QueryResourceKind.AUDIT:
        decode_time_id_key(key)
    elif expected_kind is QueryResourceKind.OPERATION:
        decode_operation_cursor_key(key)
    elif expected_kind in {
        QueryResourceKind.CONFLICT,
        QueryResourceKind.MEMORY_FACT,
        QueryResourceKind.MEMORY_JOB,
        QueryResourceKind.AUTOMATION,
    }:
        decode_integer_cursor_key(key, minimum=1)
    elif expected_kind in TWO_PHASE_RESOURCE_KINDS and phase is QueryCursorPhase.UNRESOLVED:
        decode_integer_cursor_key(key, minimum=0)
    return phase, key
