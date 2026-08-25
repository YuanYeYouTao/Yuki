"""Gated v2 canonical ingress resolver. Dormant unless runtime is complete v2."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.conversation.hydrate import HydratedConversation, ensure_canonical_conversation
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.domain.identity import AuthorKind
from qq_ai_bot.domain.messages import InboundMessage
from qq_ai_bot.gateway.registry import GatewayConnectionRegistry, RegistryClosed
from qq_ai_bot.identity.db_models import PresenceModel
from qq_ai_bot.identity.dual_write import (
    _binding_for,
    _classify,
    _create_person_binding,
    _create_presence,
    _external_id,
    _presence_for,
    _space_binding_for,
    ensure_runtime_people_row,
)
from qq_ai_bot.identity.errors import IdentityDualWriteError
from qq_ai_bot.identity.routing import PresenceRouter
from qq_ai_bot.identity.runtime import (
    IdentityRuntimeSnapshot,
    load_identity_runtime,
    require_complete_v2_runtime,
)
from qq_ai_bot.identity.write_settings import identity_write_settings
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import PersonModel


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
    """Rewrite mention/self using every same-platform Yuki Presence. v1 normalizer unchanged."""

    mentions = message.mentions_bot or any(
        item in yuki_account_ids for item in message.mentioned_user_ids
    )
    is_self = message.sender.user_id in yuki_account_ids
    if mentions == message.mentions_bot and is_self == message.is_self_message:
        return message
    return replace(message, mentions_bot=mentions, is_self_message=is_self)


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
    """Registered for every process; enabled only when identity epoch is complete v2."""

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
        """Return None on v1 so the golden matcher path stays untouched."""

        async with self._database.sessions() as session:
            runtime = await load_identity_runtime(session)
            if not runtime.complete_v2:
                return None
        async with self._database.sessions() as session, session.begin():
            runtime = await load_identity_runtime(session)
            if not runtime.complete_v2:
                return None
            return await self._admit(session, runtime, bot, message)

    async def _admit(
        self,
        session: AsyncSession,
        runtime: IdentityRuntimeSnapshot,
        bot: object | None,
        message: InboundMessage,
    ) -> IngressPreAdmit:
        del runtime
        if bot is None:
            return _drop("no_ingress_connection", message)
        try:
            connection = self._registry.resolve_by_handle(bot)
        except RegistryClosed as exc:
            return _drop(exc.category, message)
        if connection.snapshot.presence_id is None:
            presence = await _presence_for(
                session, _external_id(connection.snapshot.external_account_id)
            )
            if presence is None:
                return _drop("no_presence", message)
            self._registry.bind_presence(
                platform=connection.snapshot.platform,
                external_account_id=connection.snapshot.external_account_id,
                presence_id=presence.id,
            )
            presence_id = presence.id
        else:
            presence_id = connection.snapshot.presence_id
        handle_account = connection.snapshot.external_account_id
        handle_provider = connection.snapshot.provider
        message_account = _external_id(message.bot_user_id) if message.bot_user_id else ""
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
            binding = await _space_binding_for(session, _external_id(overlay.group_id))
            if binding is None:
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
                person_id = await _ensure_person_id(session, overlay.sender.user_id)
            if person_id is None and author_kind == AuthorKind.PERSON.value:
                return _drop("no_person", overlay)
        if author_kind == AuthorKind.PERSON.value and person_id is None:
            person_id = await _ensure_person_id(session, overlay.sender.user_id)
        author_person_id = person_id if author_kind == AuthorKind.PERSON.value else None
        await ensure_runtime_people_row(
            session,
            overlay.sender.user_id,
            nickname=overlay.sender.nickname,
            is_bot=author_kind in {AuthorKind.EXTERNAL_BOT.value, AuthorKind.YUKI.value}
            or overlay.sender.is_bot,
            now=message_now(),
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
            )
        hydrated = await _hydrate_for_message(
            session,
            overlay,
            person_id=person_id if overlay.scope_type is ScopeType.PRIVATE else None,
            space_id=space_id,
            ingress_bot_user_id=connection.snapshot.external_account_id,
        )
        overlay = replace(
            overlay,
            legacy_conversation_key=hydrated.primary_alias,
            person_id=person_id,
            space_id=space_id,
            conversation_id=hydrated.conversation_id,
            presence_id=presence_id,
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
    sender_id = _external_id(message.sender.user_id)
    settings = identity_write_settings()
    if sender_id in yuki_ids:
        presence = await _presence_for(session, sender_id)
        if presence is None:
            raise IdentityDualWriteError("unclassified")
        return AuthorKind.YUKI.value, None, presence.id
    if sender_id in settings.ignored_bot_users or message.sender.is_bot:
        return AuthorKind.EXTERNAL_BOT.value, None, None
    people = await session.get(PersonModel, sender_id)
    binding = await _binding_for(session, sender_id)
    presence = await _presence_for(session, sender_id)
    classification = _classify(
        external_id=sender_id,
        role="human",
        is_bot=False,
        people=people,
        binding=binding,
        presence=presence,
    )
    if classification == "yuki_presence":
        if presence is None:
            raise IdentityDualWriteError("unclassified")
        return AuthorKind.YUKI.value, None, presence.id
    if classification != "person":
        return AuthorKind.EXTERNAL_BOT.value, None, None
    person_id = binding.person_id if binding is not None else None
    if person_id is None and people is not None:
        person_id = people.canonical_person_id
    return AuthorKind.PERSON.value, person_id, None


async def _ensure_person_id(session: AsyncSession, user_id: str) -> str:
    from qq_ai_bot.identity.dual_write import ensure_runtime_people_row

    await require_complete_v2_runtime(session)
    external_id = _external_id(user_id)
    people = await ensure_runtime_people_row(session, external_id, now=message_now())
    binding = await _binding_for(session, external_id)
    if binding is not None:
        if people.canonical_person_id is None:
            people.canonical_person_id = binding.person_id
        return binding.person_id
    now = message_now()
    binding = await _create_person_binding(
        session,
        external_id=external_id,
        display_name=people.nickname if people is not None else "",
        now=now,
    )
    if people.canonical_person_id is None:
        people.canonical_person_id = binding.person_id
    return binding.person_id


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
    await require_complete_v2_runtime(session)
    if message.scope_type is ScopeType.PRIVATE:
        if person_id is None:
            raise IdentityDualWriteError("unclassified")
        scope = ConversationScope.private(ingress_bot_user_id, message.sender.user_id)
        return await ensure_canonical_conversation(
            session,
            kind="private",
            primary_scope_key=scope.key,
            person_id=person_id,
        )
    if space_id is None or message.group_id is None:
        raise IdentityDualWriteError("unclassified")
    scope = ConversationScope.group(ingress_bot_user_id, message.group_id)
    return await ensure_canonical_conversation(
        session,
        kind="space",
        primary_scope_key=scope.key,
        space_id=space_id,
    )


async def ensure_v2_presence(session: AsyncSession, bot_user_id: str) -> str:
    await require_complete_v2_runtime(session)
    external_id = _external_id(bot_user_id)
    presence = await _presence_for(session, external_id)
    if presence is not None:
        return presence.id
    created = await _create_presence(session, external_id=external_id, now=message_now())
    return created.id


async def ensure_v2_space(session: AsyncSession, group_id: str) -> str:
    await require_complete_v2_runtime(session)
    return await sync_space_v2(session, group_id)


async def sync_space_v2(session: AsyncSession, group_id: str) -> str:
    """Space ensure that accepts complete v2. Does not call require_v1_runtime."""

    from qq_ai_bot.identity.dual_write import _create_space_binding
    from qq_ai_bot.persistence.models import GroupModel

    await require_complete_v2_runtime(session)
    external_id = _external_id(group_id)
    group = await session.get(GroupModel, external_id)
    binding = await _space_binding_for(session, external_id)
    now = datetime.now(UTC)
    if binding is None:
        binding = await _create_space_binding(
            session,
            group_id=external_id,
            name=group.name if group is not None else "",
            enabled=True if group is None else bool(group.enabled),
            autonomous_enabled=True if group is None else bool(group.autonomous_enabled),
            require_mention=True if group is None else bool(group.require_mention),
            now=now,
        )
    if group is not None and group.canonical_space_id is None:
        group.canonical_space_id = binding.space_id
    return binding.space_id
