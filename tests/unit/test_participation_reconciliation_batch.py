"""Bounded, fair reconciliation against real SQLite WAL and original receipts."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select, update
from tests.unit.test_participation_feedback import set_work, setup, social

from qq_ai_bot.conversation.autonomy_db_models import InitiativeFeedbackModel, InitiativeRunModel
from qq_ai_bot.services import participation_feedback as feedback
from qq_ai_bot.services.semantic_participation import SemanticParticipationService

pytestmark = pytest.mark.asyncio


async def terminal_clones(database, count):
    service, item, run, task = await setup(database)
    await set_work(database, task, state="completed")
    await feedback.reconcile_run(service, run)
    async with database.immediate_session() as session:
        original = await session.get(InitiativeRunModel, run.run_id)
        base = {
            column.name: getattr(original, column.name)
            for column in InitiativeRunModel.__table__.columns
        }
        identities = tuple(f"{number:036}" for number in range(1, count + 1))
        for identity in identities:
            session.add(
                InitiativeRunModel(
                    **{
                        **base,
                        "id": identity,
                        "proposal_id": identity,
                        "state": "no_reply",
                        "feedback_sequence": 1,
                    }
                )
            )
        await session.flush()
        for identity in identities:
            session.add(
                InitiativeFeedbackModel(
                    run_id=identity,
                    sequence=1,
                    outcome="no_reply",
                    payload_json='{"effects":[]}',
                    created_at=datetime.now(UTC),
                )
            )
    service._terminal_cursor = ""
    service._terminal_ceiling = None
    service._failures = 0
    return service, item, run, identities


async def test_128_unchanged_runs_use_eight_read_snapshots_and_zero_dml(database, monkeypatch):
    service, _, _, identities = await terminal_clones(database, 128)
    service._sessions.clear()
    sessions = 0
    original = database.sessions
    statements = []

    def counted(*args, **kwargs):
        nonlocal sessions
        sessions += 1
        return original(*args, **kwargs)

    def sql(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.strip().split()[0].upper())

    monkeypatch.setattr(database, "sessions", counted)
    event.listen(database.engine.sync_engine, "before_cursor_execute", sql)
    try:
        await feedback.reconcile_page(service, identities)
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", sql)
    assert sessions == 8
    assert statements.count("SELECT") == 40
    assert statements.count("BEGIN") == 8
    assert not set(statements) & {"INSERT", "UPDATE", "DELETE", "REPLACE"}
    service._dispatch.assert_not_awaited()
    service._save.assert_awaited_once()  # Only fixture's initial run.


async def test_tail_outside_128_old_generation_late_receipt_and_failed_row_are_fair(
    database, monkeypatch
):
    service, item, original, identities = await terminal_clones(database, 128)
    oldest = await service.repository.get_run(identities[0])
    last = await service.repository.get_run(identities[-1])
    async with database.immediate_session() as session:
        await session.execute(
            update(InitiativeRunModel)
            .where(InitiativeRunModel.id == oldest.run_id)
            .values(updated_at=datetime.now(UTC) - timedelta(days=1))
        )
    async with database.sessions() as session:
        former_tail = set(
            await session.scalars(
                select(InitiativeRunModel.id)
                .where(InitiativeRunModel.state.not_in(("accepted", "running")))
                .order_by(InitiativeRunModel.updated_at.desc())
                .limit(128)
            )
        )
    assert oldest.run_id not in former_tail
    await social(database, oldest, "old-generation-late")
    await social(database, last, "last-page-late")
    # Restore only a newer generation: old Host facts cannot wake it.
    service._sessions = {(original.conversation_id, 2): item}
    before = dict(item.controller.state.effects)
    original_record = service.repository.record_feedback
    bad = identities[1]
    await social(database, await service.repository.get_run(bad), "retry-original-id")

    async def fail_one(run_id, **kwargs):
        if run_id == bad:
            raise RuntimeError("synthetic one-row failure")
        return await original_record(run_id, **kwargs)

    monkeypatch.setattr(service.repository, "record_feedback", fail_one)
    for _ in range(9):
        await SemanticParticipationService._reconcile_outbox(service)
    assert (await service.repository.get_run(oldest.run_id)).feedback_sequence == 2
    assert (await service.repository.get_run(last.run_id)).feedback_sequence == 2
    assert (await service.repository.get_run(bad)).feedback_sequence == 1
    assert service._terminal_cursor == ""
    assert service._failures == 1
    assert item.controller.state.effects == before
    monkeypatch.setattr(service.repository, "record_feedback", original_record)
    # Restart is a disposable cursor reset, not a reset of committed feedback.
    service._terminal_cursor, service._terminal_ceiling = "", None
    await SemanticParticipationService._reconcile_outbox(service)
    assert (await service.repository.get_run(bad)).feedback_sequence == 2
    service._dispatch.assert_not_awaited()


async def test_fixed_ceiling_new_insert_behind_cursor_and_empty_terminal_page_advance(database):
    service, _, _, identities = await terminal_clones(database, 20)
    first, ceiling, cursor = await service.repository.terminal_page(limit=16)
    assert first == identities[:16]
    # Move this page to active in distinct scopes isn't legal (unique active),
    # so use a single active ID to show metadata cursor does not depend on terminal filtering.
    async with database.immediate_session() as session:
        await session.execute(
            update(InitiativeRunModel)
            .where(InitiativeRunModel.id == identities[16])
            .values(state="running")
        )
    empty, same_ceiling, next_cursor = await service.repository.terminal_page(
        after=cursor, ceiling=ceiling, limit=1
    )
    assert empty == () and next_cursor == identities[16] and same_ceiling == ceiling
    # A new terminal run inserted behind current cursor appears next sweep.
    async with database.immediate_session() as session:
        base = await session.get(InitiativeRunModel, identities[0])
        values = {
            column.name: getattr(base, column.name)
            for column in InitiativeRunModel.__table__.columns
        }
        inserted = "0" * 36
        session.add(InitiativeRunModel(**{**values, "id": inserted, "proposal_id": inserted}))
    tail, _, _ = await service.repository.terminal_page(after=next_cursor, ceiling=ceiling)
    assert inserted not in tail
    head, _, _ = await service.repository.terminal_page()
    assert inserted in head


async def test_second_feedback_page_failure_keeps_first_and_recovers_without_duplicates(
    database, monkeypatch
):
    service, item, run, task = await setup(database)
    await set_work(database, task, state="completed", model_requests=120)
    original = service.repository.record_feedback

    async def fail_second(run_id, **kwargs):
        if kwargs["sequence"] == 2:
            raise RuntimeError("second page fails")
        return await original(run_id, **kwargs)

    monkeypatch.setattr(service.repository, "record_feedback", fail_second)
    with pytest.raises(RuntimeError, match="second page"):
        await feedback.reconcile_run(service, run)
    async with database.sessions() as session:
        rows = list(await session.scalars(select(InitiativeFeedbackModel)))
    assert len(rows) == 1 and len(json.loads(rows[0].payload_json)["effects"]) == 64
    assert (await service.repository.get_run(run.run_id)).state == "no_reply"
    monkeypatch.setattr(service.repository, "record_feedback", original)
    await feedback.reconcile_run(service, run)
    assert len(item.controller.state.effects) == 120
    assert (await service.repository.get_run(run.run_id)).feedback_sequence == 2
    service._dispatch.assert_not_awaited()


async def test_hot_run_stops_after_four_pages_then_resumes_original_charges(database, monkeypatch):
    service, item, run, task = await setup(database)
    await set_work(database, task, state="completed", model_requests=300)
    original = service.repository.record_feedback
    commits = 0

    async def keep_appending(run_id, **kwargs):
        nonlocal commits
        commits += 1
        result = await original(run_id, **kwargs)
        # Update root counter inside the same transaction, modeling receipts
        # which have arrived by the next page, without waiting on another writer.
        from qq_ai_bot.runtime.work_schema_v1 import work

        await kwargs["session"].execute(
            update(work).where(work.c.id == task["id"]).values(model_requests=300 + commits)
        )
        return result

    monkeypatch.setattr(service.repository, "record_feedback", keep_appending)
    await feedback.reconcile_run(service, run)
    assert commits == 4 and len(item.controller.state.effects) == 256
    monkeypatch.setattr(service.repository, "record_feedback", original)
    await feedback.reconcile_run(service, run)
    assert len(item.controller.state.effects) == 304
    assert (await service.repository.get_run(run.run_id)).feedback_sequence == 5
    service._dispatch.assert_not_awaited()


async def test_real_wal_517_reprepares_receipts_and_sequence_without_reexecution(
    database, monkeypatch
):
    service, item, run, task = await setup(database)
    await set_work(database, task, state="completed", model_requests=1)
    real_record = service.repository.record_feedback
    collided = False
    errors = []

    def capture_error(context):
        errors.append(getattr(context.original_exception, "sqlite_errorcode", None))

    async def competing_commit(run_id, **kwargs):
        nonlocal collided
        if not collided:
            collided = True
            # The first connection has already BEGIN+read all factual dependencies.
            # A genuine second connection commits a competing sequence and receipt.
            await social(database, run, "arrived-during-snapshot")
            await real_record(run_id, sequence=1, outcome="no_reply")
        return await real_record(run_id, **kwargs)

    monkeypatch.setattr(service.repository, "record_feedback", competing_commit)
    event.listen(database.engine.sync_engine, "handle_error", capture_error)
    try:
        await feedback.reconcile_run(service, run)
    finally:
        event.remove(database.engine.sync_engine, "handle_error", capture_error)
    assert errors == [517]
    current = await service.repository.get_run(run.run_id)
    assert current.state == "no_reply" and current.feedback_sequence == 2
    assert len(item.controller.state.effects) == 2
    service._dispatch.assert_not_awaited()
    async with database.sessions() as session:
        rows = list(
            await session.scalars(
                select(InitiativeFeedbackModel).order_by(InitiativeFeedbackModel.sequence)
            )
        )
    refs = [ref for row in rows for ref in json.loads(row.payload_json)["effects"]]
    assert len(refs) == len(set(refs)) == 2


async def test_corrupt_one_run_payload_does_not_block_other_runs_in_page(database):
    service, _, _, identities = await terminal_clones(database, 2)
    good = await service.repository.get_run(identities[1])
    await social(database, good, "valid-late-send")
    async with database.immediate_session() as session:
        await session.execute(
            update(InitiativeRunModel)
            .where(InitiativeRunModel.id == identities[0])
            .values(sources_json="{broken")
        )
    with pytest.raises(ValueError):
        await feedback.reconcile_page(service, identities)
    assert (await service.repository.get_run(good.run_id)).feedback_sequence == 2
    page, _, cursor = await service.repository.terminal_page()
    assert identities[0] in page and cursor  # Scan cursor never decodes faulty JSON.


async def test_front_receipt_can_commit_between_read_pages_and_is_seen_in_later_page(
    database, monkeypatch
):
    service, _, _, identities = await terminal_clones(database, 32)
    target = await service.repository.get_run(identities[-1])
    original_read = feedback._read_facts
    committed = False

    async def read_then_front_write(service, ids, session, **kwargs):
        nonlocal committed
        result = await original_read(service, ids, session, **kwargs)
        if not committed:
            committed = True
            # The reader snapshot is still open. This second connection's
            # receipt writer really completes before the next metadata page.
            await social(database, target, "foreground-receipt-between-pages")
        return result

    monkeypatch.setattr(feedback, "_read_facts", read_then_front_write)
    await feedback.reconcile_page(service, identities)
    assert committed and (await service.repository.get_run(target.run_id)).feedback_sequence == 2
    service._dispatch.assert_not_awaited()


async def test_cold_snapshot_without_proposal_restores_committed_host_effects_after_save_failure(
    database, monkeypatch
):
    import time
    from unittest.mock import AsyncMock

    from yuki_participation.controller import Controller
    from yuki_participation.models import Scope

    service, item, run, task = await setup(database)
    await set_work(database, task, state="completed", model_requests=120)
    await social(database, run, "confirmed-before-snapshot-save")
    cold = Controller(
        Scope(conversation_id=run.conversation_id, generation=run.generation), time.time()
    )
    monkeypatch.setattr(
        service, "_save", AsyncMock(side_effect=RuntimeError("snapshot commit fails"))
    )
    with pytest.raises(RuntimeError, match="snapshot commit"):
        await feedback.reconcile_run(service, run)
    persisted = await service.repository.get_run(run.run_id)
    assert persisted.feedback_sequence == 2 and persisted.state == "completed"
    # Crash loses the controller mutation and its proposal, but not Host receipts.
    item.controller = cold
    monkeypatch.setattr(service, "_save", AsyncMock())
    await feedback.reconcile_run(service, run)
    expected = dict(item.controller.state.effects)
    assert len(expected) == 121 and not item.controller.state.proposals
    assert not item.controller.state.feedback  # No synthetic admission/proposal.
    await feedback.reconcile_run(service, run)
    assert item.controller.state.effects == expected
    assert (await service.repository.get_run(run.run_id)).feedback_sequence == 2
    service._dispatch.assert_not_awaited()


async def test_feedback_commit_confirmation_loss_is_recovered_from_original_rows(
    database, monkeypatch
):
    from sqlalchemy.ext.asyncio import AsyncSession

    service, item, run, task = await setup(database)
    await set_work(database, task, state="completed", model_requests=120)
    original_commit = AsyncSession.commit
    lost = False

    async def commit_then_lose_confirmation(session):
        nonlocal lost
        feedback_page = any(isinstance(row, InitiativeFeedbackModel) for row in session.new)
        await original_commit(session)
        if feedback_page and not lost:
            lost = True
            raise RuntimeError("feedback commit confirmation lost")

    monkeypatch.setattr(AsyncSession, "commit", commit_then_lose_confirmation)
    with pytest.raises(RuntimeError, match="confirmation lost"):
        await feedback.reconcile_run(service, run)
    assert lost and (await service.repository.get_run(run.run_id)).feedback_sequence == 1
    # No blind retry after uncertain acknowledgement. The next pass reads the
    # real committed page before appending the remaining original refs.
    await feedback.reconcile_run(service, run)
    assert (await service.repository.get_run(run.run_id)).feedback_sequence == 2
    assert len(item.controller.state.effects) == 120
    service._dispatch.assert_not_awaited()


async def test_present_proposal_conflicting_run_rejection_is_not_bypassed(database):
    service, item, run, task = await setup(database)
    await set_work(database, task, state="completed", model_requests=1)
    await social(database, run, "host-send-with-controller-binding-conflict")
    item.controller.state.proposal_runs[run.proposal_id] = "different-original-run"
    await feedback.reconcile_run(service, run)
    assert (await service.repository.get_run(run.run_id)).feedback_sequence == 1
    assert not item.controller.state.effects and not item.controller.state.feedback
    assert item.controller.state.proposal_runs[run.proposal_id] == "different-original-run"
    service._dispatch.assert_not_awaited()
