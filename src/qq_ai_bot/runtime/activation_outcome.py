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

_JOURNAL_FAILURE_CODES = frozenset(
    {
        "work_journal_missing",
        "work_journal_corrupt",
        "work_journal_media_missing",
        "work_compaction_anchor_unavailable",
        "work_compaction_anchor_corrupt",
        "work_readonly_reuse_corrupt",
        "work_effect_media_corrupt",
        "work_effect_media_missing",
    }
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
    if failure.code == "WorkNoProgress":
        return {
            "work_start_not_delivered": "工作开始说明尚未确认送达，已暂停并保留已有结果。",
            "interactive_work_missing_exit": (
                "工作尚未给出明确的完成或等待决定，已暂停并保留已有结果。"
            ),
            "repeated_tool_results": "连续取得相同工具结果且没有进展，已暂停并保留已有结果。",
        }.get(
            str(failure.diagnostics.get("reason", "")),
            "连续执行没有取得进展，已暂停并保留已有结果。",
        )
    if failure.code == "sqlite_busy":
        return "数据存储暂时繁忙，本次处理未完成，请稍后重试。"
    if failure.code in {"database_failure", "sqlite_locked"}:
        return "数据存储出现异常，本次处理未完成，请联系管理员。"
    if failure.code == "context_boundary_changed":
        return "会话上下文已变化，本次处理已停止。"
    if failure.stage == "capacity":
        if failure.code in {"model_request_capacity", "prompt_dynamic_capacity"}:
            return (
                "本轮上下文超过容量限制，后续处理已停止；已有结果会保留，"
                "本次请求未完整完成。请缩小请求范围后再继续。"
            )
        return "本次处理达到容量限制，已停止继续执行；已有结果会保留，请联系管理员。"
    if failure.diagnostics.get("category") == "work_conflict":
        if failure.code == "work_journal_source_changed":
            return "会话资料在处理期间变化，已保留已有结果；请先核对任务状态。"
        return "工作状态发生冲突，已保留已有结果；请稍后核对状态。"
    if failure.code == "unsent_final_response":
        return "这次回复没有发出，请稍后重试。"
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
    if isinstance(exc, WorkNoProgress):
        reason = str(exc)
        return RuntimeFailure(
            "WorkNoProgress",
            "agent_output",
            diagnostics={"reason": reason}
            if reason
            in {
                "work_start_not_delivered",
                "interactive_work_missing_exit",
                "repeated_tool_results",
            }
            else {},
        )
    if isinstance(exc, ContextBoundaryChanged):
        return RuntimeFailure("context_boundary_changed", "context", True)
    from qq_ai_bot.prompting.compiler import PromptCapacityError
    from qq_ai_bot.runtime.work_journal import JournalUnavailable
    from qq_ai_bot.runtime.work_repository import WorkCapacityError, WorkConflict

    if isinstance(exc, JournalUnavailable):
        code = str(exc)
        return RuntimeFailure(
            code if code in _JOURNAL_FAILURE_CODES else "work_journal_unavailable", "journal"
        )

    if isinstance(exc, PromptCapacityError):
        return RuntimeFailure(
            "prompt_dynamic_capacity", "capacity", diagnostics={"category": "capacity"}
        )

    if isinstance(exc, WorkCapacityError):
        return RuntimeFailure(str(exc), "capacity", diagnostics={"category": "capacity"})

    if isinstance(exc, WorkConflict):
        return RuntimeFailure(
            exc.code,
            "context" if exc.code == "work_journal_source_changed" else stage,
            exc.code == "work_journal_source_changed",
            diagnostics={"category": "work_conflict"},
        )
    # An exact Presence lookup can fail before any gateway call when its live
    # connection drops. Restart the original activation after reconnection;
    # dispatching/unknown effects remain protected by their durable receipts.
    from qq_ai_bot.gateway.registry import RegistryClosed
    from qq_ai_bot.identity.routing import RouteSendError
    from qq_ai_bot.services.main_agent_backend import UnsentFinalResponseError

    if (
        isinstance(exc, RouteSendError)
        and isinstance(exc.__cause__, RegistryClosed)
        and exc.__cause__.category == "disconnected"
    ):
        return RuntimeFailure("gateway_disconnected", "gateway", True, "not_sent")
    if isinstance(exc, UnsentFinalResponseError):
        return RuntimeFailure("unsent_final_response", "agent_output", certainty="not_sent")
    if isinstance(exc, LLMError):
        return RuntimeFailure(
            type(exc).__name__,
            "provider",
            isinstance(exc, (LLMTimeoutError, LLMUnavailableError)),
            diagnostics=exc.diagnostics,
        )
    return RuntimeFailure(type(exc).__name__, stage)
