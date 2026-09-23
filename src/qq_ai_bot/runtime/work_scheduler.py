"""One bounded scheduler for persisted message work and completed child inputs."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, or_, select

from qq_ai_bot.adapters.onebot.sender import parse_onebot_send_receipt
from qq_ai_bot.conversation.scope import ConversationTurnSnapshot
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.domain.tool_actor import ToolActor
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.runtime.subagent_schema import children
from qq_ai_bot.runtime.trigger import WorkResumeTrigger
from qq_ai_bot.runtime.work_activation import activate_work
from qq_ai_bot.runtime.work_recovery_schema import deliveries, recovery
from qq_ai_bot.runtime.work_repository import WorkConflict, WorkLease, WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import inputs, scope, work
from qq_ai_bot.runtime.work_wait_schema import waits
from qq_ai_bot.sandbox.source_recovery import recover_self_source, recover_source
from qq_ai_bot.services.agent_tools import ToolRuntime

if TYPE_CHECKING:
    from qq_ai_bot.container import ApplicationContainer

logger = logging.getLogger(__name__)


class WorkScheduler:
    def __init__(self, app: ApplicationContainer) -> None:
        self.app = app
        self.repository = WorkRepository(app.database)
        self._worker: asyncio.Task[None] | None = None
        self._last_error: str | None = None
        self._last_reclaim = 0.0

    async def start(self) -> None:
        # Existing accepted Work must recover even when optional chat admission is off.
        # SELF always uses durable Work, including the legacy participation proposer.
        if self._worker is None:
            from qq_ai_bot.runtime.execution_receipts import PROCESS_ID

            await self.repository.repair_abandoned_inputs(PROCESS_ID)
            self._worker = asyncio.create_task(self._loop(), name="runtime-work-scheduler")

    async def close(self) -> None:
        task, self._worker = self._worker, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def health(self) -> dict[str, object]:
        async with self.app.database.sessions() as session:
            oldest = await session.scalar(
                select(func.min(inputs.c.created)).where(inputs.c.state.in_(("pending", "staged")))
            )
            active = await session.scalar(
                select(func.count())
                .select_from(work)
                .where(work.c.state.not_in(("completed", "failed", "cancelled")))
            )
            active_waits = await session.scalar(
                select(func.count()).select_from(waits).where(waits.c.status == "active")
            )
            oldest_wait = await session.scalar(
                select(func.min(waits.c.created)).where(waits.c.status == "active")
            )
        return {
            "pending_oldest_seconds": max(0, int(time.time() - oldest)) if oldest else 0,
            "active_work_count": active or 0,
            "active_wait_count": active_waits or 0,
            "active_wait_oldest_seconds": max(0, int(time.time() - oldest_wait))
            if oldest_wait
            else 0,
            "enabled": True,
            "chat_admission_enabled": self.app.settings.runtime_work_enabled,
            "running": self._worker is not None and not self._worker.done(),
            "last_error_category": self._last_error,
        }

    async def _loop(self) -> None:
        while True:
            try:
                await self.drain_once()
            except Exception as exc:
                self._last_error = type(exc).__name__
                logger.warning("work_scheduler_failed category=%s", self._last_error)
            await asyncio.sleep(2)

    async def drain_once(self) -> None:
        from qq_ai_bot.runtime.execution_receipts import PROCESS_ID

        await self.repository.repair_abandoned_inputs(PROCESS_ID)
        if time.monotonic() - self._last_reclaim > 600:
            await self.repository.reclaim_terminal()
            self._last_reclaim = time.monotonic()
        async with self.app.database.sessions() as session:
            rows = (
                (
                    await session.execute(
                        select(work)
                        .outerjoin(
                            scope,
                            scope.c.conversation_id == work.c.conversation_id,
                        )
                        .outerjoin(recovery, recovery.c.work_id == work.c.id)
                        .where(
                            or_(
                                work.c.state.in_(("queued", "running")),
                                (work.c.state == "suspended")
                                & work.c.id.in_(
                                    select(deliveries.c.work_id).where(
                                        deliveries.c.kind == "notice",
                                        deliveries.c.state.in_(("planned", "blocked")),
                                    )
                                ),
                            ),
                            or_(
                                func.json_extract(work.c.source_json, "$.owner")
                                == "plugin_invocation",
                                func.json_extract(work.c.source_json, "$.origin").in_(
                                    ("user_message", "autonomous_group", "self_initiative")
                                ),
                            ),
                            work.c.id.not_in(select(children.c.work_id)),
                            or_(scope.c.owner.is_(None), scope.c.lease_until <= time.time()),
                            or_(recovery.c.work_id.is_(None), recovery.c.not_before <= time.time()),
                        )
                        .order_by(work.c.updated)
                        .limit(8)
                    )
                )
                .mappings()
                .all()
            )
        for row in rows:
            source = json.loads(row["source_json"])
            plugin_owned = source.get("owner") == "plugin_invocation"
            if not plugin_owned and source.get("origin") not in {
                "user_message",
                "autonomous_group",
                "self_initiative",
            }:
                continue
            try:
                if plugin_owned:
                    from qq_ai_bot.plugin_host.main_turn import resume_plugin_work

                    await resume_plugin_work(self.app, dict(row), source)
                elif source.get("origin") == "self_initiative":
                    if row["state"] != "suspended":
                        await self._resume_self(dict(row), source)
                else:
                    await self._resume(dict(row), source)
            except WorkConflict:
                continue
            except Exception as exc:
                from qq_ai_bot.runtime.activation_outcome import (
                    WorkActivationHandled,
                    WorkRecoveryDeferred,
                )

                self._last_error = type(exc).__name__

                if isinstance(exc, (WorkActivationHandled, WorkRecoveryDeferred)):
                    continue
                lease = await self.repository.acquire(row["conversation_id"], row["generation"])
                if lease is not None:
                    try:
                        current = await self.repository.get(row["id"])
                        if current and current["state"] in {"queued", "running"}:
                            from qq_ai_bot.runtime.work_control import WorkControl

                            async def validate(owned_lease: WorkLease | None = lease) -> None:
                                assert owned_lease is not None
                                if not await self.repository.valid(owned_lease):
                                    raise WorkConflict("work_recovery_lease_lost")

                            control = WorkControl(
                                self.repository, lease, current["source_key"], source, validate
                            )
                            control.current = current
                            await control.recover_failure(exc)
                    finally:
                        await self.repository.release(lease)

    async def _resume_self(self, item: dict[str, Any], source: dict[str, Any]) -> None:
        """Resume the original SELF Work through the same Main Agent entry point."""
        recovered = await recover_self_source(
            self.app.database, item["conversation_id"], source, request_id=item["id"]
        )
        identity = ConversationScope.group(recovered.bot_user_id, recovered.external_target_id)
        state = await self.app.conversation_scopes.get(identity)
        if state is None or state.generation != recovered.generation:
            raise ValueError("work_generation_changed")
        key = state.runtime_scope_key or identity.key
        async with self.app.turn_coordinator.background_turn(key) as token:
            if token is None:
                return
            snapshot = ConversationTurnSnapshot(
                state.id,
                key,
                recovered.generation,
                None,
                token.version,
                identity.key,
                initiative_run_id=recovered.run_id,
            )
            resolved = await self.app.presence_router.resolve_presence(recovered.presence_id)

            async def validate() -> None:
                if (
                    await recover_self_source(
                        self.app.database, item["conversation_id"], source, request_id=item["id"]
                    )
                    != recovered
                ):
                    raise ValueError("work_source_changed")
                if not await self.app.chat._validate_turn_snapshot(snapshot):
                    raise WorkConflict("work_turn_changed")
                fresh = await self.app.presence_router.resolve_presence(recovered.presence_id)
                if fresh.connection.snapshot != resolved.connection.snapshot:
                    raise ValueError("work_connection_changed")

            async def child(run_id: str) -> dict[str, Any] | None:
                task = await self.app.sandbox_tasks.by_run(run_id)
                if task is None or json.loads(task.source_json).get("work_id") != item["id"]:
                    return None
                return {"run_id": run_id, "pending": task.status != "completed"}

            async with activate_work(
                self.repository,
                recovered.conversation_id,
                recovered.generation,
                item["source_key"],
                source,
                validate,
                child,
                work_id=item["id"],
            ) as control:
                if control.current is None or control.current["id"] != item["id"]:
                    raise WorkConflict("work_schedule_target_changed")
                self.app.chat._active_work[key] = control
                try:
                    runtime = await self.app.runtime_config.snapshot(
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
                    result = await self.app.chat.generate_self_initiative(
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
                    self._last_error = (
                        result.outcome.failure.code
                        if result.outcome and result.outcome.failure
                        else None
                    )
                finally:
                    if self.app.chat._active_work.get(key) is control:
                        self.app.chat._active_work.pop(key, None)

    async def _resume(self, item: dict[str, Any], source: dict[str, Any]) -> None:
        recovered = await recover_source(
            self.app.database, item["conversation_id"], source, request_id=item["id"]
        )
        original = await self.app.ledger.get_event(recovered.event_id)
        if original is None:
            raise ValueError("work_source_deleted")
        identity = (
            ConversationScope.group(recovered.bot_user_id, recovered.external_target_id)
            if recovered.target_space_id
            else ConversationScope.private(recovered.bot_user_id, recovered.external_target_id)
        )
        state = await self.app.conversation_scopes.get(identity)
        if state is None or state.generation != recovered.generation:
            raise ValueError("work_generation_changed")
        key = state.runtime_scope_key or identity.key
        async with self.app.turn_coordinator.background_turn(key) as token:
            if token is None:
                return
            snapshot = ConversationTurnSnapshot(
                state.id, key, recovered.generation, original.id, token.version, identity.key
            )
            resolved = await self.app.presence_router.resolve_presence(recovered.presence_id)

            async def validate() -> None:
                if (
                    await recover_source(
                        self.app.database, item["conversation_id"], source, request_id=item["id"]
                    )
                    != recovered
                ):
                    raise ValueError("work_source_changed")
                if not await self.app.chat._validate_turn_snapshot(snapshot):
                    raise WorkConflict("work_turn_changed")
                fresh = await self.app.presence_router.resolve_presence(recovered.presence_id)
                if fresh.connection.snapshot != resolved.connection.snapshot:
                    raise ValueError("work_connection_changed")

            async def deliver(text: str, effect_key: str) -> dict[str, Any]:
                from qq_ai_bot.domain.messages import OutboundMessage

                return await deliver_message(OutboundMessage(text=text), effect_key)

            async def deliver_message(message: Any, effect_key: str) -> dict[str, Any]:
                async def send() -> dict[str, Any]:
                    await validate()
                    group = original.scope_type is ScopeType.GROUP
                    call_api = getattr(resolved.connection.bot, "call_api", None)
                    if not callable(call_api):
                        raise ValueError("work_gateway_unavailable")
                    import base64

                    payload = []
                    if message.reply_to_message_id:
                        payload.append(
                            {"type": "reply", "data": {"id": message.reply_to_message_id}}
                        )
                    if message.text:
                        payload.append({"type": "text", "data": {"text": message.text}})
                    for media in message.media:
                        kind = "record" if media.kind.value == "audio" else "image"
                        if media.kind.value not in {"audio", "image"}:
                            raise ValueError("unsupported_persisted_delivery_media")
                        payload.append(
                            {
                                "type": kind,
                                "data": {
                                    "file": "base64://"
                                    + base64.b64encode(media.content).decode("ascii")
                                },
                            }
                        )
                    response = await call_api(
                        "send_group_msg" if group else "send_private_msg",
                        **{
                            "group_id" if group else "user_id": int(recovered.external_target_id),
                            "message": payload,
                        },
                    )
                    receipt = parse_onebot_send_receipt(response)
                    outcome = {
                        "transport_accepted": True,
                        "text": message.text,
                        "message_id": receipt.platform_message_id,
                    }
                    await self.repository.record_effect(effect_key, "accepted", outcome)
                    await self.app.ledger.append(
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

                return await self.app.chat._run_effect(snapshot, send)

            async def child(run_id: str) -> dict[str, Any] | None:
                task = await self.app.sandbox_tasks.by_run(run_id)
                if task is None or json.loads(task.source_json).get("work_id") != item["id"]:
                    return None
                return {"run_id": run_id, "pending": task.status != "completed"}

            async with activate_work(
                self.repository,
                recovered.conversation_id,
                recovered.generation,
                item["source_key"],
                source,
                validate,
                child,
                work_id=item["id"],
            ) as control:
                if control.current is None or control.current["id"] != item["id"]:
                    raise WorkConflict("work_schedule_target_changed")
                if item["state"] == "suspended":
                    from qq_ai_bot.runtime.delivery_intents import record, reserve

                    async with self.app.database.sessions() as session:
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
                        await reserve(control, notice["id"], "notice", payload)
                        if not await self.repository.prepare_effect(
                            control.lease, item["id"], notice["id"], "progress"
                        ):
                            await record(control, notice["id"], "unknown", {})
                            continue
                        await record(control, notice["id"], "dispatching", {})
                        try:
                            outcome = await deliver(payload["text"], notice["id"])
                        except BaseException:
                            await record(control, notice["id"], "unknown", {})
                            raise
                        await record(control, notice["id"], "accepted", outcome)
                    return
                self.app.chat._active_work[key] = control
                try:
                    from qq_ai_bot.runtime.work_delivery import repair_receipt_ledger

                    await repair_receipt_ledger(control, self.app.ledger)
                    runtime = await self.app.runtime_config.snapshot(
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
                    result = await self.app.chat.generate_main_agent_wakeup(
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
                    self._last_error = (
                        result.outcome.failure.code
                        if result.outcome and result.outcome.failure
                        else None
                    )
                    from qq_ai_bot.domain.messages import OutboundSendReceipt
                    from qq_ai_bot.runtime.work_delivery import resume_delivery_plan

                    class ResumeSender:
                        async def send_prepared(
                            self, message: Any, key: str
                        ) -> OutboundSendReceipt:
                            outcome = await deliver_message(message, key)
                            return OutboundSendReceipt(str(outcome["message_id"]))

                    if control.session and control.session.recovered_delivery == "delivery":
                        await resume_delivery_plan(control, ResumeSender())
                finally:
                    if self.app.chat._active_work.get(key) is control:
                        self.app.chat._active_work.pop(key, None)
