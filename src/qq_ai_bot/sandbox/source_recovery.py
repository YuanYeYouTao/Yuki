"""Recover message task anchors from current canonical state, without granting tools."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.identity.db_models import (
    CanonicalPersonModel,
    CanonicalSpaceModel,
    IdentityBindingModel,
    PresenceModel,
    SpaceBindingModel,
)
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.sandbox.db_models import SandboxTaskRunModel


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


async def recover_message_source(database: Database, request_id: str) -> MessageTaskSource:
    """Recheck on every use; the returned value is context, not a permission token.

    Scheduled automation has a different source/delegation contract and must never
    be reconstructed as a real user message through this entry point.
    """
    async with database.sessions() as session:
        task = await session.get(SandboxTaskRunModel, request_id)
        if task is None or task.status != "completed" or not task.completion_json:
            raise ValueError("task_not_completed")
        if json.loads(task.completion_json).get("status") == "cancelled":
            raise ValueError("task_cancelled")
        source = json.loads(task.source_json)
    return await recover_source(
        database, task.source_conversation_id, source, request_id=request_id
    )


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
            ChatEventModel.sender_user_id == source.get("actor_user_id"),
            ChatEventModel.event_kind == "message",
            ChatEventModel.direction == "inbound",
            ChatEventModel.author_kind == "person",
            ChatEventModel.id > conversation.starts_after_event_id,
        )
        events = list(await session.scalars(query.limit(2)))
        if len(events) != 1:
            raise ValueError("task_source_event_unavailable")
        event = events[0]
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
            bindings = list(
                await session.scalars(
                    select(SpaceBindingModel.id).where(
                        SpaceBindingModel.space_id == space.id,
                        SpaceBindingModel.platform == "qq",
                        SpaceBindingModel.status == "active",
                        SpaceBindingModel.external_space_id == event.group_id,
                    )
                )
            )
            if len(bindings) != 1 or not event.group_id:
                raise ValueError("task_space_binding_unavailable")
            binding_id, external_target = bindings[0], event.group_id
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
