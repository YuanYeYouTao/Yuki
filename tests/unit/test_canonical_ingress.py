"""Canonical ingress fence, receipt, and author rules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select
from tests.support.gateway import builtin_registry
from tests.support.user_profiles_helpers import observe_members

from yuki.domain.conversations import ScopeType
from yuki.domain.identity import AuthorKind
from yuki.domain.messages import InboundMessage, SenderIdentity
from yuki.gateway.provider import GatewayConnectionProfile, GatewayProviderCatalog
from yuki.gateway.registry import GatewayConnectionRegistry
from yuki.identity.canonical_repository import (
    ensure_person,
    find_identity_binding,
    set_identity_failpoint,
)
from yuki.identity.canonical_repository import (
    ensure_presence as ensure_v2_presence,
)
from yuki.identity.canonical_repository import (
    ensure_space as ensure_v2_space,
)
from yuki.identity.canonical_uow import CanonicalIngressUnitOfWork
from yuki.identity.db_models import IdentityBindingModel
from yuki.identity.errors import CanonicalIdentityError
from yuki.identity.ingress import CanonicalIngressResolver, overlay_yuki_signals
from yuki.identity.routing import PresenceRouter
from yuki.identity.write_settings import (
    IdentityWriteSettings,
    configure_identity_write_settings,
)
from yuki.persistence.database import Database
from yuki.persistence.models import ChatEventModel

_NOW = datetime(2026, 8, 24, tzinfo=UTC)


@dataclass
class _Bot:
    self_id: str

    async def call_api(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        return {}


class _LagrangeProvider:
    def media(self, handle: object) -> None:
        return None

    def activity(self, handle: object, target: object) -> None:
        return None

    def project_inbound(self, message, yuki_ids):
        from yuki.adapters.onebot.normalizer import reproject_inbound_mentions

        return reproject_inbound_mentions(message, yuki_ids)

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
) -> tuple[GatewayConnectionRegistry, CanonicalIngressResolver, CanonicalIngressUnitOfWork]:
    configure_identity_write_settings(
        IdentityWriteSettings(ignored_bot_users=frozenset({"7777"}))
    )
    registry = builtin_registry(gateway_instance_id="gw-test")
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
async def test_v2_private_and_group_dual_presence_same_person_space(database: Database) -> None:
    registry, resolver, uow = await _stack(database)
    bot_a = _Bot("8000")
    bot_b = _Bot("8001")
    async with database.sessions() as session, session.begin():
        presence_a = await ensure_v2_presence(session, "8000")
        presence_b = await ensure_v2_presence(session, "8001")
        await ensure_v2_space(session, "2001")
    registry.connect(bot_a, provider_id="snowluma")
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
    registry.connect(bot_b, provider_id="snowluma")
    registry.bind_presence(platform="qq", external_account_id="8001", presence_id=presence_b)
    other = await resolver.pre_admit(
        bot_b, _message(message_id="p2", user_id="1001", bot_user_id="8001")
    )
    assert other is not None and not other.dropped
    assert other.person_id == private.person_id
    assert other.conversation_id != private.conversation_id
    assert other.primary_alias != private.primary_alias
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
    registry.connect(bot, provider_id="snowluma")
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence_a)
    first = await resolver.pre_admit(
        bot, _message(message_id="g-ok", user_id="1001", group_id="2001")
    )
    assert first is not None and not first.dropped
    registry.connect(other, provider_id="snowluma")
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
    registry.connect(bot, provider_id="snowluma")
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
async def test_retired_presence_sender_becomes_person_without_rewriting_history(
    database: Database,
) -> None:
    from yuki.identity.db_models import PresenceModel

    registry, resolver, uow = await _stack(database)
    bot = _Bot("8000")
    async with database.immediate_session() as session:
        ingress_presence = await ensure_v2_presence(session, "8000")
        retired_presence = await ensure_v2_presence(session, "8001")
    registry.connect(bot, provider_id="snowluma")
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=ingress_presence)
    native = _message(message_id="before-retirement", user_id="8001", group_id="2001")
    original = await resolver.pre_admit(bot, native)
    assert original is not None and not original.dropped
    assert original.author_kind == AuthorKind.YUKI.value
    stored = await uow.append_inbound(original.message, original)
    assert stored.event.author_presence_id == retired_presence
    async with database.immediate_session() as session:
        presence = await session.get(PresenceModel, retired_presence)
        assert presence is not None
        presence.enabled = False
        presence.ingest_eligible = False
        presence.revision += 1
    replay = await resolver.pre_admit(bot, native)
    assert replay is not None and replay.author_kind == AuthorKind.PERSON.value
    recovered = await uow.append_inbound(replay.message, replay)
    assert not recovered.created and recovered.event.id == stored.event.id
    assert recovered.event.author_presence_id == retired_presence
    assert recovered.event.author_person_id is None
    current = await resolver.pre_admit(
        bot, _message(message_id="after-retirement", user_id="8001", group_id="2001")
    )
    assert current is not None and not current.dropped
    assert current.author_kind == AuthorKind.PERSON.value
    assert current.author_person_id is not None
    assert current.author_presence_id is None
    appended = await uow.append_inbound(current.message, current)
    assert appended.event.author_person_id == current.author_person_id
    repeated = await resolver.pre_admit(
        bot, _message(message_id="after-binding-created", user_id="8001", group_id="2001")
    )
    assert repeated is not None and repeated.author_person_id == current.author_person_id
    async with database.sessions() as session:
        historic = await session.get(ChatEventModel, stored.event.id)
        assert historic is not None
        assert historic.author_kind == AuthorKind.YUKI.value
        assert historic.author_presence_id == retired_presence
        assert historic.author_person_id is None
        presence = await session.get(PresenceModel, retired_presence)
        assert presence is not None and presence.external_account_id == "8001"


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_ensure_person_only_conflicts_with_current_presence(
    database: Database, enabled: bool
) -> None:
    from yuki.identity.db_models import PresenceModel
    from yuki.identity.event_author import project_event_author

    async with database.immediate_session() as session:
        presence_id = await ensure_v2_presence(session, "8001")
        presence = await session.get(PresenceModel, presence_id)
        assert presence is not None
        presence.enabled = enabled
        if enabled:
            with pytest.raises(CanonicalIdentityError) as error:
                await ensure_person(session, "8001", now=_NOW)
            assert error.value.category == "canonical_kind_mismatch"
        else:
            person_id = await ensure_person(session, "8001", now=_NOW)
            assert await ensure_person(session, "8001", now=_NOW) == person_id
            author = await project_event_author(session, sender_user_id="8001")
            assert author.as_tuple() == (AuthorKind.PERSON.value, person_id, None)
            assert presence.id == presence_id and presence.external_account_id == "8001"


@pytest.mark.asyncio
async def test_external_bot_does_not_create_person(database: Database) -> None:
    registry, resolver, _uow = await _stack(database)
    bot = _Bot("8000")
    async with database.sessions() as session, session.begin():
        presence = await ensure_v2_presence(session, "8000")
    registry.connect(bot, provider_id="snowluma")
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence)
    admitted = await resolver.pre_admit(
        bot, _message(message_id="bot-1", user_id="6666", is_bot=True)
    )
    assert admitted is not None and not admitted.dropped
    assert admitted.author_kind == AuthorKind.EXTERNAL_BOT.value
    assert admitted.author_person_id is None
    assert admitted.conversation_id is None
    async with database.sessions() as session:
        from yuki.identity.db_models import CanonicalPersonModel

        binding = await session.scalar(
            select(IdentityBindingModel).where(IdentityBindingModel.external_account_id == "6666")
        )
        assert binding is None
        assert list(await session.scalars(select(CanonicalPersonModel)))


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["local", "disconnected", "unknown_receipt"])
async def test_private_reply_keeps_original_connection_without_resending(failure: str) -> None:
    from unittest.mock import AsyncMock

    from yuki.adapters.onebot.sender import OneBotSender, OneBotSendError
    from yuki.domain.messages import OutboundMessage
    from yuki.gateway.registry import configure_process_registry

    @dataclass
    class SendingBot(_Bot):
        send: AsyncMock

    registry = builtin_registry(gateway_instance_id="gw-affinity")
    configure_process_registry(registry)
    try:
        ingress = SendingBot(
            "8000",
            AsyncMock(
                side_effect=ConnectionError("socket closed") if failure == "disconnected" else None,
                return_value={} if failure == "unknown_receipt" else {"message_id": "sent"},
            ),
        )
        replacement = SendingBot("8000", AsyncMock(return_value={"message_id": "replacement"}))
        other = SendingBot("8001", AsyncMock(return_value={"message_id": "other"}))
        registry.connect(ingress, presence_id="p-a", provider_id="snowluma")
        registry.connect(other, presence_id="p-b", provider_id="snowluma")
        sender = OneBotSender(ingress, event=object())  # type: ignore[arg-type]
        registry.disconnect(ingress)
        registry.connect(replacement, presence_id="p-a", provider_id="snowluma")
        message = OutboundMessage(text="" if failure == "local" else "hello")
        with pytest.raises(OneBotSendError):
            await sender.send(message)
        assert sender.bot is ingress
        if failure == "local":
            ingress.send.assert_not_awaited()
        else:
            ingress.send.assert_awaited_once()
        replacement.send.assert_not_awaited()
        other.send.assert_not_awaited()
    finally:
        configure_process_registry(None)


def test_any_presence_mention_and_reply_overlay() -> None:
    from yuki.services.policies import replies_to_bot

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
    configure_identity_write_settings(IdentityWriteSettings())
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
    registry.connect(bot, provider_id="lagrange")
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
async def test_retired_provider_provenance_remains_readable_without_recreating_event(
    database: Database,
) -> None:
    from yuki.conversation.scope_repository import ConversationScopeRepository
    from yuki.persistence.repositories import EventLedgerRepository

    registry, resolver, uow = await _stack(database)
    bot = _Bot("8000")
    async with database.sessions() as session, session.begin():
        presence = await ensure_v2_presence(session, "8000")
    registry.connect(bot, presence_id=presence, provider_id="snowluma")
    admitted = await resolver.pre_admit(bot, _message(message_id="historical-provider"))
    assert admitted is not None and not admitted.dropped
    appended = await uow.append_inbound(admitted.message, admitted)
    # Seed a historical provenance fact using the unchanged persistence schema.
    async with database.immediate_session() as session:
        row = await session.get(ChatEventModel, appended.event.id)
        assert row is not None
        row.ingress_provider = "napcat"
        row.ingress_gateway_instance_id = "historical-gateway"

    read = await EventLedgerRepository(database).get_event(appended.event.id)
    assert read is not None
    assert read.canonical_conversation_id == admitted.conversation_id
    assert read.ingress_presence_id == presence
    snapshot = await ConversationScopeRepository(database).load_prompt_snapshot(read.scope)
    assert appended.event.id in {event.id for event in snapshot.raw_events}
    duplicate = await uow.append_inbound(admitted.message, admitted)
    assert not duplicate.created
    assert duplicate.event.id == appended.event.id
    async with database.sessions() as session:
        row = await session.get(ChatEventModel, read.id)
        assert row is not None
        assert row.ingress_provider == "napcat"
        assert row.ingress_gateway_instance_id == "historical-gateway"


@pytest.mark.asyncio
async def test_unknown_group_and_unknown_presence_fail_closed(database: Database) -> None:
    from yuki.identity.db_models import CanonicalPersonModel, SpaceBindingModel

    registry, resolver, _uow = await _stack(database)
    bot = _Bot("8888")
    registry.connect(bot, presence_id=None, provider_id="snowluma")
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
    registry.connect(bot, provider_id="snowluma")
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
    from yuki.conversation.canonical_db_models import CanonicalEventReceiptModel
    from yuki.identity import canonical_uow as ingress_uow

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

    from yuki.conversation.canonical_db_models import CanonicalEventReceiptModel

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
        from yuki.conversation.hydrate import ensure_canonical_conversation

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
            identity_binding_id=(await find_identity_binding(session, "1099")).id,
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
    registry.connect(bot, provider_id="snowluma")
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
    registry.connect(bot, provider_id="snowluma")
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
    registry.connect(bot, provider_id="snowluma")
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
    from yuki.conversation.canonical_db_models import SpaceBindingIngestRouteModel

    registry, resolver, uow = await _stack(database)
    async with database.sessions() as session, session.begin():
        presence = await ensure_v2_presence(session, "8000")
        replacement_presence = await ensure_v2_presence(session, "8001")
        await ensure_v2_space(session, "2001")
    bot = _Bot("8000")
    registry.connect(bot, provider_id="snowluma")
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
    from yuki.conversation.canonical_db_models import SpaceBindingIngestRouteModel

    registry, resolver, uow = await _stack(database)
    async with database.sessions() as session, session.begin():
        presence = await ensure_v2_presence(session, "8000")
        await ensure_v2_space(session, "2001")
    bot = _Bot("8000")
    registry.connect(bot, provider_id="snowluma")
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
    registry.connect(bot, provider_id="snowluma")
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


@pytest.mark.asyncio
async def test_person_references_keep_all_people_and_the_first_mentioned_binding(
    database: Database,
) -> None:
    from yuki.identity.event_author import project_event_author, project_event_authors

    user_ids = tuple(str(61001 + index) for index in range(8))
    await observe_members(database, user_ids)
    async with database.immediate_session() as session:
        owner = await session.scalar(
            select(IdentityBindingModel).where(
                IdentityBindingModel.external_account_id == user_ids[0]
            )
        )
        assert owner is not None
        session.add(
            IdentityBindingModel(
                id=str(uuid4()),
                person_id=owner.person_id,
                platform="qq",
                external_account_id="61999",
                display_name="alternate account",
                first_seen_at=_NOW,
                last_seen_at=_NOW,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        disabled = await session.scalar(
            select(IdentityBindingModel).where(
                IdentityBindingModel.external_account_id == user_ids[-1]
            )
        )
        assert disabled is not None
        disabled.status = "disabled"
    inputs = ("61999", "8000", "7777", "404", *user_ids, "1001", user_ids[1])
    registry, resolver, uow = await _stack(database)
    bot = _Bot("8000")
    registry.connect(bot, provider_id="snowluma")
    admitted = await resolver.pre_admit(
        bot, _message(message_id="canonical-reference-input", mentioned_user_ids=inputs)
    )
    stored = await uow.append_inbound(admitted.message, admitted)
    async with database.sessions() as session:
        bindings = {
            row.external_account_id: row.person_id
            for row in await session.scalars(
                select(IdentityBindingModel).where(IdentityBindingModel.platform == "qq")
            )
        }
    expected = tuple(bindings[item] for item in ("61999", *user_ids[1:-1], "1001"))
    assert admitted.message.mentioned_person_ids == expected
    assert stored.event.mentioned_person_ids == expected
    async with database.sessions() as session:
        batch = await project_event_authors(session, sender_user_ids=inputs)
        for account in dict.fromkeys(inputs):
            assert batch[account] == await project_event_author(session, sender_user_id=account)
    assert batch["8000"].author_kind == "yuki"
    assert batch["7777"].author_kind == "yuki"
    assert batch["404"].author_person_id is None
    assert batch[user_ids[-1]].author_person_id is None


@pytest.mark.asyncio
async def test_reference_projection_rejects_disabled_owner_and_presence_person_collision(
    database: Database,
) -> None:
    from yuki.identity.db_models import CanonicalPersonModel, PresenceModel
    from yuki.identity.event_author import project_event_authors

    async with database.immediate_session() as session:
        disabled_id = await ensure_person(session, "61010", now=_NOW)
        owner = await session.get(CanonicalPersonModel, disabled_id)
        assert owner is not None
        owner.enabled = False
        presence = await session.scalar(
            select(PresenceModel).where(PresenceModel.external_account_id == "8000")
        )
        assert presence is not None
        session.add(
            IdentityBindingModel(
                id=str(uuid4()),
                person_id=disabled_id,
                platform="qq",
                external_account_id="8000",
                display_name="collision",
                first_seen_at=_NOW,
                last_seen_at=_NOW,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
    async with database.sessions() as session:
        with pytest.raises(CanonicalIdentityError) as disabled:
            await project_event_authors(session, sender_user_ids=("1001", "61010"))
        assert disabled.value.category == "canonical_owner_disabled"
        with pytest.raises(CanonicalIdentityError) as collision:
            await project_event_authors(session, sender_user_ids=("8000",))
        assert collision.value.category == "canonical_kind_mismatch"


@pytest.mark.asyncio
async def test_profile_batch_preserves_accounts_names_and_known_empty_group_card(
    database: Database,
) -> None:
    from sqlalchemy import delete

    from yuki.identity.db_models import CanonicalPersonModel, SpaceBindingModel
    from yuki.persistence.models import MembershipModel, PersonAliasModel

    people = await observe_members(database, ("61021", "61022", "61023"))
    async with database.immediate_session() as session:
        original = await session.scalar(
            select(IdentityBindingModel).where(IdentityBindingModel.external_account_id == "61021")
        )
        assert original is not None
        person_id = original.person_id
        session.add(
            IdentityBindingModel(
                id=str(uuid4()),
                person_id=person_id,
                platform="qq",
                external_account_id="61029",
                display_name="alternate display",
                first_seen_at=_NOW,
                last_seen_at=_NOW,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        disabled = await session.scalar(
            select(IdentityBindingModel).where(IdentityBindingModel.external_account_id == "61023")
        )
        assert disabled is not None
        person = await session.get(CanonicalPersonModel, disabled.person_id)
        assert person is not None
        person.enabled = False
    await people.observe(
        user_id="61021",
        nickname="",
        nickname_known=False,
        group_id="2001",
        group_card="",
        group_card_known=False,
        expected_person_id=person_id,
    )
    old = await people.get(user_id="61021", group_id="2001")
    assert old is not None and old.group_card == "card-61021"
    await people.observe(
        user_id="61021",
        nickname="",
        nickname_known=False,
        group_id="2001",
        group_card="",
        group_card_known=True,
        expected_person_id=person_id,
    )
    accounts = ("61029", "61022", "61021", "61029", "404", "61023")
    profiles = await people.get_many(accounts, group_id="2001")
    assert tuple(profiles) == ("61029", "61022", "61021")
    assert profiles["61029"].nickname == profiles["61021"].nickname == "name-61021"
    assert profiles["61029"].group_card == profiles["61021"].group_card == ""
    assert profiles["61022"].group_card == "card-61022"
    assert await people.members_in_group(accounts, "2001") == {"61029", "61021", "61022"}
    card_only = await people.get(user_id="61021", group_id="2001", include_nickname=False)
    assert card_only is not None and card_only.nickname == card_only.group_card == ""
    assert await people.find_group_members_by_exact_name("card-61021", "2001") == ("61021",)
    with pytest.raises(CanonicalIdentityError) as mismatch:
        await people.observe(
            user_id="61021", nickname="wrong owner", expected_person_id=str(uuid4())
        )
    assert mismatch.value.category == "canonical_owner_mismatch"
    async with database.sessions() as session:
        binding = await session.scalar(
            select(IdentityBindingModel).where(IdentityBindingModel.external_account_id == "61021")
        )
        space_binding = await session.scalar(
            select(SpaceBindingModel).where(SpaceBindingModel.external_space_id == "2001")
        )
        assert binding is not None and space_binding is not None
        membership = await session.get(MembershipModel, (person_id, space_binding.space_id))
        assert membership is not None
        assert membership.last_seen_at == binding.last_seen_at == space_binding.last_seen_at

    # Removing observed aliases or adding account display names does not rename
    # the Person. Platform metadata remains independent of the chosen main name.
    async with database.immediate_session() as session:
        binding = await session.scalar(
            select(IdentityBindingModel).where(IdentityBindingModel.external_account_id == "61021")
        )
        assert binding is not None
        binding.display_name = ""
        await session.execute(
            delete(PersonAliasModel).where(
                PersonAliasModel.canonical_person_id == person_id,
                PersonAliasModel.canonical_space_id.is_(None),
                PersonAliasModel.alias_type == "nickname",
            )
        )
        for account, platform, display_name, status in (
            ("~foreign", "future-platform", "cross-platform fallback", "active"),
            ("~~disabled", "qq", "disabled fallback", "disabled"),
        ):
            session.add(
                IdentityBindingModel(
                    id=str(uuid4()),
                    person_id=person_id,
                    platform=platform,
                    external_account_id=account,
                    display_name=display_name,
                    status=status,
                    first_seen_at=_NOW,
                    last_seen_at=_NOW,
                    created_at=_NOW,
                    updated_at=_NOW,
                )
            )
        other_space = await ensure_v2_space(session, "2002", now=_NOW)
        session.add(
            PersonAliasModel(
                canonical_person_id=person_id,
                canonical_space_id=other_space,
                alias="foreign scoped nickname",
                alias_type="nickname",
                first_seen_at=_NOW,
                last_seen_at=_NOW,
            )
        )
    fallback = await people.get_many(("61021", "61029"))
    assert fallback["61021"].nickname == "name-61021"
    assert fallback["61029"].nickname == "name-61021"
    assert not await people.find_group_members_by_exact_name("cross-platform fallback", "2001")
    assert not await people.find_group_members_by_exact_name("disabled fallback", "2001")
    assert not await people.find_group_members_by_exact_name("foreign scoped nickname", "2001")

    # New observed aliases stay searchable without replacing the Person name.
    async with database.immediate_session() as session:
        for name in ("tied earlier nickname", "tied later nickname"):
            session.add(
                PersonAliasModel(
                    canonical_person_id=person_id,
                    canonical_space_id=None,
                    alias=name,
                    alias_type="nickname",
                    first_seen_at=_NOW,
                    last_seen_at=_NOW,
                )
            )
    tied = await people.get_many(("61021", "61029"), group_id="2001")
    assert all(profile.nickname == "name-61021" for profile in tied.values())
    assert await people.find_group_members_by_exact_name("tied later nickname", "2001") == (
        "61021",
    )
