"""Recent Social receipts retain original caption identity without old-run replay."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from tests.unit.test_participation_feedback import setup, social

from yuki.services.participation_feedback import (
    _logical_social_effects,
    _scope_social_rows,
    _timestamp,
    sync_scope_effects,
)
from yuki.social.db_models import SocialOperationModel


@pytest.mark.parametrize(
    "parent_action,parent_status",
    [
        ("send_message", "succeeded"),
        ("send_file", "succeeded"),
        ("send_message", "failed"),
    ],
)
async def test_recent_caption_reads_original_parent_without_retiming_logical_send(
    database, parent_action, parent_status
):
    service, item, run, _ = await setup(database)
    parent = await social(database, run, "original-file", action=parent_action, seconds=-3600)
    async with database.immediate_session() as session:
        await session.execute(
            update(SocialOperationModel)
            .where(SocialOperationModel.id == parent.id)
            .values(status=parent_status)
        )
    parent.status = parent_status
    caption = await social(
        database, run, "caption", turn=f"social-caption:{parent.id}", action="send_file_caption"
    )
    await service.repository.record_feedback(run.run_id, sequence=1, outcome="completed")
    item.controller.advance(
        datetime.now(UTC).timestamp(),
        controller_epoch=item.controller.state.epoch,
        host_available=False,
    )
    rows = await _scope_social_rows(service, run.conversation_id)
    assert {row.id for row in rows} == {parent.id, caption.id}
    expected = _logical_social_effects([parent, caption])
    assert _logical_social_effects(rows) == expected
    effect = next(iter(expected.values()))[1]
    old_success = parent_action == "send_message" and parent_status == "succeeded"
    assert effect.at == _timestamp(parent.updated_at if old_success else caption.updated_at)
    await sync_scope_effects(service, item, rows=rows)
    assert (effect.effect_id in item.controller.state.effects) is not old_success
    assert (await service.repository.get_run(run.run_id)).state == "completed"
    service._dispatch.assert_not_awaited()
    async with database.read_session() as session:
        saved = await session.get(SocialOperationModel, parent.id)
        assert saved.status == parent_status and saved.action == parent_action
        assert _timestamp(saved.updated_at) == _timestamp(parent.updated_at)


async def test_recent_caption_parent_lookup_is_not_cut_off_by_recent_row_limit(database):
    service, _, run, _ = await setup(database)
    parent = await social(database, run, "original-file", action="send_file", seconds=-30)
    caption = await social(
        database, run, "caption", turn=f"social-caption:{parent.id}", action="send_file_caption"
    )
    at = datetime.now(UTC) - timedelta(seconds=10)
    async with database.immediate_session() as session:
        session.add_all(
            [
                SocialOperationModel(
                    id=str(uuid4()),
                    source_turn_id=f"{run.conversation_id}:initiative:{run.run_id}",
                    tool_call_id=f"prepared-{index}",
                    source_conversation_id=run.conversation_id,
                    action="send_message",
                    payload_hash="0" * 64,
                    target_kind="space",
                    target_id=run.space_id,
                    presence_id=run.presence_id,
                    status="prepared",
                    created_at=at,
                    updated_at=at,
                )
                for index in range(2047)
            ]
        )
    rows = await _scope_social_rows(service, run.conversation_id)
    assert len(rows) == 2049
    assert sum(row.id == parent.id for row in rows) == 1
    assert _logical_social_effects(rows) == _logical_social_effects([parent, caption])


async def test_recent_independent_send_from_terminal_run_still_updates_scope(database):
    service, item, run, task = await setup(database)
    await service.repository.record_feedback(run.run_id, sequence=1, outcome="completed")
    before = await service.work.get(task["id"])
    receipt = await social(database, run, "late-independent-send")
    await sync_scope_effects(service, item)
    effects = tuple(item.controller.state.effects.values())
    assert len(effects) == 1 and effects[0].run_ref == run.run_id
    assert effects[0].effect.at == _timestamp(receipt.updated_at)
    assert await service.work.get(task["id"]) == before
    assert (await service.repository.get_run(run.run_id)).state == "completed"
    service._dispatch.assert_not_awaited()
    async with database.read_session() as session:
        assert (
            await session.scalar(
                select(SocialOperationModel.status).where(SocialOperationModel.id == receipt.id)
            )
            == "succeeded"
        )
