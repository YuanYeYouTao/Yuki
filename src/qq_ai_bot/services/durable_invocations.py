"""Own durable synchronous invocations and replay their original saved result."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import replace

from qq_ai_bot.domain.messages import ChatMessage
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.runtime.activation_tasks import ActivationTasks
from qq_ai_bot.services.agent_runner import AgentRunResult, AgentRuntime, AgentToolBackend

PreparedRun = Callable[
    [tuple[ChatMessage, ...], AgentRuntime, AgentToolBackend | None], Awaitable[AgentRunResult]
]


class DurableInvocations:
    def __init__(
        self, database: Database, execute: PreparedRun, executions: ActivationTasks
    ) -> None:
        self.database = database
        self._execute = execute
        self.executions = executions

    async def run(
        self,
        messages: tuple[ChatMessage, ...],
        runtime: AgentRuntime,
        backend: AgentToolBackend | None,
    ) -> AgentRunResult:
        with self.executions.track():
            if runtime.canonical_conversation_id is None:
                raise ValueError("invocation_conversation_required")
            from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
            from qq_ai_bot.runtime.work_activation import activate_work
            from qq_ai_bot.runtime.work_repository import WorkConflict, WorkRepository

            database = self.database
            async with database.sessions() as session:
                conversation = await session.get(
                    CanonicalConversationModel, runtime.canonical_conversation_id
                )
                if conversation is None:
                    raise WorkConflict("work_conversation_unavailable")
                generation = conversation.generation
            if not runtime.execution_id:
                raise WorkConflict("invocation_execution_id_required")
            boundary = invocation_boundary(runtime)

            async def validate() -> None:
                if runtime.before_model_request is not None:
                    await runtime.before_model_request()

            repository = WorkRepository(database)
            previous = await repository.by_source(f"invocation:{boundary}")
            if previous is not None:
                await validate()
                prior_source = json.loads(previous["source_json"])
                requested_source = runtime.invocation_source or {}
                if prior_source.get("owner") == "plugin_invocation" and (
                    prior_source.get("approval_revision")
                    != requested_source.get("approval_revision")
                    or prior_source.get("plugin_id") != requested_source.get("plugin_id")
                ):
                    raise WorkConflict("plugin_work_authority_changed")
                from sqlalchemy import select

                from qq_ai_bot.runtime.work_recovery_schema import recovery

                async with database.sessions() as session:
                    wait_until = await session.scalar(
                        select(recovery.c.not_before).where(recovery.c.work_id == previous["id"])
                    )
                import time

                if (
                    previous["generation"] != generation
                    or previous["state"]
                    in {"suspended", "failed", "cancelled", "waiting_user", "waiting_external"}
                    or (wait_until and wait_until > time.time())
                ):
                    return AgentRunResult(
                        text="",
                        tool_calls_used=0,
                        model_requests=0,
                        web_was_used=False,
                        suppress_delivery=True,
                        work_state="cancelled"
                        if previous["generation"] != generation
                        else previous["state"],
                        work_id=previous["id"],
                    )
            if (
                previous is not None
                and previous["generation"] == generation
                and previous["state"] == "completed"
            ):
                await validate()
                saved = json.loads(previous["checkpoint_json"])
                if saved.get("archived"):
                    return AgentRunResult(
                        text="",
                        tool_calls_used=0,
                        model_requests=0,
                        web_was_used=False,
                        work_id=previous["id"],
                        work_state="archived",
                        suppress_delivery=True,
                    )
                if isinstance(saved.get("sync_result"), str):
                    return AgentRunResult(
                        text=saved["sync_result"],
                        suppress_delivery=bool(saved.get("sync_suppress_delivery", False)),
                        tool_calls_used=0,
                        model_requests=0,
                        web_was_used=False,
                        work_state="completed",
                        work_id=previous["id"],
                    )

            # Synchronous plugin/automation calls return to their owning step.
            async with activate_work(
                repository,
                runtime.canonical_conversation_id,
                generation,
                f"invocation:{boundary}",
                {
                    **(runtime.invocation_source or {}),
                    "origin": runtime.origin.value,
                    "actor_user_id": runtime.actor_user_id,
                    "execution_boundary": boundary,
                    "parent_execution_id": runtime.execution_id,
                    "delivery_contract": "return_to_caller",
                },
                validate,
            ) as bounded:
                if bounded.current is None and runtime.invocation_goal:
                    await bounded.execute(
                        "task_control",
                        {
                            "action": "accept",
                            "goal": runtime.invocation_goal,
                            "output_kind": "answer",
                            "deliver_artifacts": False,
                        },
                        "host-invocation-admission",
                    )
                result = await self._execute(
                    messages, replace(runtime, work_control=bounded), backend
                )
                bounded.final_delivery = bounded.ending == "completed"
                if bounded.current is not None:
                    if result.suppress_delivery:
                        stored = json.loads(bounded.current["checkpoint_json"])
                        if isinstance(stored.get("sync_result"), str):
                            result = replace(
                                result, text=stored["sync_result"], suppress_delivery=False
                            )
                    if bounded.ending == "completed" and bounded.final_delivery:
                        await repository.checkpoint(
                            bounded.lease,
                            bounded.current["id"],
                            {
                                "sync_result": result.text,
                                "sync_suppress_delivery": result.suppress_delivery,
                            },
                        )
                        if bounded.session is not None:
                            await bounded.session.save("delivered")
                    result = replace(
                        result,
                        work_state=bounded.ending or "running",
                        work_id=bounded.current["id"],
                    )
            # Cancellation can revoke the lease while a domain fact is being
            # settled. The stored Work, rather than a stale in-memory ending,
            # decides whether its owning callback may schedule another attempt.
            if result.work_id is not None:
                settled = await repository.get(result.work_id)
                if settled is not None:
                    result = replace(result, work_state=settled["state"])
            return replace(result, outcome=bounded.outcome)


def invocation_boundary(runtime: AgentRuntime) -> str:
    return _hash(
        [
            runtime.origin.value,
            runtime.canonical_conversation_id,
            runtime.execution_id,
            sorted(runtime.allowed_capabilities),
        ]
    )


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
