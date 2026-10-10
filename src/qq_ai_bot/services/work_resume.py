"""Recover verified persisted main work without owning a scheduling loop."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.config import Settings
from qq_ai_bot.conversation.rollup.repository import ConversationScopeRepository
from qq_ai_bot.conversation.scope import ConversationTurnSnapshot
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.identity.routing import PresenceRouter
from qq_ai_bot.persistence.event_repository import EventLedgerRepository
from qq_ai_bot.runtime.activation_bindings import ActiveWorkBindings
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.runtime.trigger import WorkResumeTrigger
from qq_ai_bot.runtime.work_activation import activate_work
from qq_ai_bot.runtime.work_repository import WorkConflict, WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import journal
from qq_ai_bot.sandbox.task_repository import SandboxTaskRepository
from qq_ai_bot.services.agent_tools import ToolRuntime
from qq_ai_bot.services.execution_sources import (
    MessageTaskSource,
    SelfTaskSource,
    recover_automation_source,
    recover_execution_source,
    recover_self_source,
    recover_source,
)
from qq_ai_bot.services.turn_coordinator import ConversationTurnCoordinator, TurnToken


@dataclass(frozen=True, slots=True)
class WorkResumeDependencies:
    settings: Settings
    ledger: EventLedgerRepository
    conversation_scopes: ConversationScopeRepository
    turn_coordinator: ConversationTurnCoordinator
    presence_router: PresenceRouter
    runtime_config: RuntimeConfigService
    sandbox_tasks: SandboxTaskRepository
    active_bindings: ActiveWorkBindings
    generate_wakeup: Callable[..., Awaitable[Any]]
    generate_self: Callable[..., Awaitable[Any]]
    validate_snapshot: Callable[[ConversationTurnSnapshot], Awaitable[bool]]
    resume_plugin: Callable[[dict[str, Any], dict[str, Any]], Awaitable[None]]
    resume_automation: Callable[[dict[str, Any], dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class _ResumeScene:
    identity: ConversationScope
    key: str
    snapshot: ConversationTurnSnapshot
    token: TurnToken
    validate: Callable[[], Awaitable[None]]


class WorkResumer:
    def __init__(self, repository: WorkRepository, services: WorkResumeDependencies) -> None:
        self.repository = repository
        self.services = services

    async def resume(self, item: dict[str, Any]) -> str | None:
        """Run one selected Work; return this run's error category, None on success."""
        if item["state"] not in {"queued", "running"}:
            return None
        source = json.loads(item["source_json"])
        try:
            if item["state"] == "running":
                # Scheduler selected an expired/absent owner. The lost process
                # cannot supply its exception; settle the original facts only.
                await self._recover_preparation_failure(
                    item, source, WorkConflict("work_activation_interrupted"), orphan=True
                )
                return None
            if source.get("owner") in {"plugin_invocation", "plugin_background"}:
                await self.services.resume_plugin(item, source)
            elif source.get("owner") == "automation":
                await self._resume_automation(item, source)
            elif source.get("origin") == "self_initiative":
                return await self._resume_self(item, source)
            elif source.get("origin") in {"user_message", "autonomous_group"}:
                return await self._resume(item, source)
            return None
        except WorkConflict:
            return None
        except Exception as exc:
            from qq_ai_bot.runtime.activation_outcome import (
                WorkActivationHandled,
            )

            category = type(exc).__name__
            if isinstance(exc, WorkActivationHandled):
                return category
            try:
                await self._recover_preparation_failure(item, source, exc)
            except BaseException as cleanup:
                exc.add_note(f"work preparation recovery deferred: {type(cleanup).__name__}")
                raise exc from exc.__cause__
            return category

    async def _recover_preparation_failure(
        self,
        item: dict[str, Any],
        source: dict[str, Any],
        exc: Exception,
        *,
        orphan: bool = False,
    ) -> None:
        from qq_ai_bot.runtime.activation_outcome import WorkRecoveryDeferred
        from qq_ai_bot.runtime.work_activation import bind_work_activation
        from qq_ai_bot.runtime.work_control import WorkControl

        deferred = exc if isinstance(exc, WorkRecoveryDeferred) else None
        lease = await self.repository.acquire(item["conversation_id"], item["generation"])
        if lease is None:
            return

        async def validate() -> None:
            if not await self.repository.valid(lease):
                raise WorkConflict("work_recovery_lease_lost")

        control = WorkControl(self.repository, lease, item["source_key"], source, validate)
        async with bind_work_activation(control):
            current = await self.repository.get(item["id"])
            if orphan:
                if (
                    current is None
                    or current["state"] != "running"
                    or current["generation"] != item["generation"]
                    or current["revision"] != item["revision"]
                ):
                    control.settled = True
                    return
                control.current = current
                async with self.repository.database.sessions() as reader:
                    phase = await reader.scalar(
                        select(journal.c.phase).where(journal.c.work_id == item["id"])
                    )
                if phase == "dispatched":
                    from qq_ai_bot.runtime.work_journal import JournalUnavailable

                    exc = JournalUnavailable("work_response_not_persisted")
                else:
                    if await control.has_pending_business_inputs():
                        # New input continues a retained response/paired boundary.
                        control.current = await self.repository.transition(
                            lease, current["id"], current["revision"], "waiting_external"
                        )
                        control.settled = True
                        return
                    control.deferred_failure = WorkRecoveryDeferred(
                        "work_activation_interrupted", work=item
                    )
            if deferred is not None:
                failed = deferred.work
                prior_lease = deferred.lease
                if (
                    failed is None
                    or prior_lease is None
                    or current is None
                    or lease.fence != prior_lease.fence + 1
                    or lease.cancel_epoch != prior_lease.cancel_epoch
                    or current["id"] != failed["id"]
                    or current["generation"] != failed["generation"]
                    or current["revision"] != failed["revision"]
                    or current["state"] != "running"
                ):
                    control.settled = True
                    return
                control.deferred_failure = deferred
            if current and current["state"] in {"queued", "running"}:
                control.current = current
                await control.recover_failure(exc)

    @asynccontextmanager
    async def _scene(
        self,
        item: dict[str, Any],
        source: dict[str, Any],
        recovered: MessageTaskSource | SelfTaskSource,
    ) -> AsyncIterator[_ResumeScene | None]:
        identity = (
            ConversationScope.group(recovered.bot_user_id, recovered.external_target_id)
            if recovered.target_space_id
            else ConversationScope.private(recovered.bot_user_id, recovered.external_target_id)
        )
        state = await self.services.conversation_scopes.get(identity)
        if state is None or state.generation != recovered.generation:
            raise ValueError("work_generation_changed")
        key = state.runtime_scope_key or identity.key
        async with self.services.turn_coordinator.background_turn(key) as token:
            if token is None:
                yield None
                return
            snapshot = ConversationTurnSnapshot(
                state.id,
                key,
                recovered.generation,
                recovered.event_id,
                token.version,
                identity.key,
                initiative_run_id=recovered.run_id
                if isinstance(recovered, SelfTaskSource)
                else None,
            )
            resolved = await self.services.presence_router.resolve_presence(recovered.presence_id)

            async def validate() -> None:
                fresh_source = await recover_execution_source(
                    self.repository.database, item["conversation_id"], source, request_id=item["id"]
                )
                if fresh_source != recovered:
                    raise ValueError("work_source_changed")
                if not await self.services.validate_snapshot(snapshot):
                    raise WorkConflict("work_turn_changed")
                fresh = await self.services.presence_router.resolve_presence(recovered.presence_id)
                if fresh.connection.snapshot != resolved.connection.snapshot:
                    raise ValueError("work_connection_changed")

            yield _ResumeScene(identity, key, snapshot, token, validate)

    def _child_resolver(self, identity: str) -> Callable[[str], Awaitable[dict[str, Any] | None]]:
        async def child(run_id: str) -> dict[str, Any] | None:
            task = await self.services.sandbox_tasks.by_run(run_id)
            if task is None or json.loads(task.source_json).get("work_id") != identity:
                return None
            return {"run_id": run_id, "pending": task.status != "completed"}

        return child

    async def _resume_automation(self, item: dict[str, Any], source: dict[str, Any]) -> None:
        recovered = await recover_automation_source(
            self.repository.database,
            item["conversation_id"],
            source,
            request_id=item["id"],
            settings=self.services.settings,
        )
        identity = (
            ConversationScope.group(recovered.bot_user_id, recovered.external_target_id)
            if recovered.target_space_id
            else ConversationScope.private(recovered.bot_user_id, recovered.external_target_id)
        )
        state = await self.services.conversation_scopes.get(identity)
        if state is None or state.generation != recovered.generation:
            raise ValueError("work_generation_changed")
        key = state.runtime_scope_key or identity.key
        async with self.services.turn_coordinator.background_turn(key) as token:
            if token is None:
                return
            resolved = await self.services.presence_router.resolve_presence(recovered.presence_id)

            async def validate() -> None:
                fresh_source = await recover_automation_source(
                    self.repository.database,
                    item["conversation_id"],
                    source,
                    request_id=item["id"],
                    settings=self.services.settings,
                )
                if fresh_source != recovered:
                    raise ValueError("work_source_changed")
                if not self.services.turn_coordinator.is_current(token):
                    raise WorkConflict("work_turn_changed")
                fresh = await self.services.presence_router.resolve_presence(recovered.presence_id)
                if fresh.connection.snapshot != resolved.connection.snapshot:
                    raise ValueError("work_connection_changed")

            async with activate_work(
                self.repository,
                recovered.conversation_id,
                recovered.generation,
                item["source_key"],
                source,
                validate,
                self._child_resolver(item["id"]),
                work_id=item["id"],
                bindings=self.services.active_bindings,
                scope_key=key,
            ) as control:
                if control.current is None or control.current["id"] != item["id"]:
                    raise WorkConflict("work_schedule_target_changed")
                await validate()
                await self.services.resume_automation(item, source)

    async def _resume_self(self, item: dict[str, Any], source: dict[str, Any]) -> str | None:
        """Resume the original SELF Work through the same Main Agent entry point."""
        recovered = await recover_self_source(
            self.repository.database, item["conversation_id"], source, request_id=item["id"]
        )
        async with self._scene(item, source, recovered) as scene:
            if scene is None:
                return None
            key, snapshot = scene.key, scene.snapshot
            token, validate = scene.token, scene.validate
            child = self._child_resolver(item["id"])

            async with activate_work(
                self.repository,
                recovered.conversation_id,
                recovered.generation,
                item["source_key"],
                source,
                validate,
                child,
                work_id=item["id"],
                bindings=self.services.active_bindings,
                scope_key=key,
            ) as control:
                if control.current is None or control.current["id"] != item["id"]:
                    raise WorkConflict("work_schedule_target_changed")
                runtime = await self.services.runtime_config.snapshot(
                    group_id=recovered.external_target_id
                )
                actor = recovered.actor(item["id"])
                result = await self.services.generate_self(
                    trigger=recovered.trigger(),
                    runtime=runtime,
                    turn_token=token,
                    turn_snapshot=snapshot,
                    before_model_request=validate,
                    source_runtime=ToolRuntime(
                        inbound=None,
                        actor_context=actor,
                        gateway=None,
                        allow_generic_onebot=False,
                        actor_is_superuser=False,
                        conversation_key=key,
                        execution_id=item["id"],
                        origin=TurnOrigin.SELF_INITIATIVE,
                        initiative_run_id=recovered.run_id,
                        conversation_id=recovered.conversation_id,
                        scope_type=ScopeType.GROUP,
                        external_target_id=recovered.external_target_id,
                        space_id=recovered.target_space_id,
                        sandbox_source={**source, "work_id": item["id"]},
                        allow_work_environment=True,
                        allow_automation=True,
                    ),
                )
                return (
                    result.outcome.failure.code
                    if result.outcome and result.outcome.failure
                    else None
                )

    async def _resume(self, item: dict[str, Any], source: dict[str, Any]) -> str | None:
        recovered = await recover_source(
            self.repository.database, item["conversation_id"], source, request_id=item["id"]
        )
        original = await self.services.ledger.get_event(recovered.event_id)
        if original is None:
            raise ValueError("work_source_deleted")
        async with self._scene(item, source, recovered) as scene:
            if scene is None:
                return None
            identity, key, snapshot = scene.identity, scene.key, scene.snapshot
            token, validate = scene.token, scene.validate

            child = self._child_resolver(item["id"])

            async with activate_work(
                self.repository,
                recovered.conversation_id,
                recovered.generation,
                item["source_key"],
                source,
                validate,
                child,
                work_id=item["id"],
                bindings=self.services.active_bindings,
                scope_key=key,
            ) as control:
                if control.current is None or control.current["id"] != item["id"]:
                    raise WorkConflict("work_schedule_target_changed")
                runtime = await self.services.runtime_config.snapshot(
                    user_id=recovered.actor_user_id, group_id=original.group_id
                )
                inbound = recovered.inbound(original, legacy_conversation_key=key)
                result = await self.services.generate_wakeup(
                    event=original,
                    trigger=WorkResumeTrigger(
                        original.id, original.scope_type.value, recovered.external_target_id
                    ),
                    identity=identity,
                    runtime=runtime,
                    turn_token=token,
                    turn_snapshot=snapshot,
                    gateway=None,
                    person_id=recovered.target_person_id,
                    space_id=recovered.target_space_id,
                    presence_id=recovered.presence_id,
                    conversation_id=recovered.conversation_id,
                    before_model_request=validate,
                    source_runtime=ToolRuntime(
                        inbound=inbound,
                        gateway=None,
                        allow_generic_onebot=False,
                        actor_is_superuser=False,
                        allow_admin_actions=False,
                        allow_automation=bool(source.get("allow_automation")),
                        conversation_key=key,
                        trigger_message_id=original.platform_message_id,
                        execution_id=item["id"],
                        origin=TurnOrigin(recovered.origin),
                        conversation_id=recovered.conversation_id,
                        person_id=recovered.actor_person_id,
                        space_id=recovered.target_space_id,
                        sandbox_source={**source, "work_id": item["id"]},
                    ),
                )
                category = (
                    result.outcome.failure.code
                    if result.outcome and result.outcome.failure
                    else None
                )
                return category
