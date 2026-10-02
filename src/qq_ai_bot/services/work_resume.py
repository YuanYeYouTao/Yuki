"""Recover verified persisted main work without owning a scheduling loop."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from qq_ai_bot.adapters.onebot.sender import OneBotRouteSender
from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.conversation.rollup.repository import ConversationScopeRepository
from qq_ai_bot.conversation.scope import ConversationTurnSnapshot
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.domain.tool_actor import ToolActor
from qq_ai_bot.identity.routing import PresenceRouter, ResolvedSend
from qq_ai_bot.persistence.event_repository import EventLedgerRepository
from qq_ai_bot.runtime.activation_bindings import ActiveWorkBindings
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.runtime.trigger import WorkResumeTrigger
from qq_ai_bot.runtime.work_activation import activate_work
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_recovery_schema import deliveries
from qq_ai_bot.runtime.work_repository import WorkConflict, WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import effects
from qq_ai_bot.sandbox.task_repository import SandboxTaskRepository
from qq_ai_bot.services.agent_tools import ToolRuntime
from qq_ai_bot.services.execution_sources import (
    MessageTaskSource,
    SelfTaskSource,
    recover_execution_source,
    recover_self_source,
    recover_source,
)
from qq_ai_bot.services.turn_coordinator import ConversationTurnCoordinator, TurnToken


@dataclass(frozen=True, slots=True)
class WorkResumeDependencies:
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
    run_effect: Callable[..., Awaitable[dict[str, Any]]]
    resume_plugin: Callable[[dict[str, Any], dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class _ResumeScene:
    identity: ConversationScope
    key: str
    snapshot: ConversationTurnSnapshot
    token: TurnToken
    resolved: ResolvedSend
    validate: Callable[[], Awaitable[None]]


class WorkResumer:
    def __init__(self, repository: WorkRepository, services: WorkResumeDependencies) -> None:
        self.repository = repository
        self.services = services
        self.last_error: str | None = None

    async def resume(self, item: dict[str, Any]) -> None:
        source = json.loads(item["source_json"])
        try:
            if source.get("owner") == "plugin_invocation":
                await self.services.resume_plugin(item, source)
            elif source.get("origin") == "self_initiative":
                if item["state"] != "suspended":
                    await self._resume_self(item, source)
            elif source.get("origin") in {"user_message", "autonomous_group"}:
                await self._resume(item, source)
        except WorkConflict as exc:
            if item["state"] == "suspended":
                try:
                    await self._recover_preparation_failure(item, source, exc)
                except BaseException as cleanup:
                    exc.add_note(f"notice reconciliation deferred: {type(cleanup).__name__}")
                    raise exc from exc.__cause__
            return
        except Exception as exc:
            from qq_ai_bot.runtime.activation_outcome import (
                WorkActivationHandled,
                WorkRecoveryDeferred,
            )

            self.last_error = type(exc).__name__
            if isinstance(exc, (WorkActivationHandled, WorkRecoveryDeferred)):
                return
            try:
                await self._recover_preparation_failure(item, source, exc)
            except BaseException as cleanup:
                exc.add_note(f"work preparation recovery deferred: {type(cleanup).__name__}")
                raise exc from exc.__cause__

    async def _recover_preparation_failure(
        self, item: dict[str, Any], source: dict[str, Any], exc: Exception
    ) -> None:
        from qq_ai_bot.runtime.work_activation import bind_work_activation
        from qq_ai_bot.runtime.work_control import WorkControl

        lease = await self.repository.acquire(item["conversation_id"], item["generation"])
        if lease is None:
            return

        async def validate() -> None:
            if not await self.repository.valid(lease):
                raise WorkConflict("work_recovery_lease_lost")

        control = WorkControl(self.repository, lease, item["source_key"], source, validate)
        async with bind_work_activation(control, meter_active_time=item["state"] != "suspended"):
            current = await self.repository.get(item["id"])
            if (
                item["state"] != "suspended"
                and current
                and current["state"] in {"queued", "running"}
            ):
                control.current = current
                await control.recover_failure(exc)
            elif current and current["state"] == "suspended":
                control.current = current
                control.ending = "suspended"
                control.settled = True
                # Preparation never dispatched a new effect. Close its existing
                # notice instead of repeatedly selecting a broken pause scene.
                async with self.repository.database.sessions() as session:
                    key = await session.scalar(
                        select(deliveries.c.id)
                        .where(
                            deliveries.c.work_id == item["id"],
                            deliveries.c.kind == "notice",
                            deliveries.c.state.in_(("planned", "blocked")),
                        )
                        .order_by(deliveries.c.created)
                        .limit(1)
                    )
                if key is not None:
                    await self._record_notice_failure(control, key, exc)

    async def _record_notice_failure(
        self, control: WorkControl, key: str, exc: BaseException
    ) -> None:
        """Keep the original effect receipt; a failed notice is never a new pause."""
        from qq_ai_bot.runtime.delivery_intents import record

        assert control.current is not None
        async with self.repository.database.sessions() as session:
            effect = (
                (
                    await session.execute(
                        select(effects).where(
                            effects.c.effect_key == key,
                            effects.c.work_id == control.current["id"],
                        )
                    )
                )
                .mappings()
                .first()
            )
        receipt = json.loads(effect["receipt_json"]) if effect else {}
        if effect and effect["state"] == "accepted" and receipt.get("transport_accepted"):
            await record(control, key, "accepted", receipt)
        else:
            await record(
                control,
                key,
                "unknown" if effect and effect["state"] != "failed" else "failed",
                {"error_category": type(exc).__name__},
            )

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

            yield _ResumeScene(identity, key, snapshot, token, resolved, validate)

    def _child_resolver(self, identity: str) -> Callable[[str], Awaitable[dict[str, Any] | None]]:
        async def child(run_id: str) -> dict[str, Any] | None:
            task = await self.services.sandbox_tasks.by_run(run_id)
            if task is None or json.loads(task.source_json).get("work_id") != identity:
                return None
            return {"run_id": run_id, "pending": task.status != "completed"}

        return child

    async def _resume_self(self, item: dict[str, Any], source: dict[str, Any]) -> None:
        """Resume the original SELF Work through the same Main Agent entry point."""
        recovered = await recover_self_source(
            self.repository.database, item["conversation_id"], source, request_id=item["id"]
        )
        async with self._scene(item, source, recovered) as scene:
            if scene is None:
                return
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
                actor = ToolActor(
                    user_id="",
                    bot_user_id=recovered.bot_user_id,
                    group_id=recovered.external_target_id,
                    origin=TurnOrigin.SELF_INITIATIVE,
                    instruction=recovered.content,
                    execution_id=item["id"],
                    conversation_id=recovered.conversation_id,
                    presence_id=recovered.presence_id,
                    principal_kind="self",
                    initiative_run_id=recovered.run_id,
                )
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
                        actor_user_id="",
                        actor_is_superuser=False,
                        current_group_id=recovered.external_target_id,
                        conversation_key=key,
                        execution_id=item["id"],
                        origin=TurnOrigin.SELF_INITIATIVE,
                        initiative_run_id=recovered.run_id,
                        conversation_id=recovered.conversation_id,
                        scope_type=ScopeType.GROUP,
                        bot_user_id=recovered.bot_user_id,
                        external_target_id=recovered.external_target_id,
                        space_id=recovered.target_space_id,
                        presence_id=recovered.presence_id,
                        sandbox_source={**source, "work_id": item["id"]},
                        allow_work_environment=True,
                        allow_automation=True,
                    ),
                )
                self.last_error = (
                    result.outcome.failure.code
                    if result.outcome and result.outcome.failure
                    else None
                )

    async def _resume(self, item: dict[str, Any], source: dict[str, Any]) -> None:
        recovered = await recover_source(
            self.repository.database, item["conversation_id"], source, request_id=item["id"]
        )
        original = await self.services.ledger.get_event(recovered.event_id)
        if original is None:
            raise ValueError("work_source_deleted")
        async with self._scene(item, source, recovered) as scene:
            if scene is None:
                return
            identity, key, snapshot = scene.identity, scene.key, scene.snapshot
            token, resolved, validate = scene.token, scene.resolved, scene.validate

            async def deliver(text: str, effect_key: str) -> dict[str, Any]:
                from qq_ai_bot.domain.messages import OutboundMessage

                return await deliver_message(OutboundMessage(text=text), effect_key)

            async def deliver_message(message: Any, effect_key: str) -> dict[str, Any]:
                async def send() -> dict[str, Any]:
                    await validate()
                    group = original.scope_type is ScopeType.GROUP
                    sender = OneBotRouteSender(
                        resolved.connection.bot,
                        group=original.scope_type is ScopeType.GROUP,
                        target_id=recovered.external_target_id,
                    )
                    receipt = await sender.send(message)
                    outcome = {
                        "transport_accepted": True,
                        "text": message.text,
                        "message_id": receipt.platform_message_id,
                    }
                    await self.repository.record_effect(effect_key, "accepted", outcome)
                    await self.services.ledger.append(
                        bot_user_id=recovered.bot_user_id,
                        platform_message_id=receipt.platform_message_id,
                        scope_type=original.scope_type,
                        sender_user_id=recovered.bot_user_id,
                        direction="outbound",
                        content=message.text,
                        group_id=original.group_id,
                        private_peer_user_id=None if group else recovered.external_target_id,
                        sender_is_bot=True,
                        origin=TurnOrigin.SYSTEM_TASK.value,
                        caused_by_event_id=original.id,
                    )
                    return outcome

                return await self.services.run_effect(snapshot, send)

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
                resume_execution=item["state"] != "suspended",
            ) as control:
                if control.current is None or control.current["id"] != item["id"]:
                    raise WorkConflict("work_schedule_target_changed")
                if item["state"] == "suspended":
                    from qq_ai_bot.runtime.delivery_intents import record, reserve

                    async with self.repository.database.sessions() as session:
                        notices = (
                            (
                                await session.execute(
                                    select(deliveries)
                                    .where(
                                        deliveries.c.work_id == item["id"],
                                        deliveries.c.kind == "notice",
                                        deliveries.c.state.in_(("planned", "blocked")),
                                    )
                                    .order_by(deliveries.c.created)
                                    .limit(1)
                                )
                            )
                            .mappings()
                            .all()
                        )
                    control.ending = "suspended"
                    for notice in notices:
                        payload = json.loads(notice["payload_json"])
                        try:
                            await reserve(control, notice["id"], "notice", payload)
                            if not await self.repository.prepare_effect(
                                control.lease, item["id"], notice["id"], "progress"
                            ):
                                await self._record_notice_failure(
                                    control, notice["id"], WorkConflict("notice_effect_exists")
                                )
                                continue
                            await record(control, notice["id"], "dispatching", {})
                            outcome = await deliver(payload["text"], notice["id"])
                            await record(control, notice["id"], "accepted", outcome)
                        except BaseException as exc:
                            try:
                                await self._record_notice_failure(control, notice["id"], exc)
                            except BaseException as cleanup:
                                exc.add_note(
                                    f"notice reconciliation deferred: {type(cleanup).__name__}"
                                )
                            raise
                    return
                from qq_ai_bot.runtime.work_delivery import repair_receipt_ledger

                await repair_receipt_ledger(control, self.services.ledger)
                runtime = await self.services.runtime_config.snapshot(
                    user_id=recovered.actor_user_id, group_id=original.group_id
                )
                inbound = InboundMessage(
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
                    legacy_conversation_key=key,
                )
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
                        actor_user_id=recovered.actor_user_id,
                        actor_is_superuser=False,
                        allow_admin_actions=False,
                        allow_automation=bool(source.get("allow_automation")),
                        current_group_id=original.group_id,
                        conversation_key=key,
                        trigger_message_id=original.platform_message_id,
                        execution_id=item["id"],
                        origin=TurnOrigin(recovered.origin),
                        conversation_id=recovered.conversation_id,
                        person_id=recovered.actor_person_id,
                        space_id=recovered.target_space_id,
                        presence_id=recovered.presence_id,
                        sandbox_source={**source, "work_id": item["id"]},
                    ),
                )
                self.last_error = (
                    result.outcome.failure.code
                    if result.outcome and result.outcome.failure
                    else None
                )
                from qq_ai_bot.domain.messages import OutboundSendReceipt
                from qq_ai_bot.runtime.work_delivery import resume_delivery_plan

                class ResumeSender:
                    async def send_prepared(self, message: Any, key: str) -> OutboundSendReceipt:
                        outcome = await deliver_message(message, key)
                        return OutboundSendReceipt(str(outcome["message_id"]))

                if control.session and control.session.recovered_delivery == "delivery":
                    await resume_delivery_plan(control, ResumeSender())
