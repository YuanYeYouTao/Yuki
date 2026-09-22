"""Canonical ingress resolver for the 3.8 message pipeline."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.adapters.onebot.normalizer import reproject_inbound_mentions
from qq_ai_bot.conversation.hydrate import HydratedConversation, ensure_canonical_conversation
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.domain.identity import AuthorKind
from qq_ai_bot.domain.messages import InboundMessage
from qq_ai_bot.gateway.registry import GatewayConnectionRegistry, RegistryClosed
from qq_ai_bot.identity.canonical_repository import (
    create_person_binding,
    external_id,
    find_identity_binding,
    find_presence,
    find_space_binding,
)
from qq_ai_bot.identity.db_models import PresenceModel
from qq_ai_bot.identity.errors import CanonicalIdentityError
from qq_ai_bot.identity.routing import PresenceRouter
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import ChatEventModel

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


def overlay_yuki_signals(
    message: InboundMessage,
    yuki_account_ids: frozenset[str],
) -> InboundMessage:
    """Rewrite mention text and self using every same-platform Yuki Presence."""

    projected = reproject_inbound_mentions(message, yuki_account_ids)
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

    async def pre_admit(
        self, bot: object | None, message: InboundMessage
    ) -> IngressPreAdmit | None:
        """Resolve one inbound event inside the canonical transaction."""

        async with self._database.sessions() as session, session.begin():
            return await self._admit(session, bot, message)

    async def _admit(
        self,
        session: AsyncSession,
        bot: object | None,
        message: InboundMessage,
    ) -> IngressPreAdmit:
        if bot is None:
            return _drop("no_ingress_connection", message)
        try:
            connection = self._registry.resolve_by_handle(bot)
        except RegistryClosed as exc:
            return _drop(exc.category, message)
        if connection.snapshot.presence_id is None:
            presence = await find_presence(
                session, external_id(connection.snapshot.external_account_id)
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
        handle_account = connection.snapshot.external_account_id
        handle_provider = connection.snapshot.provider
        message_account = external_id(message.bot_user_id) if message.bot_user_id else ""
        if message_account and message_account != handle_account:
            return _drop("bot_handle_mismatch", message)
        overlay = replace(message, bot_user_id=handle_account)
        yuki_ids = await _same_platform_yuki_accounts(session, connection.snapshot.platform)
        overlay = overlay_yuki_signals(overlay, yuki_ids)
        author_kind, author_person_id, author_presence_id = await _author_for(
            session,
            overlay,
            yuki_ids=yuki_ids,
            ingress_presence_id=presence_id,
        )
        space_id: str | None = None
        space_binding_id: str | None = None
        person_id: str | None = author_person_id
        if overlay.scope_type is ScopeType.GROUP:
            if overlay.group_id is None:
                return _drop("missing_group_id", overlay)
            binding = await find_space_binding(session, external_id(overlay.group_id))
            if binding is None or binding.status != "active":
                return _drop("no_space_binding", overlay)
            space_binding_id = binding.id
            space_id = binding.space_id
            fence = await self._router.evaluate_ingest(
                space_binding_id=binding.id,
                event_presence_id=presence_id,
            )
            if fence != "ok":
                return _drop(fence, overlay)
        else:
            if author_kind == AuthorKind.PERSON.value and person_id is None:
                person_id = await _ensure_person_id(
                    session, overlay.sender.user_id, display_name=overlay.sender.nickname
                )
            if person_id is None and author_kind == AuthorKind.PERSON.value:
                return _drop("no_person", overlay)
        if author_kind == AuthorKind.PERSON.value and person_id is None:
            person_id = await _ensure_person_id(
                session, overlay.sender.user_id, display_name=overlay.sender.nickname
            )
        author_person_id = person_id if author_kind == AuthorKind.PERSON.value else None
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
            )
        hydrated = await _hydrate_for_message(
            session,
            overlay,
            person_id=person_id if overlay.scope_type is ScopeType.PRIVATE else None,
            space_id=space_id,
            ingress_bot_user_id=connection.snapshot.external_account_id,
        )
        reply_to_yuki, reply_author_kind, reply_event_id = await resolve_canonical_reply(
            session,
            conversation_id=hydrated.conversation_id,
            reply_to_message_id=overlay.reply_to_message_id,
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
        )


async def resolve_canonical_reply(
    session: AsyncSession,
    *,
    conversation_id: str,
    reply_to_message_id: str | None,
) -> tuple[bool | None, str | None, int | None]:
    """Resolve reply-to-Yuki from the unique keeper in this conversation.

    None/None/None means no usable keeper: policy may fall back to Presence ids.
    False plus a kind (including ``ambiguous``) is a found non-Yuki verdict and
    must not be overridden by ``reply_sender_user_id``.
    """

    reply_id = (reply_to_message_id or "").strip()
    if not reply_id:
        return None, None, None
    keepers = list(
        await session.scalars(
            select(ChatEventModel).where(
                ChatEventModel.canonical_conversation_id == conversation_id,
                ChatEventModel.platform_message_id == reply_id,
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
        AuthorKind.EXTERNAL_BOT.value,
        AuthorKind.SYSTEM.value,
    }:
        return False, kind, keepers[0].id
    return None, None, None


async def _same_platform_yuki_accounts(session: AsyncSession, platform: str) -> frozenset[str]:
    rows = list(
        await session.scalars(select(PresenceModel).where(PresenceModel.platform == platform))
    )
    return frozenset(item.external_account_id for item in rows)


async def _author_for(
    session: AsyncSession,
    message: InboundMessage,
    *,
    yuki_ids: frozenset[str],
    ingress_presence_id: str,
) -> tuple[str, str | None, str | None]:
    from qq_ai_bot.identity.event_author import project_event_author

    del yuki_ids, ingress_presence_id
    author = await project_event_author(
        session,
        sender_user_id=message.sender.user_id,
        sender_is_bot=message.sender.is_bot,
    )
    return author.as_tuple()


async def _ensure_person_id(session: AsyncSession, user_id: str, *, display_name: str = "") -> str:
    account_id = external_id(user_id)
    binding = await find_identity_binding(session, account_id)
    if binding is not None:
        if binding.status != "active":
            raise CanonicalIdentityError("canonical_owner_disabled")
        return binding.person_id
    created = await create_person_binding(
        session,
        external_account_id=account_id,
        display_name=display_name,
        now=message_now(),
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
) -> HydratedConversation:
    if message.scope_type is ScopeType.PRIVATE:
        if person_id is None:
            raise CanonicalIdentityError("unclassified")
        scope = ConversationScope.private(ingress_bot_user_id, message.sender.user_id)
        return await ensure_canonical_conversation(
            session,
            kind="private",
            primary_scope_key=scope.key,
            person_id=person_id,
        )
    if space_id is None or message.group_id is None:
        raise CanonicalIdentityError("unclassified")
    scope = ConversationScope.group(ingress_bot_user_id, message.group_id)
    return await ensure_canonical_conversation(
        session,
        kind="space",
        primary_scope_key=scope.key,
        space_id=space_id,
    )


async def require_existing_presence(session: AsyncSession, bot_user_id: str) -> str:
    presence = await find_presence(session, external_id(bot_user_id))
    if presence is None:
        raise CanonicalIdentityError("no_presence")
    return presence.id


async def ensure_v2_presence(session: AsyncSession, bot_user_id: str) -> str:
    """v2 never auto-registers Presence. Control/preconfig must already exist."""

    return await require_existing_presence(session, bot_user_id)


async def ensure_v2_space(session: AsyncSession, group_id: str) -> str:
    return await sync_space_v2(session, group_id)


async def sync_space_v2(session: AsyncSession, group_id: str) -> str:
    """Require an existing SpaceBinding. Unknown groups fail closed."""

    binding = await find_space_binding(session, external_id(group_id))
    if binding is None:
        raise CanonicalIdentityError("no_space_binding")
    return binding.space_id
