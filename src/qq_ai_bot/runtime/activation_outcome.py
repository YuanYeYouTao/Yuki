"""Typed activation exits; working, waiting and delivering are distinct facts."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from qq_ai_bot.runtime.work_repository import WorkLease

from sqlalchemy.exc import OperationalError, SQLAlchemyError

from qq_ai_bot.llm.base import (
    LLMEmptyResponseError,
    LLMError,
    LLMInvalidRequestError,
    LLMMalformedFunctionCallError,
    LLMTimeoutError,
    LLMUnavailableError,
)

_JOURNAL_FAILURE_CODES = frozenset(
    {
        "work_journal_missing",
        "work_response_not_persisted",
        "work_journal_corrupt",
        "work_journal_media_missing",
        "work_compaction_anchor_corrupt",
        "work_readonly_reuse_corrupt",
        "work_effect_media_corrupt",
        "work_effect_media_missing",
    }
)


class ContextBoundaryChanged(LLMInvalidRequestError):
    """Reassemble at the explicit source boundary while preserving work and receipts."""


class ExitReason(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    SEGMENT = "segment_budget"
    EXTERNAL = "waiting_external"
    INPUT = "waiting_input"
    RETRY = "retry"
    BUDGET = "root_budget"
    CAPACITY = "checkpoint_capacity"
    PAUSED = "paused"
    CANCELLED = "cancelled"


class WorkActivationHandled(RuntimeError):
    """An owned activation has already committed its recovery decision."""


class SegmentBudgetReached(RuntimeError):
    """An auxiliary HTTP retry cannot borrow a request from the next activation."""


class WorkRecoveryDeferred(RuntimeError):
    """The exited activation requires a new, valid owner to settle its failure."""

    def __init__(
        self, message: str, *, lease: WorkLease | None = None, work: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.lease = lease
        self.work = dict(work) if work is not None else None


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
    from qq_ai_bot.services.concurrency import RequestCancelledError

    if isinstance(exc, (asyncio.CancelledError, RequestCancelledError)):
        return RuntimeFailure(
            "activation_cancelled", "cleanup", True, diagnostics=getattr(exc, "diagnostics", {})
        )
    if isinstance(exc, BaseExceptionGroup):
        failures = [classify_failure(item, stage) for item in exc.exceptions]
        return next((item for item in failures if not item.retryable), failures[0])
    if isinstance(exc, (OperationalError, sqlite3.Error)):
        original = exc.orig if isinstance(exc, OperationalError) else exc
        code = getattr(original, "sqlite_errorcode", None)
        # Text alone does not identify a retryable writer conflict. In particular,
        # LOCKED is not BUSY, even when a driver describes both as a lock error.
        if isinstance(code, int) and not isinstance(code, bool):
            primary = code & 255
            if primary in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                busy = primary == sqlite3.SQLITE_BUSY
                return RuntimeFailure(
                    "sqlite_busy" if busy else "sqlite_locked",
                    stage,
                    busy,
                    diagnostics={"sqlite_errorcode": code},
                )
        return RuntimeFailure("database_failure", stage)
    if isinstance(exc, SQLAlchemyError):
        return RuntimeFailure("database_failure", stage)
    if isinstance(exc, ContextBoundaryChanged):
        return RuntimeFailure("context_boundary_changed", "context", True)
    from qq_ai_bot.runtime.work_journal import JournalUnavailable
    from qq_ai_bot.runtime.work_repository import WorkCapacityError, WorkConflict

    if isinstance(exc, JournalUnavailable):
        code = str(exc)
        return RuntimeFailure(
            code if code in _JOURNAL_FAILURE_CODES else "work_journal_unavailable", "journal"
        )

    if isinstance(exc, WorkCapacityError):
        return RuntimeFailure(str(exc), "capacity", diagnostics={"category": "capacity"})

    if isinstance(exc, WorkConflict):
        return RuntimeFailure(
            exc.code,
            "context" if exc.code == "work_journal_source_changed" else stage,
            exc.code in {"work_journal_source_changed", "work_activation_interrupted"},
            diagnostics={"category": "work_conflict"},
        )
    # An exact Presence lookup can fail before any gateway call when its live
    # connection drops. Restart the original activation after reconnection;
    # dispatching/unknown effects remain protected by their durable receipts.
    from qq_ai_bot.gateway.registry import RegistryClosed
    from qq_ai_bot.identity.routing import RouteSendError

    if (
        isinstance(exc, RouteSendError)
        and isinstance(exc.__cause__, RegistryClosed)
        and exc.__cause__.category == "disconnected"
    ):
        return RuntimeFailure("gateway_disconnected", "gateway", True, "not_sent")
    if isinstance(exc, LLMError):
        return RuntimeFailure(
            type(exc).__name__,
            "provider",
            isinstance(
                exc,
                (
                    LLMTimeoutError,
                    LLMUnavailableError,
                    LLMEmptyResponseError,
                    LLMMalformedFunctionCallError,
                ),
            ),
            diagnostics=exc.diagnostics,
        )
    return RuntimeFailure(type(exc).__name__, stage)
