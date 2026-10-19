"""Current management ownership never replaces the original execution actor."""

from dataclasses import replace

from tests.support.automation_unified_delivery_helpers import setup_run

from yuki.automation.models import RunStatus
from yuki.identity.canonical_repository import ensure_person
from yuki.identity.db_models import CanonicalPersonModel
from yuki.identity.person_identity import merge_persons


async def merged_case(database, tmp_path):
    case = await setup_run(database, tmp_path, mode="silent")
    original = case.env.person
    async with database.immediate_session() as writer:
        root = await ensure_person(writer, "10002")
        await merge_persons(writer, original, root)
    return case, original, root


async def test_after_merge_creation_keeps_raw_actor_and_does_not_borrow_root(database, tmp_path):
    case, original, root = await merged_case(database, tmp_path)
    payload = {
        "name": "original actor after merge",
        "goal": "read internally",
        "trigger": {"type": "after", "seconds": 1},
        "delivery": {"target": "current_group"},
    }
    newer = await case.service.create_task(
        payload,
        actor=case.actor,
        conversation_key="group:80001:20001",
        creation_source_key="original-after-merge",
    )
    assert newer.canonical_creator_person_id == original
    assert newer.authority_snapshot["canonical_creator_person_id"] == original
    async with database.immediate_session() as writer:
        (await writer.get(CanonicalPersonModel, original)).enabled = False
    root_actor = replace(case.actor, person_id=root, user_id="10002")
    root_task = await case.service.create_task(
        payload,
        actor=root_actor,
        conversation_key="group:80001:20001",
        creation_source_key="root-current-authorized-source",
    )
    case.clock.advance(2)
    await case.dispatch()
    for identity in (case.row.id, newer.id):
        assert (await case.repository.run_history(identity))[0].status is RunStatus.BLOCKED
    assert (await case.repository.run_history(root_task.id))[0].status is RunStatus.SUCCEEDED
    assert len(case.provider.requests) == 1


async def test_live_family_member_can_edit_disabled_source_without_reassigning_execution(
    database, tmp_path
):
    case, original, root = await merged_case(database, tmp_path)
    async with database.immediate_session() as writer:
        (await writer.get(CanonicalPersonModel, original)).enabled = False
    updated = await case.service.update_task(
        case.row.id,
        {**case.row.script, "goal": "updated business material"},
        actor=replace(case.actor, person_id=root, user_id="10002"),
        conversation_key="group:80001:20001",
    )
    assert updated.script["goal"] == "updated business material"
    assert updated.canonical_creator_person_id == original
    assert updated.authority_snapshot["canonical_creator_person_id"] == original
    case.clock.advance(2)
    await case.dispatch()
    assert (await case.repository.run_history(updated.id))[0].status is RunStatus.BLOCKED
    assert not case.provider.requests
