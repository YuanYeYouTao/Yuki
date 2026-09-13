"""Typed activation exits; working, waiting and delivering are distinct facts."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass, field
from enum import StrEnum

from sqlalchemy.exc import OperationalError, SQLAlchemyError

from qq_ai_bot.llm.base import (
    LLMError,
    LLMInvalidRequestError,
    LLMTimeoutError,
    LLMUnavailableError,
)


class ContextBoundaryChanged(LLMInvalidRequestError):
    """Reassemble at the explicit source boundary while preserving work and receipts."""


class ExitReason(StrEnum):
    ANSWER = "answer"
    COMPLETED = "completed"
    SEGMENT = "segment_budget"
    EXTERNAL = "waiting_external"
    INPUT = "waiting_input"
    RETRY = "retry"
    BUDGET = "root_budget"
    CAPACITY = "checkpoint_capacity"
    NO_PROGRESS = "no_progress"
    PAUSED = "paused"
    CANCELLED = "cancelled"


class DeliveryDeferred(RuntimeError):
    """The intent is durable but has definitely not entered the gateway."""

    def __init__(self, message: str, *, not_before: float = 0) -> None:
        super().__init__(message)
        self.not_before = not_before


class WorkActivationHandled(RuntimeError):
    """An owned activation has already committed its recovery decision."""


class SegmentBudgetReached(RuntimeError):
    """An auxiliary HTTP retry cannot borrow a request from the next activation."""


class WorkNoProgress(RuntimeError):
    """The model repeatedly fails to advance or explicitly manage the active work."""


class WorkRecoveryDeferred(RuntimeError):
    """Recovery persistence failed; durable intents and the expiring lease remain authoritative."""


@dataclass(frozen=True)
class RuntimeFailure:
    code: str
    stage: str
    retryable: bool = False
    certainty: str = "unknown"
    diagnostics: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ActivationOutcome:
    reason: ExitReason
    work_id: str | None = None
    failure: RuntimeFailure | None = None
    activation_id: str | None = None
    checkpoint_id: str | None = None
    pending_execution_ids: tuple[str, ...] = ()
    model_requests: int = 0
    tool_calls: int = 0


def classify_failure(exc: BaseException, stage: str = "activation") -> RuntimeFailure:
    if isinstance(exc, asyncio.CancelledError):
        return RuntimeFailure("activation_cancelled", "cleanup", True)
    if isinstance(exc, BaseExceptionGroup):
        failures = [classify_failure(item, stage) for item in exc.exceptions]
        return next((item for item in failures if not item.retryable), failures[0])
    if isinstance(exc, OperationalError):
        code = getattr(exc.orig, "sqlite_errorcode", 0)
        busy = (
            isinstance(code, int) and code & 255 in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
        ) or str(exc.orig).lower() in {"database is locked", "database table is locked"}
        return RuntimeFailure("sqlite_busy" if busy else "database_failure", stage, busy)
    if isinstance(exc, SQLAlchemyError):
        return RuntimeFailure("database_failure", stage)
    if isinstance(exc, ContextBoundaryChanged):
        return RuntimeFailure("context_boundary_changed", "context", True)
    if isinstance(exc, LLMError):
        return RuntimeFailure(
            type(exc).__name__,
            "provider",
            isinstance(exc, (LLMTimeoutError, LLMUnavailableError)),
            diagnostics=exc.diagnostics,
        )
    if isinstance(exc, DeliveryDeferred):
        return RuntimeFailure("delivery_deferred", "delivery", False, "not_dispatched")
    return RuntimeFailure(type(exc).__name__, stage)
