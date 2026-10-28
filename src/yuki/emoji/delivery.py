"""Optional illustration selection, independent of the producing model activation."""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time
from dataclasses import asdict
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.domain.tool_actor import ToolActor
from yuki.emoji.models import EmojiAsset
from yuki.gateway.social import TransportMessage, prepared_transport, transport_payload
from yuki.identity.person_identity import same_person_id
from yuki.persistence.database import write_failure_committed, write_failure_phase
from yuki.persistence.worker_recovery import recover_database_loop
from yuki.runtime.origin import TurnOrigin
from yuki.runtime.subagent_schema import children
from yuki.runtime.work_schema_v1 import scope
from yuki.social.db_models import SocialEmojiIntentModel
from yuki.social.emoji_intents import (
    EmojiIntent,
    EmojiIntentRepository,
    PreparedEmojiIntent,
    derived_call_id,
    validate_source,
)
from yuki.social.models import SocialError, SocialTarget
from yuki.social.repository import payload_hash
from yuki.workspace.store import WorkspaceError

if TYPE_CHECKING:
    from yuki.emoji.library import EmojiLibrary
    from yuki.identity.routing import ResolvedSend
    from yuki.social.service import SocialContext, SocialService

logger = logging.getLogger(__name__)


def _mark(intent_id: str, stage: str, started: float, phase_started: float) -> float:
    now = time.monotonic()
    logger.info(
        "emoji_stage intent_id=%s stage=%s since_attempt_ms=%d phase_ms=%d",
        intent_id,
        stage,
        int((now - started) * 1000),
        int((now - phase_started) * 1000),
    )
    return now


class EmojiDeliveryService:
    def __init__(self, social: SocialService, library: EmojiLibrary) -> None:
        self.social = social
        self.library = library
        self.intents = EmojiIntentRepository(social.database)
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._deliveries: dict[str, asyncio.Task[None]] = {}

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(
            self._run(), name="social-illustrations", context=contextvars.Context()
        )
        self.wake()

    def wake(self) -> None:
        self._wake.set()

    async def close(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        tasks = tuple(self._deliveries.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def source(
        self,
        context: SocialContext,
        route: ResolvedSend,
    ) -> dict[str, Any]:
        """Capture real source facts before text dispatch, without retaining its runtime."""
        from yuki.runtime.effect_outcomes import current_result_capture
        from yuki.runtime.work_activation import current_work_control

        actor = asdict(context.actor) if context.actor is not None else None
        if actor is not None:
            actor["instruction"] = ""
            actor["mentioned_user_ids"] = ()
            actor["mentioned_person_ids"] = ()
        original = {
            key: getattr(context, key)
            for key in (
                "turn_id",
                "call_id",
                "conversation_id",
                "space_id",
                "trigger_event_id",
                "origin",
                "caused_by_event_id",
                "initiative_run_id",
                "automation_run_id",
                "conversation_key",
            )
        }
        original.update(
            actor=actor,
            presence_id=context.presence_id,
            reply_presence_id=context.reply_presence_id,
        )
        control = current_work_control.get()
        capture = current_result_capture.get()
        if capture is not None and not (capture.work_id and capture.effect_key):
            capture = None
        work_id = (
            capture.work_id
            if capture is not None
            else control.current["id"]
            if control is not None and control.current is not None
            else None
        )
        async with self.social.database.read_session() as reader:
            await reader.execute(text("BEGIN"))
            conversation = await reader.get(CanonicalConversationModel, context.conversation_id)
            if conversation is None:
                raise SocialError("emoji_source_removed")
            epoch = await reader.scalar(
                select(scope.c.cancel_epoch).where(scope.c.conversation_id == conversation.id)
            )
            child_epoch = (
                await reader.scalar(
                    select(children.c.cancel_epoch).where(children.c.work_id == work_id)
                )
                if work_id is not None
                else None
            )
        return {
            "context": original,
            "content": transport_payload(TransportMessage()),
            "binding_id": route.binding_id,
            "target_presence_id": route.presence_id,
            "platform": route.platform,
            "route": {
                "presence_id": route.presence_id,
                "binding_id": route.binding_id,
                "conversation_id": route.conversation_id,
                "platform": route.platform,
                "kind": route.kind,
                "thread_id": route.thread_id,
                "provider_id": route.connection.snapshot.provider,
                "local_ledger": "local_ledger" in route.connection.snapshot.capabilities,
            },
            "generation": control.lease.generation
            if control is not None
            else conversation.generation,
            "cancel_epoch": (
                control.lease.cancel_epoch
                if control is not None and control.lease.work_id is None
                else epoch or 0
            ),
            "work_id": work_id,
            "work_cancel_epoch": (
                control.lease.cancel_epoch
                if control is not None and control.lease.work_id is not None
                else child_epoch
            ),
        }

    def prepare_admission(
        self,
        arguments: dict[str, Any],
        context: SocialContext,
        target: SocialTarget,
        source: dict[str, Any],
    ) -> PreparedEmojiIntent:
        return self.intents.prepare(
            turn_id=context.turn_id,
            call_id=context.call_id,
            conversation_id=context.conversation_id,
            target=target,
            arguments=arguments,
            source=source,
        )

    async def admit(
        self,
        *,
        prepared: PreparedEmojiIntent,
        session: AsyncSession | None = None,
    ) -> dict[str, Any]:
        from yuki.runtime.effect_outcomes import current_result_capture

        capture = current_result_capture.get()
        if capture is not None and not (capture.work_id and capture.effect_key):
            capture = None
        receipt = await self.intents.admit(
            prepared=prepared,
            work_effect=capture if not prepared.has_text else None,
            session=session,
        )
        if session is None:
            self.wake()
        return receipt

    async def project(self, context: SocialContext) -> dict[str, Any] | None:
        intent = await self.intents.find(context.turn_id, context.call_id)
        return await self._project(intent) if intent is not None else None

    async def replay(
        self, arguments: dict[str, Any], context: SocialContext
    ) -> dict[str, Any] | None:
        intent = await self.intents.find(context.turn_id, context.call_id)
        if intent is None:
            return None
        if intent.payload_hash != payload_hash(
            "send_message", intent.target, arguments, context.conversation_id
        ):
            raise SocialError("idempotency_conflict")
        if not intent.closed:
            self.wake()
        return await self._project(intent)

    async def _project(self, intent: EmojiIntent) -> dict[str, Any]:
        async with self.social.database.read_session() as session:
            await session.execute(text("BEGIN"))
            return await self.intents.project(session, intent)

    async def validate_claim(
        self, session: AsyncSession, identity: str, target: SocialTarget, route: ResolvedSend
    ) -> None:
        def source(path: str) -> Any:
            return func.json_extract(SocialEmojiIntentModel.source_json, path)

        current = (
            await session.execute(
                select(
                    SocialEmojiIntentModel.target_kind,
                    SocialEmojiIntentModel.target_id,
                    source("$.target_presence_id").label("presence_id"),
                    source("$.binding_id").label("binding_id"),
                    source("$.platform").label("platform"),
                    source("$.context.conversation_id").label("conversation_id"),
                    source("$.generation").label("generation"),
                    source("$.cancel_epoch").label("cancel_epoch"),
                    source("$.work_id").label("work_id"),
                    source("$.work_cancel_epoch").label("work_cancel_epoch"),
                    source("$.context.trigger_event_id").label("trigger_event_id"),
                    source("$.context.caused_by_event_id").label("caused_by_event_id"),
                    source("$.context.actor.person_id").label("person_id"),
                    source("$.route.conversation_id").label("target_conversation_id"),
                    source("$.route.thread_id").label("target_thread_id"),
                ).where(SocialEmojiIntentModel.id == identity)
            )
        ).first()
        if current is None:
            raise SocialError("emoji_source_removed")
        same_target = current.target_kind == target.kind and (
            current.target_id == str(target.id)
            or (
                target.kind == "person"
                and await same_person_id(session, current.target_id, str(target.id))
            )
        )
        if (
            not same_target
            or current.presence_id != route.presence_id
            or current.binding_id != route.binding_id
            or current.platform != route.platform
            or (
                current.target_conversation_id
                and current.target_conversation_id != route.conversation_id
            )
            or (current.target_thread_id and current.target_thread_id != route.thread_id)
        ):
            raise SocialError("emoji_route_changed")
        await validate_source(
            session,
            {
                "context": {
                    "conversation_id": current.conversation_id,
                    "trigger_event_id": current.trigger_event_id,
                    "caused_by_event_id": current.caused_by_event_id,
                    "actor": {"person_id": current.person_id},
                },
                "generation": current.generation,
                "cancel_epoch": current.cancel_epoch,
                "work_id": current.work_id,
                "work_cancel_epoch": current.work_cancel_epoch,
            },
        )

    async def _run(self) -> None:
        await recover_database_loop(self._run_queue, stop=self._stop, logger=logger)

    async def _run_queue(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            cursor = None
            while pending := await self.intents.pending(cursor, active=self._deliveries):
                for intent in pending:
                    if intent.id not in self._deliveries:
                        task = asyncio.create_task(
                            self._run_intent(intent), name=f"social-illustration:{intent.id}"
                        )
                        self._deliveries[intent.id] = task
                        task.add_done_callback(partial(self._finished, intent.id))
                cursor = (pending[-1].created_at, pending[-1].id)
            await self._wake.wait()

    def _finished(self, identity: str, task: asyncio.Task[None]) -> None:
        self._deliveries.pop(identity, None)
        if not task.cancelled() and task.exception() is not None:
            logger.error("social_illustration_task_failed", exc_info=task.exception())
        self.wake()

    async def _run_intent(self, intent: EmojiIntent) -> None:
        async def resume() -> None:
            async with self.social.database.read_session() as session:
                current = await self.intents.get(intent.id, session=session)
            if not current.closed:
                await self._deliver(current)

        await recover_database_loop(resume, stop=self._stop, logger=logger)

    @staticmethod
    def _send_input(intent: EmojiIntent, artifact_id: str) -> tuple[dict[str, Any], SocialContext]:
        from yuki.social.service import SocialContext

        values = dict(intent.source["context"])
        actor = values.pop("actor")
        if actor is not None:
            actor = {key: value for key, value in actor.items() if key != "platform_message_id"}
            actor["origin"] = TurnOrigin(actor["origin"])
            values["actor"] = ToolActor(**actor)
        content = prepared_transport(intent.source["content"])
        reply_id = intent.source.get("reply_to_event_id")
        stored_route = intent.source.get("route") or {}
        values.update(
            call_id=derived_call_id(intent.id),
            deferred_emoji_id=intent.id,
            media_metadata=intent.selected,
            target_presence_id=stored_route.get("presence_id")
            or intent.source["target_presence_id"],
            prepared_content=intent.source["content"],
        )
        target = {"kind": intent.target.kind, "target_id": str(intent.target.id)}
        if stored_route.get("conversation_id"):
            target["conversation_id"] = stored_route["conversation_id"]
        if intent.target.kind == "person":
            target["binding_id"] = stored_route.get("binding_id") or intent.source["binding_id"]
        args: dict[str, Any] = {
            "text": content.text,
            "target": target,
            "artifact_id": artifact_id,
            "attachment_kind": "image",
        }
        if reply_id is not None:
            args["reply_to_event_id"] = reply_id
        return args, SocialContext(**values)

    async def _committed_target_conversation(self, intent: EmojiIntent) -> str | None:
        """Use the conversation the original text send already committed."""
        import json

        from yuki.social.db_models import SocialOperationModel
        from yuki.social.repository import sequence_prefix

        parent_id = intent.source.get("after_operation_id")
        if not parent_id:
            return None
        async with self.social.database.read_session() as session:
            parent = await session.get(SocialOperationModel, parent_id)
            if parent is None:
                return None
            if parent.prepared_json:
                route = json.loads(parent.prepared_json).get("route")
                if isinstance(route, dict) and route.get("conversation_id"):
                    return str(route["conversation_id"])
            if parent.original_conversation_id:
                return parent.original_conversation_id
            if parent.action != "send_message_sequence":
                return None
            prefix = sequence_prefix(parent.tool_call_id)
            return await session.scalar(
                select(SocialOperationModel.original_conversation_id).where(
                    SocialOperationModel.source_turn_id == parent.source_turn_id,
                    SocialOperationModel.tool_call_id.in_(
                        tuple(f"{prefix}{index}" for index in range(parent.planned_parts or 0))
                    ),
                    SocialOperationModel.original_conversation_id.is_not(None),
                )
            )

    async def _deliver(self, intent: EmojiIntent) -> None:
        started = time.monotonic()
        phase_started = started
        phase = "source_validation"
        artifact_id = None
        created = (
            intent.created_at.replace(tzinfo=UTC)
            if intent.created_at.tzinfo is None
            else intent.created_at
        )
        logger.info(
            "emoji_attempt intent_id=%s call_id=%s created_to_attempt_ms=%d",
            intent.id,
            derived_call_id(intent.id),
            int((datetime.now(UTC) - created).total_seconds() * 1000),
        )
        try:
            if intent.selected is not None:
                facts = await self._project(intent)
                if facts["status"] != "accepted":
                    await self.intents.close(intent.id)
                    return
            async with self.social.database.read_session() as session:
                await validate_source(session, intent.source)
            phase_started = _mark(intent.id, "source_validated", started, phase_started)
            phase = "selection"
            if intent.selected is None:
                try:
                    asset = await self.library.select(
                        **intent.intent, platform=intent.source["platform"]
                    )
                except (asyncio.CancelledError, SQLAlchemyError):
                    raise
                except Exception as exc:
                    if write_failure_phase(exc) is not None and (
                        write_failure_committed(exc) is not False
                        or not isinstance(exc, (SocialError, WorkspaceError))
                    ):
                        raise
                    intent = await self.intents.close(
                        intent.id,
                        str(exc)
                        if isinstance(exc, (SocialError, WorkspaceError))
                        else type(exc).__name__,
                        unselected_only=True,
                    )
                else:
                    intent = (
                        await self.intents.close(
                            intent.id, "emoji_no_candidate", unselected_only=True
                        )
                        if asset is None
                        else await self.intents.select(
                            intent.id,
                            asset.model_dump(mode="json", exclude={"description", "ocr_text"}),
                        )
                    )
            if intent.closed:
                return
            assert intent.selected is not None
            asset = EmojiAsset.model_validate(intent.selected)
            phase_started = _mark(intent.id, "selection_settled", started, phase_started)
            phase = "artifact_publication"
            metadata = await self.library.publish(asset, request_id=intent.id)
            artifact_id = metadata["artifact_id"]
            phase_started = _mark(intent.id, "artifact_published", started, phase_started)
            phase = "text_dispatch_check"
            async with self.social.database.read_session() as session:
                text_finished = await EmojiIntentRepository._text_dispatch_finished(session, intent)
            phase_started = _mark(intent.id, "text_dispatch_checked", started, phase_started)
            if not text_finished:
                phase = "waiting_for_text"
                return
            stored_route = dict(intent.source.get("route") or {})
            if not stored_route.get("conversation_id"):
                conversation_id = await self._committed_target_conversation(intent)
                if not conversation_id:
                    await self.intents.close(intent.id, "emoji_target_unavailable")
                    return
                stored_route["conversation_id"] = conversation_id
                intent.source["route"] = stored_route
            args, context = self._send_input(intent, metadata["artifact_id"])
            phase = "social_execution"
            await self.social.execute("send", args, context)
            phase_started = _mark(intent.id, "social_finished", started, phase_started)
            phase = "intent_close"
            await self.intents.close(intent.id)
            _mark(intent.id, "intent_closed", started, phase_started)
            phase = "closed"
        except asyncio.CancelledError:
            raise
        except SQLAlchemyError:
            # Only database preparation/settlement is resumed; Social owns its
            # original dispatch CAS and never repeats an unknown network effect.
            raise
        except Exception as exc:
            if write_failure_phase(exc) is not None and (
                write_failure_committed(exc) is not False
                or not isinstance(exc, (SocialError, WorkspaceError))
            ):
                raise
            logger.warning("social_illustration_failed category=%s", type(exc).__name__)
            await self.intents.close(
                intent.id,
                str(exc) if isinstance(exc, (SocialError, WorkspaceError)) else type(exc).__name__,
            )
        finally:
            logger.info(
                "emoji_attempt_finished intent_id=%s call_id=%s artifact_id=%s "
                "phase=%s since_attempt_ms=%d",
                intent.id,
                derived_call_id(intent.id),
                artifact_id,
                phase,
                int((time.monotonic() - started) * 1000),
            )
