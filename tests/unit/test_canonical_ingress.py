"""C17 gated canonical ingress: v1 dormant, v2 fence/receipt/author rules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.identity import AuthorKind
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.gateway.registry import GatewayConnectionRegistry
from qq_ai_bot.identity.canonical_uow import CanonicalIngressUnitOfWork
from qq_ai_bot.identity.db_models import IdentityBindingModel, IdentityRuntimeStateModel
from qq_ai_bot.identity.dual_write import set_identity_failpoint
from qq_ai_bot.identity.ingress import CanonicalIngressResolver, overlay_yuki_signals
from qq_ai_bot.identity.routing import PresenceRouter
from qq_ai_bot.identity.write_settings import (
    IdentityWriteSettings,
    configure_identity_write_settings,
)
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import ChatEventModel, PersonModel

_NOW = datetime(2026, 8, 24, tzinfo=UTC)
_CUTOVER = "550e8400-e29b-41d4-a716-446655440099"


@dataclass
class _Bot:
    self_id: str

    async def call_api(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        return {}


def _message(
    *,
    message_id: str,
    user_id: str = "1001",
    group_id: str | None = None,
    text: str = "hello",
    bot_user_id: str = "8000",
    mentions_bot: bool = False,
    mentioned_user_ids: tuple[str, ...] = (),
    reply_sender_user_id: str | None = None,
    is_bot: bool = False,
) -> InboundMessage:
    return InboundMessage(
        message_id=message_id,
        event_type="message:test",
        scope_type=ScopeType.GROUP if group_id else ScopeType.PRIVATE,
        sender=SenderIdentity(user_id=user_id, is_bot=is_bot),
        text=text,
        bot_user_id=bot_user_id,
        group_id=group_id,
        mentions_bot=mentions_bot,
        mentioned_user_ids=mentioned_user_ids,
        reply_sender_user_id=reply_sender_user_id,
    )


async def _flip_v2(database: Database) -> None:
    async with database.sessions() as session, session.begin():
        row = await session.get(IdentityRuntimeStateModel, 1)
        assert row is not None
        row.state = "v2"
        row.cutover_id = _CUTOVER
        row.source_fingerprint = "cutover-fingerprint"
        row.completed_at = _NOW


async def _stack(
    database: Database,
) -> tuple[GatewayConnectionRegistry, CanonicalIngressResolver, CanonicalIngressUnitOfWork]:
    configure_identity_write_settings(
        IdentityWriteSettings(superusers=frozenset({"9000"}), ignored_bot_users=frozenset({"7777"}))
    )
    registry = GatewayConnectionRegistry(gateway_instance_id="gw-test")
    router = PresenceRouter(
        database,
        registry,
        membership_probe=lambda *_args, **_kwargs: _true(),
    )
    return (
        registry,
        CanonicalIngressResolver(database, registry, router),
        CanonicalIngressUnitOfWork(database, router),
    )


async def _true(*_args: object, **_kwargs: object) -> bool:
    return True


@pytest.mark.asyncio
async def test_v1_runtime_does_not_activate_canonical_ingress(database: Database) -> None:
    registry, resolver, _uow = await _stack(database)
    bot = _Bot("8000")
    registry.connect(bot, presence_id=None)
    admitted = await resolver.pre_admit(bot, _message(message_id="v1-keep"))
    assert admitted is None


@pytest.mark.asyncio
async def test_v2_private_and_group_dual_presence_same_person_space(database: Database) -> None:
    from qq_ai_bot.identity.ingress import ensure_v2_presence, ensure_v2_space

    registry, resolver, uow = await _stack(database)
    await _flip_v2(database)
    bot_a = _Bot("8000")
    bot_b = _Bot("8001")
    async with database.sessions() as session, session.begin():
        presence_a = await ensure_v2_presence(session, "8000")
        presence_b = await ensure_v2_presence(session, "8001")
        await ensure_v2_space(session, "2001")
    registry.connect(bot_a)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence_a)
    private = await resolver.pre_admit(bot_a, _message(message_id="p1", user_id="1001"))
    assert private is not None and not private.dropped
    assert private.author_kind == AuthorKind.PERSON.value
    assert private.person_id is not None
    assert private.conversation_id is not None
    group = await resolver.pre_admit(
        bot_a, _message(message_id="g1", user_id="1001", group_id="2001")
    )
    assert group is not None and not group.dropped
    assert group.space_id is not None
    registry.connect(bot_b)
    registry.bind_presence(platform="qq", external_account_id="8001", presence_id=presence_b)
    other = await resolver.pre_admit(
        bot_b, _message(message_id="p2", user_id="1001", bot_user_id="8001")
    )
    assert other is not None and not other.dropped
    assert other.person_id == private.person_id
    assert other.conversation_id == private.conversation_id
    assert other.primary_alias == private.primary_alias
    again = await resolver.pre_admit(
        bot_b, _message(message_id="g2", user_id="1001", group_id="2001", bot_user_id="8001")
    )
    assert again is not None and again.dropped
    assert again.reason == "not_ingest"
    appended = await uow.append_inbound(private.message, private)
    assert appended.created is True
    duplicate = await uow.append_inbound(private.message, private)
    assert duplicate.created is False
    assert duplicate.event.id == appended.event.id


@pytest.mark.asyncio
async def test_non_ingest_drops_before_policy_without_body(
    database: Database, caplog: pytest.LogCaptureFixture
) -> None:
    from qq_ai_bot.identity.ingress import ensure_v2_presence, ensure_v2_space

    registry, resolver, _uow = await _stack(database)
    await _flip_v2(database)
    bot = _Bot("8000")
    other = _Bot("8001")
    async with database.sessions() as session, session.begin():
        presence_a = await ensure_v2_presence(session, "8000")
        presence_b = await ensure_v2_presence(session, "8001")
        await ensure_v2_space(session, "2001")
    registry.connect(bot)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence_a)
    first = await resolver.pre_admit(
        bot, _message(message_id="g-ok", user_id="1001", group_id="2001")
    )
    assert first is not None and not first.dropped
    registry.connect(other)
    registry.bind_presence(platform="qq", external_account_id="8001", presence_id=presence_b)
    dropped = await resolver.pre_admit(
        other,
        _message(message_id="g-drop", user_id="1001", group_id="2001", bot_user_id="8001"),
    )
    assert dropped is not None and dropped.dropped
    assert dropped.reason == "not_ingest"
    still = await resolver.pre_admit(
        bot, _message(message_id="g-still", user_id="1001", group_id="2001")
    )
    assert still is not None and not still.dropped
    assert "secret" not in dropped.reason
    assert "1001" not in caplog.text


@pytest.mark.asyncio
async def test_fence_failpoint_is_same_immediate_and_receipt_dedupe(database: Database) -> None:
    from qq_ai_bot.identity.ingress import ensure_v2_presence

    registry, resolver, uow = await _stack(database)
    await _flip_v2(database)
    bot = _Bot("8000")
    async with database.sessions() as session, session.begin():
        presence = await ensure_v2_presence(session, "8000")
    registry.connect(bot)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence)
    admitted = await resolver.pre_admit(bot, _message(message_id="fence-1"))
    assert admitted is not None and not admitted.dropped
    set_identity_failpoint(
        lambda name: (
            (_ for _ in ()).throw(RuntimeError(name)) if name == "after_fence_recheck" else None
        )
    )
    try:
        with pytest.raises(RuntimeError, match="after_fence_recheck"):
            await uow.append_inbound(admitted.message, admitted)
    finally:
        set_identity_failpoint(None)
    async with database.sessions() as session:
        rows = list(await session.scalars(select(ChatEventModel)))
        assert rows == []
    await uow.append_inbound(admitted.message, admitted)
    again = await uow.append_inbound(admitted.message, admitted)
    assert again.created is False


@pytest.mark.asyncio
async def test_external_bot_does_not_create_person(database: Database) -> None:
    from qq_ai_bot.identity.ingress import ensure_v2_presence

    registry, resolver, _uow = await _stack(database)
    await _flip_v2(database)
    bot = _Bot("8000")
    async with database.sessions() as session, session.begin():
        presence = await ensure_v2_presence(session, "8000")
    registry.connect(bot)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence)
    admitted = await resolver.pre_admit(
        bot, _message(message_id="bot-1", user_id="7777", is_bot=True)
    )
    assert admitted is not None and not admitted.dropped
    assert admitted.author_kind == AuthorKind.EXTERNAL_BOT.value
    assert admitted.author_person_id is None
    assert admitted.conversation_id is None
    async with database.sessions() as session:
        assert await session.get(PersonModel, "7777") is not None
        bindings = list(await session.scalars(select(IdentityBindingModel)))
        assert bindings == []


def test_private_reply_prefers_ingress_and_fails_over_same_presence_only() -> None:
    from qq_ai_bot.adapters.onebot.sender import OneBotSender
    from qq_ai_bot.gateway.registry import configure_process_registry

    registry = GatewayConnectionRegistry(gateway_instance_id="gw-affinity")
    configure_process_registry(registry)
    try:
        ingress = _Bot("8000")
        replacement = _Bot("8000")
        other = _Bot("8001")
        registry.connect(ingress, presence_id="p-a")
        registry.connect(other, presence_id="p-b")
        sender = OneBotSender(ingress, event=object())  # type: ignore[arg-type]
        assert sender.bot is ingress
        registry.disconnect(ingress)
        registry.connect(replacement, presence_id="p-a")
        assert sender._same_presence_bot() is replacement
        assert sender._same_presence_bot() is not other
    finally:
        configure_process_registry(None)


def test_any_presence_mention_and_reply_without_changing_v1_default() -> None:
    from qq_ai_bot.services.policies import replies_to_bot

    message = _message(
        message_id="m1",
        mentioned_user_ids=("8001",),
        reply_sender_user_id="8001",
    )
    assert message.mentions_bot is False
    assert replies_to_bot(message) is False
    overlay = overlay_yuki_signals(message, frozenset({"8000", "8001"}))
    assert overlay.mentions_bot is True
    assert replies_to_bot(overlay, yuki_account_ids=frozenset({"8000", "8001"})) is True


@pytest.mark.asyncio
async def test_ingress_uses_handle_provider_and_rejects_bot_mismatch(
    database: Database,
) -> None:
    from qq_ai_bot.identity.ingress import ensure_v2_presence

    configure_identity_write_settings(IdentityWriteSettings(superusers=frozenset({"9000"})))
    registry = GatewayConnectionRegistry(gateway_instance_id="gw-lagrange", provider="lagrange")
    router = PresenceRouter(database, registry, membership_probe=lambda *_a, **_k: _true())
    resolver = CanonicalIngressResolver(database, registry, router)
    uow = CanonicalIngressUnitOfWork(database, router)
    await _flip_v2(database)
    bot = _Bot("8000")
    async with database.sessions() as session, session.begin():
        presence = await ensure_v2_presence(session, "8000")
    registry.connect(bot)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence)
    mismatch = await resolver.pre_admit(
        bot, _message(message_id="mismatch", user_id="1001", bot_user_id="9999")
    )
    assert mismatch is not None and mismatch.dropped
    assert mismatch.reason == "bot_handle_mismatch"
    admitted = await resolver.pre_admit(bot, _message(message_id="ok-prov", user_id="1001"))
    assert admitted is not None and not admitted.dropped
    assert admitted.provider == "lagrange"
    assert admitted.handle_external_account_id == "8000"
    assert admitted.message.bot_user_id == "8000"
    appended = await uow.append_inbound(admitted.message, admitted)
    async with database.sessions() as session:
        row = await session.get(ChatEventModel, appended.event.id)
        assert row is not None
        assert row.ingress_provider == "lagrange"
        assert row.bot_user_id == "8000"
