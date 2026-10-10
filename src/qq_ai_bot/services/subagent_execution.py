"""Execute one child Work with explicit worker capabilities and shared activation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import ExitStack
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.codemode.api_projection import ScriptApi, project
from qq_ai_bot.codemode.contract import CODE_API_REVISION
from qq_ai_bot.codemode.tool_visibility import DIRECT_TOOL_NAMES, model_definitions
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import (
    ChatMessage,
    ChatTool,
)
from qq_ai_bot.persistence.event_repository import EventLedgerRepository
from qq_ai_bot.runtime.activation_bindings import ActiveWorkBindings
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.runtime.subagent_repository import SubagentRepository
from qq_ai_bot.runtime.subagent_tools import WORKER_NAMES, WORKER_REQUIRED_NAMES, worker_prompt
from qq_ai_bot.runtime.work_activation import bind_work_activation
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_repository import WorkConflict, WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import work
from qq_ai_bot.sandbox.client import SandboxClient
from qq_ai_bot.sandbox.task_repository import SandboxTaskRepository
from qq_ai_bot.services.agent_runner import AgentRunner, AgentToolBackend
from qq_ai_bot.services.agent_tools import ToolRuntime
from qq_ai_bot.services.execution_sources import (
    AutomationTaskSource,
    MessageTaskSource,
    SelfTaskSource,
    recover_execution_source,
)
from qq_ai_bot.services.invocation_context import InvocationContextFactory
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
    # The real backend receives the worker's complete frozen tool contract as
    # its execution ceiling; there is no separate worker wrapper.
    backend_factory: Callable[[ToolRuntime, frozenset[str]], AgentToolBackend]
    web_capabilities: Callable[[RuntimeConfigSnapshot], frozenset[str]]


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
        self.script_api: ScriptApi | None = None

    @property
    def code_enabled(self) -> bool:
        contract = self.services.runner.main_contract
        return contract is not None and contract.mode == "code"

    def required_names(self) -> frozenset[str]:
        return WORKER_REQUIRED_NAMES | (
            {"execute_code", "lookup_tools"} if self.code_enabled else set()
        )

    async def prepare(self, *, admission_enabled: bool) -> None:
        if self.definitions is None:
            self.definitions = tuple(
                t for t in await self.services.load_tools() if t.name in WORKER_NAMES
            )
        if admission_enabled and not self.required_names() <= frozenset(
            t.name for t in self.definitions
        ):
            raise ValueError("incomplete_worker_tool_manifest")
        if not self.code_enabled:
            self.script_api = None
            return
        if self.script_api is not None:
            return
        revision = hashlib.sha256(
            json.dumps(
                {
                    "worker_contract": 4,
                    "code_api": CODE_API_REVISION,
                    "direct_names": sorted(DIRECT_TOOL_NAMES),
                    "tools": [asdict(tool) for tool in self.definitions],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        self.script_api = project(self.definitions, revision)

    async def cancel_commands(self) -> None:
        """Reconcile cancellation outside the database transaction, by original run ID."""
        from qq_ai_bot.sandbox.db_models import SandboxTaskRunModel

        async with self.repository.database.sessions() as session:
            ids = list(
                await session.scalars(
                    select(SandboxTaskRunModel.run_id)
                    .join(
                        work,
                        work.c.id
                        == func.json_extract(SandboxTaskRunModel.source_json, "$.work_id"),
                    )
                    .where(
                        work.c.state.in_(("failed", "cancelled")),
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
            error_category: str | None = None

            async def finish(owned: WorkControl) -> None:
                # Only the committed Work row decides what the parent reads.
                await self.children.finish(owned.lease)

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
                    settings = (
                        self.services.runner.code_mode_settings
                        if source.get("origin") == TurnOrigin.SCHEDULED_AUTOMATION.value
                        else None
                    )
                    control.source_key = row["source_key"]
                    control.source = source
                    recovered = await recover_execution_source(
                        self.repository.database,
                        row["conversation_id"],
                        source,
                        request_id=identity,
                        settings=settings,
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
                    if original is None and isinstance(recovered, MessageTaskSource):
                        raise WorkConflict("worker_source_deleted")
                    child = await self.children.related(row["parent_work_id"], identity)

                    async def validate() -> None:
                        if not await self.repository.valid(lease):
                            raise WorkConflict("worker_lease_obsolete")
                        if (
                            await recover_execution_source(
                                self.repository.database,
                                row["conversation_id"],
                                source,
                                request_id=identity,
                                settings=settings,
                            )
                            != recovered
                        ):
                            raise WorkConflict("worker_authority_changed")

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
                    if recovered.target_space_id is not None:
                        group_id = recovered.external_target_id
                    else:
                        group_id = original.group_id if original is not None else None
                    config = await self.services.runtime_config.snapshot(
                        user_id=recovered.actor_user_id, group_id=group_id
                    )
                    inbound = (
                        recovered.inbound(original)
                        if isinstance(recovered, MessageTaskSource) and original is not None
                        else None
                    )

                    if isinstance(recovered, AutomationTaskSource):
                        from qq_ai_bot.memory.runtime.resolver import resolve_memory_access
                        from qq_ai_bot.memory.runtime.turn_session import TurnMemorySession
                        from qq_ai_bot.runtime.keys import ResolvedMemoryScope

                        memory = TurnMemorySession(
                            decision=resolve_memory_access(origin=TurnOrigin.SCHEDULED_AUTOMATION),
                            scope=ResolvedMemoryScope.for_group(recovered.external_target_id)
                            if recovered.target_space_id
                            else ResolvedMemoryScope.for_private(recovered.actor_user_id),
                        )
                    elif isinstance(recovered, SelfTaskSource):
                        memory = await self.services.open_self_memory(recovered.trigger())
                    else:
                        assert inbound is not None
                        memory = self.services.open_memory(
                            inbound,
                            autonomous=recovered.origin == "autonomous_group",
                        )
                    actor = (
                        recovered.actor(identity)
                        if isinstance(recovered, (SelfTaskSource, AutomationTaskSource))
                        else None
                    )
                    tool_runtime = ToolRuntime(
                        inbound=inbound,
                        actor_context=actor,
                        gateway=None,
                        allow_generic_onebot=False,
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
                        person_id=recovered.actor_person_id,
                        space_id=recovered.target_space_id,
                        allow_work_environment=not isinstance(recovered, MessageTaskSource),
                        scope_type=ScopeType.GROUP
                        if recovered.target_space_id
                        else ScopeType.PRIVATE,
                        external_target_id=recovered.external_target_id,
                    )
                    runner = self.services.runner
                    if self.definitions is None:
                        await self.prepare(admission_enabled=True)
                    assert self.definitions is not None
                    names = frozenset(t.name for t in self.definitions)
                    if not self.required_names() <= names:
                        raise ValueError("incomplete_worker_tool_manifest")
                    backend = self.services.backend_factory(tool_runtime, names)
                    now = datetime.now(UTC)
                    brief = json.loads(child["brief_json"])
                    brief.update(
                        child_id=identity, cwd=f"/workspace/tasks/{identity}", work_id=identity
                    )
                    brief_message = ChatMessage(
                        role="user", content=json.dumps(brief, ensure_ascii=False)
                    )
                    await runner.run(
                        (
                            ChatMessage(
                                role="system",
                                content=worker_prompt(code_enabled=self.code_enabled),
                            ),
                            brief_message,
                        ),
                        replace(
                            InvocationContextFactory.from_tools(
                                tool_runtime,
                                current_time=TimeContext(now, now, "UTC"),
                                allowed_capabilities=self.services.web_capabilities(config),
                                max_tool_calls=32,
                                max_model_requests=24,
                            ),
                            before_model_request=validate,
                            dynamic_context_prepared=True,
                            work_control=control,
                            fixed_tools=model_definitions(
                                self.definitions,
                                enabled=self.code_enabled,
                            ),
                            script_api=self.script_api,
                            compaction_brief=brief_message,
                        ),
                        backend,
                    )
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
