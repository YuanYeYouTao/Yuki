"""Account round trips and the event-proven group recovery command boundary."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import MemorySender, make_settings

from qq_ai_bot.admin.control_resolution import ControlAccess
from qq_ai_bot.admin.models import ControlAuditRef
from qq_ai_bot.container import ApplicationContainer
from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationModel,
    ControlCommandReceiptModel,
    SpaceActiveRouteModel,
    SpaceBindingIngestRouteModel,
)
from qq_ai_bot.conversation.hydrate import ensure_canonical_conversation
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.identity.canonical_repository import (
    ensure_person,
    ensure_presence,
    ensure_space,
    find_identity_binding,
)
from qq_ai_bot.identity.db_models import (
    CanonicalPersonModel,
    CanonicalSpaceModel,
    PresenceModel,
    SpaceBindingModel,
)
from qq_ai_bot.identity.routing import RouteCandidate, RouteSendError
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import AdminOperationEventModel, ChatEventModel
from qq_ai_bot.services.admin.group_recovery import GroupRecoveryService


@dataclass
class Bot:
    self_id: str
    groups: frozenset[str] = frozenset({"2001", "2002"})

    async def call_api(self, action: str, **kwargs: object) -> dict[str, object]:
        assert action == "get_group_member_info"
        if str(kwargs["group_id"]) not in self.groups:
            raise RuntimeError("not a member")
        return {"user_id": int(self.self_id)}


class Sender(MemorySender):
    def __init__(self, bot: Bot) -> None:
        super().__init__()
        self.bot = bot


def message(text: str = "/ai on", *, message_id: str = "restore-1") -> InboundMessage:
    return InboundMessage(
        message_id=message_id,
        event_type="message:group:normal",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity(user_id="9000"),
        group_id="2001",
        bot_user_id="8000",
        text=text,
    )


async def stack(database: Database) -> tuple[ApplicationContainer, Bot, str, str, str]:
    from pathlib import Path

    from sqlalchemy.engine import make_url
    from tests.support.model_profiles import write_fake_profiles

    profiles = write_fake_profiles(Path(make_url(database.url).database).parent / "models.toml")
    app = ApplicationContainer(
        make_settings(database.url, plugin_system_enabled=False, model_profiles_file=profiles),
        database=database,
    )
    async with database.immediate_session() as session:
        await ensure_person(session, "9000")
        presence = await ensure_presence(session, "8000")
        space = await ensure_space(session, "2001")
        binding = await session.scalar(
            select(SpaceBindingModel).where(SpaceBindingModel.space_id == space)
        )
        assert binding is not None
        binding_id = binding.id
        await ensure_canonical_conversation(
            session,
            kind="space",
            primary_scope_key="bot:8000:group:2001",
            space_id=space,
        )
    bot = Bot("8000")
    app.gateway_registry.connect(bot, provider_id="snowluma", presence_id=presence)
    await app.route_monitor.on_connection_change()
    return app, bot, presence, space, binding_id


async def pause(database: Database, space: str, binding: str) -> None:
    async with database.immediate_session() as session:
        ingest = await session.get(SpaceBindingIngestRouteModel, binding)
        outgoing = await session.get(SpaceActiveRouteModel, space)
        assert ingest is not None and outgoing is not None
        ingest.paused = outgoing.paused = True
        ingest.revision += 1
        outgoing.revision += 1


async def state(database: Database, space: str, binding: str) -> tuple[object, ...]:
    async with database.sessions() as session:
        group = await session.get(CanonicalSpaceModel, space)
        ingest = await session.get(SpaceBindingIngestRouteModel, binding)
        outgoing = await session.get(SpaceActiveRouteModel, space)
        conversation = await session.scalar(
            select(CanonicalConversationModel).where(CanonicalConversationModel.space_id == space)
        )
        assert (
            group is not None
            and ingest is not None
            and outgoing is not None
            and conversation is not None
        )
        return (
            group.enabled,
            ingest.paused,
            outgoing.paused,
            ingest.ingest_presence_id,
            outgoing.presence_id,
            ingest.route_generation,
            outgoing.route_generation,
            conversation.id,
            conversation.generation,
            conversation.primary_alias_id,
            conversation.starts_after_event_id,
        )


@pytest.mark.asyncio
async def test_account_round_trip_preserves_exclusive_group_and_manual_pause(
    database: Database,
) -> None:
    app, bot, presence, space, binding = await stack(database)
    before = await state(database, space, binding)
    async with database.immediate_session() as session:
        other_presence = await ensure_presence(session, "8001")
        shared_space = await ensure_space(session, "2002")
        shared_binding = await session.scalar(
            select(SpaceBindingModel).where(SpaceBindingModel.space_id == shared_space)
        )
        assert shared_binding is not None
        shared_binding_id = shared_binding.id
        await ensure_canonical_conversation(
            session, kind="space", primary_scope_key="bot:8000:group:2002", space_id=shared_space
        )
    await app.route_monitor.on_connection_change()
    shared_before = await state(database, shared_space, shared_binding_id)
    other = Bot("8001", frozenset({"2002"}))
    app.gateway_registry.disconnect(bot)
    app.gateway_registry.connect(other, provider_id="snowluma", presence_id=other_presence)
    await app.route_monitor.on_connection_change()
    assert await state(database, space, binding) == before
    assert (await state(database, shared_space, shared_binding_id))[3:5] == (
        other_presence,
        other_presence,
    )
    app.gateway_registry.disconnect(other)
    app.gateway_registry.connect(bot, provider_id="snowluma", presence_id=presence)
    await app.route_monitor.on_connection_change()
    assert await state(database, space, binding) == before
    shared_after = await state(database, shared_space, shared_binding_id)
    assert shared_after[3:5] == (presence, presence)
    assert shared_after[5:7] == (shared_before[5] + 2, shared_before[6] + 2)
    assert shared_after[7:] == shared_before[7:]
    admitted = await app.canonical_ingress.pre_admit(bot, message("/ai ping"))
    assert admitted is not None and not admitted.dropped
    await pause(database, space, binding)
    await app.route_monitor.on_connection_change()
    assert (await state(database, space, binding))[1:3] == (True, True)


@pytest.mark.asyncio
async def test_superuser_on_recovers_legacy_pauses_and_commands_without_chat_side_effects(
    database: Database,
) -> None:
    app, bot, _presence, space, binding = await stack(database)
    await pause(database, space, binding)
    async with database.immediate_session() as session:
        group = await session.get(CanonicalSpaceModel, space)
        assert group is not None
        group.enabled = False
    before = await state(database, space, binding)
    sender = Sender(bot)
    hint = await app.processor.handle(message("/ai status"), sender)
    assert hint.reason == "group_route_paused"
    assert "/ai on" in sender.messages[-1].text
    result = await app.processor.handle(message(), sender)
    assert result.reason == "group_recovery"
    assert "恢复可用路由" in sender.messages[-1].text
    after = await state(database, space, binding)
    assert after[:3] == (True, False, False)
    assert after[3:5] == before[3:5]
    assert after[5:7] == (before[5] + 1, before[6] + 1)
    assert after[7:] == before[7:]
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ChatEventModel)) == 0
        assert await session.scalar(select(func.count()).select_from(AdminOperationEventModel)) == 1
        assert (
            await session.scalar(select(func.count()).select_from(ControlCommandReceiptModel)) == 1
        )
    result = await app.processor.handle(message("/ai ping", message_id="after-recovery"), sender)
    assert result.handled and sender.messages[-1].text.startswith("pong")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "non_admin",
        "bot_author",
        "forged_handle",
        "mismatched_presence",
        "missing_member",
        "disabled_presence",
        "ineligible_presence",
        "disabled_person",
        "image",
    ],
)
async def test_recovery_rejects_untrusted_or_unreachable_requests(
    database: Database, case: str
) -> None:
    from qq_ai_bot.domain.messages import AttachmentKind, MessageAttachment

    app, bot, presence, space, binding = await stack(database)
    await pause(database, space, binding)
    request = message()
    if case == "non_admin":
        request = replace(request, sender=SenderIdentity(user_id="1001"))
    elif case == "bot_author":
        request = replace(request, sender=SenderIdentity(user_id="9000", is_bot=True))
    elif case == "forged_handle":
        request = replace(request, bot_user_id="8001")
    elif case == "mismatched_presence":
        async with database.immediate_session() as session:
            other_presence = await ensure_presence(session, "8001")
        app.gateway_registry.disconnect(bot)
        app.gateway_registry.connect(bot, provider_id="snowluma", presence_id=other_presence)
    elif case == "missing_member":
        bot.groups = frozenset()
    elif case in {"disabled_presence", "ineligible_presence"}:
        async with database.immediate_session() as session:
            row = await session.get(PresenceModel, presence)
            assert row is not None
            if case == "disabled_presence":
                row.enabled = False
            else:
                row.ingest_eligible = False
    elif case == "disabled_person":
        async with database.immediate_session() as session:
            actor = await find_identity_binding(session, "9000")
            assert actor is not None
            person = await session.get(CanonicalPersonModel, actor.person_id)
            assert person is not None
            person.enabled = False
    elif case == "image":
        request = replace(
            request, attachments=(MessageAttachment(kind=AttachmentKind.IMAGE, label="image"),)
        )
    before = await state(database, space, binding)
    sender = Sender(bot)
    await app.processor.handle(request, sender)
    assert await state(database, space, binding) == before
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AdminOperationEventModel)) == 0
        assert (
            await session.scalar(select(func.count()).select_from(ControlCommandReceiptModel)) == 0
        )
        assert await session.scalar(select(func.count()).select_from(ChatEventModel)) == 0


@pytest.mark.asyncio
async def test_recovery_replay_does_not_reapply_after_an_operator_pause(database: Database) -> None:
    app, bot, _presence, space, binding = await stack(database)
    await pause(database, space, binding)
    first, second = Sender(bot), Sender(bot)
    await asyncio.gather(
        app.processor.handle(message(), first), app.processor.handle(message(), second)
    )
    assert (await state(database, space, binding))[1:3] == (False, False)
    await pause(database, space, binding)
    await app.processor.handle(message(), first)
    assert "已处理" in first.messages[-1].text
    assert (await state(database, space, binding))[1:3] == (True, True)
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AdminOperationEventModel)) == 1
        assert (
            await session.scalar(select(func.count()).select_from(ControlCommandReceiptModel)) == 1
        )


@pytest.mark.asyncio
async def test_recovery_uses_unique_member_but_never_steals_a_healthy_pin(
    database: Database,
) -> None:
    app, bot, _presence, space, binding = await stack(database)
    await pause(database, space, binding)
    async with database.immediate_session() as session:
        other_presence = await ensure_presence(session, "8001")
    other = Bot("8001")
    app.gateway_registry.connect(other, provider_id="snowluma", presence_id=other_presence)
    before = await state(database, space, binding)
    sender = Sender(other)
    await app.processor.handle(replace(message(), bot_user_id="8001"), sender)
    assert await state(database, space, binding) == before
    app.gateway_registry.disconnect(bot)
    async with database.immediate_session() as session:
        outgoing = await session.get(SpaceActiveRouteModel, space)
        assert outgoing is not None
        outgoing.paused = False  # An unreachable unpaused pin must also be repairable.
    await app.processor.handle(
        replace(message(message_id="second-request"), bot_user_id="8001"), sender
    )
    after = await state(database, space, binding)
    assert after[:5] == (True, False, False, other_presence, other_presence)
    assert after[7:] == before[7:]


@pytest.mark.asyncio
async def test_recovery_audit_failure_rolls_back_routes_and_enable(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, bot, _presence, space, binding = await stack(database)
    await pause(database, space, binding)
    async with database.immediate_session() as session:
        group = await session.get(CanonicalSpaceModel, space)
        assert group is not None
        group.enabled = False
    before = await state(database, space, binding)

    async def fail_audit(*args: object, **kwargs: object) -> None:
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr("qq_ai_bot.services.admin.group_recovery.add_audit_event", fail_audit)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        await app.processor.handle(message(), Sender(bot))
    assert await state(database, space, binding) == before
    async with database.sessions() as session:
        assert (
            await session.scalar(select(func.count()).select_from(ControlCommandReceiptModel)) == 0
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["route_revision", "connection"])
async def test_recovery_race_fails_closed(
    database: Database, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    app, bot, presence, space, binding = await stack(database)
    await pause(database, space, binding)
    before = await state(database, space, binding)
    original = app.presence_router.group_recovery_candidate

    async def raced(binding_id: str) -> RouteCandidate:
        candidate = await original(binding_id)
        if change == "connection":
            app.gateway_registry.disconnect(bot)
            app.gateway_registry.connect(Bot("8000"), provider_id="snowluma", presence_id=presence)
        else:
            async with database.immediate_session() as session:
                row = await session.get(SpaceBindingIngestRouteModel, binding_id)
                assert row is not None
                row.revision += 1
        return candidate

    monkeypatch.setattr(app.presence_router, "group_recovery_candidate", raced)
    await app.processor.handle(message(), Sender(bot))
    assert await state(database, space, binding) == before
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AdminOperationEventModel)) == 0


@pytest.mark.asyncio
async def test_recovery_rejects_multiple_replacements(database: Database) -> None:
    app, bot, _presence, space, binding = await stack(database)
    await pause(database, space, binding)
    app.gateway_registry.disconnect(bot)
    async with database.immediate_session() as session:
        presence_b = await ensure_presence(session, "8001")
        presence_c = await ensure_presence(session, "8002")
    other = Bot("8001")
    app.gateway_registry.connect(other, provider_id="snowluma", presence_id=presence_b)
    app.gateway_registry.connect(Bot("8002"), provider_id="snowluma", presence_id=presence_c)
    before = await state(database, space, binding)
    sender = Sender(other)
    await app.processor.handle(replace(message(), bot_user_id="8001"), sender)
    assert "多个可用账号" in sender.messages[-1].text
    assert await state(database, space, binding) == before


@pytest.mark.asyncio
async def test_recovery_preserves_independently_healthy_send_route(database: Database) -> None:
    app, bot, _presence, space, binding = await stack(database)
    await pause(database, space, binding)
    async with database.immediate_session() as session:
        other_presence = await ensure_presence(session, "8001")
        outgoing = await session.get(SpaceActiveRouteModel, space)
        assert outgoing is not None
        outgoing.paused = False
        outgoing.presence_id = other_presence
    app.gateway_registry.connect(Bot("8001"), provider_id="snowluma", presence_id=other_presence)
    before = await state(database, space, binding)
    await app.processor.handle(message(), Sender(bot))
    after = await state(database, space, binding)
    assert after[:3] == (True, False, False)
    assert after[4] == other_presence
    assert after[6:] == before[6:]


@pytest.mark.asyncio
async def test_recovery_creates_missing_routes_only_for_the_proven_existing_group(
    database: Database,
) -> None:
    app, bot, _presence, space, binding = await stack(database)
    before = await state(database, space, binding)
    async with database.immediate_session() as session:
        ingest = await session.get(SpaceBindingIngestRouteModel, binding)
        outgoing = await session.get(SpaceActiveRouteModel, space)
        assert ingest is not None and outgoing is not None
        await session.delete(ingest)
        await session.delete(outgoing)
    sender = Sender(bot)
    await app.processor.handle(message(), sender)
    assert "恢复可用路由" in sender.messages[-1].text
    assert await state(database, space, binding) == before


NEW_GROUP = "490001"


async def new_group_binding(database: Database) -> SpaceBindingModel | None:
    async with database.sessions() as session:
        return await session.scalar(
            select(SpaceBindingModel)
            .where(
                SpaceBindingModel.platform == "qq",
                SpaceBindingModel.external_space_id == NEW_GROUP,
            )
            .limit(1)
        )


@pytest.mark.asyncio
async def test_superuser_on_registers_proven_new_group_atomically_and_replay_is_stable(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, bot, presence, _space, _binding = await stack(database)
    bot.groups |= {NEW_GROUP}
    assert await new_group_binding(database) is None
    writer_active = False
    original_writer = database.immediate_session

    @asynccontextmanager
    async def writer() -> AsyncIterator[AsyncSession]:
        nonlocal writer_active
        async with original_writer() as session:
            writer_active = True
            try:
                yield session
            finally:
                writer_active = False

    original_probe = bot.call_api
    probes = 0

    async def probe(action: str, **kwargs: object) -> dict[str, object]:
        nonlocal probes
        assert not writer_active
        probes += 1
        return await original_probe(action, **kwargs)

    monkeypatch.setattr(database, "immediate_session", writer)
    monkeypatch.setattr(bot, "call_api", probe)
    request = replace(message(), group_id=NEW_GROUP)
    sender = Sender(bot)
    await app.processor.handle(request, sender)
    assert "恢复可用路由" in sender.messages[-1].text
    binding = await new_group_binding(database)
    assert binding is not None
    assert probes == 1
    async with database.sessions() as session:
        space = await session.get(CanonicalSpaceModel, binding.space_id)
        ingest = await session.get(SpaceBindingIngestRouteModel, binding.id)
        outgoing = await session.get(SpaceActiveRouteModel, binding.space_id)
        assert space is not None and space.enabled
        assert ingest is not None and not ingest.paused and ingest.ingest_presence_id == presence
        assert outgoing is not None and not outgoing.paused and outgoing.presence_id == presence
        assert outgoing.space_binding_id == binding.id
        assert ingest.route_generation == outgoing.route_generation == 1
        assert await session.scalar(select(func.count()).select_from(ChatEventModel)) == 0
        assert await session.scalar(select(func.count()).select_from(AdminOperationEventModel)) == 1
        assert (
            await session.scalar(select(func.count()).select_from(ControlCommandReceiptModel)) == 1
        )
        assert (
            await session.scalar(
                select(CanonicalConversationModel.id).where(
                    CanonicalConversationModel.space_id == binding.space_id
                )
            )
            is None
        )
    await pause(database, binding.space_id, binding.id)
    await app.processor.handle(request, sender)
    assert "已处理" in sender.messages[-1].text
    replayed = await new_group_binding(database)
    assert replayed is not None and replayed.id == binding.id
    async with database.sessions() as session:
        ingest = await session.get(SpaceBindingIngestRouteModel, binding.id)
        assert ingest is not None and ingest.paused
        assert (
            await session.scalar(select(func.count()).select_from(ControlCommandReceiptModel)) == 1
        )
    await app.processor.handle(replace(request, message_id="new-enable"), sender)
    await app.processor.handle(replace(request, text="/ai ping", message_id="new-ping"), sender)
    assert sender.messages[-1].text.startswith("pong")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "non_admin",
        "missing_member",
        "ambiguous",
        "disabled_presence",
        "ineligible_presence",
        "forged_handle",
        "mismatched_presence",
        "disabled_person",
    ],
)
async def test_new_group_registration_rejects_untrusted_or_unproven_requests(
    database: Database, case: str
) -> None:
    app, bot, presence, _space, _binding = await stack(database)
    bot.groups |= {NEW_GROUP}
    request = replace(message(), group_id=NEW_GROUP)
    if case == "non_admin":
        request = replace(request, sender=SenderIdentity(user_id="1001"))
    elif case == "missing_member":
        bot.groups = frozenset()
    elif case == "ambiguous":
        async with database.immediate_session() as session:
            other_presence = await ensure_presence(session, "8001")
        app.gateway_registry.connect(
            Bot("8001", frozenset({NEW_GROUP})),
            provider_id="snowluma",
            presence_id=other_presence,
        )
    elif case == "forged_handle":
        request = replace(request, bot_user_id="8001")
    elif case == "mismatched_presence":
        async with database.immediate_session() as session:
            other_presence = await ensure_presence(session, "8001")
        app.gateway_registry.disconnect(bot)
        app.gateway_registry.connect(bot, provider_id="snowluma", presence_id=other_presence)
    elif case in {"disabled_presence", "ineligible_presence"}:
        async with database.immediate_session() as session:
            row = await session.get(PresenceModel, presence)
            assert row is not None
            if case == "disabled_presence":
                row.enabled = False
            else:
                row.ingest_eligible = False
    elif case == "disabled_person":
        async with database.immediate_session() as session:
            actor = await find_identity_binding(session, "9000")
            assert actor is not None
            person = await session.get(CanonicalPersonModel, actor.person_id)
            assert person is not None
            person.enabled = False
    sender = Sender(bot)
    await app.processor.handle(request, sender)
    assert await new_group_binding(database) is None
    if case == "ambiguous":
        assert "多个可用账号" in sender.messages[-1].text
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AdminOperationEventModel)) == 0
        assert (
            await session.scalar(select(func.count()).select_from(ControlCommandReceiptModel)) == 0
        )
        assert await session.scalar(select(func.count()).select_from(ChatEventModel)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["connection", "presence_revision", "existing_owner"])
async def test_new_group_registration_race_never_creates_or_replaces_owner(
    database: Database, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    app, bot, presence, _space, _binding = await stack(database)
    bot.groups |= {NEW_GROUP}
    original = app.presence_router.unbound_group_recovery_presence

    async def raced(group_id: str) -> str:
        candidate = await original(group_id)
        if change == "connection":
            app.gateway_registry.disconnect(bot)
            app.gateway_registry.connect(
                Bot("8000", frozenset({NEW_GROUP})), provider_id="snowluma", presence_id=presence
            )
        else:
            async with database.immediate_session() as session:
                if change == "presence_revision":
                    row = await session.get(PresenceModel, presence)
                    assert row is not None
                    row.revision += 1
                else:
                    space_id = await ensure_space(session, NEW_GROUP)
                    space = await session.get(CanonicalSpaceModel, space_id)
                    assert space is not None
                    space.enabled = False
        return candidate

    monkeypatch.setattr(app.presence_router, "unbound_group_recovery_presence", raced)
    sender = Sender(bot)
    await app.processor.handle(replace(message(), group_id=NEW_GROUP), sender)
    assert "刚发生变化" in sender.messages[-1].text
    binding = await new_group_binding(database)
    if change == "existing_owner":
        assert binding is not None
        async with database.sessions() as session:
            space = await session.get(CanonicalSpaceModel, binding.space_id)
            assert space is not None and not space.enabled
            assert await session.get(SpaceBindingIngestRouteModel, binding.id) is None
            assert await session.get(SpaceActiveRouteModel, binding.space_id) is None
    else:
        assert binding is None
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AdminOperationEventModel)) == 0
        assert (
            await session.scalar(select(func.count()).select_from(ControlCommandReceiptModel)) == 0
        )


@pytest.mark.asyncio
async def test_new_group_registration_audit_failure_rolls_back_identity_and_routes(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, bot, _presence, _space, _binding = await stack(database)
    bot.groups |= {NEW_GROUP}
    async with database.sessions() as session:
        space_count = await session.scalar(select(func.count()).select_from(CanonicalSpaceModel))

    async def fail_audit(*args: object, **kwargs: object) -> None:
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr("qq_ai_bot.services.admin.group_recovery.add_audit_event", fail_audit)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        await app.processor.handle(replace(message(), group_id=NEW_GROUP), Sender(bot))
    assert await new_group_binding(database) is None
    async with database.sessions() as session:
        assert (
            await session.scalar(select(func.count()).select_from(CanonicalSpaceModel))
            == space_count
        )
        assert (
            await session.scalar(select(func.count()).select_from(ControlCommandReceiptModel)) == 0
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["group", "event_type", "trigger_message"])
async def test_new_group_original_control_request_recovers_receipt_and_rejects_reused_id(
    database: Database, changed: str
) -> None:
    app, bot, presence, _space, _binding = await stack(database)
    bot.groups |= {NEW_GROUP}
    access = ControlAccess(database, superuser_ids=frozenset({"9000"}))
    principal = await access.principal_for_qq("9000")
    context = access.context(principal, await access.space_target(NEW_GROUP))
    assert context.canonical_target.space_id is None
    connection_id = app.gateway_registry.resolve_active(presence).snapshot.connection_id
    audit = ControlAuditRef(
        user_id="9000",
        bot_user_id="8000",
        trigger_message_id="original-request",
        conversation_key=f"bot:8000:group:{NEW_GROUP}",
    )
    service = GroupRecoveryService(database, app.gateway_registry, app.presence_router)
    results = await asyncio.gather(
        *(
            service.enable(
                context,
                presence_id=presence,
                connection_id=connection_id,
                audit=audit,
                event_type="message:group:normal",
            )
            for _ in range(2)
        )
    )
    assert sorted(results) == [False, True]
    binding = await new_group_binding(database)
    assert binding is not None
    await pause(database, binding.space_id, binding.id)
    assert not await service.enable(
        context,
        presence_id=presence,
        connection_id=connection_id,
        audit=audit,
        event_type="message:group:normal",
    )
    altered = context
    event_type = "message:group:normal"
    if changed == "group":
        altered = replace(
            context, canonical_target=replace(context.canonical_target, storage_group_id="490002")
        )
    elif changed == "event_type":
        event_type = "message:group:anonymous"
    else:
        audit = replace(audit, trigger_message_id="different-request")
    with pytest.raises(RouteSendError) as error:
        await service.enable(
            altered,
            presence_id=presence,
            connection_id=connection_id,
            audit=audit,
            event_type=event_type,
        )
    assert error.value.category == "conflict"
    unchanged = await new_group_binding(database)
    assert unchanged is not None and unchanged.id == binding.id
    async with database.sessions() as session:
        ingest = await session.get(SpaceBindingIngestRouteModel, binding.id)
        outgoing = await session.get(SpaceActiveRouteModel, binding.space_id)
        assert ingest is not None and outgoing is not None
        assert ingest.paused and outgoing.paused
        assert ingest.route_generation == outgoing.route_generation == 1
        assert await session.scalar(select(func.count()).select_from(AdminOperationEventModel)) == 1
        assert (
            await session.scalar(select(func.count()).select_from(ControlCommandReceiptModel)) == 1
        )
