"""Equivalent-task traversal grows with candidates and ignores ownership/name."""

from dataclasses import replace

from sqlalchemy import select
from tests.support.automation_unified_delivery_helpers import setup_run
from tests.support.canonical_identity import ensure_person
from tests.support.event_seed import seed_scoped_event

from yuki.domain.conversations import ConversationScope
from yuki.persistence.models import ChatEventModel
from yuki.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork


async def test_equivalence_uses_normalized_task_and_not_the_owner_or_name(database, tmp_path):
    case = await setup_run(database, tmp_path, mode="silent")
    async with database.immediate_session() as session:
        person = await ensure_person(session, "10002")
    await seed_scoped_event(
        ScopedEventLedgerUnitOfWork(database),
        scope=ConversationScope.group(
            case.actor.bot_user_id,
            case.actor.group_id,
            presence_id=case.actor.presence_id,
            conversation_id=case.actor.conversation_id,
        ),
        platform_message_id="second-real-owner",
        sender_user_id="10002",
        direction="inbound",
        content="find the same task",
    )
    async with database.read_session() as session:
        event = await session.scalar(
            select(ChatEventModel).where(ChatEventModel.platform_message_id == "second-real-owner")
        )
    actor = replace(case.actor, user_id="10002", person_id=person, event_id=event.id)
    requested = {**case.row.script, "name": "another label for the same real plan"}
    matches = await case.service.find_equivalent_directory_entries(requested, actor=actor)
    assert [entry.record.id for entry in matches] == [case.row.id]
    assert matches[0].record.canonical_creator_person_id == case.actor.person_id
    assert (
        await case.service.find_equivalent_directory_entries(requested, actor=actor, max_runs=1)
        == ()
    )
    assert not case.provider.requests
