"""Native Telegram metadata updates local names without remote enrichment or chat."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

from sqlalchemy import func, select
from tests.integration.test_telegram_ingress_content import _service_update
from tests.integration.test_telegram_runtime import drain, update
from tests.integration.test_telegram_runtime import telegram_env as telegram_env

from yuki.conversation.canonical_db_models import CanonicalEventReceiptModel
from yuki.identity.canonical_repository import (
    create_space_binding,
    find_identity_binding,
    find_space_binding,
)
from yuki.identity.db_models import (
    AccountNameObservationModel,
    CanonicalPersonModel,
    CanonicalSpaceModel,
)
from yuki.persistence.models import ChatEventModel, MembershipModel, PersonAliasModel


async def test_new_group_message_updates_native_names_in_its_original_admission(
    telegram_env, database
):
    env = telegram_env
    await env.gateway.receive(update(chat=-10042, text="!ai first"))
    await drain(env)
    async with database.sessions() as session:
        first = await session.scalar(
            select(ChatEventModel).where(ChatEventModel.direction == "inbound")
        )
        original = first.id, first.canonical_event_id, first.author_person_id, first.sender_nickname
        receipt_id = await session.scalar(
            select(CanonicalEventReceiptModel.id).where(
                CanonicalEventReceiptModel.canonical_event_id == first.canonical_event_id
            )
        )
    incoming = update(2, chat=-10042, text="!ai next")
    incoming["message"]["chat"]["title"] = "New group"
    incoming["message"]["from"]["first_name"] = "New nickname"

    await env.gateway.receive(incoming)
    await drain(env)

    async with database.sessions() as session:
        binding = await find_space_binding(session, "-10042", platform="telegram")
        space = await session.get(CanonicalSpaceModel, binding.space_id)
        events = list(
            await session.scalars(
                select(ChatEventModel)
                .where(ChatEventModel.direction == "inbound")
                .order_by(ChatEventModel.id)
            )
        )
        person = await session.get(CanonicalPersonModel, events[0].author_person_id)
        account = await find_identity_binding(
            session, events[0].sender_user_id, platform="telegram"
        )
        assert space.name == binding.display_name == "New group"
        assert person.display_name == "P" and account.display_name == "New nickname"
        assert [row.sender_nickname for row in events] == ["P", "New nickname"]
        assert (
            events[0].id,
            events[0].canonical_event_id,
            events[0].author_person_id,
            events[0].sender_nickname,
        ) == original
        assert (
            await session.scalar(
                select(CanonicalEventReceiptModel.id).where(
                    CanonicalEventReceiptModel.canonical_event_id == events[0].canonical_event_id
                )
            )
            == receipt_id
        )
        assert events[0].author_person_id == events[1].author_person_id
        assert await session.get(MembershipModel, (person.id, space.id)) is not None
        aliases = set(
            await session.scalars(
                select(PersonAliasModel.alias).where(
                    PersonAliasModel.canonical_person_id == person.id
                )
            )
        )
        assert not {"P", "New nickname"} & aliases
        names = set(
            await session.scalars(
                select(AccountNameObservationModel.name).where(
                    AccountNameObservationModel.account_id == account.account_id,
                    AccountNameObservationModel.assignment_id == account.id,
                    AccountNameObservationModel.space_binding_id.is_(None),
                )
            )
        )
        assert {"P", "New nickname"} <= names
    assert {method for method, _ in env.wire} <= {
        "getMe",
        "getChatMember",
        "sendMessage",
        "sendChatAction",
    }


async def test_group_rename_updates_only_known_names_without_chat(
    telegram_env, database, monkeypatch
):
    env = telegram_env
    async with database.immediate_session() as session:
        binding = await create_space_binding(
            session,
            external_space_id="-10042",
            name="Old group",
            enabled=False,
            autonomous_enabled=False,
            require_mention=True,
            now=datetime.now(UTC),
            platform="telegram",
        )
        original_id, space_id = binding.id, binding.space_id
    async with database.sessions() as session:
        people_before = await session.scalar(select(func.count()).select_from(CanonicalPersonModel))
    handle = AsyncMock(wraps=env.harness.processor.handle)
    monkeypatch.setattr(env.harness.processor, "handle", handle)

    await env.gateway.receive(_service_update("new_chat_title", "Renamed group"))

    handle.assert_not_awaited()
    assert not env.model.requests and not env.gateway._turns
    assert [method for method, _ in env.wire] == ["getMe"]
    async with database.sessions() as session:
        current = await find_space_binding(session, "-10042", platform="telegram")
        space = await session.get(CanonicalSpaceModel, space_id)
        assert current.id == original_id and current.space_id == space_id
        assert current.display_name == space.name == "Renamed group"
        assert not space.enabled and not space.autonomous_enabled and space.require_mention
        assert await session.scalar(select(func.count()).select_from(ChatEventModel)) == 0
        assert (
            await session.scalar(select(func.count()).select_from(CanonicalPersonModel))
            == people_before
        )


async def test_replayed_or_edited_old_message_does_not_restore_old_names(telegram_env, database):
    env = telegram_env
    original = update(chat=-10042, text="!ai first")
    await env.gateway.receive(original)
    await drain(env)
    await env.gateway.receive(_service_update("new_chat_title", "Renamed", identifier=2))

    await env.gateway.receive(original)
    edited = update(3, chat=-10042, text="Edited original", edit=True)
    edited["edited_message"]["from"]["first_name"] = "Stale nickname"
    await env.gateway.receive(edited)
    await drain(env)

    async with database.sessions() as session:
        binding = await find_space_binding(session, "-10042", platform="telegram")
        space = await session.get(CanonicalSpaceModel, binding.space_id)
        event = await session.scalar(
            select(ChatEventModel).where(ChatEventModel.direction == "inbound")
        )
        account = await find_identity_binding(session, event.sender_user_id, platform="telegram")
        assert space.name == binding.display_name == "Renamed"
        assert account.display_name == "P"
        assert event.content == "Edited original"
    assert len(env.model.requests) == 2
