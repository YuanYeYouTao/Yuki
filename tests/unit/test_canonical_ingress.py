"""Canonical ingress fence, receipt, and author rules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from tests.conftest import make_settings
from tests.support.gateway import napcat_registry

from qq_ai_bot.container import ApplicationContainer
from qq_ai_bot.conversation.rollup.models import RollupPolicyConfig
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.identity import AuthorKind
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.gateway.provider import GatewayConnectionProfile, GatewayProviderCatalog
from qq_ai_bot.gateway.registry import GatewayConnectionRegistry
from qq_ai_bot.identity.canonical_repository import (
    ensure_person,
    set_identity_failpoint,
)
from qq_ai_bot.identity.canonical_repository import (
    ensure_presence as ensure_v2_presence,
)
from qq_ai_bot.identity.canonical_repository import (
    ensure_space as ensure_v2_space,
)
from qq_ai_bot.identity.canonical_uow import CanonicalIngressUnitOfWork
from qq_ai_bot.identity.db_models import IdentityBindingModel
from qq_ai_bot.identity.errors import CanonicalIdentityError
from qq_ai_bot.identity.ingress import CanonicalIngressResolver, overlay_yuki_signals
from qq_ai_bot.identity.routing import PresenceRouter
from qq_ai_bot.identity.write_settings import (
    IdentityWriteSettings,
    configure_identity_write_settings,
)
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import ChatEventModel

_NOW = datetime(2026, 8, 24, tzinfo=UTC)


@dataclass
class _Bot:
    self_id: str

    async def call_api(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        return {}


class _LagrangeProvider:
    provider_id = "lagrange"

    def describe_connection(self, handle: object) -> GatewayConnectionProfile:
        return GatewayConnectionProfile(
            provider_id=self.provider_id,
            platform="qq",
            external_account_id=str(getattr(handle, "self_id", "")),
            capabilities=frozenset({"send_private", "send_group", "group_member_probe"}),
        )


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
    reply_to_message_id: str | None = None,
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
        reply_to_message_id=reply_to_message_id,
    )


async def _stack(
    database: Database,
    *,
    config: RollupPolicyConfig | None = None,
) -> tuple[GatewayConnectionRegistry, CanonicalIngressResolver, CanonicalIngressUnitOfWork]:
    configure_identity_write_settings(
        IdentityWriteSettings(superusers=frozenset({"9000"}), ignored_bot_users=frozenset({"7777"}))
    )
    registry = napcat_registry(gateway_instance_id="gw-test")
    router = PresenceRouter(
        database,
        registry,
        membership_probe=lambda *_args, **_kwargs: _true(),
    )
    return (
        registry,
        CanonicalIngressResolver(database, registry, router),
        CanonicalIngressUnitOfWork(database, router, config=config),
    )


async def _true(*_args: object, **_kwargs: object) -> bool:
    return True


@pytest.mark.asyncio
async def test_v2_private_and_group_dual_presence_same_person_space(database: Database) -> None:
    registry, resolver, uow = await _stack(database)
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
    registry, resolver, _uow = await _stack(database)
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
    registry, resolver, uow = await _stack(database)
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
    registry, resolver, _uow = await _stack(database)
    bot = _Bot("8000")
    async with database.sessions() as session, session.begin():
        presence = await ensure_v2_presence(session, "8000")
    registry.connect(bot)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence)
    admitted = await resolver.pre_admit(
        bot, _message(message_id="bot-1", user_id="6666", is_bot=True)
    )
    assert admitted is not None and not admitted.dropped
    assert admitted.author_kind == AuthorKind.EXTERNAL_BOT.value
    assert admitted.author_person_id is None
    assert admitted.conversation_id is None
    async with database.sessions() as session:
        from qq_ai_bot.identity.db_models import CanonicalPersonModel

        binding = await session.scalar(
            select(IdentityBindingModel).where(IdentityBindingModel.external_account_id == "6666")
        )
        assert binding is None
        assert list(await session.scalars(select(CanonicalPersonModel)))


def test_private_reply_prefers_ingress_and_fails_over_same_presence_only() -> None:
    from qq_ai_bot.adapters.onebot.sender import OneBotSender
    from qq_ai_bot.gateway.registry import configure_process_registry

    registry = napcat_registry(gateway_instance_id="gw-affinity")
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


def test_any_presence_mention_and_reply_overlay() -> None:
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
    configure_identity_write_settings(IdentityWriteSettings(superusers=frozenset({"9000"})))
    registry = GatewayConnectionRegistry(
        providers=GatewayProviderCatalog(
            (_LagrangeProvider(),),
        ),
        gateway_instance_id="gw-lagrange",
    )
    router = PresenceRouter(database, registry, membership_probe=lambda *_a, **_k: _true())
    resolver = CanonicalIngressResolver(database, registry, router)
    uow = CanonicalIngressUnitOfWork(database, router)
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


@pytest.mark.asyncio
async def test_unknown_group_and_unknown_presence_fail_closed(database: Database) -> None:
    from qq_ai_bot.identity.db_models import CanonicalPersonModel, SpaceBindingModel

    registry, resolver, _uow = await _stack(database)
    bot = _Bot("8888")
    registry.connect(bot, presence_id=None)
    missing_presence = await resolver.pre_admit(
        bot, _message(message_id="no-p", bot_user_id="8888")
    )
    assert missing_presence is not None and missing_presence.dropped
    assert missing_presence.reason == "no_presence"
    async with database.sessions() as session, session.begin():
        presence = await ensure_v2_presence(session, "8888")
    registry.bind_presence(platform="qq", external_account_id="8888", presence_id=presence)
    unknown_group = await resolver.pre_admit(
        bot,
        _message(message_id="no-g", user_id="1001", group_id="404", bot_user_id="8888"),
    )
    assert unknown_group is not None and unknown_group.dropped
    assert unknown_group.reason == "no_space_binding"
    async with database.sessions() as session:
        assert (
            await session.scalar(
                select(SpaceBindingModel).where(SpaceBindingModel.external_space_id == "404")
            )
            is None
        )
        assert list(await session.scalars(select(CanonicalPersonModel)))


async def _admit_private(database: Database, message_id: str, *, text: str = "hello"):
    registry, resolver, uow = await _stack(database)
    bot = _Bot("8000")
    async with database.sessions() as session, session.begin():
        presence = await ensure_v2_presence(session, "8000")
    registry.connect(bot)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence)
    admitted = await resolver.pre_admit(
        bot, _message(message_id=message_id, user_id="1001", text=text)
    )
    assert admitted is not None and not admitted.dropped
    return uow, admitted


@pytest.mark.asyncio
async def test_canonical_ingress_content_conflict_keeps_original(database: Database) -> None:
    uow, admitted = await _admit_private(database, "ingress-conflict-1", text="original-body")
    first = await uow.append_inbound(admitted.message, admitted)
    from dataclasses import replace

    tampered = replace(admitted.message, text="tampered-body")
    with pytest.raises(CanonicalIdentityError) as exc:
        await uow.append_inbound(tampered, admitted)
    assert exc.value.category == "receipt_conflict"
    assert "tampered" not in str(exc.value)
    async with database.sessions() as session:
        row = await session.get(ChatEventModel, first.event.id)
        events = list(await session.scalars(select(ChatEventModel)))
    assert row is not None
    assert row.content == "original-body"
    assert len(events) == 1


@pytest.mark.asyncio
async def test_canonical_ingress_segments_or_occurred_at_conflict_fails_closed(
    database: Database,
) -> None:
    from dataclasses import replace
    from datetime import timedelta

    uow, admitted = await _admit_private(database, "ingress-payload-1", text="payload")
    first = await uow.append_inbound(admitted.message, admitted)
    with pytest.raises(CanonicalIdentityError) as segments:
        await uow.append_inbound(
            replace(admitted.message, segments=({"type": "text", "data": {"text": "two"}},)),
            admitted,
        )
    with pytest.raises(CanonicalIdentityError) as occurred:
        await uow.append_inbound(
            replace(
                admitted.message, received_at=admitted.message.received_at + timedelta(minutes=1)
            ),
            admitted,
        )
    assert segments.value.category == "receipt_conflict"
    assert occurred.value.category == "receipt_conflict"
    async with database.sessions() as session:
        row = await session.get(ChatEventModel, first.event.id)
    assert row is not None
    assert row.content == "payload"


@pytest.mark.asyncio
async def test_canonical_ingress_race_rereads_winner(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qq_ai_bot.conversation.canonical_db_models import CanonicalEventReceiptModel
    from qq_ai_bot.identity import canonical_uow as ingress_uow

    uow, admitted = await _admit_private(database, "ingress-race-1", text="race-winner")
    first = await uow.append_inbound(admitted.message, admitted)

    original = ingress_uow._existing_claimed_event
    calls = {"n": 0}

    async def _miss_first(*args: object, **kwargs: object) -> object:
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return await original(*args, **kwargs)

    monkeypatch.setattr(ingress_uow, "_existing_claimed_event", _miss_first)
    replayed = await uow.append_inbound(admitted.message, admitted)
    assert replayed.created is False
    assert replayed.event.id == first.event.id
    from dataclasses import replace

    with pytest.raises(CanonicalIdentityError) as exc:
        await uow.append_inbound(replace(admitted.message, text="race-loser"), admitted)
    assert exc.value.category == "receipt_conflict"
    async with database.sessions() as session:
        receipts = list(await session.scalars(select(CanonicalEventReceiptModel)))
        events = list(await session.scalars(select(ChatEventModel)))
    assert len(receipts) == 1
    assert len(events) == 1


@pytest.mark.asyncio
async def test_canonical_ingress_dangling_or_forged_receipt_fails_closed(
    database: Database,
) -> None:
    from uuid import uuid4

    from qq_ai_bot.conversation.canonical_db_models import CanonicalEventReceiptModel

    uow, admitted = await _admit_private(database, "ingress-forge-1", text="kept")
    first = await uow.append_inbound(admitted.message, admitted)
    async with database.sessions() as session, session.begin():
        receipt = await session.scalar(select(CanonicalEventReceiptModel))
        assert receipt is not None
        receipt.canonical_event_id = str(uuid4())
    with pytest.raises(CanonicalIdentityError) as dangling:
        await uow.append_inbound(admitted.message, admitted)
    assert dangling.value.category == "receipt_conflict"
    async with database.sessions() as session, session.begin():
        from qq_ai_bot.conversation.hydrate import ensure_canonical_conversation

        receipt = await session.scalar(select(CanonicalEventReceiptModel))
        assert receipt is not None
        row = await session.get(ChatEventModel, first.event.id)
        assert row is not None
        receipt.canonical_event_id = row.canonical_event_id
        other_person = await ensure_person(session, "1099", now=_NOW)
        other = await ensure_canonical_conversation(
            session,
            kind="private",
            primary_scope_key="bot:8000:private:1099",
            person_id=other_person,
        )
        row.canonical_conversation_id = other.conversation_id
    with pytest.raises(CanonicalIdentityError) as forged:
        await uow.append_inbound(admitted.message, admitted)
    assert forged.value.category == "receipt_conflict"
    assert "8000" not in str(forged.value)


@pytest.mark.asyncio
async def test_canonical_ingress_receipt_with_only_duplicate_fails_closed(
    database: Database,
) -> None:
    uow, admitted = await _admit_private(database, "ingress-dup-only", text="kept-body")
    first = await uow.append_inbound(admitted.message, admitted)
    async with database.sessions() as session, session.begin():
        row = await session.get(ChatEventModel, first.event.id)
        assert row is not None
        assert row.utterance_fingerprint
        row.suppression_status = "duplicate"
    with pytest.raises(CanonicalIdentityError) as exc:
        await uow.append_inbound(admitted.message, admitted)
    assert exc.value.category == "receipt_conflict"
    assert "kept-body" not in str(exc.value)
    async with database.sessions() as session:
        events = list(await session.scalars(select(ChatEventModel)))
    assert len(events) == 1
    assert events[0].suppression_status == "duplicate"


async def _insert_keeper(
    database: Database,
    *,
    conversation_id: str,
    platform_message_id: str,
    author_kind: str,
    bot_user_id: str = "8000",
    sender_user_id: str = "8000",
    suppression_status: str = "keeper",
    canonical_event_id: str | None = None,
) -> None:
    from uuid import uuid4

    async with database.sessions() as session, session.begin():
        author_presence_id = (
            await ensure_v2_presence(session, bot_user_id)
            if author_kind == AuthorKind.YUKI.value
            else None
        )
        session.add(
            ChatEventModel(
                bot_user_id=bot_user_id,
                platform_message_id=platform_message_id,
                scope_type="private",
                private_peer_user_id="1001",
                sender_user_id=sender_user_id,
                sender_nickname="",
                sender_group_card="",
                direction="outbound",
                event_kind="message",
                content="prior",
                visual_summary="",
                segments_json="[]",
                origin="user_message",
                occurred_at=_NOW,
                observed_at=_NOW,
                canonical_event_id=canonical_event_id or str(uuid4()),
                canonical_conversation_id=conversation_id,
                author_kind=author_kind,
                author_presence_id=author_presence_id,
                utterance_fingerprint=("a" * 64 if suppression_status == "duplicate" else None),
                suppression_status=suppression_status,
            )
        )


@pytest.mark.asyncio
async def test_canonical_reply_uses_keeper_author_and_ignores_other_conversation(
    database: Database,
) -> None:
    registry, resolver, uow = await _stack(database)
    bot = _Bot("8000")
    async with database.sessions() as session, session.begin():
        presence = await ensure_v2_presence(session, "8000")
    registry.connect(bot)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence)
    first = await resolver.pre_admit(bot, _message(message_id="reply-base", user_id="1001"))
    assert first is not None and not first.dropped and first.conversation_id
    await uow.append_inbound(first.message, first)
    other = await resolver.pre_admit(bot, _message(message_id="other-base", user_id="1002"))
    assert other is not None and not other.dropped and other.conversation_id
    await uow.append_inbound(other.message, other)
    await _insert_keeper(
        database,
        conversation_id=first.conversation_id,
        platform_message_id="yuki-out",
        author_kind=AuthorKind.YUKI.value,
    )
    await _insert_keeper(
        database,
        conversation_id=other.conversation_id,
        platform_message_id="foreign-yuki",
        author_kind=AuthorKind.YUKI.value,
        sender_user_id="1002",
    )
    await _insert_keeper(
        database,
        conversation_id=first.conversation_id,
        platform_message_id="ext-out",
        author_kind=AuthorKind.EXTERNAL_BOT.value,
        sender_user_id="7777",
    )
    yuki = await resolver.pre_admit(
        bot,
        _message(
            message_id="reply-yuki",
            user_id="1001",
            reply_to_message_id="yuki-out",
            reply_sender_user_id=None,
        ),
    )
    spoof = await resolver.pre_admit(
        bot,
        _message(
            message_id="reply-spoof",
            user_id="1001",
            reply_to_message_id="ext-out",
            reply_sender_user_id="8000",
        ),
    )
    cross = await resolver.pre_admit(
        bot,
        _message(
            message_id="reply-cross",
            user_id="1001",
            reply_to_message_id="foreign-yuki",
            reply_sender_user_id="8000",
        ),
    )
    missing = await resolver.pre_admit(
        bot,
        _message(
            message_id="reply-missing",
            user_id="1001",
            reply_to_message_id="no-such",
            reply_sender_user_id="8001",
        ),
    )
    assert yuki is not None and yuki.message.canonical_reply_to_yuki is True
    assert yuki.message.canonical_reply_author_kind == AuthorKind.YUKI.value
    assert yuki.message.reply_to_event_id is not None
    appended = await uow.append_inbound(yuki.message, yuki)
    assert appended.event.reply_to_event_id == yuki.message.reply_to_event_id
    async with database.sessions() as session:
        persisted = await session.get(ChatEventModel, appended.event.id)
        anchor = await session.get(ChatEventModel, persisted.reply_to_event_id)
        assert anchor.platform_message_id == "yuki-out"
        assert anchor.canonical_conversation_id == first.conversation_id
    # A duplicate ingress replay keeps the committed internal reference unchanged.
    repeated = await uow.append_inbound(yuki.message, yuki)
    assert not repeated.created
    assert repeated.event.reply_to_event_id == appended.event.reply_to_event_id
    assert spoof is not None and spoof.message.canonical_reply_to_yuki is False
    assert spoof.message.canonical_reply_author_kind == AuthorKind.EXTERNAL_BOT.value
    assert spoof.message.reply_sender_user_id == "8000"
    assert cross is not None and cross.message.canonical_reply_to_yuki is None
    assert cross.message.reply_to_event_id is None
    assert missing is not None and missing.message.canonical_reply_to_yuki is None
    assert missing.message.reply_to_event_id is None


@pytest.mark.asyncio
async def test_canonical_reply_ambiguous_or_duplicate_only(
    database: Database,
) -> None:
    from uuid import uuid4

    registry, resolver, uow = await _stack(database)
    bot = _Bot("8000")
    async with database.sessions() as session, session.begin():
        presence = await ensure_v2_presence(session, "8000")
    registry.connect(bot)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence)
    first = await resolver.pre_admit(bot, _message(message_id="amb-base", user_id="1001"))
    assert first is not None and not first.dropped and first.conversation_id
    await uow.append_inbound(first.message, first)
    await _insert_keeper(
        database,
        conversation_id=first.conversation_id,
        platform_message_id="amb-id",
        author_kind=AuthorKind.YUKI.value,
        canonical_event_id=str(uuid4()),
    )
    await _insert_keeper(
        database,
        conversation_id=first.conversation_id,
        platform_message_id="amb-id",
        author_kind=AuthorKind.YUKI.value,
        bot_user_id="8001",
        canonical_event_id=str(uuid4()),
    )
    await _insert_keeper(
        database,
        conversation_id=first.conversation_id,
        platform_message_id="sup-only",
        author_kind=AuthorKind.YUKI.value,
        suppression_status="duplicate",
        canonical_event_id=str(uuid4()),
    )
    ambiguous = await resolver.pre_admit(
        bot,
        _message(
            message_id="reply-amb",
            user_id="1001",
            reply_to_message_id="amb-id",
            reply_sender_user_id="8000",
        ),
    )
    suppressed = await resolver.pre_admit(
        bot,
        _message(
            message_id="reply-sup",
            user_id="1001",
            reply_to_message_id="sup-only",
            reply_sender_user_id="8000",
        ),
    )
    assert ambiguous is not None
    assert ambiguous.message.canonical_reply_to_yuki is False
    assert ambiguous.message.canonical_reply_author_kind == "ambiguous"
    assert ambiguous.message.reply_to_event_id is None
    assert suppressed is not None
    assert suppressed.message.canonical_reply_to_yuki is None
    assert suppressed.message.reply_to_event_id is None


@pytest.mark.asyncio
async def test_canonical_ingress_durable_characters_match_recount(database: Database) -> None:
    from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
    from qq_ai_bot.conversation.rollup.prompt_accounting import (
        durable_uncovered_characters,
        durable_uncovered_event_characters,
        prompt_accounting_characters,
    )
    from qq_ai_bot.conversation.rollup.repository import recount_canonical_uncovered
    from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork

    registry, resolver, ingress = await _stack(database)
    bot = _Bot("8000")
    async with database.sessions() as session, session.begin():
        presence = await ensure_v2_presence(session, "8000")
    registry.connect(bot)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence)
    first_admit = await resolver.pre_admit(
        bot, _message(message_id="dur-c1", user_id="1001", text="hello")
    )
    second_admit = await resolver.pre_admit(
        bot, _message(message_id="dur-c2", user_id="1001", text="again")
    )
    assert first_admit is not None and second_admit is not None
    first = await ingress.append_inbound(first_admit.message, first_admit)
    second = await ingress.append_inbound(second_admit.message, second_admit)
    kwargs = {
        "bot_display_name": ingress._config.bot_display_name,
        "timezone": ingress._config.timezone,
    }
    expected = durable_uncovered_event_characters(
        first.event, **kwargs
    ) + durable_uncovered_event_characters(second.event, **kwargs)
    grouped = prompt_accounting_characters((first.event, second.event), **kwargs)
    assert second.scope.uncovered_character_count == expected
    assert expected != grouped
    assert expected != len("hello") + len("again")
    async with database.immediate_session() as session:
        conversation = await session.get(
            CanonicalConversationModel, first.event.canonical_conversation_id
        )
        assert conversation is not None
        recounted = await recount_canonical_uncovered(session, conversation, ingress._config)
    assert recounted == (2, expected)

    scoped = ScopedEventLedgerUnitOfWork(database, config=ingress._config)
    from qq_ai_bot.domain.conversations import ConversationScope

    scope = ConversationScope.private("8000", "1001")
    third = await scoped.append(
        scope=scope,
        platform_message_id="dur-c3",
        sender_user_id="1001",
        direction="inbound",
        content="after-recount",
        occurred_at=_NOW,
    )
    live_events = (first.event, second.event, third.event)
    after = durable_uncovered_characters(live_events, **kwargs)
    assert third.scope.uncovered_character_count == after
    async with database.immediate_session() as session:
        conversation = await session.get(
            CanonicalConversationModel, first.event.canonical_conversation_id
        )
        assert conversation is not None
        recounted_again = await recount_canonical_uncovered(session, conversation, ingress._config)
    snapshot_total = recounted_again[1]
    assert snapshot_total == third.scope.uncovered_character_count
    assert recounted_again[0] == 3


def test_production_container_wires_canonical_uow_from_rollup_repository(
    database: Database,
    tmp_path,
) -> None:
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    settings = make_settings(
        database.url,
        plugin_directory=plugin_dir,
        plugin_system_enabled=False,
        bot_display_name="远野",
        default_timezone="America/New_York",
    )
    container = ApplicationContainer(settings, database=database)
    assert container.canonical_uow._config is container.conversation_rollups.config
    assert container.canonical_uow._config.bot_display_name == "远野"
    assert container.canonical_uow._config.timezone == "America/New_York"


@pytest.mark.asyncio
async def test_custom_policy_live_append_matches_recount_without_reading_ingress_config(
    database: Database,
) -> None:
    from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
    from qq_ai_bot.conversation.rollup.prompt_accounting import durable_uncovered_characters
    from qq_ai_bot.conversation.rollup.repository import (
        ConversationRollupRepository,
        recount_canonical_uncovered,
    )
    from qq_ai_bot.domain.conversations import ConversationScope
    from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork

    bot_display_name = "远野"
    timezone = "America/New_York"
    policy = RollupPolicyConfig(
        bot_display_name=bot_display_name,
        timezone=timezone,
    )
    kwargs = {"bot_display_name": bot_display_name, "timezone": timezone}
    registry, resolver, ingress = await _stack(database, config=policy)
    bot = _Bot("8000")
    async with database.sessions() as session, session.begin():
        presence = await ensure_v2_presence(session, "8000")
    registry.connect(bot)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence)
    first_admit = await resolver.pre_admit(
        bot, _message(message_id="pol-1", user_id="1001", text="hello")
    )
    assert first_admit is not None
    first = await ingress.append_inbound(first_admit.message, first_admit)
    scoped = ScopedEventLedgerUnitOfWork(database, config=policy)
    scope = ConversationScope.private("8000", "1001")
    await scoped.append(
        scope=scope,
        platform_message_id="pol-out",
        sender_user_id="8000",
        direction="outbound",
        content="yuki reply",
        occurred_at=_NOW,
        sender_is_bot=True,
        origin="agent_reply",
    )
    reply_admit = await resolver.pre_admit(
        bot,
        _message(
            message_id="pol-2",
            user_id="1001",
            text="replying",
            reply_to_message_id="pol-out",
            reply_sender_user_id="8000",
        ),
    )
    assert reply_admit is not None
    await ingress.append_inbound(reply_admit.message, reply_admit)
    await scoped.set_visual_summary(first.event.id, "a visual caption")
    async with database.immediate_session() as session:
        conversation = await session.get(
            CanonicalConversationModel, first.event.canonical_conversation_id
        )
        assert conversation is not None
        recounted = await recount_canonical_uncovered(session, conversation, policy)
    state, _rollup, _job = await ConversationRollupRepository(database, policy).status(scope)
    assert state is not None
    assert recounted == (state.uncovered_event_count, state.uncovered_character_count)
    snapshot = await ConversationRollupRepository(database, policy).load_prompt_snapshot(scope)
    expected = durable_uncovered_characters(snapshot.raw_events, **kwargs)
    assert (
        expected
        != durable_uncovered_characters(
            snapshot.raw_events,
            bot_display_name="Yuki",
            timezone="Asia/Shanghai",
        )
        or bot_display_name == "Yuki"
    )
    assert state.uncovered_character_count == expected
    third = await scoped.append(
        scope=scope,
        platform_message_id="pol-3",
        sender_user_id="1001",
        direction="inbound",
        content="after-recount",
        occurred_at=_NOW,
    )
    after_snapshot = await ConversationRollupRepository(database, policy).load_prompt_snapshot(
        scope
    )
    after_expected = durable_uncovered_characters(after_snapshot.raw_events, **kwargs)
    assert third.scope.uncovered_character_count == after_expected
    async with database.immediate_session() as session:
        conversation = await session.get(
            CanonicalConversationModel, first.event.canonical_conversation_id
        )
        assert conversation is not None
        recounted_after = await recount_canonical_uncovered(session, conversation, policy)
    assert recounted_after == (len(after_snapshot.raw_events), after_expected)


@pytest.mark.asyncio
async def test_gateway_probe_during_pre_admission_does_not_hold_sqlite_writer(
    database: Database,
) -> None:
    import asyncio

    from sqlalchemy import text

    registry, resolver, uow = await _stack(database)
    async with database.sessions() as session, session.begin():
        presence = await ensure_v2_presence(session, "8000")
        await ensure_v2_space(session, "2001")
    bot = _Bot("8000")
    registry.connect(bot)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence)
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_probe(*args: object, **kwargs: object) -> bool:
        entered.set()
        await release.wait()
        return True

    resolver._router._probe = slow_probe
    pending = asyncio.create_task(
        resolver.pre_admit(bot, _message(message_id="slow-probe", group_id="2001"))
    )
    try:
        await asyncio.wait_for(entered.wait(), 3)
        async with database.sessions() as session:
            await session.execute(text("PRAGMA busy_timeout=30"))
            await session.execute(text("BEGIN IMMEDIATE"))
            await session.commit()
    finally:
        release.set()
    admitted = await pending
    assert admitted is not None and not admitted.dropped
    assert (await uow.append_inbound(admitted.message, admitted)).created


@pytest.mark.asyncio
@pytest.mark.parametrize("route_state", ["active", "paused", "transferred"])
async def test_admitted_group_message_survives_disconnect_but_not_route_change(
    database: Database, route_state: str
) -> None:
    from qq_ai_bot.conversation.canonical_db_models import SpaceBindingIngestRouteModel

    registry, resolver, uow = await _stack(database)
    async with database.sessions() as session, session.begin():
        presence = await ensure_v2_presence(session, "8000")
        replacement_presence = await ensure_v2_presence(session, "8001")
        await ensure_v2_space(session, "2001")
    bot = _Bot("8000")
    registry.connect(bot)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence)
    admitted = await resolver.pre_admit(
        bot, _message(message_id="admitted-before-disconnect", group_id="2001")
    )
    assert admitted is not None and not admitted.dropped
    registry.disconnect(bot)
    if route_state != "active":
        async with database.immediate_session() as session:
            route = await session.get(SpaceBindingIngestRouteModel, admitted.space_binding_id)
            assert route is not None
            if route_state == "paused":
                route.paused = True
            else:
                route.ingest_presence_id = replacement_presence
    if route_state != "active":
        with pytest.raises(CanonicalIdentityError) as failure:
            await uow.append_inbound(admitted.message, admitted)
        assert failure.value.category == ("paused" if route_state == "paused" else "not_ingest")
        async with database.sessions() as session:
            assert not list(await session.scalars(select(ChatEventModel)))
    else:
        assert (await uow.append_inbound(admitted.message, admitted)).created


@pytest.mark.asyncio
@pytest.mark.parametrize("route_paused", [False, True])
async def test_admitted_group_new_survives_socket_disconnect_but_not_route_pause(
    database: Database, route_paused: bool
) -> None:
    from qq_ai_bot.conversation.canonical_db_models import SpaceBindingIngestRouteModel

    registry, resolver, uow = await _stack(database)
    async with database.sessions() as session, session.begin():
        presence = await ensure_v2_presence(session, "8000")
        await ensure_v2_space(session, "2001")
    bot = _Bot("8000")
    registry.connect(bot)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence)
    admitted = await resolver.pre_admit(
        bot,
        _message(
            message_id="group-new-disconnect",
            user_id="9000",
            group_id="2001",
            text="/ai new",
        ),
    )
    assert admitted is not None and not admitted.dropped
    assert admitted.space_binding_id is not None
    registry.disconnect(bot)
    if route_paused:
        async with database.immediate_session() as session:
            route = await session.get(SpaceBindingIngestRouteModel, admitted.space_binding_id)
            assert route is not None
            route.paused = True
    if route_paused:
        with pytest.raises(CanonicalIdentityError) as failure:
            await uow.append_new_generation(admitted.message, admitted)
        assert failure.value.category == "paused"
        async with database.sessions() as session:
            assert not list(await session.scalars(select(ChatEventModel)))
    else:
        changed = await uow.append_new_generation(admitted.message, admitted)
        assert changed.generation_changed
        assert changed.scope.generation == 2


@pytest.mark.asyncio
async def test_internal_reply_reference_rejects_a_foreign_conversation(database: Database) -> None:
    from dataclasses import replace

    registry, resolver, uow = await _stack(database)
    bot = _Bot("8000")
    async with database.sessions() as session, session.begin():
        presence = await ensure_v2_presence(session, "8000")
    registry.connect(bot)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence)
    first = await resolver.pre_admit(bot, _message(message_id="owned-anchor", user_id="1001"))
    anchor = await uow.append_inbound(first.message, first)
    other = await resolver.pre_admit(bot, _message(message_id="foreign-ref", user_id="1002"))
    forged = replace(other.message, reply_to_event_id=anchor.event.id)
    with pytest.raises(CanonicalIdentityError) as error:
        await uow.append_inbound(forged, other)
    assert error.value.category == "receipt_conflict"
    async with database.sessions() as session:
        assert (
            await session.scalar(
                select(ChatEventModel).where(ChatEventModel.platform_message_id == "foreign-ref")
            )
            is None
        )
