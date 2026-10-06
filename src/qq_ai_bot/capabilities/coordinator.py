"""Provider-neutral ordered execution for one model tool-call batch."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from qq_ai_bot.domain.messages import ToolCall
from qq_ai_bot.execution_trace.recorder import trace_span

logger = logging.getLogger(__name__)
TOOL_RESULT_MISSING = "tool_result_missing"
MISSING_TOOL_RESULT = json.dumps(
    {
        "ok": False,
        "error": TOOL_RESULT_MISSING,
        "detail": "工具批次缺少这次调用的回执，已跳过而不是中断整轮。",
    },
    ensure_ascii=False,
)


class CoordinatedToolBackend(Protocol):
    async def execute(self, name: str, arguments_json: str, runtime: Any) -> str: ...

    def parallel_safe(self, name: str, runtime: Any) -> bool: ...


@dataclass(frozen=True, slots=True)
class CoordinatedToolResult:
    calls: tuple[tuple[ToolCall, str, bool], ...]
    executed_count: int
    reused_count: int = 0


class ToolInvocationCoordinator:
    """Run read-safe stretches concurrently while preserving model call order."""

    async def execute_batch(
        self,
        calls: tuple[ToolCall, ...],
        backend: CoordinatedToolBackend | None,
        runtime: Any,
        *,
        remaining_calls: int,
        max_parallel_calls: int,
        before_execute: Callable[[ToolCall], Awaitable[str | None]] | None = None,
    ) -> CoordinatedToolResult:
        if remaining_calls < 0 or max_parallel_calls <= 0:
            raise ValueError("tool call budgets must be non-negative and parallelism positive")
        if backend is None:
            unavailable = json.dumps(
                {
                    "ok": False,
                    "error": "tools_unavailable",
                    "executed": False,
                    "mutation_committed": False,
                },
                ensure_ascii=False,
            )
            return CoordinatedToolResult(
                tuple((call, unavailable, False) for call in calls),
                0,
            )

        def counts_toward_limit(call: ToolCall) -> bool:
            check = getattr(backend, "counts_toward_limit", None)
            return not callable(check) or bool(check(call.function.name, runtime))

        overflow_ids: set[str] = set()
        rejected_ids: set[str] = set()
        counted_executions = 0
        results: dict[str, str] = {}

        async def admit(call: ToolCall) -> bool:
            nonlocal counted_executions
            if before_execute is not None:
                rejection = await before_execute(call)
                if rejection is not None:
                    results[call.id] = rejection
                    rejected_ids.add(call.id)
                    return False
            counted = counts_toward_limit(call)
            if counted and counted_executions >= remaining_calls:
                overflow_ids.add(call.id)
                return False
            if counted:
                counted_executions += 1
            return True

        semaphore = asyncio.Semaphore(max_parallel_calls)

        async def execute_one(call: ToolCall) -> None:
            async with semaphore:
                async with trace_span("tool", {"call": asdict(call)}) as span:
                    await execute_recorded(call)
                    span.result = results[call.id]

        async def execute_recorded(call: ToolCall) -> None:
            nonlocal counted_executions

            async def invoke() -> str:
                return await backend.execute(call.function.name, call.function.arguments, runtime)

            control = getattr(runtime, "work_control", None)
            session = getattr(control, "session", None)
            check_effect = getattr(backend, "is_side_effecting", None)
            side_effecting = not callable(check_effect) or bool(
                check_effect(
                    call.function.name,
                    call.function.arguments,
                    runtime,
                )
            )
            results[call.id] = (
                await session.execute(
                    call,
                    invoke,
                    side_effecting=side_effecting,
                    allow_pending=call.function.name == "send_message",
                )
                if session
                else await invoke()
            )
            try:
                receipt = json.loads(results[call.id])
            except ValueError:
                receipt = None
            if isinstance(receipt, dict) and receipt.get("executed") is False:
                rejected_ids.add(call.id)
                if counts_toward_limit(call):
                    counted_executions -= 1

        def is_parallel_safe(call: ToolCall) -> bool:
            # Delivery must finish before a subsequent read-safe stretch can start.
            if call.function.name == "send_message":
                return False
            check = getattr(backend, "parallel_safe", None)
            return bool(callable(check) and check(call.function.name, runtime))

        index = 0
        while index < len(calls):
            call = calls[index]
            if not is_parallel_safe(call):
                if await admit(call):
                    await execute_one(call)
                index += 1
                continue
            end = index + 1
            while end < len(calls) and is_parallel_safe(calls[end]):
                end += 1
            admitted = [candidate for candidate in calls[index:end] if await admit(candidate)]
            async with asyncio.TaskGroup() as group:
                for candidate in admitted:
                    group.create_task(execute_one(candidate))
            index = end

        limited = json.dumps(
            {
                "ok": False,
                "error": "tool_limit_exceeded",
                "executed": False,
                "mutation_committed": False,
            },
            ensure_ascii=False,
        )
        return CoordinatedToolResult(
            _attach_batch_results(
                calls,
                results=results,
                overflow_ids=overflow_ids,
                limited=limited,
                rejected_ids=rejected_ids,
            ),
            counted_executions,
        )


def _attach_batch_results(
    calls: tuple[ToolCall, ...],
    *,
    results: dict[str, str],
    overflow_ids: set[str],
    limited: str,
    rejected_ids: set[str] | None = None,
) -> tuple[tuple[ToolCall, str, bool], ...]:
    """Map each model call id back to a payload without raising on a missing key."""

    ordered: list[tuple[ToolCall, str, bool]] = []
    for call in calls:
        if call.id in overflow_ids:
            ordered.append((call, limited, False))
            continue
        payload = results.get(call.id)
        if payload is None:
            logger.error("tool_result_missing call_id=%s", call.id)
            ordered.append((call, MISSING_TOOL_RESULT, False))
            continue
        ordered.append((call, payload, call.id not in (rejected_ids or set())))
    return tuple(ordered)
