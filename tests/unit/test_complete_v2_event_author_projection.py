"""Stage 2A-2C-1: scoped and canonical UoW share Presence-first author projection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from qq_ai_bot.conversation.rollup.models import RollupPolicyConfig
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.domain.identity import AuthorKind
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.gateway.registry import GatewayConnectionRegistry
from qq_ai_bot.identity.canonical_uow import CanonicalIngressUnitOfWork
from qq_ai_bot.identity.db_models import (
    CanonicalPersonModel,
    IdentityBindingModel,
    IdentityRuntimeStateModel,
    PresenceModel,
)
from qq_ai_bot.identity.dual_write import (
    _create_person_binding,
    ensure_v2_space,
)
from qq_ai_bot.identity.dual_write import (
    ensure_canonical_presence_preconfig as ensure_v2_presence,
)
from qq_ai_bot.identity.event_author import project_complete_v2_event_author
from qq_ai_bot.identity.ingress import CanonicalIngressResolver
from qq_ai_bot.identity.inventory import IDENTITY_PLATFORM
from qq_ai_bot.identity.routing import PresenceRouter
from qq_ai_bot.identity.write_settings import (
    IdentityWriteSettings,
    configure_identity_write_settings,
)
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import ChatEventModel, PersonModel
from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork

_NOW = datetime(2026, 8, 24, tzinfo=UTC)
_CUTOVER = "550e8400-e29b-41d4-a716-446655440099"


@dataclass
class _Bot:
    self_id: str

    async def call_api(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        return {}


def _author_tuple(row: ChatEventModel) -> tuple[str | None, str | None, str | None]:
    return (row.author_kind, row.author_person_id, row.author_presence_id)


async def _true(*_args: object, **_kwargs: object) -> bool:
    return True


async def _flip_v2(database: Database) -> None:
    async with database.sessions() as session, session.begin():
        row = await session.get(IdentityRuntimeStateModel, 1)
        assert row is not None
        row.state = "v2"
        row.cutover_id = _CUTOVER
        row.source_fingerprint = "cutover-fingerprint"
        row.completed_at = _NOW


def _settings(*, ignored: tuple[str, ...] = ("8100", "7777")) -> None:
    configure_identity_write_settings(
        IdentityWriteSettings(superusers=frozenset({"9000"}), ignored_bot_users=frozenset(ignored))
    )


def _scoped(database: Database) -> ScopedEventLedgerUnitOfWork:
    return ScopedEventLedgerUnitOfWork(database, config=RollupPolicyConfig())


def _inbound(
    *,
    message_id: str,
    user_id: str,
    bot_user_id: str = "8000",
    group_id: str = "2001",
    is_bot: bool = False,
) -> InboundMessage:
    return InboundMessage(
        message_id=message_id,
        event_type="message:test",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity(user_id=user_id, is_bot=is_bot),
        text="hello",
        bot_user_id=bot_user_id,
        group_id=group_id,
    )


async def _canonical_stack(
    database: Database,
) -> tuple[GatewayConnectionRegistry, CanonicalIngressResolver, CanonicalIngressUnitOfWork]:
    registry = GatewayConnectionRegistry(gateway_instance_id="gw-author")
    router = PresenceRouter(database, registry, membership_probe=lambda *_a, **_k: _true())
    return (
        registry,
        CanonicalIngressResolver(database, registry, router),
        CanonicalIngressUnitOfWork(database, router),
    )


async def _bind_handle(
    database: Database,
    registry: GatewayConnectionRegistry,
    bot: _Bot,
) -> str:
    async with database.sessions() as session:
        presence = await session.scalar(
            select(PresenceModel).where(PresenceModel.external_account_id == bot.self_id)
        )
    assert presence is not None
    registry.connect(bot)
    registry.bind_presence(
        platform="qq",
        external_account_id=bot.self_id,
        presence_id=presence.id,
    )
    return presence.id


async def _append_both(
    database: Database,
    *,
    sender_user_id: str,
    sender_is_bot: bool = False,
) -> tuple[ChatEventModel, ChatEventModel]:
    scoped = _scoped(database)
    scoped_result = await scoped.append(
        scope=ConversationScope.group("8000", "2001"),
        platform_message_id=f"scoped-{sender_user_id}-{uuid4().hex[:8]}",
        sender_user_id=sender_user_id,
        direction="inbound",
        content="hello",
        sender_is_bot=sender_is_bot,
        origin="user_message",
    )
    registry, resolver, canonical = await _canonical_stack(database)
    bot = _Bot("8000")
    await _bind_handle(database, registry, bot)
    admitted = await resolver.pre_admit(
        bot,
        _inbound(
            message_id=f"canon-{sender_user_id}-{uuid4().hex[:8]}",
            user_id=sender_user_id,
            is_bot=sender_is_bot,
        ),
    )
    assert admitted is not None and not admitted.dropped
    canon_result = await canonical.append_inbound(admitted.message, admitted)
    async with database.sessions() as session:
        scoped_row = await session.get(ChatEventModel, scoped_result.event.id)
        canon_row = await session.get(ChatEventModel, canon_result.event.id)
    assert scoped_row is not None and canon_row is not None
    return scoped_row, canon_row


async def _counts(database: Database) -> tuple[int, int]:
    async with database.sessions() as session:
        persons = int(
            await session.scalar(select(func.count()).select_from(CanonicalPersonModel)) or 0
        )
        bindings = int(
            await session.scalar(select(func.count()).select_from(IdentityBindingModel)) or 0
        )
    return persons, bindings


@pytest.mark.asyncio
async def test_other_presence_sender_is_yuki_with_distinct_ingress(
    database: Database,
) -> None:
    _settings()
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        ingress_id = await ensure_v2_presence(session, "8000")
        author_id = await ensure_v2_presence(session, "8001")
        await ensure_v2_space(session, "2001")
    scoped_row, canon_row = await _append_both(database, sender_user_id="8001")
    async with database.sessions() as session:
        helper = await project_complete_v2_event_author(session, sender_user_id="8001")
        binding_8001 = await session.scalar(
            select(IdentityBindingModel).where(IdentityBindingModel.external_account_id == "8001")
        )
        people_8001 = await session.get(PersonModel, "8001")
    expected = (AuthorKind.YUKI.value, None, author_id)
    assert helper.as_tuple() == expected
    assert _author_tuple(scoped_row) == expected
    assert _author_tuple(canon_row) == expected
    assert scoped_row.ingress_presence_id == ingress_id
    assert canon_row.ingress_presence_id == ingress_id
    assert scoped_row.author_presence_id != scoped_row.ingress_presence_id
    assert scoped_row.origin == "user_message"
    assert canon_row.origin == "user_message"
    assert binding_8001 is None
    assert people_8001 is None
    assert await _counts(database) == (0, 0)


@pytest.mark.asyncio
async def test_ignored_external_sender_is_external_bot(database: Database) -> None:
    _settings(ignored=("8100",))
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        ingress_id = await ensure_v2_presence(session, "8000")
        await ensure_v2_presence(session, "8001")
        await ensure_v2_space(session, "2001")
    scoped_row, canon_row = await _append_both(database, sender_user_id="8100")
    async with database.sessions() as session:
        helper = await project_complete_v2_event_author(session, sender_user_id="8100")
        binding = await session.scalar(
            select(IdentityBindingModel).where(IdentityBindingModel.external_account_id == "8100")
        )
        people = await session.get(PersonModel, "8100")
    expected = (AuthorKind.EXTERNAL_BOT.value, None, None)
    assert helper.as_tuple() == expected
    assert _author_tuple(scoped_row) == expected
    assert _author_tuple(canon_row) == expected
    assert scoped_row.ingress_presence_id == ingress_id
    assert canon_row.ingress_presence_id == ingress_id
    assert binding is None
    assert people is None


@pytest.mark.asyncio
async def test_secondary_binding_shares_one_person(database: Database) -> None:
    _settings()
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        ingress_id = await ensure_v2_presence(session, "8000")
        await ensure_v2_presence(session, "8001")
        await ensure_v2_space(session, "2001")
        primary = await _create_person_binding(
            session, external_id="1001", display_name="", now=_NOW
        )
        session.add(
            IdentityBindingModel(
                id=str(uuid4()),
                person_id=primary.person_id,
                platform=IDENTITY_PLATFORM,
                external_account_id="1002",
                display_name="",
                status="active",
                revision=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        person_id = primary.person_id
    scoped_row, canon_row = await _append_both(database, sender_user_id="1002")
    async with database.sessions() as session:
        helper = await project_complete_v2_event_author(session, sender_user_id="1002")
        primary_helper = await project_complete_v2_event_author(session, sender_user_id="1001")
        persons = int(
            await session.scalar(select(func.count()).select_from(CanonicalPersonModel)) or 0
        )
    expected = (AuthorKind.PERSON.value, person_id, None)
    assert helper.as_tuple() == expected
    assert primary_helper.as_tuple() == expected
    assert _author_tuple(scoped_row) == expected
    assert _author_tuple(canon_row) == expected
    assert scoped_row.ingress_presence_id == ingress_id
    assert canon_row.ingress_presence_id == ingress_id
    assert persons == 1


@pytest.mark.asyncio
async def test_scoped_and_canonical_author_tuples_match_all_cases(database: Database) -> None:
    _settings(ignored=("8100",))
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        await ensure_v2_presence(session, "8000")
        await ensure_v2_presence(session, "8001")
        await ensure_v2_space(session, "2001")
        created = await _create_person_binding(
            session, external_id="1001", display_name="", now=_NOW
        )
        session.add(
            IdentityBindingModel(
                id=str(uuid4()),
                person_id=created.person_id,
                platform=IDENTITY_PLATFORM,
                external_account_id="1002",
                display_name="",
                status="active",
                revision=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
    for sender, is_bot in (("8001", False), ("8100", False), ("1002", False), ("8000", True)):
        scoped_row, canon_row = await _append_both(
            database, sender_user_id=sender, sender_is_bot=is_bot
        )
        async with database.sessions() as session:
            helper = await project_complete_v2_event_author(
                session, sender_user_id=sender, sender_is_bot=is_bot
            )
        assert _author_tuple(scoped_row) == helper.as_tuple()
        assert _author_tuple(canon_row) == helper.as_tuple()
        assert scoped_row.ingress_presence_id == canon_row.ingress_presence_id


@pytest.mark.asyncio
async def test_v1_current_bot_equality_still_classifies_yuki(database: Database) -> None:
    _settings()
    result = await _scoped(database).append(
        scope=ConversationScope.private("8000", "1001"),
        platform_message_id="v1-self",
        sender_user_id="8000",
        direction="outbound",
        content="reply",
        sender_is_bot=True,
        origin="scheduled_automation",
    )
    async with database.sessions() as session:
        row = await session.get(ChatEventModel, result.event.id)
        presence = await session.scalar(
            select(PresenceModel).where(PresenceModel.external_account_id == "8000")
        )
        runtime = await session.get(IdentityRuntimeStateModel, 1)
    assert row is not None
    assert runtime is not None and runtime.state == "v1"
    assert row.author_kind == AuthorKind.YUKI.value
    assert presence is not None
    assert row.author_presence_id == presence.id
    assert row.author_person_id is None
    assert row.origin == "scheduled_automation"
    assert row.ingress_presence_id == presence.id
