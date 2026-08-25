"""Control command and result DTOs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import final

from qq_ai_bot.control_plane.json_types import JsonObject, freeze_json_object
from qq_ai_bot.control_plane.operations import OperationRef
from qq_ai_bot.control_plane.tokens import MAX_RESOURCE_TOKEN_LENGTH, require_opaque_token
from qq_ai_bot.domain.identity import RequestId


@final
@dataclass(frozen=True, slots=True)
class ControlCommand:
    """Idempotent mutation envelope. Payload is JSON-compatible only."""

    request_id: RequestId
    expected_revision: int
    payload: JsonObject

    def __init__(
        self,
        *,
        request_id: RequestId,
        expected_revision: int,
        payload: object,
    ) -> None:
        if type(request_id) is not RequestId:
            raise TypeError("request_id must be RequestId")
        if type(expected_revision) is bool or type(expected_revision) is not int:
            raise TypeError("expected_revision must be an int")
        if expected_revision < 0:
            raise ValueError("expected_revision must be non-negative")
        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "expected_revision", expected_revision)
        object.__setattr__(self, "payload", freeze_json_object(payload))


@final
@dataclass(frozen=True, slots=True)
class ControlResult:
    """Synchronous command outcome with optional long-running operation."""

    success: bool
    resource_id: str
    revision: int
    audit_id: str
    effective_state: JsonObject
    operation: OperationRef | None = None

    def __init__(
        self,
        *,
        success: bool,
        resource_id: str,
        revision: int,
        audit_id: str,
        effective_state: object,
        operation: OperationRef | None = None,
    ) -> None:
        if type(success) is not bool:
            raise TypeError("success must be a bool")
        resource_id = require_opaque_token(
            resource_id, name="resource_id", max_length=MAX_RESOURCE_TOKEN_LENGTH
        )
        audit_id = require_opaque_token(
            audit_id, name="audit_id", max_length=MAX_RESOURCE_TOKEN_LENGTH
        )
        if type(revision) is bool or type(revision) is not int:
            raise TypeError("revision must be an int")
        if revision < 0:
            raise ValueError("revision must be non-negative")
        if operation is not None and type(operation) is not OperationRef:
            raise TypeError("operation must be OperationRef or None")
        object.__setattr__(self, "success", success)
        object.__setattr__(self, "resource_id", resource_id)
        object.__setattr__(self, "revision", revision)
        object.__setattr__(self, "audit_id", audit_id)
        object.__setattr__(self, "effective_state", freeze_json_object(effective_state))
        object.__setattr__(self, "operation", operation)
