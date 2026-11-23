"""Read-only watchdog facts for explicit model fallback chains."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from yuki.model_runtime.executor import TaskModelExecutor
    from yuki.model_runtime.repository import ModelInvocationRepository


async def model_fallback_facts(
    executor: TaskModelExecutor, invocations: ModelInvocationRepository
) -> dict[str, dict[str, object]]:
    """Report, per task with a configured chain, whether its latest success was a fallback.

    State only, no time window: the head's next success clears it. A task without a
    success yet, or whose latest success is outside the current chain, is undetermined.
    """
    facts: dict[str, dict[str, object]] = {}
    for task, route in executor.router.catalog.routes.items():
        if not route.fallback_profile_ids:
            continue
        latest = await invocations.latest_success_profile(task)
        facts[f"model_fallback:{task.value}"] = {
            "healthy": True
            if latest == route.profile_id
            else False
            if latest in route.fallback_profile_ids
            else None,
            "state": latest or "unknown",
            "durable": True,
        }
    return facts
