"""Execute one child Work with explicit worker capabilities and shared activation."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import ChatMessage, ChatTool, InboundMessage, SenderIdentity
from qq_ai_bot.domain.tool_actor import ToolActor
from qq_ai_bot.persistence.event_repository import EventLedgerRepository
from qq_ai_bot.runtime.activation_bindings import ActiveWorkBindings
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.runtime.subagent_repository import SubagentRepository
from qq_ai_bot.runtime.subagent_schema import children
from qq_ai_bot.runtime.subagent_tools import WORKER_NAMES, WORKER_PROMPT, WORKER_REQUIRED_NAMES
from qq_ai_bot.runtime.work_activation import bind_work_activation
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_repository import WorkConflict, WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import work
from qq_ai_bot.sandbox.client import SandboxClient
from qq_ai_bot.sandbox.task_repository import SandboxTaskRepository
from qq_ai_bot.services.agent_runner import AgentRunner, AgentRuntime, AgentToolBackend
from qq_ai_bot.services.agent_tools import ToolRuntime
from qq_ai_bot.services.execution_sources import SelfTaskSource, recover_execution_source
from qq_ai_bot.time.models import TimeContext

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SubagentExecutionDependencies:
    active_bindings: ActiveWorkBindings
    ledger: EventLedgerRepository
    runtime_config: RuntimeConfigService
    sandbox_tasks: SandboxTaskRepository
    sandbox_client: SandboxClient
    runner: AgentRunner
    load_tools: Callable[[], Awaitable[tuple[ChatTool, ...]]]
    open_memory: Callable[..., Any]
    open_self_memory: Callable[..., Awaitable[Any]]
    backend_factory: Callable[[ToolRuntime], AgentToolBackend]
    web_capabilities: Callable[[RuntimeConfigSnapshot], frozenset[str]]


class WorkerBackend:
    def __init__(self, delegate: Any, names: frozenset[str]) -> None:
        self.delegate, self.names = delegate, names

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    def work_control_allowed(self, name: str) -> bool:
        return name in self.names

    def work_query_allowed(self, action: str) -> bool:
        # A worker's lifecycle view is fenced to its own Work by WorkQueries;
        # it does not inherit the main Agent's global Automation read authority.
        return "task_control" in self.names and action in {"get", "list"}

    async def execute(self, name: str, arguments_json: str, runtime: AgentRuntime) -> str:
        if name not in self.names:
            return '{"ok":false,"error":"worker_tool_not_declared"}'
        return str(await self.delegate.execute(name, arguments_json, runtime))


class SubagentExecution:
    def __init__(
        self,
        repository: WorkRepository,
        children: SubagentRepository,
        services: SubagentExecutionDependencies,
    ) -> None:
        self.repository = repository
        self.children = children
        self.services = services
        self.definitions: tuple[ChatTool, ...] | None = None

    async def prepare(self, *, admission_enabled: bool) -> None:
        self.definitions = tuple(
            t for t in await self.services.load_tools() if t.name in WORKER_NAMES
        )
        if admission_enabled and not WORKER_REQUIRED_NAMES <= frozenset(
            t.name for t in self.definitions
        ):
            raise ValueError("incomplete_worker_tool_manifest")

    async def cancel_commands(self) -> None:
        """Reconcile cancellation outside the database transaction, by original run ID."""
        from qq_ai_bot.sandbox.db_models import SandboxTaskRunModel

        async with self.repository.database.sessions() as session:
            ids = list(
                await session.scalars(
                    select(SandboxTaskRunModel.run_id)
                    .join(
                        children,
                        children.c.work_id
                        == func.json_extract(SandboxTaskRunModel.source_json, "$.work_id"),
                    )
                    .join(work, work.c.id == children.c.work_id)
                    .where(
                        work.c.state == "cancelled",
                        SandboxTaskRunModel.status == "waiting",
                        SandboxTaskRunModel.run_id.is_not(None),
                    )
                    .limit(8)
                )
            )
        for run_id in ids:
            await self.services.sandbox_client.execute(
                "cancel_code_run", {"run_id": run_id}, request_id=f"worker-cancel:{run_id}"
            )

    async def run(self, identity: str) -> str | None:
        with self.services.active_bindings.executions.track():
            lease = await self.children.acquire(identity)
            if lease is None:
                return None
            memory = None

            async def validate_lease() -> None:
                if not await self.repository.valid(lease):
                    raise WorkConflict("worker_lease_obsolete")

            control = WorkControl(self.repository, lease, "recovery", {}, validate_lease)
            bindings = ExitStack()
            result_text = ""
            error_category: str | None = None

            async def finish(owned: WorkControl) -> None:
                if not owned.settled:
                    await owned.settle(delivered=True, pending_inputs=bool(await owned.pending()))
                await self.children.finish(
                    owned.lease,
                    "工作暂停，已保留执行记录。" if owned.ending == "suspended" else result_text,
                )

            try:
                async with bind_work_activation(
                    control,
                    finish=finish,
                    bindings=self.services.active_bindings,
                    scope_key=f"worker:{identity}",
                ):
                    row = await self.repository.get(identity)
                    assert row is not None
                    control.current = row
                    source = json.loads(row["source_json"])
                    control.source_key = row["source_key"]
                    control.source = source
                    recovered = await recover_execution_source(
                        self.repository.database,
                        row["conversation_id"],
                        source,
                        request_id=identity,
                    )
                    from qq_ai_bot.runtime.observability import (
                        RuntimeTurnCorrelation,
                        bind_runtime_turn,
                    )

                    bindings.enter_context(
                        bind_runtime_turn(
                            RuntimeTurnCorrelation(
                                turn_id=f"worker:{identity}",
                                origin=TurnOrigin(recovered.origin),
                            )
                        )
                    )
                    original = (
                        await self.services.ledger.get_event(recovered.event_id)
                        if recovered.event_id is not None
                        else None
                    )
                    if original is None and not isinstance(recovered, SelfTaskSource):
                        raise WorkConflict("worker_source_deleted")
                    child = await self.children.related(source["parent_work_id"], identity)

                    async def validate() -> None:
                        if not await self.repository.valid(lease):
                            raise WorkConflict("worker_lease_obsolete")
                        if (
                            await recover_execution_source(
                                self.repository.database,
                                row["conversation_id"],
                                source,
                                request_id=identity,
                            )
                            != recovered
                        ):
                            raise WorkConflict("worker_authority_changed")
                        parent = await self.repository.get(source["parent_work_id"])
                        if parent is None or parent["state"] in {
                            "completed",
                            "failed",
                            "cancelled",
                        }:
                            raise WorkConflict("worker_parent_obsolete")

                    async def command(run_id: str) -> dict[str, Any] | None:
                        record = await self.services.sandbox_tasks.by_run(run_id)
                        if (
                            record is None
                            or json.loads(record.source_json).get("work_id") != identity
                        ):
                            return None
                        return {"run_id": run_id, "pending": record.status != "completed"}

                    control.validate = validate
                    control.resolve_child = command
                    group_id: str | None
                    if isinstance(recovered, SelfTaskSource):
                        group_id = recovered.external_target_id
                    else:
                        assert original is not None
                        group_id = original.group_id
                    config = await self.services.runtime_config.snapshot(
                        user_id=recovered.actor_user_id, group_id=group_id
                    )
                    inbound = (
                        InboundMessage(
                            message_id=original.platform_message_id,
                            source_event_id=original.id,
                            event_type="message",
                            scope_type=original.scope_type,
                            sender=SenderIdentity(recovered.actor_user_id),
                            text=original.content,
                            bot_user_id=recovered.bot_user_id,
                            group_id=original.group_id,
                            received_at=original.occurred_at,
                            person_id=recovered.actor_person_id,
                            space_id=recovered.target_space_id,
                            conversation_id=recovered.conversation_id,
                            presence_id=recovered.presence_id,
                        )
                        if original is not None
                        else None
                    )
                    from qq_ai_bot.memory.runtime.resolver import MemoryStructuredCommand

                    if isinstance(recovered, SelfTaskSource):
                        memory = await self.services.open_self_memory(
                            recovered.trigger(), config, row["goal"]
                        )
                    else:
                        assert inbound is not None
                        memory = self.services.open_memory(
                            inbound,
                            inbound.scope(),
                            row["goal"],
                            config,
                            autonomous=recovered.origin == "autonomous_group",
                            visual_input_present=False,
                            structured_command=MemoryStructuredCommand.NONE,
                        )
                    actor = (
                        ToolActor(
                            user_id="",
                            bot_user_id=recovered.bot_user_id,
                            group_id=group_id,
                            origin=TurnOrigin.SELF_INITIATIVE,
                            instruction=recovered.content,
                            execution_id=identity,
                            conversation_id=recovered.conversation_id,
                            presence_id=recovered.presence_id,
                            principal_kind="self",
                            initiative_run_id=recovered.run_id,
                        )
                        if isinstance(recovered, SelfTaskSource)
                        else None
                    )
                    tool_runtime = ToolRuntime(
                        inbound=inbound,
                        actor_context=actor,
                        gateway=None,
                        allow_generic_onebot=False,
                        actor_user_id=recovered.actor_user_id,
                        current_group_id=group_id,
                        conversation_key=f"worker:{identity}",
                        trigger_message_id=original.platform_message_id if original else "",
                        trigger_event_id=original.id if original else None,
                        initiative_run_id=(
                            recovered.run_id if isinstance(recovered, SelfTaskSource) else None
                        ),
                        runtime_config=config,
                        origin=TurnOrigin(recovered.origin),
                        execution_id=identity,
                        sandbox_source={**source, "work_id": identity},
                        memory_session=memory,
                        conversation_id=recovered.conversation_id,
                        presence_id=recovered.presence_id,
                        person_id=recovered.actor_person_id,
                        space_id=recovered.target_space_id,
                        allow_work_environment=isinstance(recovered, SelfTaskSource),
                        scope_type=ScopeType.GROUP
                        if isinstance(recovered, SelfTaskSource)
                        else None,
                        bot_user_id=recovered.bot_user_id,
                        external_target_id=recovered.external_target_id,
                    )
                    runner = self.services.runner
                    if self.definitions is None:
                        self.definitions = tuple(
                            t for t in await self.services.load_tools() if t.name in WORKER_NAMES
                        )
                    names = frozenset(t.name for t in self.definitions)
                    if not WORKER_REQUIRED_NAMES <= names:
                        raise ValueError("incomplete_worker_tool_manifest")
                    backend = WorkerBackend(self.services.backend_factory(tool_runtime), names)
                    now = datetime.now(UTC)
                    brief = json.loads(child["brief_json"])
                    brief.update(
                        child_id=identity, cwd=f"/workspace/tasks/{identity}", work_id=identity
                    )
                    brief_message = ChatMessage(
                        role="user", content=json.dumps(brief, ensure_ascii=False)
                    )
                    result = await runner.run(
                        (
                            ChatMessage(role="system", content=WORKER_PROMPT),
                            brief_message,
                        ),
                        AgentRuntime(
                            origin=TurnOrigin(recovered.origin),
                            actor_user_id=recovered.actor_user_id,
                            actor_is_superuser=False,
                            delegated_authority=None,
                            conversation_key=f"worker:{identity}",
                            current_group_id=group_id,
                            bot_user_id=recovered.bot_user_id,
                            gateway=None,
                            runtime_config=config,
                            current_time=TimeContext(now, now, "UTC"),
                            allowed_capabilities=self.services.web_capabilities(config),
                            max_tool_calls=32,
                            max_model_requests=24,
                            before_model_request=validate,
                            canonical_conversation_id=recovered.conversation_id,
                            dynamic_context_prepared=True,
                            work_control=control,
                            execution_id=identity,
                            fixed_tools=self.definitions,
                            compaction_brief=brief_message,
                        ),
                        backend,
                    )
                    result_text = result.text
                    error_category = (
                        control.outcome.failure.code
                        if control.outcome and control.outcome.failure
                        else None
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error_category = type(exc).__name__
                logger.warning("subagent_run_failed category=%s", type(exc).__name__)
            finally:
                if memory:
                    try:
                        await memory.close()
                    except Exception as cleanup:
                        logger.warning(
                            "worker_cleanup_deferred stage=memory category=%s",
                            type(cleanup).__name__,
                        )
                bindings.close()
            return error_category
