"""Equivalent-task traversal grows with candidates and ignores ownership/name."""

from dataclasses import replace

from sqlalchemy import insert, select
from tests.support.automation_unified_delivery_helpers import setup_run
from tests.support.canonical_identity import ensure_person
from tests.support.event_seed import seed_scoped_event
from tests.support.sqlite_cost import capture_reads, read_vm_steps

from yuki.domain.conversations import ConversationScope
from yuki.persistence.models import AutomationModel, ChatEventModel
from yuki.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork


async def test_no_match_directory_read_work_grows_with_candidate_count(database, tmp_path):
    case = await setup_run(database, tmp_path, mode="silent")
    async with database.sessions() as session:
        original = dict(
            (
                await session.execute(
                    select(AutomationModel.__table__).where(AutomationModel.id == case.row.id)
                )
            )
            .mappings()
            .one()
        )
    requested = {**case.row.script, "goal": "no equivalent task in this isolated directory"}
    costs = []
    previous = 1
    sizes = (50, 1000)
    for count in sizes:
        async with database.immediate_session() as session:
            await session.execute(
                insert(AutomationModel),
                [
                    {
                        **{key: value for key, value in original.items() if key != "id"},
                        "creation_source_key": f"directory-cost:{index}",
                    }
                    for index in range(previous, count)
                ],
            )
        with capture_reads(database) as statements:
            assert (
                await case.service.find_equivalent_directory_entries(requested, actor=case.actor)
                == ()
            )
        # The captured public operation only read; replay sees the same rows.
        costs.append(read_vm_steps(database, statements))
        previous = count
    assert costs[0] > 0
    assert costs[1] <= costs[0] * (sizes[1] / sizes[0]) * 2, (sizes, costs)
    assert not case.provider.requests


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
    async with database.sessions() as session:
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
