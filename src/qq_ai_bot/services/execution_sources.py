"""Recover message task anchors from current canonical state, without granting tools."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import select

from qq_ai_bot.config import Settings
from qq_ai_bot.conversation.autonomy_db_models import InitiativeRunModel
from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.domain.tool_actor import ToolActor
from qq_ai_bot.identity.db_models import (
    CanonicalPersonModel,
    CanonicalSpaceModel,
    IdentityBindingModel,
    PresenceModel,
    SpaceBindingModel,
)
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import AutomationModel, AutomationRunModel, ChatEventModel
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.runtime.trigger import SelfInitiativeTrigger


@dataclass(frozen=True)
class MessageTaskSource:
    request_id: str
    conversation_id: str
    generation: int
    event_id: int
    origin: str
    actor_person_id: str
    actor_user_id: str
    target_person_id: str | None
    target_space_id: str | None
    presence_id: str
    bot_user_id: str
    binding_id: str
    external_target_id: str
    content: str

    def inbound(self, original: Any, **extra: Any) -> InboundMessage:
        """The original ledger event as the trusted inbound; never a new message."""
        return InboundMessage(
            message_id=original.platform_message_id,
            source_event_id=original.id,
            event_type="message",
            scope_type=original.scope_type,
            sender=SenderIdentity(self.actor_user_id),
            text=original.content,
            bot_user_id=self.bot_user_id,
            group_id=original.group_id,
            received_at=original.occurred_at,
            person_id=self.actor_person_id,
            space_id=self.target_space_id,
            conversation_id=self.conversation_id,
            presence_id=self.presence_id,
            **extra,
        )


@dataclass(frozen=True)
class SelfTaskSource:
    """Accepted SELF scene; it deliberately has no person or message anchor."""

    request_id: str
    conversation_id: str
    generation: int
    run_id: str
    target_space_id: str
    presence_id: str
    bot_user_id: str
    binding_id: str
    external_target_id: str
    content: str
    origin: str = TurnOrigin.SELF_INITIATIVE.value
    actor_user_id: str = ""
    actor_person_id: None = None
    event_id: None = None

    def actor(self, execution_id: str) -> ToolActor:
        """The stable SELF principal of the original run; no person is borrowed."""
        return ToolActor(
            user_id="",
            bot_user_id=self.bot_user_id,
            group_id=self.external_target_id,
            origin=TurnOrigin.SELF_INITIATIVE,
            instruction=self.content,
            execution_id=execution_id,
            conversation_id=self.conversation_id,
            presence_id=self.presence_id,
            principal_kind="self",
            initiative_run_id=self.run_id,
        )

    def trigger(self) -> SelfInitiativeTrigger:
        return SelfInitiativeTrigger(
            run_id=self.run_id,
            conversation_id=self.conversation_id,
            generation=self.generation,
            space_id=self.target_space_id,
            presence_id=self.presence_id,
            group_id=self.external_target_id,
            bot_user_id=self.bot_user_id,
            instruction=self.content,
        )


@dataclass(frozen=True)
class AutomationTaskSource:
    """The original scheduled run and creator, without a message or initiative."""

    request_id: str
    conversation_id: str
    generation: int
    automation_run_id: int
    principal_kind: Literal["person", "self"]
    actor_person_id: str | None
    actor_user_id: str = field(compare=False)
    target_person_id: str | None
    target_space_id: str | None
    presence_id: str
    bot_user_id: str
    external_target_id: str = field(compare=False)
    content: str = field(compare=False)
    origin: str = TurnOrigin.SCHEDULED_AUTOMATION.value
    event_id: None = None

    def actor(self, execution_id: str) -> ToolActor:
        return ToolActor(
            user_id=self.actor_user_id,
            bot_user_id=self.bot_user_id,
            group_id=self.external_target_id if self.target_space_id else None,
            origin=TurnOrigin.SCHEDULED_AUTOMATION,
            instruction=self.content,
            execution_id=execution_id,
            person_id=self.actor_person_id,
            conversation_id=self.conversation_id,
            presence_id=self.presence_id,
            principal_kind=self.principal_kind,
            automation_run_id=self.automation_run_id if self.principal_kind == "self" else None,
        )


async def recover_automation_source(
    database: Database,
    conversation_id: str,
    source: dict[str, Any],
    *,
    request_id: str,
    settings: Settings | None,
) -> AutomationTaskSource:
    from qq_ai_bot.runtime.work_recovery_schema import invocations
    from qq_ai_bot.runtime.work_schema_v1 import work

    async with database.sessions() as session:
        run = await session.get(AutomationRunModel, source.get("automation_run_id"))
        owner = await session.get(AutomationModel, run.automation_id) if run else None
        conversation = await session.get(CanonicalConversationModel, conversation_id)
        cursor = (
            (await session.execute(select(invocations).where(invocations.c.run_id == run.id)))
            .mappings()
            .first()
            if run
            else None
        )
        if (
            run is None
            or owner is None
            or conversation is None
            or cursor is None
            or run.status != "running"
            or owner.status != "active"
            or source.get("owner") != "automation"
            or owner.id != source.get("automation_id")
            or cursor["script_hash"] != owner.script_hash
            or source.get("parent_execution_id")
            != f"automation:{run.id}:{source.get('step_id')}:{owner.script_hash}"
            or source.get("conversation_id") != conversation_id
            or conversation.generation != source.get("generation")
            or owner.creator_kind != source.get("principal_kind", "person")
            or owner.canonical_creator_person_id != source.get("actor_person_id")
            or owner.canonical_presence_id != source.get("presence_id")
            or owner.bot_user_id != source.get("bot_user_id")
            or owner.canonical_target_space_id != conversation.space_id
            or owner.canonical_target_person_id != conversation.person_id
        ):
            raise ValueError("automation_task_source_changed")
        presence = await session.get(PresenceModel, owner.canonical_presence_id)
        if (
            presence is None
            or not presence.enabled
            or presence.platform != "qq"
            or presence.external_account_id != owner.bot_user_id
        ):
            raise ValueError("task_presence_disabled")
        actor_person = owner.canonical_creator_person_id
        actor_user = ""
        if owner.creator_kind == "self":
            scene = json.loads(owner.authority_snapshot_json)
            if (
                actor_person is not None
                or source.get("actor_user_id")
                or source.get("actor_person_id")
                or source.get("trigger_event_id") is not None
                or source.get("initiative_run_id") is not None
                or scene.get("canonical_conversation_id") != conversation_id
                or scene.get("conversation_generation") != conversation.generation
                or scene.get("canonical_space_id") != conversation.space_id
                or scene.get("canonical_presence_id") != presence.id
            ):
                raise ValueError("invalid_self_automation_source")
        else:
            from qq_ai_bot.automation.control_context import resolve_execution_identity

            assert settings is not None and actor_person is not None
            actor_user, _ = await resolve_execution_identity(
                session, settings, owner_id=actor_person
            )
        if conversation.space_id:
            space = await session.get(CanonicalSpaceModel, conversation.space_id)
            binding = await session.scalar(
                select(SpaceBindingModel).where(
                    SpaceBindingModel.space_id == conversation.space_id,
                    SpaceBindingModel.platform == "qq",
                    SpaceBindingModel.status == "active",
                    SpaceBindingModel.external_space_id == source.get("current_group_id"),
                )
            )
            if space is None or not space.enabled or binding is None:
                raise ValueError("task_space_binding_unavailable")
            target = binding.external_space_id
            if owner.creator_kind == "self" and target != scene.get("current_group_id"):
                raise ValueError("automation_task_source_changed")
        else:
            target = actor_user
        content = await session.scalar(select(work.c.goal).where(work.c.id == request_id))
        return AutomationTaskSource(
            request_id,
            conversation_id,
            conversation.generation,
            run.id,
            "self" if owner.creator_kind == "self" else "person",
            actor_person,
            actor_user,
            conversation.person_id,
            conversation.space_id,
            presence.id,
            owner.bot_user_id,
            target,
            content or "",
        )


async def recover_self_source(
    database: Database,
    conversation_id: str,
    source: dict[str, Any],
    *,
    request_id: str,
) -> SelfTaskSource:
    """Recheck the accepted run and original scene, never a controller's latest actor.

    Owner switches stop admission only. They cannot invalidate already accepted work;
    generation reset, disabled identity, or a terminal run still fence execution.
    """
    if (
        source.get("origin") != TurnOrigin.SELF_INITIATIVE.value
        or source.get("principal_kind") != "self"
        or source.get("actor_user_id")
        or source.get("person_id")
        or source.get("actor_person_id")
        or source.get("trigger_event_id") is not None
        or source.get("conversation_id") != conversation_id
        or not isinstance(source.get("instruction"), str)
        or not source["instruction"].strip()
    ):
        raise ValueError("invalid_self_task_source")
    async with database.sessions() as session:
        run = await session.get(InitiativeRunModel, source.get("initiative_run_id"))
        conversation = await session.get(CanonicalConversationModel, conversation_id)
        if (
            run is None
            or conversation is None
            or conversation.kind != "space"
            or run.conversation_id != conversation.id
            or type(source.get("generation")) is not int
            or run.generation != source["generation"]
            or conversation.generation != run.generation
            or run.space_id != conversation.space_id
            or run.space_id != source.get("space_id")
            or run.presence_id != source.get("presence_id")
        ):
            raise ValueError("self_task_source_changed")
        if run.state not in {"accepted", "running"}:
            raise ValueError("self_task_terminal")
        space = await session.get(CanonicalSpaceModel, run.space_id)
        presence = await session.get(PresenceModel, run.presence_id)
        if space is None or not space.enabled:
            raise ValueError("task_space_disabled")
        if (
            presence is None
            or not presence.enabled
            or presence.platform != "qq"
            or presence.external_account_id != source.get("bot_user_id")
        ):
            raise ValueError("task_presence_disabled")
        binding = await session.scalar(
            select(SpaceBindingModel.id).where(
                SpaceBindingModel.space_id == space.id,
                SpaceBindingModel.platform == "qq",
                SpaceBindingModel.status == "active",
                SpaceBindingModel.external_space_id == source.get("group_id"),
            )
        )
        if binding is None or not source.get("group_id"):
            raise ValueError("task_space_binding_unavailable")
        recovered = SelfTaskSource(
            request_id,
            conversation.id,
            run.generation,
            run.id,
            space.id,
            presence.id,
            presence.external_account_id,
            binding,
            source["group_id"],
            source["instruction"],
        )
    from qq_ai_bot.conversation.self_initiative import validate_self_initiative

    try:
        await validate_self_initiative(
            database,
            recovered.run_id,
            conversation_id=recovered.conversation_id,
            space_id=recovered.target_space_id,
            presence_id=recovered.presence_id,
        )
    except PermissionError as exc:
        raise ValueError(str(exc)) from exc
    return recovered


async def recover_execution_source(
    database: Database,
    conversation_id: str,
    source: dict[str, Any],
    *,
    request_id: str,
    settings: Settings | None = None,
) -> MessageTaskSource | SelfTaskSource | AutomationTaskSource:
    if source.get("origin") == TurnOrigin.SCHEDULED_AUTOMATION.value:
        return await recover_automation_source(
            database, conversation_id, source, request_id=request_id, settings=settings
        )
    if source.get("origin") == TurnOrigin.SELF_INITIATIVE.value:
        return await recover_self_source(database, conversation_id, source, request_id=request_id)
    return await recover_source(database, conversation_id, source, request_id=request_id)


async def recover_source(
    database: Database,
    conversation_id: str,
    source: dict[str, Any],
    *,
    request_id: str,
) -> MessageTaskSource:
    """Shared canonical validation for a persisted message-origin work source."""
    async with database.sessions() as session:
        if source.get("origin") not in {"user_message", "autonomous_group"}:
            raise ValueError("not_a_message_task")
        conversation = await session.get(CanonicalConversationModel, conversation_id)
        generation = source.get("generation")
        if (
            conversation is None
            or type(generation) is not int
            or conversation.generation != generation
        ):
            raise ValueError("task_conversation_changed")
        event_id = source.get("trigger_event_id")
        if type(event_id) is not int or event_id <= 0:
            raise ValueError("invalid_task_event_anchor")
        query = select(ChatEventModel).where(
            ChatEventModel.id == event_id,
            ChatEventModel.canonical_conversation_id == conversation.id,
            ChatEventModel.bot_user_id == source.get("bot_user_id"),
            ChatEventModel.event_kind == "message",
            ChatEventModel.direction == "inbound",
            ChatEventModel.author_kind == "person",
            ChatEventModel.id > conversation.starts_after_event_id,
        )
        events = list(await session.scalars(query.limit(2)))
        if len(events) != 1:
            raise ValueError("task_source_event_unavailable")
        event = events[0]
        if (
            source.get("actor_person_id") is not None
            and event.author_person_id != source["actor_person_id"]
        ):
            raise ValueError("task_source_identity_unavailable")
        if not event.author_person_id or not event.ingress_presence_id:
            raise ValueError("task_source_identity_unavailable")
        if event.ingress_presence_id != source.get("presence_id"):
            raise ValueError("task_source_presence_changed")
        actor = await session.get(CanonicalPersonModel, event.author_person_id)
        presence = await session.get(PresenceModel, event.ingress_presence_id)
        if actor is None or not actor.enabled:
            raise ValueError("task_actor_disabled")
        if (
            presence is None
            or not presence.enabled
            or presence.platform != "qq"
            or presence.external_account_id != event.bot_user_id
        ):
            raise ValueError("task_presence_disabled")
        actor_binding = await session.scalar(
            select(IdentityBindingModel.id).where(
                IdentityBindingModel.person_id == actor.id,
                IdentityBindingModel.external_account_id == event.sender_user_id,
                IdentityBindingModel.platform == "qq",
                IdentityBindingModel.status == "active",
            )
        )
        if actor_binding is None:
            raise ValueError("task_actor_binding_unavailable")
        if conversation.kind == "private":
            if (
                conversation.person_id != actor.id
                or event.private_peer_user_id != event.sender_user_id
            ):
                raise ValueError("task_target_changed")
            binding_id, external_target = actor_binding, event.sender_user_id
        else:
            space = await session.get(CanonicalSpaceModel, conversation.space_id)
            if space is None or not space.enabled:
                raise ValueError("task_space_disabled")
            if source["origin"] == "autonomous_group" and not space.autonomous_enabled:
                raise ValueError("task_autonomous_disabled")
            binding = await session.scalar(
                select(SpaceBindingModel.id).where(
                    SpaceBindingModel.space_id == space.id,
                    SpaceBindingModel.platform == "qq",
                    SpaceBindingModel.status == "active",
                    SpaceBindingModel.external_space_id == event.group_id,
                )
            )
            if binding is None or not event.group_id:
                raise ValueError("task_space_binding_unavailable")
            binding_id, external_target = binding, event.group_id
        return MessageTaskSource(
            request_id,
            conversation.id,
            generation,
            event.id,
            source["origin"],
            actor.id,
            event.sender_user_id,
            conversation.person_id,
            conversation.space_id,
            presence.id,
            presence.external_account_id,
            binding_id,
            external_target,
            event.content,
        )
