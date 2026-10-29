"""Execute one child Work with explicit worker capabilities and shared activation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import ExitStack
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select

from yuki.admin.config_service import RuntimeConfigService
from yuki.admin.models import RuntimeConfigSnapshot
from yuki.codemode.api_projection import ScriptApi
from yuki.codemode.tool_visibility import model_definitions
from yuki.domain.conversations import ConversationScope, ScopeType
from yuki.domain.messages import (
    ChatMessage,
    ChatTool,
)
from yuki.persistence.event_repository import EventLedgerRepository
from yuki.runtime.activation_bindings import ActiveWorkBindings
from yuki.runtime.origin import TurnOrigin
from yuki.runtime.subagent_repository import SubagentRepository
from yuki.runtime.work_activation import bind_work_activation
from yuki.runtime.work_control import WorkControl
from yuki.runtime.work_repository import WorkConflict, WorkRepository
from yuki.runtime.work_schema_v1 import work
from yuki.sandbox.client import SandboxClient
from yuki.sandbox.task_repository import SandboxTaskRepository
from yuki.services.agent_runner import AgentRunner, AgentToolBackend
from yuki.services.agent_tools import ToolRuntime
from yuki.services.execution_sources import (
    AutomationTaskSource,
    BackgroundTaskSource,
    MessageTaskSource,
    SelfTaskSource,
    recover_execution_source,
)
from yuki.services.invocation_context import InvocationContextFactory

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
    static_messages: Callable[[], tuple[ChatMessage, ...]]
    lookup_plugin_host: Callable[[str], Any]
    backend_factory: Callable[[ToolRuntime, Any], AgentToolBackend]
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

    async def prepare(self) -> None:
        if self.definitions is None:
            self.definitions = await self.services.load_tools()
        contract = self.services.runner.main_contract
        self.script_api = contract.script_api if contract is not None else None

    async def cancel_commands(self) -> None:
        """Reconcile cancellation outside the database transaction, by original run ID."""
        from yuki.sandbox.db_models import SandboxTaskRunModel

        async with self.repository.database.read_session() as session:
            ids = list(
                await session.scalars(
                    select(SandboxTaskRunModel)
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
                )
            )
        for row in ids:
            run_id = row.run_id
            await self.services.sandbox_client.execute(
                "terminal_control",
                {"run_id": run_id, "action": "cancel"},
                request_id=f"worker-cancel:{run_id}",
                source=json.loads(row.source_json),
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
                    control.source_key = row["source_key"]
                    control.source = source
                    recovered = await recover_execution_source(
                        self.repository.database,
                        row["conversation_id"],
                        source,
                        request_id=identity,
                        lookup_plugin_host=self.services.lookup_plugin_host,
                    )
                    from yuki.runtime.observability import (
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
                                lookup_plugin_host=self.services.lookup_plugin_host,
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
                        user_id=recovered.actor_person_id, group_id=recovered.target_space_id
                    )
                    inbound = (
                        recovered.inbound(original)
                        if isinstance(recovered, MessageTaskSource) and original is not None
                        else None
                    )

                    if isinstance(recovered, (AutomationTaskSource, BackgroundTaskSource)):
                        from yuki.memory.runtime.resolver import resolve_memory_access
                        from yuki.memory.runtime.turn_session import TurnMemorySession

                        memory = TurnMemorySession(
                            decision=resolve_memory_access(origin=TurnOrigin(recovered.origin)),
                        )
                    elif isinstance(recovered, SelfTaskSource):
                        memory = await self.services.open_self_memory(recovered.trigger())
                    else:
                        assert inbound is not None
                        memory = self.services.open_memory(
                            autonomous=recovered.origin == "autonomous_group",
                        )
                    actor = (
                        recovered.actor(identity)
                        if isinstance(recovered, (SelfTaskSource, AutomationTaskSource))
                        else None
                    )
                    actor_is_superuser = getattr(recovered, "actor_is_superuser", False)
                    if isinstance(recovered, MessageTaskSource):
                        from yuki.identity.authority import person_is_superuser

                        async with self.repository.database.read_session() as session:
                            actor_is_superuser = await person_is_superuser(
                                session, recovered.actor_person_id
                            )
                    delegated = source.get("owner") == "plugin_invocation"
                    host: Any = None
                    memory_scopes = None
                    if delegated:
                        from yuki.memory.enums import MemoryScopeType
                        from yuki_plugin_sdk.permissions import PluginPermission

                        host = self.services.lookup_plugin_host(source["plugin_id"])
                        memory_scopes = (
                            (MemoryScopeType.PERSON, MemoryScopeType.PERSON_GROUP)
                            if PluginPermission.MEMORY_PERSON_READ in host._approved_permissions
                            else ()
                        ) + (
                            (MemoryScopeType.GROUP,)
                            if PluginPermission.MEMORY_GROUP_READ in host._approved_permissions
                            else ()
                        )
                    tool_runtime = ToolRuntime(
                        inbound=inbound,
                        actor_context=actor,
                        actor_is_superuser=actor_is_superuser,
                        conversation_key=f"worker:{identity}",
                        trigger_event_id=original.id if original else None,
                        initiative_run_id=(
                            recovered.run_id if isinstance(recovered, SelfTaskSource) else None
                        ),
                        runtime_config=config,
                        origin=TurnOrigin.PLUGIN_SESSION
                        if delegated and inbound is not None
                        else TurnOrigin(recovered.origin),
                        execution_id=identity,
                        sandbox_source={
                            **(source.get("execution_source") or source),
                            "work_id": identity,
                        },
                        memory_session=memory,
                        conversation_id=recovered.conversation_id,
                        person_id=recovered.actor_person_id,
                        space_id=recovered.target_space_id,
                        scope_type=ScopeType.GROUP
                        if recovered.target_space_id
                        else ScopeType.PRIVATE,
                        external_target_id=recovered.external_target_id,
                        read_scope=ConversationScope.group(
                            recovered.bot_user_id,
                            group_id,
                            platform=recovered.platform,
                            presence_id=recovered.presence_id,
                            conversation_id=recovered.conversation_id,
                            thread_id=recovered.thread_id,
                        )
                        if group_id is not None
                        else ConversationScope.private(
                            recovered.bot_user_id,
                            recovered.external_target_id,
                            platform=recovered.platform,
                            presence_id=recovered.presence_id,
                            conversation_id=recovered.conversation_id,
                        ),
                        read_target_id=(recovered.target_space_id or recovered.actor_person_id)
                        if delegated
                        else None,
                        target_presence_id=recovered.presence_id,
                        before_model_request=validate,
                        context_plugin_id=source.get("plugin_id") if delegated else None,
                        memory_allowed_scopes=memory_scopes,
                        context_read_contract=hashlib.sha256(
                            json.dumps(
                                [
                                    source["permission"],
                                    source["context_profile"],
                                    *(
                                        [source["allowed_tools"]]
                                        if "allowed_tools" in source
                                        else []
                                    ),
                                ],
                                separators=(",", ":"),
                            ).encode()
                        ).hexdigest()
                        if delegated
                        else None,
                        activation_context=(lambda: recovered.activation_context(datetime.now(UTC)))
                        if isinstance(recovered, AutomationTaskSource)
                        else None,
                    )
                    runner = self.services.runner
                    contract = runner.main_contract
                    definitions = await self.services.load_tools()
                    script_api = contract.script_api if contract is not None else None
                    backend = self.services.backend_factory(tool_runtime, host)
                    brief = json.loads(child["brief_json"])
                    brief.update(
                        child_id=identity,
                        cwd=f"/workspace/tasks/{identity}",
                        work_id=identity,
                        parent_work_id=row["parent_work_id"],
                    )
                    brief_message = ChatMessage(
                        role="user", content=json.dumps(brief, ensure_ascii=False)
                    )
                    await runner.run(
                        (
                            *self.services.static_messages(),
                            brief_message,
                        ),
                        replace(
                            InvocationContextFactory.from_tools(
                                tool_runtime,
                                allowed_capabilities=(
                                    frozenset({"web"})
                                    if PluginPermission.WEB_SEARCH in host._approved_permissions
                                    else frozenset()
                                )
                                if delegated
                                else self.services.web_capabilities(config),
                                max_tool_calls=None,
                                max_model_requests=None,
                            ),
                            before_model_request=validate,
                            dynamic_context_prepared=True,
                            work_control=control,
                            fixed_tools=model_definitions(
                                definitions,
                                enabled=self.code_enabled,
                            ),
                            script_api=script_api,
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
                bindings.close()
            return error_category
