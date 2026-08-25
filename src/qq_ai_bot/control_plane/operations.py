"""Long-running operation projection. This is not a generic executor."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import final

from qq_ai_bot.control_plane.problems import ProblemCode
from qq_ai_bot.control_plane.tokens import require_aware_datetime

_ERROR_CATEGORY = re.compile(r"\A[a-z][a-z0-9_]{0,63}\Z")
_OPERATION_ID = re.compile(r"\A[A-Za-z0-9._:-]{1,128}\Z")


@final
class OperationStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@final
class StateEpoch(StrEnum):
    V1 = "v1"
    V2 = "v2"


def _sanitize_error_category(value: object) -> str:
    if type(value) is ProblemCode:
        return value.value
    if type(value) is not str:
        raise TypeError("error_category must be a sanitized token")
    token = value.strip().casefold()
    if not token or _ERROR_CATEGORY.fullmatch(token) is None:
        raise ValueError("error_category must be a sanitized token")
    return token


@final
@dataclass(frozen=True, slots=True)
class OperationRef:
    """Projection of an existing backfill/rebuild/dream/automation run."""

    operation_id: str
    status: OperationStatus
    progress: float
    state_epoch: StateEpoch
    error_category: str | None
    created_at: datetime
    updated_at: datetime

    def __init__(
        self,
        *,
        operation_id: str,
        status: OperationStatus,
        progress: float,
        state_epoch: StateEpoch,
        error_category: str | ProblemCode | None = None,
        created_at: datetime,
        updated_at: datetime,
    ) -> None:
        if type(operation_id) is not str:
            raise TypeError("operation_id must be a str")
        if _OPERATION_ID.fullmatch(operation_id) is None:
            raise ValueError("operation_id must be a sanitized token")
        if type(status) is not OperationStatus:
            raise TypeError("status must be OperationStatus")
        if type(state_epoch) is not StateEpoch:
            raise TypeError("state_epoch must be StateEpoch")
        if type(progress) is bool or type(progress) not in (int, float):
            raise TypeError("progress must be a real number")
        numeric = float(progress)
        if not math.isfinite(numeric):
            raise ValueError("progress must be finite")
        if numeric < 0.0 or numeric > 1.0:
            raise ValueError("progress must be between 0 and 1")
        created = require_aware_datetime(created_at, name="created_at")
        updated = require_aware_datetime(updated_at, name="updated_at")
        try:
            if updated < created:
                raise ValueError("updated_at must not precede created_at")
        except TypeError as exc:
            raise ValueError("updated_at must be comparable to created_at") from exc
        sanitized: str | None
        if error_category is None:
            sanitized = None
        else:
            sanitized = _sanitize_error_category(error_category)
        if status is OperationStatus.SUCCEEDED and sanitized is not None:
            raise ValueError("succeeded cannot carry an error_category")
        if status is OperationStatus.FAILED and sanitized is None:
            raise ValueError("failed requires a sanitized error_category")
        if status in {OperationStatus.QUEUED, OperationStatus.RUNNING, OperationStatus.CANCELLED}:
            if sanitized is not None:
                raise ValueError(f"{status.value} cannot carry an error_category")
        object.__setattr__(self, "operation_id", operation_id)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "progress", numeric)
        object.__setattr__(self, "state_epoch", state_epoch)
        object.__setattr__(self, "error_category", sanitized)
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "updated_at", updated)
