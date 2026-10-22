"""Ownership intervals survive both erasure orders and a later account owner."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from tests.support.canonical_identity import ensure_person
from tests.support.event_seed import seed_scoped_event
from tests.support.memory_identity import transfer_account
from tests.support.person_merge import merge_persons
from tests.support.social_identity_cases import social_env
from tests.unit.test_client_identity import code_for
from tests.unit.test_rebuild_receipt_finalization import _seed

from yuki.client_api.errors import ClientError
from yuki.domain.conversations import ConversationScope
from yuki.identity.canonical_repository import find_identity_binding
from yuki.identity.db_models import IdentityBindingModel
from yuki.memory.rebuild.repository import MemoryRebuildRepository
from yuki.persistence.event_repository import EventLedgerRepository
from yuki.persistence.models import (
    ChatEventModel,
    MemoryRebuildItemModel,
    MemoryRebuildProposalModel,
    MemoryRebuildRunModel,
)
from yuki.persistence.people_repository import PeopleRepository
from yuki.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork
from yuki.runtime.work_repository import WorkRepository


async def append(database, message, account="1001", group=False):
    return (
        await seed_scoped_event(
            ScopedEventLedgerUnitOfWork(database),
            scope=ConversationScope.group("8000", "2001")
            if group
            else ConversationScope.private("8000", account),
            platform_message_id=str(uuid4()),
            sender_user_id=account,
            direction="inbound",
            content=message,
            segments=(
                {"type": "at", "data": {"qq": 1001}},
                {"type": "text", "data": {"text": message}},
            ),
        )
    ).event


async def pair_owner(service, person):
    code = await service.issue_for_operator(person, str(uuid4()), str(uuid4()))
    device = await service.pair(code["code"], "owner")
    return device


def values(row):
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


@pytest.mark.parametrize("forget_current", [False, True])
async def test_transfer_erases_only_selected_person_and_source_period(database, forget_current):
    seeded = await _seed(database, 2, proposals=True)
    service, current_person, _code = await code_for(database, "1002")
    async with database.read_session() as session:
        old = await find_identity_binding(session, "1001")
        old_person, old_binding = old.person_id, old.id
    old_mention = await append(database, "old period mentions 1001", account="1003", group=True)
    old_device = await pair_owner(service, old_person)
    await transfer_account(database, "1001")
    current = await append(database, "current owner 1001")
    current_mention = await append(
        database, "current period mentions 1001", account="1003", group=True
    )
    current_device = await pair_owner(service, current_person)
    async with database.immediate_session() as session:
        # One run built after transfer explicitly selects source events from both periods.
        run = await session.get(MemoryRebuildRunModel, seeded.run_id)
        run.created_at = datetime.now(UTC) + timedelta(days=1)
        run.selection_json = '{"sender_user_ids":["1001"],"all_events":true}'
        run.selection_hash = "f" * 64
        selection = run.selection_json
        item = MemoryRebuildItemModel(
            run_id=run.id,
            event_id=current.id,
            status="staged",
            source_event_hash="a" * 64,
            claim_count=1,
            created_at=run.created_at,
            updated_at=run.created_at,
        )
        session.add(item)
        await session.flush()
        session.add(
            MemoryRebuildProposalModel(
                run_id=run.id,
                item_id=item.id,
                event_id=current.id,
                claim_index=0,
                claim_json="{}",
                claim_hash="b" * 64,
                scope_type="person",
                subject_user_id="1001",
                operation="assert",
                kind="fact",
                authority="observed",
                confidence=0.9,
                review_status="approved",
                commit_status="committed",
                created_at=run.created_at,
                updated_at=run.created_at,
            )
        )
    survivor = old_person if forget_current else current_person
    survivor_event = seeded.event_ids[0] if forget_current else current.id
    work = WorkRepository(database)
    async with database.read_session() as session:
        event = await session.get(ChatEventModel, survivor_event)
        snapshot = values(event)
        conversation_id = event.canonical_conversation_id
    lease = await work.acquire(conversation_id, 1)
    accepted = await work.accept(
        lease,
        source_key="surviving-owner",
        source={"actor_person_id": survivor},
        goal="keep original owner",
    )
    await work.release(lease)
    work_before = await work.get(accepted["id"])
    people = PeopleRepository(database, memory_rebuilds=MemoryRebuildRepository(database))
    if forget_current:
        assert await people.delete_person("1001")
    else:
        assert await people.delete_person_id(old_person)
    assert await work.get(accepted["id"]) == work_before
    async with database.read_session() as session:
        assert values(await session.get(ChatEventModel, survivor_event)) == snapshot
        assert (await session.get(MemoryRebuildRunModel, seeded.run_id)).selection_json == selection
        sources = set(await session.scalars(select(MemoryRebuildProposalModel.event_id)))
        assert sources == (set(seeded.event_ids) if forget_current else {current.id})
        old_text = (await session.get(ChatEventModel, old_mention.id)).content
        current_text = (await session.get(ChatEventModel, current_mention.id)).content
        assert old_text == (
            "old period mentions 1001" if forget_current else "old period mentions [已删除用户]"
        )
        assert current_text == (
            "current period mentions [已删除用户]"
            if forget_current
            else "current period mentions 1001"
        )
        assert (await session.get(IdentityBindingModel, old_binding) is None) == (
            not forget_current
        )
        active = await find_identity_binding(session, "1001")
        assert (active is None) == forget_current
    alive, revoked = (
        (old_device, current_device) if forget_current else (current_device, old_device)
    )
    assert (await service.authenticate(alive["token"])).person_id == survivor
    with pytest.raises(ClientError, match="unauthenticated"):
        await service.authenticate(revoked["token"])
    if forget_current:
        async with database.immediate_session() as session:
            later_person = await ensure_person(session, "1001")
            later = await find_identity_binding(session, "1001")
            assert later.person_id == later_person != old_person
            assert later.history_starts_at == later.created_at
        later_event = await append(database, "third owner")
        assert later_event.author_person_id == later_person
        assert (
            await EventLedgerRepository(database).list_scope_recent(
                ConversationScope.private("8000", "1001"), limit=20
            )
        )[0].id == later_event.id
        assert await people.delete_person_id(later_person)
        async with database.read_session() as session:
            assert values(await session.get(ChatEventModel, survivor_event)) == snapshot


@pytest.mark.parametrize("forget_previous", [False, True])
async def test_live_history_uses_current_row_even_after_previous_owner_is_forgotten(
    database, tmp_path, forget_previous
):
    env = await social_env(database, tmp_path)
    assert await env.router.cas_takeover_person(env.person) == "taken"
    receipt = await env.service.execute(
        "send",
        {"target": {"kind": "person", "target_id": env.person}, "text": "old"},
        env.context,
    )
    await transfer_account(database, "10001")
    new_event = await append(database, "new owner's message", account="10001")
    if forget_previous:
        assert await PeopleRepository(database).delete_person_id(env.person)
    else:
        original = await env.service.execute(
            "query_chat_history",
            {
                "view": "recent",
                "event_kind": "all",
                "target": {"operation_id": receipt["operation_id"]},
            },
            env.context,
        )
        assert original["source"] == "canonical_ledger"
        assert all("new owner's message" not in item["content"] for item in original["events"])
    env.bot.calls.clear()
    result = await env.service.execute(
        "query_chat_history",
        {
            "view": "recent",
            "event_kind": "all",
            "target": {"conversation_id": new_event.canonical_conversation_id},
        },
        env.context,
    )
    assert [item["event_id"] for item in result["events"]] == [new_event.id]
    assert "new owner's message" in result["events"][0]["content"]
    assert "[提及1001]" not in result["events"][0]["content"]
    assert env.bot.calls == []


async def test_forget_gate_scope_does_not_cancel_previous_owner_group_work(database):
    await seed_scoped_event(
        ScopedEventLedgerUnitOfWork(database),
        scope=ConversationScope.group("8000", "2002"),
        platform_message_id=str(uuid4()),
        sender_user_id="1001",
        direction="inbound",
        content="previous owner's group message",
    )
    await transfer_account(database, "1001")
    await append(database, "current owner's private message")
    from yuki.identity.canonical_repository import find_identity_binding

    async with database.read_session() as reader:
        current = await find_identity_binding(reader, "1001", platform="qq")
    scopes = await PeopleRepository(database).affected_conversation_scopes(current.person_id)
    assert all(scope.group_id != "2002" for scope in scopes)


async def test_forget_gate_covers_legacy_mentions_of_every_merged_person(database):
    async with database.immediate_session() as writer:
        source = await ensure_person(writer, "1001")
        target = await ensure_person(writer, "1002")
    event = await append(database, "old native mention", account="1003", group=True)
    async with database.immediate_session() as writer:
        await merge_persons(writer, source, target)

    scopes = await PeopleRepository(database).affected_conversation_scopes(target)
    assert any(scope.conversation_id == event.canonical_conversation_id for scope in scopes)
