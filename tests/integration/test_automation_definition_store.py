"""Definition edits retain original occurrence and creation identities."""

from datetime import timedelta

import pytest
from sqlalchemy import select
from tests.support.automation_unified_delivery_helpers import setup_run
from tests.support.canonical_identity import ensure_person

from yuki.automation.models import AutomationStatus, RunStatus
from yuki.persistence.models import AutomationModel, AutomationVersionModel
from yuki.persistence.people_repository import PeopleRepository


async def test_overdue_once_goal_edit_keeps_slot_and_original_version(database, tmp_path):
    case = await setup_run(database, tmp_path, mode="silent")
    payload = {
        "goal": "原完整目标",
        "name": "旧显示名",
        "trigger": {
            "type": "once",
            "local_datetime": (case.clock.now() + timedelta(seconds=1)).isoformat(),
        },
    }
    once = await case.service.create_task(
        payload,
        actor=case.actor,
        conversation_key="original-scene",
        creation_source_key="original-once",
    )
    occurrence = await case.repository.create_run(
        once.id,
        scheduled_for=once.next_run_at,
        actual_started_at=case.clock.now(),
        version_id=once.version_id,
    )
    case.clock.advance(10)
    updated = await case.service.update_task(
        once.id,
        {"goal": "后来的完整要求", "trigger": payload["trigger"]},
        actor=case.actor,
        conversation_key="management-scene",
        max_runs=7,
    )
    assert updated.next_run_at == once.next_run_at
    assert updated.name == "旧显示名"
    original = await case.repository.get_for_run(occurrence.id)
    assert original.script == once.script
    assert original.script_hash == once.script_hash
    assert original.canonical_conversation_id == once.canonical_conversation_id
    assert updated.version_id != original.version_id
    cleared = await case.service.update_task(
        once.id,
        {"goal": "完整要求", "trigger": payload["trigger"], "name": None},
        actor=case.actor,
        conversation_key="management-scene",
        max_runs=None,
    )
    assert cleared.name == f"自动化 #{once.id}" and cleared.max_runs is None
    assert cleared.next_run_at == once.next_run_at


async def test_private_target_cleanup_uses_old_version_after_current_definition_changed(
    database, tmp_path
):
    case = await setup_run(database, tmp_path, mode="silent")
    async with database.immediate_session() as writer:
        target = await ensure_person(writer, "historical-private-recipient")
        original = await writer.get(AutomationVersionModel, case.row.version_id)
        original.canonical_target_person_id = target
        original.canonical_target_space_id = None
    updated = await case.service.update_task(
        case.row.id,
        {"goal": "新的群内要求", "trigger": case.row.script["trigger"]},
        actor=case.actor,
        conversation_key="original-group",
    )
    unrelated = await case.service.create_task(
        {"goal": "另一份无关任务", "trigger": {"type": "after", "seconds": 30}},
        actor=case.actor,
        conversation_key="original-group",
        creation_source_key="unrelated-operation",
    )
    async with database.immediate_session() as writer:
        latest = await writer.get(AutomationVersionModel, updated.version_id)
        assert latest.canonical_target_person_id is None
        await PeopleRepository._forget_person_automations(writer, (target,))
    async with database.read_session() as reader:
        assert await reader.get(AutomationModel, case.row.id) is None
        assert await reader.get(AutomationModel, unrelated.id) is not None
        assert not list(
            await reader.scalars(
                select(AutomationVersionModel.id).where(
                    AutomationVersionModel.automation_id == case.row.id
                )
            )
        )


async def test_missing_original_authority_is_not_rebuilt_from_latest_definition(database, tmp_path):
    case = await setup_run(database, tmp_path, mode="silent")
    occurrence = await case.repository.create_run(
        case.row.id,
        scheduled_for=case.row.next_run_at,
        actual_started_at=case.clock.now(),
        version_id=case.row.version_id,
    )
    await case.service.update_task(
        case.row.id,
        {"goal": "新的完整要求", "trigger": case.row.script["trigger"]},
        actor=case.actor,
        conversation_key="original-group",
    )
    async with database.immediate_session() as writer:
        original = await writer.get(AutomationVersionModel, occurrence.version_id)
        original.authority_snapshot_json = None
    assert await case.repository.get_for_run(occurrence.id) is None
    assert (await case.repository.get(case.row.id)).script["goal"] == "新的完整要求"


async def test_edit_reprepares_on_actual_version_change_and_keeps_omitted_fields(
    database, tmp_path, monkeypatch
):
    case = await setup_run(database, tmp_path, mode="silent")
    original_get = case.repository.get
    interleaved = False

    async def get_with_concurrent_edit(automation_id, *, session=None):
        nonlocal interleaved
        snapshot = await original_get(automation_id, session=session)
        if not interleaved:
            interleaved = True
            await case.service.update_task(
                case.row.id,
                {
                    "goal": "另一个获准编辑",
                    "name": "并发的新名称",
                    "trigger": {"type": "interval", "seconds": 30},
                },
                actor=case.actor,
                conversation_key="same-original-scene",
                max_runs=9,
            )
        return snapshot

    monkeypatch.setattr(case.repository, "get", get_with_concurrent_edit)
    updated = await case.service.update_task(
        case.row.id,
        {
            "goal": "本次完整新目标",
            "trigger": {"type": "after", "seconds": 60},
        },
        actor=case.actor,
        conversation_key="same-original-scene",
    )
    assert updated.script["goal"] == "本次完整新目标"
    assert updated.name == "并发的新名称"
    assert updated.max_runs == 9 and updated.creation_max_runs is None
    assert updated.next_run_at == case.clock.now() + timedelta(seconds=60)
    async with database.read_session() as reader:
        accepted = await reader.get(AutomationVersionModel, case.row.version_id)
        assert accepted.script_hash == case.row.script_hash
        assert (
            accepted.script_json
            != (await reader.get(AutomationVersionModel, updated.version_id)).script_json
        )


@pytest.mark.parametrize("running", [False, True])
async def test_lowered_occurrence_cap_finishes_future_without_cancelling_accepted_run(
    database, tmp_path, running
):
    case = await setup_run(database, tmp_path, mode="silent")
    async with database.immediate_session() as writer:
        owner = await writer.get(AutomationModel, case.row.id)
        owner.run_count = 3
    occurrence = None
    if running:
        occurrence = await case.repository.create_run(
            case.row.id,
            scheduled_for=case.row.next_run_at,
            actual_started_at=case.clock.now(),
            version_id=case.row.version_id,
        )
    updated = await case.service.update_task(
        case.row.id, case.row.script, actor=case.actor, conversation_key="same-source", max_runs=2
    )
    assert updated.run_count == 3
    if occurrence is None:
        assert updated.status is AutomationStatus.COMPLETED and updated.next_run_at is None
    else:
        assert updated.status is AutomationStatus.ACTIVE
        assert (await case.repository.run_history(case.row.id))[0].status is RunStatus.RUNNING
        assert await case.repository.finish_run(
            occurrence.id,
            status=RunStatus.SUCCEEDED,
            llm_calls=0,
            tool_calls=0,
            messages_sent=0,
            error_category=None,
            summary={"internal_only": True},
            finished_at=case.clock.now(),
        )
        settled = await case.repository.get(case.row.id)
        assert settled.run_count == 4
        assert settled.status is AutomationStatus.COMPLETED and settled.next_run_at is None
