"""Recover message task anchors from current canonical state, without granting tools."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.config import Settings
from yuki.conversation.autonomy_db_models import InitiativeRunModel
from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.domain.messages import InboundMessage, SenderIdentity
from yuki.domain.tool_actor import ToolActor
from yuki.identity.db_models import (
    CanonicalPersonModel,
    CanonicalSpaceModel,
    PresenceModel,
    SpaceBindingModel,
)
from yuki.identity.person_identity import current_person_id, same_person_id
from yuki.persistence.database import Database
from yuki.persistence.models import AutomationRunModel, ChatEventModel
from yuki.runtime.origin import TurnOrigin
from yuki.runtime.trigger import SelfInitiativeTrigger


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
    platform: str
    thread_id: str | None = None

    def inbound(self, original: Any, **extra: Any) -> InboundMessage:
        """The original ledger event as the trusted inbound; never a new message."""

        return InboundMessage(
            message_id=original.platform_message_id,
            source_event_id=original.id,
            event_type="message",
            scope_type=original.scope_type,
            sender=SenderIdentity(
                self.actor_user_id, nickname=original.sender_nickname, nickname_known=True
            ),
            text=original.content,
            bot_user_id=self.bot_user_id,
            platform=self.platform,
            group_id=original.group_id,
            received_at=original.occurred_at,
            person_id=self.actor_person_id,
            space_id=self.target_space_id,
            conversation_id=self.conversation_id,
            presence_id=self.presence_id,
            thread_id=self.thread_id,
            mentioned_person_ids=original.mentioned_person_ids,
            reply_to_event_id=original.reply_to_event_id,
            reply_to_message_id=original.reply_to_message_id,
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
    platform: str
    thread_id: str | None = None
    origin: str = TurnOrigin.SELF_INITIATIVE.value
    actor_user_id: str = ""
    actor_person_id: None = None
    event_id: None = None
    actor_is_superuser: bool = False

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
            platform=self.platform,
            space_id=self.target_space_id,
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
            platform=self.platform,
            thread_id=self.thread_id,
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
    platform: str
    thread_id: str | None = None
    origin: str = TurnOrigin.SCHEDULED_AUTOMATION.value
    event_id: None = None
    actor_is_superuser: bool = False

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
            platform=self.platform,
            space_id=self.target_space_id,
        )


@dataclass(frozen=True)
class BackgroundTaskSource:
    """Original plugin Job and canonical target, without a fabricated Person."""

    request_id: str
    conversation_id: str
    generation: int
    event_id: int
    plugin_id: str
    job_id: int
    target_person_id: str | None
    target_space_id: str | None
    presence_id: str
    bot_user_id: str
    external_target_id: str
    content: str
    platform: str
    thread_id: str | None = None
    origin: str = TurnOrigin.PLUGIN_BACKGROUND.value
    actor_person_id: None = None
    actor_user_id: str = ""
    actor_is_superuser: bool = False


async def _original_space_binding(
    session: AsyncSession,
    conversation: CanonicalConversationModel,
    presence: PresenceModel,
    legacy_external_id: str | None,
) -> SpaceBindingModel | None:
    if conversation.space_binding_id is not None:
        binding = await session.get(SpaceBindingModel, conversation.space_binding_id)
    else:
        # Actual old Conversations can predate the entry column. Their saved
        # transport provenance is only a reader, never a new owner lookup.
        binding = await session.scalar(
            select(SpaceBindingModel).where(
                SpaceBindingModel.space_id == conversation.space_id,
                SpaceBindingModel.platform == presence.platform,
                SpaceBindingModel.external_space_id == legacy_external_id,
            )
        )
    if (
        binding is None
        or binding.space_id != conversation.space_id
        or binding.platform != presence.platform
    ):
        return None
    return binding


async def recover_background_source(
    database: Database, conversation_id: str, source: dict[str, Any], *, request_id: str
) -> BackgroundTaskSource:
    from yuki.plugin_host.db_models import PluginBackgroundTurnJobModel
    from yuki.plugin_host.notification_repository import (
        PluginNotificationRepository,
        _turn_record,
    )

    async with database.sessions() as session:
        job = await session.scalar(
            select(PluginBackgroundTurnJobModel).where(
                PluginBackgroundTurnJobModel.source_event_id == source.get("trigger_event_id"),
                PluginBackgroundTurnJobModel.plugin_id == source.get("plugin_id"),
                PluginBackgroundTurnJobModel.canonical_conversation_id == conversation_id,
            )
        )
        if job is None:
            raise ValueError("plugin_turn_work_source_mismatch")
        record = _turn_record(job, generation=source["generation"])
    context = await PluginNotificationRepository(database).load_background_context(
        record, work_id=request_id
    )
    async with database.sessions() as session:
        presence = await session.get(PresenceModel, source.get("presence_id"))
        event = await session.get(ChatEventModel, record.source_event_id)
        conversation = await session.get(CanonicalConversationModel, conversation_id)
        if (
            event is None
            or conversation is None
            or presence is None
            or not presence.enabled
            or presence.external_account_id != event.bot_user_id
        ):
            raise ValueError("task_presence_disabled")
    return BackgroundTaskSource(
        request_id,
        conversation_id,
        context.generation,
        record.source_event_id,
        record.plugin_id,
        record.id,
        context.person_id,
        context.space_id,
        presence.id,
        presence.external_account_id,
        event.group_id or event.private_peer_user_id or "",
        event.content,
        presence.platform,
        conversation.thread_id,
    )


async def recover_automation_source(
    database: Database,
    conversation_id: str,
    source: dict[str, Any],
    *,
    request_id: str,
    settings: Settings | None,
) -> AutomationTaskSource:
    from yuki.automation.repository import AutomationRepository
    from yuki.runtime.work_schema_v1 import work

    async with database.sessions() as session:
        run = await session.get(AutomationRunModel, source.get("automation_run_id"))
        owner = (
            await AutomationRepository(database).get_for_run(run.id, session=session)
            if run
            else None
        )
        conversation = await session.get(CanonicalConversationModel, conversation_id)
        if (
            run is None
            or owner is None
            or conversation is None
            or source.get("owner") != "automation"
            or owner.id != source.get("automation_id")
            or source.get("parent_execution_id")
            != (
                f"automation:{run.id}:{source['step_id']}:{owner.script_hash}"
                if source.get("step_id") is not None
                else f"automation:{run.id}:{owner.script_hash}"
            )
            or (
                source.get("script_hash") is not None and source["script_hash"] != owner.script_hash
            )
            or source.get("conversation_id") != conversation_id
            or conversation.generation != source.get("generation")
            or owner.creator_kind != source.get("principal_kind", "person")
            or owner.canonical_creator_person_id != (source.get("actor_person_id") or None)
            or owner.canonical_presence_id != source.get("presence_id")
            or owner.bot_user_id != source.get("bot_user_id")
            or (
                owner.authority_snapshot.get("canonical_conversation_id") is not None
                and owner.authority_snapshot["canonical_conversation_id"] != conversation.id
            )
        ):
            raise ValueError("automation_task_source_changed")
        presence = await session.get(PresenceModel, owner.canonical_presence_id)
        if presence is None or presence.external_account_id != owner.bot_user_id:
            raise ValueError("task_presence_disabled")
        actor_person = owner.canonical_creator_person_id
        actor_user = ""
        actor_is_superuser = False
        if owner.creator_kind == "self":
            scene = owner.authority_snapshot
            if (
                actor_person is not None
                or source.get("actor_user_id")
                or source.get("actor_person_id")
                or source.get("trigger_event_id") is not None
                or source.get("initiative_run_id") is not None
                or scene.get("canonical_conversation_id") != conversation_id
                or scene.get("canonical_space_id") != conversation.space_id
                or scene.get("canonical_presence_id") != presence.id
            ):
                raise ValueError("invalid_self_automation_source")
        else:
            from yuki.automation.control_context import resolve_execution_identity

            assert settings is not None and actor_person is not None
            actor_user, permission = await resolve_execution_identity(
                session,
                settings,
                owner_id=actor_person,
                conversation_id=conversation.id,
                presence_id=presence.id,
            )
            actor_user = source.get("actor_user_id") or owner.creator_user_id or actor_user
            actor_is_superuser = permission.value == "superuser"
        if conversation.space_id:
            space = await session.get(CanonicalSpaceModel, conversation.space_id)
            binding = await _original_space_binding(
                session, conversation, presence, source.get("current_group_id")
            )
            if space is None or not space.enabled or binding is None:
                raise ValueError("task_space_binding_unavailable")
            target = source.get("current_group_id") or ""
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
            content or source.get("instruction", ""),
            presence.platform,
            conversation.thread_id,
            actor_is_superuser=actor_is_superuser,
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
    generation reset and disabled identity still fence execution. Work owns its
    continuation; settling the original initiative does not close its children.
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
        space = await session.get(CanonicalSpaceModel, run.space_id)
        presence = await session.get(PresenceModel, run.presence_id)
        if space is None or not space.enabled:
            raise ValueError("task_space_disabled")
        if (
            presence is None
            or not presence.enabled
            or presence.external_account_id != source.get("bot_user_id")
        ):
            raise ValueError("task_presence_disabled")
        binding = await _original_space_binding(
            session, conversation, presence, source.get("group_id")
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
            binding.id,
            source["group_id"],
            source["instruction"],
            presence.platform,
            thread_id=conversation.thread_id,
        )
    from yuki.conversation.self_initiative import validate_self_initiative

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
    lookup_plugin_host: Callable[[str], Any] | None = None,
) -> MessageTaskSource | SelfTaskSource | AutomationTaskSource | BackgroundTaskSource:
    if source.get("owner") == "plugin_invocation":
        from yuki_plugin_sdk.permissions import PluginPermission

        host = lookup_plugin_host(source["plugin_id"]) if lookup_plugin_host is not None else None
        if host is None or source.get("approval_revision") != host._services.approval_revision:
            raise ValueError("plugin_work_owner_changed")
        host._require(PluginPermission(source["permission"]), require_invocation=False)
        underlying = source.get("execution_source")
        if underlying is None:
            underlying = {**source, "owner": None, "origin": source["actor_source"]["origin"]}
        return await recover_execution_source(
            database,
            conversation_id,
            underlying,
            request_id=request_id,
            settings=settings,
            lookup_plugin_host=lookup_plugin_host,
        )
    if source.get("owner") == "plugin_background":
        return await recover_background_source(
            database, conversation_id, source, request_id=request_id
        )
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
        current_actor = await session.get(
            CanonicalPersonModel, await current_person_id(session, event.author_person_id)
        )
        presence = await session.get(PresenceModel, event.ingress_presence_id)
        if actor is None or not actor.enabled or current_actor is None or not current_actor.enabled:
            raise ValueError("task_actor_disabled")
        if presence is None or presence.external_account_id != event.bot_user_id:
            raise ValueError("task_presence_disabled")
        if conversation.kind == "private":
            if (
                not conversation.person_id
                or not await same_person_id(session, conversation.person_id, actor.id)
                or event.private_peer_user_id != event.sender_user_id
            ):
                raise ValueError("task_target_changed")
            if conversation.identity_binding_id is None:
                raise ValueError("task_target_changed")
            binding_id, external_target = conversation.identity_binding_id, event.sender_user_id
        else:
            space = await session.get(CanonicalSpaceModel, conversation.space_id)
            if space is None or not space.enabled:
                raise ValueError("task_space_disabled")
            if source["origin"] == "autonomous_group" and not space.autonomous_enabled:
                raise ValueError("task_autonomous_disabled")
            binding = await _original_space_binding(session, conversation, presence, event.group_id)
            if binding is None or not event.group_id:
                raise ValueError("task_space_binding_unavailable")
            binding_id, external_target = binding.id, event.group_id
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
            platform=presence.platform,
            thread_id=conversation.thread_id,
        )
