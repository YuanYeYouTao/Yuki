"""Journal CAS retries prove the original selected source without replaying effects."""

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select, update
from tests.support.canonical_identity import ensure_space
from tests.support.semantic_participation_host_helpers import _event_and_route
from tests.support.work_session import WorkSession, invoke_tool
from tests.unit.test_work_source_guard import _guard

from yuki.conversation.canonical_db_models import (
    CanonicalConversationModel,
)
from yuki.domain.messages import ChatMessage, ToolCall, ToolFunction
from yuki.persistence.event_repository import EventLedgerRepository
from yuki.persistence.models import ChatEventModel
from yuki.runtime.work_control import WorkControl
from yuki.runtime.work_journal import decode_transcript
from yuki.runtime.work_repository import WorkConflict
from yuki.runtime.work_schema_v1 import journal
from yuki.services.turn_transcript import TurnTranscript


async def _session(database, initial=None):
    ledger = EventLedgerRepository(database)
    selected = await _event_and_route(database, ledger)
    unselected = await _event_and_route(database, ledger, content="not selected")
    guard, holder = await _guard(database, selected)

    async def validate():
        assert await holder.repository.valid(holder.lease)

    control = WorkControl(holder.repository, holder.lease, "journal-retry", {}, validate)
    control.current = await holder.repository.accept(
        holder.lease, source_key="journal-retry", source={}, goal="preserve paired results"
    )
    session = WorkSession(control, "fixed-contract")
    control.session = session
    await session.restore(TurnTranscript(initial or (ChatMessage("user", "original task"),)))
    session.source_guard = guard
    assert await guard.check(control)
    await session.save("paired")
    return control, session, selected, unselected


async def _change(database, event, nickname="changed unselected metadata"):
    async with database.immediate_session() as writer:
        await writer.execute(
            update(ChatEventModel)
            .where(ChatEventModel.id == event.id)
            .values(sender_nickname=nickname)
        )


async def _saved(database, control):
    async with database.read_session() as reader:
        return dict(
            (
                await reader.execute(
                    select(journal).where(journal.c.work_id == control.current["id"])
                )
            )
            .mappings()
            .one()
        )


@pytest.mark.parametrize("phase", ["response", "paired"])
async def test_unselected_event_revision_preserves_normal_journal(database, phase):
    control, session, _selected, unselected = await _session(database)
    old_revision = session.source_revision
    call = ToolCall("original-call", ToolFunction("terminal_exec", "{}"))
    session.transcript.append(ChatMessage("assistant", "", tool_calls=(call,)))
    if phase == "paired":
        invoke = AsyncMock(return_value='{"status":"running","run_id":"original-run"}')
        result = await invoke_tool(session, call, invoke)
        session.transcript.append_result(call.id, result)
    await _change(database, unselected)
    await session.save(phase, (call,) if phase == "response" else ())
    assert session.source_revision > old_revision
    saved = await _saved(database, control)
    assert saved["phase"] == phase
    assert saved["chain_id"] == session.transcript.chain_id
    restored = await session.journal.load(control.lease, control.current["id"], session.contract)
    assert restored.reason == "resume"
    payload = json.loads(restored.record["payload_json"])
    assert decode_transcript(payload["transcript"]).request() == session.transcript.request()
    if phase == "paired":
        invoke.assert_awaited_once()
        # Original effect receipt, not another invocation, serves subsequent recovery.
        assert await invoke_tool(session, call, invoke) == result
        invoke.assert_awaited_once()


@pytest.mark.parametrize("change", ["selected", "deleted", "generation", "owner"])
async def test_real_source_mutation_cannot_retry_journal(database, change):
    control, session, selected, _unselected = await _session(database)
    before = await _saved(database, control)
    session.transcript.append(ChatMessage("assistant", "response not yet saved"))
    async with database.immediate_session() as writer:
        if change == "selected":
            await writer.execute(
                update(ChatEventModel)
                .where(ChatEventModel.id == selected.id)
                .values(sender_nickname="changed selected author")
            )
        elif change == "deleted":
            await writer.execute(delete(ChatEventModel).where(ChatEventModel.id == selected.id))
        elif change == "generation":
            source = await writer.get(CanonicalConversationModel, control.lease.conversation_id)
            source.generation += 1
        elif change == "owner":
            space = await ensure_space(writer, "other-space", name="other owner")
            source = await writer.get(CanonicalConversationModel, control.lease.conversation_id)
            source.space_id = space
    with pytest.raises(WorkConflict):
        await session.save("response")
    assert await _saved(database, control) == before


async def test_retry_preserves_upstream_authorization(database):
    control, session, _selected, unselected = await _session(database)
    before = await _saved(database, control)
    await _change(database, unselected)
    control.validate = AsyncMock(side_effect=WorkConflict("work_authority_changed"))
    guard_check = AsyncMock(wraps=session.source_guard.check)
    session.source_guard.check = guard_check
    with pytest.raises(WorkConflict, match="work_authority_changed"):
        await session.save("response")
    control.validate.assert_awaited_once()
    guard_check.assert_not_awaited()
    assert await _saved(database, control) == before


@pytest.mark.parametrize("protected", ["missing_guard", "compaction"])
async def test_retry_requires_original_guard_and_excludes_compaction(database, protected):
    control, session, _selected, unselected = await _session(database)
    before = await _saved(database, control)
    await _change(database, unselected)
    control.validate = AsyncMock()
    if protected == "missing_guard":
        session.source_guard = None
    with pytest.raises(WorkConflict, match="work_journal_source_changed"):
        await session.save(
            "paired",
            compaction_versions=(session.source_revision, 0) if protected == "compaction" else None,
        )
    control.validate.assert_not_awaited()
    assert await _saved(database, control) == before


async def test_repeated_source_changes_reprepare_same_journal_outside_writer(database):
    control, session, _selected, unselected = await _session(database)
    session.transcript.append(ChatMessage("assistant", "response not yet saved"))
    await _change(database, unselected)
    guard_check = session.source_guard.check
    checks = 0

    async def check_then_race(current):
        nonlocal checks
        checks += 1
        assert await guard_check(current)
        # A separate real writer can complete here; the failed journal writer
        # must have released its lock before source fingerprint revalidation.
        if checks <= 2:
            await _change(database, unselected, f"concurrent update {checks}")
        return True

    session.source_guard.check = check_then_race
    await session.save("response")
    assert checks == 3
    saved = await _saved(database, control)
    assert saved["phase"] == "response"
    assert saved["chain_id"] == session.transcript.chain_id
    restored = await session.journal.load(control.lease, control.current["id"], session.contract)
    assert restored.reason == "resume"
    payload = json.loads(restored.record["payload_json"])
    assert decode_transcript(payload["transcript"]).request() == session.transcript.request()


@pytest.mark.parametrize("committed", [True, None])
async def test_source_change_category_cannot_replay_a_journal_with_unknown_commit(
    database, monkeypatch, committed
):
    control, session, _selected, _unselected = await _session(database)
    session.transcript.append(ChatMessage("assistant", "already published response"))
    original_writer = database.immediate_session
    calls = 0
    error = WorkConflict("work_journal_source_changed")
    error.add_note("database_write_phase:cleanup" if committed else "database_write_phase:commit")
    if committed:
        error.add_note("database_commit:confirmed")

    @asynccontextmanager
    async def lost_acknowledgement():
        nonlocal calls
        calls += 1
        async with original_writer() as writer:
            yield writer
        raise error

    monkeypatch.setattr(database, "immediate_session", lost_acknowledgement)
    control.validate = AsyncMock()
    with pytest.raises(WorkConflict) as raised:
        await session.save("response")
    assert raised.value is error and calls == 1
    control.validate.assert_not_awaited()
    assert (await _saved(database, control))["phase"] == "response"


@pytest.mark.parametrize("stage", ["settle", "release"])
@pytest.mark.parametrize("database_failure", [True, False])
async def test_cleanup_preserves_finished_work_and_only_defers_real_database_failures(
    database, monkeypatch, stage, database_failure
):
    from yuki.runtime.work_activation import bind_work_activation

    control, _work_session, _selected, _unselected = await _session(database)
    control.settled = True
    original_release = control.repository.release
    error = OSError("cleanup acknowledgement lost")
    if database_failure:
        error.add_note("database_write_phase:cleanup")
        error.add_note("database_commit:confirmed")

    async def finish(_control):
        if stage == "settle":
            raise error

    async def release():
        await original_release(control.lease)
        if stage == "release":
            raise error

    async def activate():
        async with bind_work_activation(control, finish=finish, release=release):
            pass

    if database_failure:
        await activate()
    else:
        with pytest.raises(OSError) as raised:
            await activate()
        assert raised.value is error
    assert not await control.repository.valid(control.lease)


@pytest.mark.parametrize("committed", [True, None])
@pytest.mark.parametrize("category", ["conflict", "duplicate"])
async def test_abandoned_input_repair_does_not_swallow_original_commit_uncertainty(
    database, monkeypatch, committed, category
):
    from yuki.conversation.ordinary_admission import (
        OrdinaryAdmissionConflict,
        OrdinaryAdmissionDuplicate,
    )

    control, _work_session, _selected, _unselected = await _session(database)
    repository = control.repository
    identity = await repository.enqueue(
        control.lease.conversation_id,
        control.lease.generation,
        "abandoned-original-callback",
        kind="control",
        work_id=control.current["id"],
        ready=False,
    )
    prepare = repository.prepare_input
    failure = (
        OrdinaryAdmissionConflict if category == "conflict" else OrdinaryAdmissionDuplicate
    )()
    failure.add_note("database_write_phase:cleanup" if committed else "database_write_phase:commit")
    if committed:
        failure.add_note("database_commit:confirmed")
    attempts = 0

    async def lose_acknowledgement(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        assert await prepare(*args, **kwargs)
        raise failure

    monkeypatch.setattr(repository, "prepare_input", lose_acknowledgement)
    with pytest.raises(type(failure)) as raised:
        await repository.repair_abandoned_inputs("restarted-process")
    assert raised.value is failure and attempts == 1
    from yuki.runtime.work_schema_v1 import inputs

    async with database.read_session() as reader:
        assert await reader.scalar(select(inputs.c.ready).where(inputs.c.id == identity))
