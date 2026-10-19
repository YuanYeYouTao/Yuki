"""Real indexed mention lookup, account identity and conversation-local disambiguation."""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import event

from yuki.adapters.onebot.social import OneBotSocialOperations
from yuki.conversation.hydrate import ensure_canonical_conversation
from yuki.identity.canonical_repository import create_space_binding, find_identity_binding
from yuki.identity.db_models import (
    AccountModel,
    AccountNameObservationModel,
    AccountSpaceProfileModel,
    IdentityBindingModel,
)
from yuki.persistence.models import ChatEventModel
from yuki.social.content import prepare_for_entry

NOW = datetime(2026, 10, 20, tzinfo=UTC)


@contextmanager
def queries(database):
    captured = []

    def record(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().startswith("SELECT"):
            captured.append((statement, parameters))

    engine = database.engine.sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        yield captured
    finally:
        event.remove(engine, "before_cursor_execute", record)


@pytest_asyncio.fixture
async def entry(database):
    async with database.sessions() as session, session.begin():
        space = await create_space_binding(
            session,
            external_space_id="71999",
            name="mention tests",
            enabled=True,
            autonomous_enabled=True,
            require_mention=False,
            now=NOW,
        )
        conversation = await ensure_canonical_conversation(
            session,
            kind="space",
            primary_scope_key="bot:9000:group:71999",
            space_id=space.space_id,
            space_binding_id=space.id,
        )
    return SimpleNamespace(space=space, conversation_id=conversation.conversation_id)


async def observe(
    session,
    entry,
    *,
    name="Ice",
    native="71001",
    account_id=None,
    platform="qq",
    scoped=True,
    assignment_id=None,
):
    account = AccountModel(
        id=account_id or str(uuid4()),
        platform=platform,
        external_account_id=native,
        display_name=name,
    )
    session.add(account)
    await session.flush()
    session.add(
        AccountSpaceProfileModel(
            account_id=account.id,
            space_binding_id=entry.space.id,
            display_name=name,
            first_seen_at=NOW,
            last_seen_at=NOW,
        )
    )
    session.add(
        AccountNameObservationModel(
            account_id=account.id,
            assignment_id=assignment_id,
            space_binding_id=entry.space.id if scoped else None,
            name=name,
            first_seen_at=NOW,
            last_seen_at=NOW,
        )
    )
    await session.flush()
    return account


async def prepare(database, entry, text, **kwargs):
    async with database.sessions() as session:
        return await prepare_for_entry(
            session,
            text,
            platform="qq",
            space_binding_id=entry.space.id,
            operations=OneBotSocialOperations(),
            conversation_id=entry.conversation_id,
            **kwargs,
        )


def message(account, conversation_id, *, when=NOW, duplicate=False, external=False):
    """An explicit accepted historical fact, independent of today's recipient Assignment."""
    return ChatEventModel(
        bot_user_id="9000",
        platform_message_id=str(uuid4()),
        scope_type="group",
        group_id="71999",
        sender_user_id=account.external_account_id,
        direction="external" if external else "inbound",
        event_kind="external_event" if external else "message",
        content="actual earlier message",
        canonical_event_id=str(uuid4()),
        canonical_conversation_id=conversation_id,
        author_kind="external_bot",
        occurred_at=when,
        observed_at=when,
        suppression_status="duplicate" if duplicate else "keeper",
        utterance_fingerprint="duplicate" if duplicate else None,
        source_plugin_id="mention-test" if external else None,
        external_source="fixture" if external else None,
        external_event_key=str(uuid4()) if external else None,
        external_event_type="fixture" if external else None,
        external_payload_json="{}" if external else None,
        external_target_id="fixture" if external else None,
        origin="plugin_background" if external else "user_message",
    )


@pytest.mark.asyncio
async def test_plain_text_and_protected_addresses_do_not_read_identity(database, entry):
    with queries(database) as captured:
        for body in ("普通消息", r"\@Ice", "a@Ice.example https://test/@Ice"):
            content = await prepare(database, entry, body)
            assert not content.message.mentions
    assert captured == []


@pytest.mark.asyncio
async def test_same_account_old_and_null_observations_use_one_current_snapshot(database, entry):
    async with database.sessions() as session, session.begin():
        binding = await find_identity_binding(session, "1001")
        account = await session.get(AccountModel, binding.account_id)
        old = IdentityBindingModel(
            id=str(uuid4()),
            account_id=account.id,
            person_id=binding.person_id,
            status="ended",
            started_at=None,
            ended_at=None,
        )
        session.add(old)
        await session.flush()
        session.add(
            AccountSpaceProfileModel(
                account_id=account.id,
                space_binding_id=entry.space.id,
                display_name="Ice",
                first_seen_at=NOW,
                last_seen_at=NOW,
            )
        )
        for assignment in (None, old.id, binding.id):
            session.add(
                AccountNameObservationModel(
                    account_id=account.id,
                    assignment_id=assignment,
                    space_binding_id=entry.space.id,
                    name="Ice",
                    first_seen_at=NOW,
                    last_seen_at=NOW,
                )
            )
        binding.status = "disabled"
    with queries(database) as captured:
        content = await prepare(database, entry, "@Ice 再@ Ice")
    assert [item.account_id for item in content.message.mentions] == [account.id] * 2
    assert all(item.assignment_id == binding.id for item in content.message.mentions)
    assert not content.unresolved
    assert len(captured) == 1 and "chat_events" not in captured[0][0]


@pytest.mark.asyncio
async def test_longest_name_does_not_query_recent_shorter_accounts(database, entry):
    async with database.sessions() as session, session.begin():
        await observe(session, entry, name="Ice", native="71001")
        longest = await observe(session, entry, name="Iceberg", native="71002")
    with queries(database) as captured:
        content = await prepare(database, entry, "@Iceberg 看这里")
    assert content.message.mentions[0].account_id == longest.id
    assert content.message.text == "@Iceberg 看这里"
    assert len(captured) == 1 and "chat_events" not in captured[0][0]


@pytest.mark.asyncio
async def test_at_inside_a_longest_name_does_not_add_a_second_recipient_lookup(database, entry):
    async with database.sessions() as session, session.begin():
        whole = await observe(session, entry, name="A@B", native="71001")
        await observe(session, entry, name="B", native="71002")
        await observe(session, entry, name="B", native="71003")
    with queries(database) as captured:
        content = await prepare(database, entry, "@A@B 看这里")
    assert content.message.text == "@A@B 看这里"
    assert [item.account_id for item in content.message.mentions] == [whole.id]
    assert len(captured) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("activity", ["none", "time", "id"])
async def test_real_equal_names_choose_latest_message_then_id_then_account(
    database, entry, activity
):
    async with database.sessions() as session, session.begin():
        first = await observe(
            session, entry, native="71001", account_id="11111111-1111-4111-8111-111111111111"
        )
        second = await observe(
            session, entry, native="71002", account_id="22222222-2222-4222-8222-222222222222"
        )
        if activity == "time":
            session.add(message(second, entry.conversation_id, when=NOW + timedelta(seconds=1)))
            await session.flush()
            session.add(message(first, entry.conversation_id))
        elif activity == "id":
            session.add(message(first, entry.conversation_id))
            await session.flush()
            session.add(message(second, entry.conversation_id))
    with queries(database) as captured:
        content = await prepare(database, entry, "@Ice 和@Ice，再@ Ice")
    expected = first.id if activity == "none" else second.id
    assert [item.account_id for item in content.message.mentions] == [expected] * 3
    assert content.message.text == "@Ice 和@Ice，再@ Ice"
    assert not content.unresolved
    assert len(captured) == 2
    assert sum("chat_events" in statement for statement, _ in captured) == 1


@pytest.mark.asyncio
async def test_recent_choice_ignores_other_conversations_duplicates_and_plugin_events(
    database, entry
):
    async with database.sessions() as session, session.begin():
        first = await observe(session, entry, native="71001")
        second = await observe(session, entry, native="71002")
        other = await ensure_canonical_conversation(
            session,
            kind="space",
            primary_scope_key="bot:9000:group:71999:thread:other",
            space_id=entry.space.space_id,
            space_binding_id=entry.space.id,
            thread_id="other",
        )
        session.add_all(
            [
                message(second, entry.conversation_id),
                message(first, entry.conversation_id, when=NOW + timedelta(days=1), duplicate=True),
                message(first, entry.conversation_id, when=NOW + timedelta(days=2), external=True),
                message(first, other.conversation_id, when=NOW + timedelta(days=3)),
            ]
        )
    content = await prepare(database, entry, "@Ice")
    assert content.message.mentions[0].account_id == second.id


@pytest.mark.asyncio
async def test_private_names_only_come_from_original_participant_accounts(database, entry):
    async with database.sessions() as session, session.begin():
        binding = await find_identity_binding(session, "1001")
        account = await session.get(AccountModel, binding.account_id)
        session.add(
            AccountNameObservationModel(
                account_id=account.id,
                assignment_id=None,
                space_binding_id=None,
                name="Ice",
                first_seen_at=NOW,
                last_seen_at=NOW,
            )
        )
        outsider = await observe(session, entry, name="Ice", scoped=False)
    async with database.sessions() as session:
        content = await prepare_for_entry(
            session,
            "@Ice",
            platform="qq",
            space_binding_id=None,
            participant_assignment_ids=(binding.id,),
            operations=OneBotSocialOperations(),
        )
    assert content.message.mentions[0].account_id == account.id
    assert content.message.mentions[0].account_id != outsider.id


@pytest.mark.asyncio
async def test_explicit_accounts_are_physical_and_unknown_references_keep_at(database, entry):
    async with database.sessions() as session, session.begin():
        physical = await observe(session, entry)
        physical.display_name = "\tIce\nOld\x00 "
        other_platform = await observe(session, entry, native="71002", platform="telegram")
    unknown = str(uuid4())
    body = f"@{{account:{physical.id}}} @{{account:{unknown}}} @{{account:{other_platform.id}}}"
    with queries(database) as captured:
        content = await prepare(database, entry, body)
    assert (
        content.message.text == f"@IceOld @{{account:{unknown}}} @{{account:{other_platform.id}}}"
    )
    assert len(content.message.mentions) == 1
    assert content.message.mentions[0].account_id == physical.id
    assert content.message.mentions[0].assignment_id is None
    assert len(content.unresolved) == 2
    assert len(captured) == 1
    async with database.engine.connect() as connection:
        statement, parameters = captured[0]
        rows = await connection.exec_driver_sql("EXPLAIN QUERY PLAN " + statement, parameters)
        plan = "\n".join(row[3] for row in rows)
    assert "sqlite_autoindex_accounts_1 (id=?)" in plan
    assert "platform=?" not in plan and "SCAN accounts" not in plan


@pytest.mark.asyncio
async def test_name_and_recent_queries_use_covering_scope_and_message_indexes(database, entry):
    async with database.sessions() as session, session.begin():
        await observe(session, entry)
        second = await observe(session, entry, native="71002")
        session.add(message(second, entry.conversation_id))
    with queries(database) as captured:
        await prepare(database, entry, "@Ice " * 1000)
    assert len(captured) == 2
    async with database.engine.connect() as connection:
        plans = [
            (await connection.exec_driver_sql("EXPLAIN QUERY PLAN " + statement, parameters)).all()
            for statement, parameters in captured
        ]
    names = "\n".join(row[3] for row in plans[0])
    recent = "\n".join(row[3] for row in plans[1])
    assert names.count("ix_account_names_name_scope_account (name=? AND space_binding_id=?)") == 2
    assert "ix_chat_events_conversation_message_sender_time" in recent
    assert "canonical_conversation_id=? AND sender_user_id=?" in recent
    assert "SCAN chat_events" not in recent
