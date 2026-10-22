"""Canonical ingress resolver for the 3.8 message pipeline."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.conversation.canonical_db_models import (
    CanonicalConversationModel,
    ConversationLegacyAliasModel,
)
from yuki.conversation.hydrate import (
    HydratedConversation,
    binding_for_scope,
    conversation_for_owner,
    ensure_canonical_conversation,
    require_primary_alias_for_conversation,
)
from yuki.domain.conversations import ConversationScope, ScopeType
from yuki.domain.identity import AuthorKind
from yuki.domain.messages import InboundMessage, snapshot_mention_segments
from yuki.gateway.local_clients import LOCAL_CLIENT_PLATFORMS
from yuki.gateway.models import ConnectionSnapshot
from yuki.gateway.registry import GatewayConnectionRegistry, RegistryClosed
from yuki.identity.canonical_repository import (
    create_person_binding,
    external_id,
    find_identity_binding,
    find_presence,
    find_space_binding,
)
from yuki.identity.db_models import IdentityBindingModel, PresenceModel
from yuki.identity.errors import CanonicalIdentityError
from yuki.identity.event_author import EventAuthorProjection
from yuki.identity.mentions import resolve_message_mentions
from yuki.identity.person_identity import person_enabled_condition
from yuki.identity.receipt_compat import (
    load_claimed_keeper,
    require_claimed_event,
    transport_receipt,
)
from yuki.identity.routing import PresenceRouter
from yuki.persistence.database import Database, write_failure_committed, write_failure_phase
from yuki.persistence.models import ChatEventModel

_KEEPER_STATUS = "keeper"
_AMBIGUOUS_REPLY_AUTHOR = "ambiguous"


@dataclass(frozen=True, slots=True)
class IngressPreAdmit:
    dropped: bool
    reason: str
    message: InboundMessage
    yuki_account_ids: frozenset[str]
    presence_id: str | None
    connection_id: str | None
    gateway_instance_id: str | None
    person_id: str | None
    space_id: str | None
    space_binding_id: str | None
    conversation_id: str | None
    primary_alias: str
    author_kind: str
    author_person_id: str | None
    author_presence_id: str | None
    provider: str
    handle_external_account_id: str
    author_account_id: str | None = None
    author_assignment_id: str | None = None


@dataclass(frozen=True, slots=True)
class _ColdIngressRecovery:
    """Only the original ingress fences; never a durable route or admission."""

    connection: ConnectionSnapshot
    presence_revision: int
    space_binding_id: str
    space_id: str
    binding_revision: int


def overlay_yuki_signals(
    message: InboundMessage,
    yuki_account_ids: frozenset[str],
) -> InboundMessage:
    """Classify native mentions and self using same-platform Yuki accounts."""

    projected = replace(
        message,
        yuki_account_ids=yuki_account_ids,
        mentions_bot=message.mentions_bot
        or any(mention.target_user_id in yuki_account_ids for mention in message.mentions),
    )
    is_self = projected.sender.user_id in yuki_account_ids
    if is_self == projected.is_self_message:
        return projected
    return replace(projected, is_self_message=is_self)


def _drop(reason: str, message: InboundMessage) -> IngressPreAdmit:
    return IngressPreAdmit(
        dropped=True,
        reason=reason,
        message=message,
        yuki_account_ids=frozenset(),
        presence_id=None,
        connection_id=None,
        gateway_instance_id=None,
        person_id=None,
        space_id=None,
        space_binding_id=None,
        conversation_id=None,
        primary_alias="",
        author_kind=AuthorKind.SYSTEM.value,
        author_person_id=None,
        author_presence_id=None,
        provider="",
        handle_external_account_id="",
    )


class _ColdIdentityWrite(Exception):
    """A target identity or alias is absent in the hot read snapshot."""


class CanonicalIngressResolver:
    """Resolve every admitted OneBot event through canonical identity."""

    def __init__(
        self,
        database: Database,
        registry: GatewayConnectionRegistry,
        router: PresenceRouter,
    ) -> None:
        self._database = database
        self._registry = registry
        self._router = router

    async def pre_admit(self, bot: object | None, message: InboundMessage) -> IngressPreAdmit:
        """Keep hot admission in one session; release it before cold probes."""

        started = time.perf_counter()
        try:
            result = await self._admit_local(bot, message)
        except CanonicalIdentityError as exc:
            if exc.category != "canonical_owner_disabled" or (
                write_failure_phase(exc) is not None and write_failure_committed(exc) is not False
            ):
                raise
            return _drop(
                "private_not_allowed" if message.scope_type is ScopeType.PRIVATE else exc.category,
                message,
            )
        initial_read_seconds = time.perf_counter() - started
        if isinstance(result, IngressPreAdmit):
            logging.getLogger(__name__).info(
                "ingress_preparation path=local initial_read_seconds=%.6f outcome=%s",
                initial_read_seconds,
                "dropped" if result.dropped else "admitted",
            )
            return result
        recovery_started = time.perf_counter()
        fence = await self._router.evaluate_ingest(
            space_binding_id=result.space_binding_id,
            event_presence_id=result.connection.presence_id or "",
            expected_connection=result.connection,
            expected_presence_revision=result.presence_revision,
            expected_binding=(result.space_id, result.binding_revision),
        )
        if fence != "ok":
            logging.getLogger(__name__).info(
                "ingress_preparation path=cold initial_read_seconds=%.6f "
                "recovery_seconds=%.6f outcome=dropped",
                initial_read_seconds,
                time.perf_counter() - recovery_started,
            )
            return _drop(fence, message)
        # Recovery may have committed even if its acknowledgement was lost.
        # Propagate uncertainty; never retry or undo that route here.
        recovery_seconds = time.perf_counter() - recovery_started
        recheck_started = time.perf_counter()
        admitted = await self._admit_local(bot, message, recovered=result)
        assert isinstance(admitted, IngressPreAdmit)
        logging.getLogger(__name__).info(
            "ingress_preparation path=cold initial_read_seconds=%.6f recovery_seconds=%.6f "
            "recheck_seconds=%.6f outcome=%s",
            initial_read_seconds,
            recovery_seconds,
            time.perf_counter() - recheck_started,
            "dropped" if admitted.dropped else "admitted",
        )
        return admitted

    async def _admit_local(
        self,
        bot: object | None,
        message: InboundMessage,
        *,
        recovered: _ColdIngressRecovery | None = None,
    ) -> IngressPreAdmit | _ColdIngressRecovery:
        try:
            async with self._database.read_session() as reader:
                await reader.execute(text("BEGIN"))
                return await self._admit(reader, bot, message, recovered=recovered)
        except _ColdIdentityWrite:
            # Only missing target identity/Conversation/alias needs admission.
            # Every route/owner is resolved again from the writer's current rows.
            async with self._database.immediate_session() as writer:
                result = await self._admit(
                    writer, bot, message, recovered=recovered, allow_write=True, render_reply=False
                )
            if isinstance(result, IngressPreAdmit) and not result.dropped:
                reply_id = result.message.reply_to_event_id
                if reply_id is not None:
                    from yuki.persistence.repository_helpers import _event_record

                    async with self._database.read_session() as reader:
                        original = await reader.get(ChatEventModel, reply_id)
                        if (
                            original is not None
                            and original.canonical_conversation_id == result.conversation_id
                        ):
                            reply = _event_record(original)
                            result = replace(
                                result,
                                message=replace(
                                    result.message,
                                    reply_text=reply.perceived_content,
                                    reply_segments=reply.segments,
                                    reply_mentions=(),
                                ),
                            )
            return result

    async def _admit(
        self,
        session: AsyncSession,
        bot: object | None,
        message: InboundMessage,
        *,
        recovered: _ColdIngressRecovery | None = None,
        allow_write: bool = False,
        render_reply: bool = True,
    ) -> IngressPreAdmit | _ColdIngressRecovery:
        if bot is None:
            return _drop("no_ingress_connection", message)
        try:
            connection = self._registry.resolve_by_handle(bot)
        except RegistryClosed as exc:
            return _drop(exc.category, message)
        if recovered is not None and connection.snapshot != recovered.connection:
            return _drop("ingress_connection_changed", message)
        if connection.snapshot.presence_id is None:
            presence = await find_presence(
                session,
                external_id(connection.snapshot.external_account_id),
                platform=connection.snapshot.platform,
            )
            if presence is None or not presence.enabled:
                return _drop("no_presence", message)
            self._registry.bind_presence(
                platform=connection.snapshot.platform,
                external_account_id=connection.snapshot.external_account_id,
                presence_id=presence.id,
            )
            presence_id = presence.id
        else:
            presence_id = connection.snapshot.presence_id
            presence = await session.get(PresenceModel, presence_id)
            if (
                presence is None
                or not presence.enabled
                or presence.platform != connection.snapshot.platform
                or presence.external_account_id != connection.snapshot.external_account_id
            ):
                return _drop("no_presence", message)
        if recovered is not None and presence.revision != recovered.presence_revision:
            return _drop("no_presence", message)
        handle_account = connection.snapshot.external_account_id
        handle_provider = connection.snapshot.provider
        message_account = external_id(message.bot_user_id) if message.bot_user_id else ""
        if message_account and message_account != handle_account:
            return _drop("bot_handle_mismatch", message)
        overlay = replace(
            message,
            bot_user_id=handle_account,
            platform=presence.platform,
            presence_id=presence_id,
        )
        yuki_ids = await _same_platform_yuki_accounts(session, connection.snapshot.platform)
        overlay = self._registry.project_inbound(connection, overlay, yuki_ids)
        overlay = overlay_yuki_signals(overlay, yuki_ids)
        # Transport replay is an original fact, before today's owner or names.
        receipt = await transport_receipt(
            session,
            presence_id=presence_id,
            scope=overlay.scope(),
            event_type=overlay.event_type,
            platform_message_id=overlay.message_id,
        )
        if receipt is not None:
            original = require_claimed_event(
                receipt, await load_claimed_keeper(session, receipt.canonical_event_id)
            )
            conversation = (
                await session.get(CanonicalConversationModel, original.canonical_conversation_id)
                if original.canonical_conversation_id
                else None
            )
            original_message = replace(
                overlay,
                presence_id=receipt.ingress_presence_id,
                conversation_id=original.canonical_conversation_id,
                person_id=original.author_person_id,
                author_kind=original.author_kind,
                author_account_id=original.author_account_id,
                space_id=conversation.space_id if conversation else None,
            )
            return IngressPreAdmit(
                dropped=False,
                reason="receipt_replay",
                message=original_message,
                yuki_account_ids=yuki_ids,
                presence_id=receipt.ingress_presence_id,
                connection_id=connection.snapshot.connection_id,
                gateway_instance_id=connection.snapshot.gateway_instance_id,
                person_id=original.author_person_id,
                space_id=conversation.space_id if conversation else None,
                space_binding_id=conversation.space_binding_id if conversation else None,
                conversation_id=original.canonical_conversation_id,
                primary_alias=(
                    await require_primary_alias_for_conversation(session, conversation.id)
                    if conversation
                    else ""
                ),
                author_kind=original.author_kind,
                author_person_id=original.author_person_id,
                author_presence_id=original.author_presence_id,
                provider=handle_provider,
                handle_external_account_id=handle_account,
                author_account_id=original.author_account_id,
                author_assignment_id=conversation.identity_binding_id if conversation else None,
            )
        if presence.platform in LOCAL_CLIENT_PLATFORMS:
            conversation = (
                await session.get(CanonicalConversationModel, overlay.conversation_id)
                if overlay.conversation_id is not None
                else None
            )
            client_binding = (
                await session.get(IdentityBindingModel, conversation.identity_binding_id)
                if conversation is not None and conversation.identity_binding_id
                else None
            )
            if (
                client_binding is None
                or client_binding.status != "active"
                or client_binding.person_id != overlay.person_id
                or client_binding.platform != presence.platform
                or client_binding.external_account_id != overlay.sender.user_id
                or conversation is None
                or conversation.entry_presence_id != presence_id
                or conversation.thread_id != overlay.thread_id
                or not await session.scalar(
                    select(person_enabled_condition(client_binding.person_id))
                )
            ):
                return _drop("private_not_allowed", overlay)
            author = EventAuthorProjection(
                AuthorKind.PERSON.value,
                client_binding.person_id,
                None,
                assignment_id=client_binding.id,
            )
        else:
            author = await _author_for(session, overlay, platform=presence.platform)
        author_kind, author_person_id, author_presence_id = author.as_tuple()
        space_id: str | None = None
        space_binding_id: str | None = None
        person_id: str | None = author_person_id
        if overlay.scope_type is ScopeType.GROUP:
            if overlay.group_id is None:
                return _drop("missing_group_id", overlay)
            binding = await find_space_binding(
                session, external_id(overlay.group_id), platform=presence.platform
            )
            if binding is None or binding.status != "active":
                return _drop("no_space_binding", overlay)
            if recovered is not None and (
                binding.id,
                binding.space_id,
                binding.revision,
            ) != (
                recovered.space_binding_id,
                recovered.space_id,
                recovered.binding_revision,
            ):
                return _drop("no_space_binding", overlay)
            space_binding_id = binding.id
            space_id = binding.space_id
            fence = await self._router.authenticated_ingest_status_in_session(
                session,
                space_binding_id=binding.id,
                event_presence_id=presence_id,
                connection=connection,
            )
            if fence is None:
                if recovered is not None:
                    return _drop("not_ingest", overlay)
                try:
                    frozen_connection = self._registry.resolve_by_handle(bot).snapshot
                except RegistryClosed as exc:
                    return _drop(exc.category, overlay)
                if frozen_connection != replace(connection.snapshot, presence_id=presence_id):
                    return _drop("ingress_connection_changed", overlay)
                return _ColdIngressRecovery(
                    connection=frozen_connection,
                    presence_revision=presence.revision,
                    space_binding_id=binding.id,
                    space_id=binding.space_id,
                    binding_revision=binding.revision,
                )
            if fence != "ok":
                return _drop(fence, overlay)
        if overlay.mentions or overlay.reply_mentions:
            sender_binding = (
                await find_identity_binding(
                    session, overlay.sender.user_id, platform=presence.platform
                )
                if overlay.scope_type is ScopeType.PRIVATE
                else None
            )
            original_conversation = (
                await conversation_for_owner(
                    session,
                    kind="private" if overlay.scope_type is ScopeType.PRIVATE else "space",
                    identity_binding_id=sender_binding.id if sender_binding else None,
                    presence_id=presence_id if overlay.scope_type is ScopeType.PRIVATE else None,
                    space_id=space_id,
                    space_binding_id=space_binding_id,
                    thread_id=overlay.thread_id,
                )
                if overlay.scope_type is ScopeType.GROUP or sender_binding is not None
                else None
            )
            overlay = await resolve_message_mentions(
                session,
                replace(
                    overlay,
                    conversation_id=original_conversation.id if original_conversation else None,
                ),
                space_id=space_id,
            )
            overlay = replace(
                overlay,
                segments=snapshot_mention_segments(overlay.segments, overlay.mentions),
                reply_segments=snapshot_mention_segments(
                    overlay.reply_segments, overlay.reply_mentions
                ),
            )
        if author_kind == AuthorKind.PERSON.value and person_id is None:
            if not allow_write:
                raise _ColdIdentityWrite()
            person_id = await _ensure_person_id(
                session,
                overlay.sender.user_id,
                display_name=overlay.sender.nickname,
                platform=presence.platform,
                now=overlay.occurred_at or overlay.received_at,
            )
            created_binding = await find_identity_binding(
                session, overlay.sender.user_id, platform=presence.platform
            )
            assert created_binding is not None
            author = replace(author, assignment_id=created_binding.id)
        author_person_id = person_id if author_kind == AuthorKind.PERSON.value else None
        overlay = replace(
            overlay, author_kind=author_kind, author_account_id=author.author_account_id
        )
        if author_person_id is not None and overlay.mentions:
            mentions = tuple(
                replace(mention, person_id=author_person_id)
                if mention.kind == "member" and mention.target_user_id == overlay.sender.user_id
                else mention
                for mention in overlay.mentions
            )
            overlay = replace(
                overlay,
                mentions=mentions,
                mentioned_person_ids=tuple(
                    dict.fromkeys(mention.person_id for mention in mentions if mention.person_id)
                ),
                segments=snapshot_mention_segments(overlay.segments, mentions),
            )
        if author_kind != AuthorKind.PERSON.value and overlay.scope_type is ScopeType.PRIVATE:
            return IngressPreAdmit(
                dropped=False,
                reason="admitted",
                message=overlay,
                yuki_account_ids=yuki_ids,
                presence_id=presence_id,
                connection_id=connection.snapshot.connection_id,
                gateway_instance_id=connection.snapshot.gateway_instance_id,
                person_id=None,
                space_id=None,
                space_binding_id=None,
                conversation_id=None,
                primary_alias="",
                author_kind=author_kind,
                author_person_id=None,
                author_presence_id=author_presence_id,
                provider=handle_provider,
                handle_external_account_id=handle_account,
                author_account_id=author.author_account_id,
                author_assignment_id=author.assignment_id,
            )
        hydrated = await _hydrate_for_message(
            session,
            overlay,
            person_id=person_id if overlay.scope_type is ScopeType.PRIVATE else None,
            space_id=space_id,
            ingress_bot_user_id=connection.snapshot.external_account_id,
            presence_id=presence_id,
            space_binding_id=space_binding_id,
            author_assignment_id=author.assignment_id,
            allow_write=allow_write,
        )
        reply_to_yuki, reply_author_kind, reply_event_id = await resolve_canonical_reply(
            session,
            conversation_id=hydrated.conversation_id,
            reply_to_message_id=overlay.reply_to_message_id,
            reply_to_event_id=overlay.reply_to_event_id,
        )
        overlay = replace(
            overlay,
            legacy_conversation_key=hydrated.primary_alias,
            person_id=person_id,
            space_id=space_id,
            conversation_id=hydrated.conversation_id,
            presence_id=presence_id,
            reply_to_event_id=reply_event_id,
            canonical_reply_to_yuki=reply_to_yuki,
            canonical_reply_author_kind=reply_author_kind,
        )
        if reply_event_id is not None and render_reply:
            from yuki.persistence.repository_helpers import _event_record

            original_reply = await session.get(ChatEventModel, reply_event_id)
            assert original_reply is not None
            reply = _event_record(original_reply)
            overlay = replace(
                overlay,
                reply_text=reply.perceived_content,
                reply_segments=reply.segments,
                reply_mentions=(),
            )
        return IngressPreAdmit(
            dropped=False,
            reason="admitted",
            message=overlay,
            yuki_account_ids=yuki_ids,
            presence_id=presence_id,
            connection_id=connection.snapshot.connection_id,
            gateway_instance_id=connection.snapshot.gateway_instance_id,
            person_id=person_id,
            space_id=space_id,
            space_binding_id=space_binding_id,
            conversation_id=hydrated.conversation_id,
            primary_alias=hydrated.primary_alias,
            author_kind=author_kind,
            author_person_id=author_person_id,
            author_presence_id=author_presence_id,
            provider=handle_provider,
            handle_external_account_id=handle_account,
            author_account_id=author.author_account_id,
            author_assignment_id=author.assignment_id,
        )


async def resolve_canonical_reply(
    session: AsyncSession,
    *,
    conversation_id: str,
    reply_to_message_id: str | None,
    reply_to_event_id: int | None = None,
) -> tuple[bool | None, str | None, int | None]:
    """Resolve reply-to-Yuki from the unique keeper in this conversation.

    None/None/None means no usable keeper: policy may fall back to Presence ids.
    False plus a kind (including ``ambiguous``) is a found non-Yuki verdict and
    must not be overridden by ``reply_sender_user_id``.
    """

    if reply_to_event_id is not None:
        target = ChatEventModel.id == reply_to_event_id
    else:
        reply_id = (reply_to_message_id or "").strip()
        if not reply_id:
            return None, None, None
        target = ChatEventModel.platform_message_id == reply_id
    keepers = list(
        await session.scalars(
            select(ChatEventModel).where(
                ChatEventModel.canonical_conversation_id == conversation_id,
                target,
                ChatEventModel.suppression_status == _KEEPER_STATUS,
            )
        )
    )
    if len(keepers) > 1:
        return False, _AMBIGUOUS_REPLY_AUTHOR, None
    if not keepers:
        return None, None, None
    kind = keepers[0].author_kind
    if kind == AuthorKind.YUKI.value:
        return True, kind, keepers[0].id
    if kind in {
        AuthorKind.PERSON.value,
        AuthorKind.ACCOUNT.value,
        AuthorKind.EXTERNAL_BOT.value,
        AuthorKind.SYSTEM.value,
    }:
        return False, kind, keepers[0].id
    return None, None, None


async def _same_platform_yuki_accounts(session: AsyncSession, platform: str) -> frozenset[str]:
    rows = list(
        await session.scalars(
            select(PresenceModel).where(
                PresenceModel.platform == platform, PresenceModel.enabled.is_(True)
            )
        )
    )
    return frozenset(item.external_account_id for item in rows)


async def _author_for(
    session: AsyncSession,
    message: InboundMessage,
    *,
    platform: str,
) -> EventAuthorProjection:
    from yuki.identity.event_author import project_event_author

    author = await project_event_author(
        session,
        sender_user_id=message.sender.user_id,
        sender_is_bot=message.sender.is_bot,
        platform=platform,
        occurred_at=message.occurred_at,
    )
    return author


async def _ensure_person_id(
    session: AsyncSession,
    user_id: str,
    *,
    platform: str,
    display_name: str = "",
    now: datetime | None = None,
) -> str:
    account_id = external_id(user_id)
    binding = await find_identity_binding(session, account_id, platform=platform)
    if binding is not None:
        if binding.status != "active":
            raise CanonicalIdentityError("canonical_owner_disabled")
        return binding.person_id
    created = await create_person_binding(
        session,
        external_account_id=account_id,
        display_name=display_name,
        now=now or message_now(),
        platform=platform,
    )
    return created.person_id


def message_now() -> datetime:
    return datetime.now(UTC)


async def _hydrate_for_message(
    session: AsyncSession,
    message: InboundMessage,
    *,
    person_id: str | None,
    space_id: str | None,
    ingress_bot_user_id: str,
    presence_id: str,
    space_binding_id: str | None,
    author_assignment_id: str | None = None,
    allow_write: bool = True,
) -> HydratedConversation:
    if message.scope_type is ScopeType.PRIVATE:
        if person_id is None:
            raise CanonicalIdentityError("unclassified")
        scope = ConversationScope.private(
            ingress_bot_user_id,
            message.sender.user_id,
            platform=message.platform,
            presence_id=presence_id,
            thread_id=message.thread_id,
        )
        binding = (
            await session.get(IdentityBindingModel, author_assignment_id)
            if author_assignment_id is not None
            else await binding_for_scope(session, scope)
        )
        if binding is None or binding.person_id != person_id:
            raise CanonicalIdentityError("no_person")
        if not allow_write:
            return await _existing_hydrated(
                session,
                scope,
                kind="private",
                identity_binding_id=binding.id,
                presence_id=presence_id,
                thread_id=message.thread_id,
            )
        return await ensure_canonical_conversation(
            session,
            kind="private",
            primary_scope_key=scope.key,
            person_id=person_id,
            identity_binding_id=binding.id,
            presence_id=presence_id,
            thread_id=message.thread_id,
        )
    if space_id is None or message.group_id is None:
        raise CanonicalIdentityError("unclassified")
    scope = ConversationScope.group(
        ingress_bot_user_id,
        message.group_id,
        platform=message.platform,
        presence_id=presence_id,
        thread_id=message.thread_id,
    )
    if not allow_write:
        return await _existing_hydrated(
            session,
            scope,
            kind="space",
            space_id=space_id,
            space_binding_id=space_binding_id,
            thread_id=message.thread_id,
        )
    return await ensure_canonical_conversation(
        session,
        kind="space",
        primary_scope_key=scope.key,
        space_id=space_id,
        space_binding_id=space_binding_id,
        thread_id=message.thread_id,
    )


async def _existing_hydrated(
    session: AsyncSession,
    scope: ConversationScope,
    **owner: Any,
) -> HydratedConversation:
    existing = await conversation_for_owner(session, **owner)
    if existing is None:
        raise _ColdIdentityWrite()
    primary = await require_primary_alias_for_conversation(session, existing.id)
    alias_owner = await session.scalar(
        select(ConversationLegacyAliasModel.conversation_id).where(
            ConversationLegacyAliasModel.scope_key == scope.key
        )
    )
    if alias_owner is None:
        raise _ColdIdentityWrite()
    if alias_owner != existing.id and existing.kind != "private":
        raise CanonicalIdentityError("canonical_owner_mismatch")
    return HydratedConversation(
        conversation_id=existing.id,
        kind=existing.kind,
        person_id=existing.person_id,
        space_id=existing.space_id,
        primary_alias=primary,
        generation=int(existing.generation),
    )
