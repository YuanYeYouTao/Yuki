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


def failure_status_text(failure: RuntimeFailure) -> str:
    """Operational status when no owned activation can recover; never provider details."""
    if failure.code == "sqlite_busy":
        return "数据存储暂时繁忙，本次处理未完成，请稍后重试。"
    if failure.code == "database_failure":
        return "数据存储出现异常，本次处理未完成，请联系管理员。"
    if failure.code == "context_boundary_changed":
        return "会话上下文已变化，本次处理已停止。"
    if failure.stage == "provider":
        if failure.code in {"LLMAuthenticationError", "LLMConfigurationError"}:
            return "AI 服务配置或认证异常，请联系管理员。"
        if failure.code in {"LLMInvalidRequestError", "LLMUnsupportedFeatureError"}:
            return "模型请求或功能配置不兼容，请联系管理员。"
        if failure.code == "LLMTimeoutError":
            return "模型响应超时，本次处理未完成，请稍后重试。"
        if failure.retryable:
            return "AI 服务暂时不可用，请稍后重试。"
        return "模型未能完成这次回复，请稍后重试。"
    return "这次处理遇到内部错误，请稍后重试；持续出现请联系管理员。"


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
    return RuntimeFailure(type(exc).__name__, stage)
