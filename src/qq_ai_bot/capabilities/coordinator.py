"""Provider-neutral ordered execution for one model tool-call batch."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from qq_ai_bot.capabilities.invocation import Invocation, direct_invocations
from qq_ai_bot.domain.messages import ToolCall
from qq_ai_bot.execution_trace.recorder import trace_span
from qq_ai_bot.services.invocation_service import BatchPlan, InvocationService

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
    async def execute_call(self, invocation: Invocation) -> str: ...

    def parallel_safe(self, name: str, runtime: Any) -> bool: ...

    def is_side_effecting(self, name: str, arguments: str, runtime: Any) -> bool:
        return True

    def counts_toward_limit(self, name: str, runtime: Any) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class CoordinatedToolResult:
    calls: tuple[tuple[ToolCall, str, bool], ...]
    executed_count: int
    reused_count: int = 0
    evidence: dict[str, dict[str, Any]] = field(default_factory=dict)


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
        chain_id: str = "",
        request_sequence: int = 0,
        manifest_revision: str = "",
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
            return backend.counts_toward_limit(call.function.name, runtime)

        overflow_ids: set[str] = set()
        rejected_ids: set[str] = set()
        counted_executions = 0
        results: dict[str, str] = {}
        facts: dict[str, dict[str, Any]] = {}
        plan = BatchPlan.prepare(calls)
        invocations = {
            invocation.call.id: invocation
            for invocation in direct_invocations(
                calls,
                runtime,
                chain_id=chain_id,
                request_sequence=request_sequence,
                manifest_revision=manifest_revision,
            )
        }
        service = InvocationService()

        async def admit(call: ToolCall) -> bool:
            nonlocal counted_executions
            if call.id in plan.conflicting_ids:
                results[call.id] = json.dumps(
                    {"ok": False, "executed": False, "error": "duplicate_provider_call_id"}
                )
                rejected_ids.add(call.id)
                return False
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
                return await backend.execute_call(invocations[call.id])

            side_effecting = backend.is_side_effecting(
                call.function.name, call.function.arguments, runtime
            )
            from qq_ai_bot.runtime.effect_outcomes import (
                ResultCapture,
                current_result_capture,
                execution_evidence,
            )

            capture = ResultCapture("", invocations[call.id].identity.operation_id)
            token = current_result_capture.set(capture)
            try:
                results[call.id] = await service.invoke(
                    invocations[call.id], invoke, side_effecting=side_effecting
                )
            finally:
                current_result_capture.reset(token)
            if capture.evidence is not None:
                facts[call.id] = capture.evidence
            elif capture.outcome is not None:
                facts[call.id] = execution_evidence(
                    capture.outcome,
                    tool=call.function.name,
                    side_effecting=side_effecting,
                    arguments=call.function.arguments,
                )
            if facts.get(call.id, {}).get("executed") is False:
                rejected_ids.add(call.id)
                if counts_toward_limit(call):
                    counted_executions -= 1

        def is_parallel_safe(call: ToolCall) -> bool:
            # Delivery must finish before a subsequent read-safe stretch can start.
            if call.function.name == "send_message":
                return False
            return backend.parallel_safe(call.function.name, runtime)

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
            evidence=facts,
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
