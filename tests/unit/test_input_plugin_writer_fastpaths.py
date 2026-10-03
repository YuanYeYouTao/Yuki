"""Real existing input/context reads do not queue behind an unrelated writer."""

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from tests.unit.test_external_event_runtime_fences import _seed_job
from tests.unit.test_observation_writer_fastpaths import held_writer
from tests.unit.test_work_input_preparation import enqueue
from tests.unit.test_work_protocol_continuity import _control

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.plugin_host.db_models import PluginBackgroundTurnJobModel
from qq_ai_bot.plugin_host.notification_repository import BackgroundTurnFenceError
from qq_ai_bot.runtime.work_repository import WorkConflict
from qq_ai_bot.runtime.work_schema_v1 import inputs


async def test_duplicate_input_keeps_original_after_stage_and_consumption(database, tmp_path):
    control = await _control(database, tmp_path)
    identity = await enqueue(control)
    assert await control.repository.prepare_input(identity, {"text": "original"})
    async with database.sessions() as reader:
        original_payload = await reader.scalar(
            select(inputs.c.payload_json).where(inputs.c.id == identity)
        )
    await control.take_inputs("original-attempt")
    await control.confirm_inputs()
    async with held_writer(database):
        assert await enqueue(control) == identity
        assert await control.repository.prepare_input(identity, {"text": "replacement"})
        with pytest.raises(WorkConflict, match="input_conflict"):
            await control.repository.enqueue(
                control.lease.conversation_id,
                control.lease.generation,
                "attachment",
                kind="control",
                work_id=control.current["id"],
            )
    async with database.sessions() as reader:
        row = (await reader.execute(select(inputs).where(inputs.c.id == identity))).mappings().one()
    assert row["state"] == "consumed" and row["payload_json"] == original_payload


async def test_explicit_resume_retains_writer_and_original_work_identity(database, tmp_path):
    control = await _control(database, tmp_path)
    arguments = dict(
        conversation_id=control.lease.conversation_id,
        generation=control.lease.generation,
        source_key="explicit-resume",
        kind="control",
        work_id=control.current["id"],
        resume=(control.lease, {"signal": True}),
    )
    identity = await control.repository.enqueue(**arguments)
    original = await control.repository.get(control.current["id"])
    statements = []
    from sqlalchemy import event

    def capture(_conn, _cursor, statement, *_args):
        statements.append(statement)

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        assert await control.repository.enqueue(**arguments) == identity
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    updated = await control.repository.get(control.current["id"])
    assert "BEGIN IMMEDIATE" in statements
    assert updated["id"] == original["id"] and updated["revision"] == original["revision"] + 1


async def test_plugin_context_success_and_obsolete_attempt_are_read_only(database):
    repository, _event_id, conversation_id = await _seed_job(database)
    job = await repository.claim_turn()
    assert job is not None
    async with held_writer(database):
        context = await repository.load_background_context(job)
        assert context.conversation_id == conversation_id
        with pytest.raises(BackgroundTurnFenceError):
            await repository.load_background_context(replace(job, attempts=job.attempts + 1))


async def test_plugin_invalid_generation_is_cancelled_only_for_current_attempt(database):
    repository, _event_id, _conversation_id = await _seed_job(database)
    job = await repository.claim_turn()
    assert job is not None
    with pytest.raises(BackgroundTurnFenceError):
        await repository.load_background_context(replace(job, generation=job.generation + 1))
    async with database.sessions() as reader:
        saved = await reader.get(PluginBackgroundTurnJobModel, job.id)
        assert saved.status == "cancelled"


async def test_reclaimed_plugin_attempt_cannot_cancel_its_new_owner(database):
    repository, _event_id, _conversation_id = await _seed_job(database)
    old = await repository.claim_turn(lease_seconds=1)
    async with database.immediate_session() as writer:
        row = await writer.get(PluginBackgroundTurnJobModel, old.id)
        row.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    current = await repository.claim_turn()
    assert current.attempts == old.attempts + 1
    async with held_writer(database):
        with pytest.raises(BackgroundTurnFenceError):
            await repository.load_background_context(old)
    async with database.sessions() as reader:
        row = await reader.get(PluginBackgroundTurnJobModel, old.id)
        assert row.status == "processing" and row.attempts == current.attempts


async def test_plugin_cancellation_rechecks_a_claimant_changed_after_read(database, monkeypatch):
    repository, _event_id, conversation_id = await _seed_job(database)
    job = await repository.claim_turn()
    async with database.immediate_session() as writer:
        await writer.execute(
            update(CanonicalConversationModel)
            .where(CanonicalConversationModel.id == conversation_id)
            .values(generation=job.generation + 1)
        )
    original = database.immediate_session
    changed = False

    @asynccontextmanager
    async def reclaimed_writer():
        nonlocal changed
        if not changed:
            changed = True
            async with original() as writer:
                row = await writer.get(PluginBackgroundTurnJobModel, job.id)
                row.attempts += 1
        async with original() as writer:
            yield writer

    monkeypatch.setattr(database, "immediate_session", reclaimed_writer)
    with pytest.raises(BackgroundTurnFenceError):
        await repository.load_background_context(job)
    async with database.sessions() as reader:
        row = await reader.get(PluginBackgroundTurnJobModel, job.id)
        assert row.status == "processing" and row.attempts == job.attempts + 1
