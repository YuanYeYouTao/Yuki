"""Ownership intervals survive both erasure orders and a later account owner."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from tests.support.social_identity_cases import social_env
from tests.unit.test_client_identity import code_for
from tests.unit.test_rebuild_receipt_finalization import _seed

from qq_ai_bot.client_api.errors import ClientError
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.identity.canonical_repository import ensure_person, find_identity_binding
from qq_ai_bot.identity.db_models import IdentityBindingModel
from qq_ai_bot.memory.rebuild.repository import MemoryRebuildRepository
from qq_ai_bot.persistence.event_repository import EventLedgerRepository
from qq_ai_bot.persistence.models import (
    ChatEventModel,
    MemoryRebuildItemModel,
    MemoryRebuildProposalModel,
    MemoryRebuildRunModel,
)
from qq_ai_bot.persistence.people_repository import PeopleRepository
from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.social.models import SocialError


async def append(database, message, account="1001", group=False):
    return (
        await ScopedEventLedgerUnitOfWork(database).append(
            scope=ConversationScope.group("8000", "2001")
            if group
            else ConversationScope.private("8000", account),
            platform_message_id=str(uuid4()),
            sender_user_id=account,
            direction="inbound",
            content=message,
            segments=({"type": "at", "data": {"qq": 1001}},),
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
    service, current_person, code = await code_for(database, "1002")
    async with database.sessions() as session:
        old = await find_identity_binding(session, "1001")
        old_person, old_binding = old.person_id, old.id
    old_mention = await append(database, "old period mentions 1001", account="1003", group=True)
    old_device = await pair_owner(service, old_person)
    await service.claim_account(
        code["code"], platform="qq", account="1001", display_name="B", message_id=str(uuid4())
    )
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
    async with database.sessions() as session:
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
    async with database.sessions() as session:
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
        async with database.sessions() as session:
            assert values(await session.get(ChatEventModel, survivor_event)) == snapshot


@pytest.mark.parametrize("forget_previous", [False, True])
async def test_live_history_uses_current_row_even_after_previous_owner_is_forgotten(
    database, tmp_path, forget_previous
):
    env = await social_env(database, tmp_path)
    assert await env.router.cas_takeover_person(env.person) == "taken"
    receipt = await env.service.execute(
        "send_message",
        {"target": {"kind": "person", "target_id": env.person}, "text": "old"},
        env.context,
    )
    service, target, code = await code_for(database, "1002")
    await service.claim_account(
        code["code"], platform="qq", account="10001", display_name="B", message_id=str(uuid4())
    )
    await append(database, "new owner's message", account="10001")
    async with database.sessions() as session:
        binding = await find_identity_binding(session, "10001")
        boundary = binding.history_starts_at.replace(tzinfo=UTC).timestamp()
        new_binding = binding.id
    if forget_previous:
        assert await PeopleRepository(database).delete_person_id(env.person)
    else:
        with pytest.raises(SocialError, match="binding_unavailable"):
            await env.service.execute(
                "read_conversation_history", {"operation_id": receipt["operation_id"]}, env.context
            )
    env.bot.history = {
        "messages": [
            {"message_id": 1, "time": boundary - 1, "message": "before"},
            {"message_id": 2, "time": boundary + 1, "message": "after"},
            {"message_id": 3, "message": "unknown"},
        ]
    }
    for context in (env.context, replace(env.context, conversation_id=str(uuid4()))):
        result = await env.service.execute(
            "read_conversation_history",
            {
                "kind": "person",
                "target_id": target,
                "binding_id": new_binding,
                "presence_id": env.presence,
            },
            context,
        )
        assert [item["text"] for item in result["messages"]] == ["after"]
        assert result["withheld_unattributed"] == 2


async def test_forget_gate_scope_does_not_cancel_previous_owner_group_work(database):
    await ScopedEventLedgerUnitOfWork(database).append(
        scope=ConversationScope.group("8000", "2002"),
        platform_message_id=str(uuid4()),
        sender_user_id="1001",
        direction="inbound",
        content="previous owner's group message",
    )
    service, _, code = await code_for(database, "1002")
    await service.claim_account(
        code["code"], platform="qq", account="1001", display_name="B", message_id=str(uuid4())
    )
    await append(database, "current owner's private message")
    scopes = await PeopleRepository(database).affected_conversation_scopes("1001")
    assert all(scope.group_id != "2002" for scope in scopes)
