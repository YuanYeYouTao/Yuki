"""Journal CAS retries prove the original selected source without replaying effects."""

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select, update
from tests.support.work_session import WorkSession
from tests.unit.test_semantic_participation_host import _event_and_route
from tests.unit.test_work_source_guard import _guard

from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationModel,
    CanonicalConversationRollupModel,
)
from qq_ai_bot.domain.messages import ChatMessage, ToolCall, ToolFunction
from qq_ai_bot.identity.canonical_repository import ensure_space
from qq_ai_bot.persistence.event_repository import EventLedgerRepository
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_journal import decode_transcript
from qq_ai_bot.runtime.work_repository import WorkConflict
from qq_ai_bot.runtime.work_schema_v1 import journal
from qq_ai_bot.services.turn_transcript import TurnTranscript


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
    async with database.sessions() as writer, writer.begin():
        await writer.execute(
            update(ChatEventModel)
            .where(ChatEventModel.id == event.id)
            .values(sender_nickname=nickname)
        )


async def _saved(database, control):
    async with database.sessions() as reader:
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
        result = await session.execute(call, invoke)
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
        assert await session.execute(call, invoke) == result
        invoke.assert_awaited_once()


@pytest.mark.parametrize("change", ["selected", "deleted", "generation", "owner"])
async def test_real_source_mutation_cannot_retry_journal(database, change):
    control, session, selected, _unselected = await _session(database)
    before = await _saved(database, control)
    session.transcript.append(ChatMessage("assistant", "response not yet saved"))
    async with database.sessions() as writer, writer.begin():
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


async def test_legacy_guard_still_checks_summary_without_derived_revision_fence(database):
    control, session, selected, _unselected = await _session(database)
    revision = session.source_revision
    now = datetime.now(UTC)
    async with database.sessions() as writer, writer.begin():
        writer.add(
            CanonicalConversationRollupModel(
                conversation_id=control.lease.conversation_id,
                generation=1,
                covered_through_event_id=selected.id,
                summary_text="new semantic source",
                summary_kind="model",
                source_fingerprint="a" * 64,
                revision=1,
                created_at=now,
                updated_at=now,
            )
        )
    async with database.sessions() as reader:
        source = await reader.get(CanonicalConversationModel, control.lease.conversation_id)
        assert source.prompt_source_revision == revision
    # Old journals without a frozen selected summary still compare their actual
    # effective Rollup. A new pure derived row is not a global CAS mutation.
    assert session.source_guard.version.selected_summary_text is None
    assert not await session.source_guard.check(control)


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


async def test_retry_is_bounded_and_guard_runs_outside_writer(database):
    control, session, _selected, unselected = await _session(database)
    before = await _saved(database, control)
    await _change(database, unselected)
    guard_check = session.source_guard.check
    checks = 0

    async def check_then_race(current):
        nonlocal checks
        checks += 1
        assert await guard_check(current)
        # A separate real writer can complete here; the failed journal writer
        # must have released its lock before source fingerprint revalidation.
        await _change(database, unselected, "another concurrent update")
        return True

    session.source_guard.check = check_then_race
    with pytest.raises(WorkConflict, match="work_journal_source_changed"):
        await session.save("response")
    assert checks == 1
    assert await _saved(database, control) == before
