"""C-domain prepared facts are rechecked in the writer that publishes them."""

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from tests.unit.test_client_api import bearer
from tests.unit.test_client_works import admin, app_env, guard_for, owner, seeded
from tests.unit.test_notification_preparation_sources import prepare_fixture
from tests.unit.test_participation_feedback import set_work, setup, social

from yuki.domain.messages import ChatMessage
from yuki.persistence.models import ChatEventModel
from yuki.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork
from yuki.plugin_host.db_models import PluginNotificationOutboxModel
from yuki.runtime.work_journal import WorkJournal
from yuki.runtime.work_repository import WorkConflict
from yuki.runtime.work_schema_v1 import inputs, journal
from yuki.services.participation_feedback import reconcile_page
from yuki.services.turn_transcript import TurnTranscript
from yuki.social.db_models import SocialOperationModel

__all__ = ["admin", "app_env"]


@pytest.mark.parametrize("change", [None, "content", "mailbox", "author"])
async def test_journal_rechecks_original_cross_entry_in_its_publication_writer(
    app_env, database, monkeypatch, change
):
    env = app_env
    device, person = await owner(env, database)
    original = await seeded(env, database, group=True, retain_lease=True)
    guard, control = await guard_for(original, database)
    response = await env.client.post(
        f"/client/v1/works/{original.row['id']}/inputs",
        json={"request_id": str(uuid4()), "text": "original cross entry input"},
        headers=bearer(device["token"]),
    )
    assert response.status_code == 200, response.text
    received = response.json()["data"]
    control.session.event_ids.append(received["event_id"])
    assert await guard.check(control)
    async with database.read_session() as reader:
        before = dict(
            (await reader.execute(select(journal).where(journal.c.work_id == original.row["id"])))
            .mappings()
            .one()
        )
    original_writer = database.immediate_session
    changed = False

    @asynccontextmanager
    async def intervening_writer():
        nonlocal changed
        if change and not changed:
            changed = True
            async with original_writer() as writer:
                if change == "mailbox":
                    await writer.execute(
                        update(inputs)
                        .where(inputs.c.id == received["input_id"])
                        .values(
                            payload_json=json.dumps({"origin": "client_input", "text": "changed"})
                        )
                    )
                else:
                    await writer.execute(
                        update(ChatEventModel)
                        .where(ChatEventModel.id == received["event_id"])
                        .values(
                            **(
                                {"content": "changed"}
                                if change == "content"
                                else {
                                    "author_kind": "system",
                                    "author_person_id": None,
                                    "sender_user_id": "system",
                                }
                            )
                        )
                    )
        async with original_writer() as writer:
            yield writer

    monkeypatch.setattr(database, "immediate_session", intervening_writer)
    publication = AsyncMock()
    save = WorkJournal(original.repository).save(
        original.lease,
        original.row["id"],
        "fixed-contract",
        TurnTranscript((ChatMessage("user", "original"), ChatMessage("assistant", "already paid"))),
        phase="response",
        pending=[],
        source_revision=control.session.source_revision,
        metadata={},
        source_guard=guard,
        source_actor_person_id=person,
        publication=publication,
    )
    if change:
        with pytest.raises(WorkConflict, match="work_journal_source_changed"):
            await save
        publication.assert_not_awaited()
    else:
        await save
        publication.assert_awaited_once()
    async with database.read_session() as reader:
        after = dict(
            (await reader.execute(select(journal).where(journal.c.work_id == original.row["id"])))
            .mappings()
            .one()
        )
    assert (after == before) is bool(change)
    assert await original.repository.get(original.row["id"]) == original.row
    await original.repository.release(original.lease)


async def test_notification_finish_and_ledger_rollback_together_after_platform_receipt(
    database, tmp_path, monkeypatch
):
    env, repository, worker, event_id = await prepare_fixture(database, tmp_path)
    await worker._prepare_pending()
    item = await repository.claim_outbox()
    assert item is not None
    original_append = ScopedEventLedgerUnitOfWork.append
    observed = []

    async def fail_ledger(self, **kwargs):
        row = await kwargs["session"].get(PluginNotificationOutboxModel, item.id)
        observed.append((row.status, row.origin_key))
        await original_append(self, **kwargs)
        raise RuntimeError("settlement fault after ledger append")

    monkeypatch.setattr(ScopedEventLedgerUnitOfWork, "append", fail_ledger)
    await worker._deliver(item)
    assert len(observed) == 1 and observed[0][0] == "sent"
    async with database.read_session() as reader:
        row = await reader.get(PluginNotificationOutboxModel, item.id)
        assert row.status == "uncertain" and row.origin_key == observed[0][1]
        assert row.last_error_category == "post_send_persistence_failed"
        assert not tuple(
            await reader.scalars(
                select(ChatEventModel.id).where(
                    ChatEventModel.caused_by_event_id == event_id,
                    ChatEventModel.direction == "outbound",
                )
            )
        )
    assert len([call for call in env.bot.calls if call[0] == "send_group_msg"]) == 1
    assert await repository.claim_outbox() is None


async def test_feedback_reprepares_changed_selected_receipt_without_publishing_stale_page(
    database, monkeypatch
):
    service, item, run, task = await setup(database)
    await set_work(database, task, state="completed", model_requests=2)
    receipt = await social(database, run, "selected-send")
    original_writer = database.immediate_session
    changed = False

    @asynccontextmanager
    async def intervening_writer():
        nonlocal changed
        if not changed:
            changed = True
            async with original_writer() as writer:
                await writer.execute(
                    update(SocialOperationModel)
                    .where(SocialOperationModel.id == receipt.id)
                    .values(updated_at=datetime.now(UTC) + timedelta(seconds=1))
                )
        async with original_writer() as writer:
            yield writer

    monkeypatch.setattr(database, "immediate_session", intervening_writer)
    await reconcile_page(service, (run,))
    assert (await service.repository.get_run(run.run_id)).feedback_sequence == 0
    assert not item.controller.state.effects
    await reconcile_page(service, (run,))
    assert (await service.repository.get_run(run.run_id)).feedback_sequence == 1
    assert len(item.controller.state.effects) == 3
    assert await service.work.get(task["id"]) == {**task, "state": "completed", "model_requests": 2}
    service._dispatch.assert_not_awaited()


async def test_feedback_late_send_cannot_commit_a_stale_no_reply(database, monkeypatch):
    service, item, run, task = await setup(database)
    await set_work(database, task, state="completed", model_requests=1)
    original_writer = database.immediate_session
    inserted = False

    @asynccontextmanager
    async def late_receipt():
        nonlocal inserted
        if not inserted:
            inserted = True
            await social(database, run, "late-confirmed-send")
        async with original_writer() as writer:
            yield writer

    monkeypatch.setattr(database, "immediate_session", late_receipt)
    await reconcile_page(service, (run,))
    assert (await service.repository.get_run(run.run_id)).feedback_sequence == 0
    assert not item.controller.state.effects
    await reconcile_page(service, (run,))
    settled = await service.repository.get_run(run.run_id)
    assert settled.feedback_sequence == 1 and settled.state == "completed"
    assert item.controller.state.feedback[run.run_id].outcome == "completed"
    assert len(item.controller.state.effects) == 2
    service._dispatch.assert_not_awaited()
