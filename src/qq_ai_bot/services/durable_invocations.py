"""Own durable synchronous invocations and replay their original saved result."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

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
            requested = dict(runtime.invocation_source or {})
            # Host-only admission facts of the plugin Job; never persisted.
            plugin_turn = requested.pop("_plugin_turn", None)

            async def validate() -> None:
                if runtime.before_model_request is not None:
                    await runtime.before_model_request()

            repository = WorkRepository(database)
            bound_id = plugin_turn.get("work_id") if isinstance(plugin_turn, dict) else None
            if bound_id is not None:
                return await self._resume_bound_plugin_work(
                    repository, messages, runtime, backend, bound_id, requested, validate
                )
            previous = await repository.by_source(f"invocation:{boundary}")
            if previous is not None and isinstance(plugin_turn, dict):
                # Admission and binding commit together, so an unbound Job never
                # owns an existing Work; historical rows are resolved by 0100.
                raise WorkConflict("plugin_turn_work_unbound")
            if previous is not None:
                result = await self.read_result(runtime, previous, generation)
                if result is not None:
                    return result

            source = {
                **requested,
                "origin": runtime.origin.value,
                "actor_user_id": runtime.actor_user_id,
                "execution_boundary": boundary,
                "parent_execution_id": runtime.execution_id,
                "delivery_contract": "return_to_caller",
            }
            # Synchronous plugin/automation calls return to their owning step.
            async with activate_work(
                repository,
                runtime.canonical_conversation_id,
                generation,
                f"invocation:{boundary}",
                source,
                validate,
            ) as bounded:
                if (
                    bounded.current is None
                    and runtime.invocation_goal
                    and isinstance(plugin_turn, dict)
                ):
                    from qq_ai_bot.plugin_host.notification_repository import (
                        admit_background_turn_work,
                    )

                    # The plugin Job owner admits and binds in one writer.
                    await validate()
                    bounded.current = await admit_background_turn_work(
                        database,
                        bounded.lease,
                        job_id=int(plugin_turn["job_id"]),
                        attempt=int(plugin_turn["attempt"]),
                        generation=generation,
                        source_key=f"invocation:{boundary}",
                        source=source,
                        goal=runtime.invocation_goal,
                    )
                elif bounded.current is None and runtime.invocation_goal:
                    await bounded.execute(
                        "task_control",
                        {
                            "action": "accept",
                            "goal": runtime.invocation_goal,
                        },
                        "host-invocation-admission",
                    )
                result = await self._execute(
                    messages, replace(runtime, work_control=bounded), backend
                )
                if bounded.current is not None:
                    result = replace(result, work_id=bounded.current["id"])
            # Settlement has committed. The stored Work, rather than an in-memory
            # candidate, decides the state and the only result the caller reads.
            if result.work_id is not None:
                settled = await repository.get(result.work_id)
                if settled is not None:
                    stored = json.loads(settled["checkpoint_json"]).get("sync_result")
                    result = replace(
                        result,
                        work_state=settled["state"],
                        text=stored
                        if settled["state"] == "completed" and isinstance(stored, str)
                        else "",
                        suppress_delivery=settled["state"] != "completed",
                    )
            return replace(result, outcome=bounded.outcome)

    async def read_result(
        self, runtime: AgentRuntime, previous: dict[str, Any], generation: int
    ) -> AgentRunResult | None:
        """Read an existing invocation without preparing or activating its execution."""
        import time

        from sqlalchemy import select

        from qq_ai_bot.runtime.work_recovery_schema import recovery
        from qq_ai_bot.runtime.work_repository import WorkConflict

        if runtime.before_model_request is not None:
            await runtime.before_model_request()
        prior_source = json.loads(previous["source_json"])
        requested_source = runtime.invocation_source or {}
        if prior_source.get("owner") == "plugin_invocation" and (
            prior_source.get("approval_revision") != requested_source.get("approval_revision")
            or prior_source.get("plugin_id") != requested_source.get("plugin_id")
        ):
            raise WorkConflict("plugin_work_authority_changed")
        async with self.database.sessions() as session:
            wait_until = await session.scalar(
                select(recovery.c.not_before).where(recovery.c.work_id == previous["id"])
            )
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
        if previous["state"] == "completed":
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
            return AgentRunResult(
                text=saved.get("sync_result") or "",
                suppress_delivery=False,
                tool_calls_used=0,
                model_requests=0,
                web_was_used=False,
                work_state="completed",
                work_id=previous["id"],
            )
        return None

    async def _resume_bound_plugin_work(
        self,
        repository: Any,
        messages: tuple[ChatMessage, ...],
        runtime: AgentRuntime,
        backend: AgentToolBackend | None,
        identity: str,
        requested: dict[str, Any],
        validate: Callable[[], Awaitable[None]],
    ) -> AgentRunResult:
        """Continue the exact Work bound to a plugin Job with its original source.

        The capability-bound boundary hash is not recomputed, and no new Work is
        admitted: a changed capability set keeps the original identity.
        """
        import time

        from sqlalchemy import select

        from qq_ai_bot.runtime.work_activation import activate_work
        from qq_ai_bot.runtime.work_recovery_schema import recovery
        from qq_ai_bot.runtime.work_repository import TERMINAL, WorkConflict

        previous = await repository.get(identity)
        if previous is None:
            raise WorkConflict("plugin_turn_work_missing")
        original = json.loads(previous["source_json"])
        if (
            original.get("owner") != "plugin_background"
            or any(
                original.get(key) != requested.get(key)
                for key in ("owner", "plugin_id", "trigger_event_id", "conversation_id")
            )
            or previous["conversation_id"] != runtime.canonical_conversation_id
        ):
            raise WorkConflict("plugin_turn_work_source_mismatch")
        await validate()
        async with self.database.sessions() as session:
            wait_until = await session.scalar(
                select(recovery.c.not_before).where(recovery.c.work_id == identity)
            )
        if (
            previous["state"] in TERMINAL
            or previous["state"] in {"suspended", "waiting_user", "waiting_external"}
            or previous["generation"] != requested.get("generation")
            or (wait_until and wait_until > time.time())
        ):
            return AgentRunResult(
                text="",
                tool_calls_used=0,
                model_requests=0,
                web_was_used=False,
                suppress_delivery=True,
                work_state=previous["state"]
                if previous["generation"] == requested.get("generation")
                else "cancelled",
                work_id=identity,
            )
        async with activate_work(
            repository,
            previous["conversation_id"],
            previous["generation"],
            previous["source_key"],
            original,
            validate,
            work_id=identity,
        ) as bounded:
            if bounded.current is None:
                raise WorkConflict("plugin_turn_work_changed")
            result = await self._execute(messages, replace(runtime, work_control=bounded), backend)
            result = replace(result, work_id=identity)
        settled = await repository.get(identity)
        if settled is not None:
            stored = json.loads(settled["checkpoint_json"]).get("sync_result")
            result = replace(
                result,
                work_state=settled["state"],
                text=stored if settled["state"] == "completed" and isinstance(stored, str) else "",
                suppress_delivery=settled["state"] != "completed",
            )
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
