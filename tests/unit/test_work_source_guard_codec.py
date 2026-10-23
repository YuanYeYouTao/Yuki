"""New source proofs omit diagnostics; original paid codec2 stays original."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from tests.unit.test_admin_api import admin as admin
from tests.unit.test_client_works import app_env as app_env
from tests.unit.test_client_works import bearer, guard_for, owner, seeded
from tests.unit.test_work_journal_source_retry import _saved, _session
from tests.unit.test_work_source_guard import _event_and_route, _guard

from yuki.domain.messages import ChatMessage, ProviderContinuation, ToolCall, ToolFunction
from yuki.execution_trace.db_models import ExecutionTraceStateModel
from yuki.identity.db_models import CanonicalPersonModel
from yuki.persistence.event_repository import EventLedgerRepository
from yuki.persistence.models import ChatEventModel
from yuki.persistence.people_repository import PeopleRepository
from yuki.runtime.context_preparation import select_protocol_recovery
from yuki.runtime.work_journal import JournalUnavailable
from yuki.runtime.work_repository import WorkConflict
from yuki.runtime.work_schema_v1 import effects
from yuki.runtime.work_session import WorkSession
from yuki.runtime.work_source_guard import WorkSourceGuard
from yuki.services.turn_transcript import TurnTranscript


async def advance_diagnostic_privacy(database):
    async with database.immediate_session() as writer:
        state = await writer.get(ExecutionTraceStateModel, 1)
        if state is None:
            writer.add(ExecutionTraceStateModel(id=1, privacy_generation=1))
        else:
            state.privacy_generation += 1


async def test_forget_accountless_unrelated_person_does_not_change_plain_work_sources(database):
    source = await _event_and_route(database, EventLedgerRepository(database))
    guard, control = await _guard(database, source)
    assert await guard.check(control)
    original = guard.snapshot()
    unrelated = str(uuid4())
    now = datetime.now(UTC)
    async with database.immediate_session() as writer:
        writer.add(
            CanonicalPersonModel(
                id=unrelated, enabled=True, is_superuser=False, created_at=now, updated_at=now
            )
        )
    assert await PeopleRepository(database).delete_person_id(unrelated)
    assert await guard.check(control)
    assert await WorkSourceGuard.restore(original).check(control)
    assert guard.snapshot() == original
    assert (await EventLedgerRepository(database).get_event(source.id)).author_person_id == (
        source.author_person_id
    )
    await control.repository.release(control.lease)


async def test_accepted_cross_input_ignores_diagnostics_but_keeps_its_real_row_proof(
    app_env, database
):
    env = app_env
    device, _person = await owner(env, database)
    original = await seeded(env, database, group=True, retain_lease=True)
    guard, control = await guard_for(original, database)
    response = await env.client.post(
        f"/client/v1/works/{original.row['id']}/inputs",
        json={"request_id": str(uuid4()), "text": "original cross-entry input"},
        headers=bearer(device["token"]),
    )
    assert response.status_code == 200, response.text
    event_id = response.json()["data"]["event_id"]
    control.session.event_ids.append(event_id)
    assert await guard.check(control)
    selected = guard.snapshot()
    await advance_diagnostic_privacy(database)
    assert await guard.check(control)
    async with database.immediate_session() as writer:
        await guard.validate_cross_sources(
            writer,
            work_id=original.row["id"],
            actor_person_id=original.source["actor_person_id"],
        )
    assert guard.snapshot() == selected
    async with database.immediate_session() as writer:
        await writer.execute(
            update(ChatEventModel)
            .where(ChatEventModel.id == event_id)
            .values(content="edited original input")
        )
    async with database.immediate_session() as writer:
        with pytest.raises(WorkConflict, match="work_journal_source_changed"):
            await guard.validate_cross_sources(
                writer,
                work_id=original.row["id"],
                actor_person_id=original.source["actor_person_id"],
            )
    assert not await guard.check(control)
    await original.repository.release(original.lease)


@pytest.mark.parametrize(
    ("phase", "counter_changed"), [("response", False), ("response", True), ("dispatched", True)]
)
async def test_codec2_paid_and_unknown_checkpoints_do_not_gain_reexecution(
    database, phase, counter_changed
):
    control, first, _source, _unselected = await _session(database)
    transcript = first.transcript
    guard = WorkSourceGuard(first.source_guard.version)
    guard.codec = 2
    assert await guard.check(control)
    first.source_guard = guard
    await control.reserve_request()
    if phase == "response":
        transcript.accept(
            ProviderContinuation(
                "openai",
                "responses",
                ({"type": "message", "role": "assistant", "content": "original paid output"},),
            )
        )
    call = ToolCall("original-call", ToolFunction("send", '{"text":"original"}'))
    await first.save(phase, (call,) if phase == "response" else ())
    before = await _saved(database, control)
    budget = await control.repository.get(control.current["id"])
    if counter_changed:
        await advance_diagnostic_privacy(database)
    if phase == "dispatched":
        with pytest.raises(JournalUnavailable, match="work_response_not_persisted"):
            await select_protocol_recovery(control, first.contract)
    elif counter_changed:
        # codec2 did not store its old counter separately. Preserve that unresolved
        # original proof instead of certifying today's sources or buying a response.
        with pytest.raises(WorkConflict, match="work_source_changed"):
            await select_protocol_recovery(control, first.contract)
    else:
        selected = await select_protocol_recovery(control, first.contract)
        assert selected is not None and selected.guard.codec == 2
        control.protocol_recovery_preparation = selected
        resumed = WorkSession(control, first.contract)
        restored = await resumed.restore(TurnTranscript((ChatMessage("user", "today"),)))
        assert restored.request() == transcript.request()
        assert resumed.call_key("original-call") == first.call_key("original-call")
    after = await _saved(database, control)
    async with database.read_session() as reader:
        assert not (await reader.execute(select(effects))).all()
    assert after == before
    current = await control.repository.get(control.current["id"])
    assert current["id"] == budget["id"]
    assert (current["model_requests"], current["tool_calls"]) == (
        budget["model_requests"],
        budget["tool_calls"],
    )
    assert current["model_requests"] == 1
    await control.repository.release(control.lease)
