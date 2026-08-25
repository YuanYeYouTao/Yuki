"""Frozen control-plane problem codes. User-facing text stays in renderers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final, final

from qq_ai_bot.control_plane.json_types import JsonObject, freeze_json_object


@final
class ProblemCode(StrEnum):
    """Stable machine codes. Values are the public protocol tokens."""

    UNAUTHENTICATED = "unauthenticated"
    CAPABILITY_DENIED = "capability_denied"
    NOT_FOUND = "not_found"
    VALIDATION_ERROR = "validation_error"
    VERSION_CONFLICT = "version_conflict"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    BINDING_AMBIGUOUS = "binding_ambiguous"
    ROUTE_AMBIGUOUS = "route_ambiguous"
    ROUTE_PAUSED = "route_paused"
    POPULATED_MERGE_FORBIDDEN = "populated_merge_forbidden"
    STATE_MISMATCH = "state_mismatch"
    PRECONDITION_FAILED = "precondition_failed"
    SECRET_NOT_READABLE = "secret_not_readable"
    OPERATION_UNAVAILABLE = "operation_unavailable"


FROZEN_PROBLEM_CODES: Final[frozenset[str]] = frozenset(code.value for code in ProblemCode)


@final
@dataclass(frozen=True, slots=True)
class Problem:
    """Machine-readable denial. No localized or Chinese prompt field."""

    code: ProblemCode
    details: JsonObject

    def __init__(
        self,
        code: ProblemCode,
        details: JsonObject | None = None,
    ) -> None:
        if type(code) is not ProblemCode:
            raise TypeError("code must be ProblemCode")
        object.__setattr__(self, "code", code)
        object.__setattr__(self, "details", freeze_json_object({} if details is None else details))
