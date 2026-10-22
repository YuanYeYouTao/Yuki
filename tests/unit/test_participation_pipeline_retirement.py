"""Real Host admission/queue/feedback boundaries with local synthetic observations."""

import asyncio
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event as sqlalchemy_events
from sqlalchemy import insert, update
from tests.conftest import build_harness, make_settings
from tests.support.semantic_participation_host_helpers import (
    _choice,
    _event_and_route,
    _host,
    _item,
    _observation,
)
from yuki_participation.models import Snapshot
from yuki_participation.participation import ParticipationCheckpoint
from yuki_participation.self_report import SelfDelta, SelfReport

from yuki.llm.fake import FakeLLMProvider
from yuki.memory.models import MemoryFactCreate
from yuki.memory.repository import MemoryFactRepository
from yuki.memory.self_origin import read_self_initiative_facts
from yuki.persistence.models import ChatEventModel, MemoryEvidenceModel, MemoryFactModel
from yuki.services.participation_feedback import admission_unit_binding


async def _relationship(database, tmp_path, *, engage="stay"):
    host, _ = await _host(database, tmp_path)
    a = await _event_and_route(database, host.app.ledger, content="synthetic original exchange")
    item = await _item(host, a)
    binding = await host.binding_for_event(a.id)
    prepared = host.ordinary.prepare(a, item.scene.generation, 1, binding, reason="mention")
    assert await host.ordinary.commit(prepared)
    await host.on_ordinary_admitted(binding, prepared.admission)
    unit = admission_unit_binding(prepared.admission)
    source = item.controller.state.events[f"event:{a.id}"]
    assert item.controller.observe_unit_hint(
        unit,
        SelfReport(
            run_ref=prepared.admission.activation_id,
            sequence=1,
            response_id="synthetic-original-response",
            at=source.at,
            delta=SelfDelta(engage=engage),
        ),
    )
    await host._save(item)
    return host, item, a, prepared, unit


async def test_committed_fast_input_recovers_once_and_still_gets_background_observation(
    database, tmp_path
):
    host, item, a, _original, _unit = await _relationship(database, tmp_path)
    try:
        b = await _event_and_route(database, host.app.ledger, content="synthetic next exchange")
        binding, reason = await host.continuation_for_event(b.id)
        assert reason == "continuation"
        prepared = host.ordinary.prepare(b, item.scene.generation, 2, binding, reason=reason)
        assert await host.ordinary.commit(prepared)
        # Crash after the real admission committed, before on_ordinary_admitted
        # or a new controller checkpoint. The old A checkpoint is still on disk.
        host._sessions.clear()
        restored = await host._session(item.scene)
        await host._hydrate(restored)
        ref = restored.controller.state.events[f"event:{b.id}"].ref
        assert restored.controller.state.consumed[ref.event_id] == ref.revision
        assert ref.event_id in restored.observation.queue.pending
        checkpoint = ParticipationCheckpoint.model_validate(
            restored.controller.state.host_checkpoint["participation_v1"]
        )
        assert next(iter(checkpoint.units.values())).input_ref == ref
        for _ in range(3):
            await host._hydrate(restored)
        assert (
            ParticipationCheckpoint.model_validate(
                restored.controller.state.host_checkpoint["participation_v1"]
            )
            == checkpoint
        )
        assert (await host.ordinary.get(a.id)).activation_id != prepared.admission.activation_id
        assert not host._observer.calls
    finally:
        await host.close()


@pytest.mark.parametrize("state", ["stay", "quiet", "closed"])
async def test_observer_uses_one_schedule_without_purchasing_state_change_observations(
    database, tmp_path, monkeypatch, state
):
    host, item, a, _original, _unit = await _relationship(
        database, tmp_path, engage="quiet" if state == "quiet" else "stay"
    )
    try:
        if state == "closed":
            source = item.controller.state.events[f"event:{a.id}"]
            snapshot = Snapshot(
                scope=item.scene.scope,
                focus=source,
                context=(),
                sequence=1,
                issued_at=time.time(),
            )
            assert item.controller.apply_semantic_observation(
                _observation(snapshot, act="ask_yuki_stop")
            )
        evaluate = AsyncMock(return_value=None)
        monkeypatch.setattr(item.observation, "evaluate_due", evaluate)
        monkeypatch.setattr(item.controller, "advance", lambda *_args, **_kwargs: None)
        await host._advance_scene(item)
        assert evaluate.await_count == 1
        assert "active" not in evaluate.await_args.kwargs
        if state == "quiet":
            # Keep the report queryable for later snapshots/corrections.
            assert any(
                unit.engage == "quiet" for unit in item.controller.participating_units(time.time())
            )
        assert not host._observer.calls
    finally:
        await host.close()


async def test_quiet_closes_unadmitted_continuation_at_writer_without_blocking_direct_input(
    database, tmp_path
):
    host, item, _a, original, unit = await _relationship(database, tmp_path)
    try:
        b = await _event_and_route(
            database, host.app.ledger, content="synthetic queued continuation"
        )
        binding, reason = await host.continuation_for_event(b.id)
        prepared = host.ordinary.prepare(b, item.scene.generation, 2, binding, reason=reason)
        assert await host.continuation_current(binding)
        assert item.controller.observe_unit_hint(
            unit,
            SelfReport(
                run_ref=original.admission.activation_id,
                sequence=2,
                response_id="synthetic-quiet-response",
                at=item.controller.state.events[f"event:{original.event.id}"].at,
                delta=SelfDelta(engage="quiet"),
            ),
        )
        provider = FakeLLMProvider()
        processor = build_harness(
            database,
            make_settings(database.url, conversation_semantic_participation_enabled=True),
            provider,
        ).processor
        processor.set_participation(host)
        continuation = await host.prepare_continuation(binding)

        async def allowed(writer):
            return await processor._ordinary_admission_allowed(
                prepared, "continuation", writer, continuation=continuation
            )

        assert not await host.ordinary.commit(prepared, admission_allowed=allowed)
        assert await host.ordinary.get(b.id) is None
        assert (
            await host.ordinary.get(original.event.id)
        ).activation_id == original.admission.activation_id

        direct = host.ordinary.prepare(b, item.scene.generation, 2, binding, reason="mention")

        async def direct_allowed(writer):
            return await processor._ordinary_admission_allowed(direct, "mention", writer)

        assert await host.ordinary.commit(direct, admission_allowed=direct_allowed)
        assert not provider.requests
    finally:
        await host.close()


@pytest.mark.parametrize("exit_reason", ["quiet", "semantic_correction"])
async def test_selected_original_activation_preserves_qualification_after_review_exit(
    database, tmp_path, exit_reason
):
    host, item, _a, original, unit = await _relationship(database, tmp_path)
    provider = FakeLLMProvider()
    processor = build_harness(
        database,
        make_settings(database.url, conversation_semantic_participation_enabled=True),
        provider,
    ).processor
    processor.set_participation(host)
    try:
        admitted_refs = []
        for index in range(6):
            content = f"synthetic selected continuation {index}"
            event = await _event_and_route(database, host.app.ledger, content=content)
            binding, reason = await host.continuation_for_event(event.id)
            incoming = host.ordinary.prepare(
                event, item.scene.generation, 2, binding, reason=reason
            )

            async def selected_allowed(writer, original_input=incoming):
                return await processor._ordinary_admission_allowed(
                    original_input, "continuation", writer, check_continuation=False
                )

            async with database.immediate_session() as writer:
                assert await host.ordinary.commit_to_activation(
                    incoming,
                    original.admission,
                    session=writer,
                    admission_allowed=selected_allowed,
                )
            await host.on_ordinary_admitted(binding, await host.ordinary.get(event.id))
            admitted_refs.append(item.controller.state.events[f"event:{event.id}"].ref)
            assert (
                await host.ordinary.get(event.id)
            ).activation_id == original.admission.activation_id

        checkpoint = ParticipationCheckpoint.model_validate(
            item.controller.state.host_checkpoint["participation_v1"]
        )
        assert next(iter(checkpoint.units.values())).continuation_refs == tuple(admitted_refs)
        d = await _event_and_route(database, host.app.ledger, content="synthetic queued D")
        binding, reason = await host.continuation_for_event(d.id)
        assert await host.continuation_current(binding)
        incoming = host.ordinary.prepare(d, item.scene.generation, 3, binding, reason=reason)

        async def selected_allowed(writer):
            return await processor._ordinary_admission_allowed(
                incoming, "continuation", writer, check_continuation=False
            )

        if exit_reason == "quiet":
            assert item.controller.observe_unit_hint(
                unit,
                SelfReport(
                    run_ref=original.admission.activation_id,
                    sequence=2,
                    response_id="synthetic-new-quiet",
                    at=item.controller.state.events[f"event:{original.event.id}"].at,
                    delta=SelfDelta(engage="quiet"),
                ),
            )
        else:
            focus = item.controller.state.events[f"event:{event.id}"]
            selected = next(
                option.key for option in focus.unit_options if option.thread == unit.unit.thread
            )
            snapshot = item.controller.freeze_observation(
                Snapshot(
                    scope=item.scene.scope,
                    focus=focus,
                    context=(),
                    sequence=1,
                    issued_at=time.time(),
                )
            )
            result = _observation(snapshot, act="other_exchange", unit=selected)
            result = result.model_copy(
                update={
                    "answers": {**result.answers, "floor_state": _choice("floor_state", "other")}
                }
            )
            assert item.controller.apply_semantic_observation(result)

        async with database.immediate_session() as writer:
            assert await host.ordinary.commit_to_activation(
                incoming,
                original.admission,
                session=writer,
                admission_allowed=selected_allowed,
            )
        selected_admission = await host.ordinary.get(d.id)
        assert selected_admission.activation_id == original.admission.activation_id
        await host.on_ordinary_admitted(binding, selected_admission)
        later = await _event_and_route(
            database, host.app.ledger, content="synthetic unselected after exit"
        )
        assert await host.continuation_for_event(later.id) is None
        assert not provider.requests
    finally:
        await host.close()


@pytest.mark.parametrize("revoked", ["focus", "context"])
async def test_waiting_review_rechecks_its_actual_sources_after_observer_returns(
    database, tmp_path, monkeypatch, revoked
):
    host, item, _a, _original, unit = await _relationship(database, tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    snapshots = []
    task = None
    try:
        context = await _event_and_route(
            database, host.app.ledger, content="synthetic context not in the unit basis"
        )
        b = await _event_and_route(database, host.app.ledger, content="synthetic reviewed B")
        binding, reason = await host.continuation_for_event(b.id)
        prepared = host.ordinary.prepare(b, item.scene.generation, 2, binding, reason=reason)
        assert await host.ordinary.commit(prepared)
        await host.on_ordinary_admitted(binding, prepared.admission)
        item.observation.queue.pending.clear()
        focus = item.controller.state.events[f"event:{b.id}"]
        assert item.observation.request_observation(focus.ref)

        async def paused(snapshot, **_kwargs):
            snapshots.append(snapshot)
            assert snapshot.focus.ref == focus.ref
            assert any(event.ref.event_id == f"event:{context.id}" for event in snapshot.context)
            entered.set()
            await release.wait()
            selected = next(
                option.key
                for option in snapshot.focus.unit_options
                if option.thread == unit.unit.thread
            )
            return _observation(snapshot, act="other_exchange", unit=selected)

        monkeypatch.setattr(host._observer, "evaluate", paused)
        monkeypatch.setattr(item.controller, "advance", lambda *_args, **_kwargs: None)
        task = asyncio.create_task(host._advance_scene(item))
        await asyncio.wait_for(entered.wait(), 10)
        changed = b if revoked == "focus" else context
        old_ref = item.controller.state.events[f"event:{changed.id}"].ref
        async with database.immediate_session() as writer:
            await writer.execute(
                update(ChatEventModel)
                .where(ChatEventModel.id == changed.id)
                .values(
                    **(
                        {"suppression_status": "duplicate", "utterance_fingerprint": "f" * 64}
                        if revoked == "focus"
                        else {"content": "synthetic context changed during HTTP"}
                    )
                )
            )
        release.set()
        await asyncio.wait_for(task, 10)
        state = item.controller.state
        assert state.invalidated[old_ref.event_id] >= old_ref.revision
        assert focus.ref.event_id not in state.observations
        assert all(
            current.review is None
            or current.review.observation_id
            != f"fixture:{focus.ref.event_id}:{snapshots[0].sequence}"
            or not all(
                item.controller._unit_ref_valid(ref, current) for ref in current.review.basis
            )
            for current in item.controller._participation_checkpoint().units.values()
        )
        assert (await host.ordinary.get(b.id)).activation_id == prepared.admission.activation_id
        assert len(snapshots) == 1
    finally:
        release.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await host.close()


async def test_idle_ticks_and_cold_restore_do_not_query_available_long_term_memories(
    database, tmp_path
):
    host, _ = await _host(database, tmp_path)
    queries = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        if "memory_facts" in statement.lower() or "memory_evidence" in statement.lower():
            queries.append(statement)

    try:
        source = await _event_and_route(
            database, host.app.ledger, content="synthetic old group evidence"
        )
        async with database.immediate_session() as writer:
            await writer.execute(
                update(ChatEventModel)
                .where(ChatEventModel.id == source.id)
                .values(occurred_at=datetime.now(UTC) - timedelta(minutes=30))
            )
            first = await MemoryFactRepository(database).create_fact(
                MemoryFactCreate(
                    scope_type="group",
                    group_id=source.group_id,
                    memory_key="synthetic:idle-memory:0",
                    category="topic",
                    content="synthetic available long term memory",
                    source_type="automatic",
                    authority="group_report",
                ),
                normalized_content="synthetic available long term memory",
                supersedes_id=None,
                session=writer,
            )
            template = {
                column.name: getattr(first, column.name) for column in first.__table__.columns
            }
            template.pop("id")
            ids = (
                first.id,
                *await writer.scalars(
                    insert(MemoryFactModel).returning(MemoryFactModel.id),
                    [
                        dict(template, memory_key=f"synthetic:idle-memory:{number}")
                        for number in range(1, 140)
                    ],
                ),
            )
            await writer.execute(
                insert(MemoryEvidenceModel),
                [
                    dict(
                        fact_id=identity,
                        event_id=source.id,
                        source_speaker_user_id=source.sender_user_id,
                        relation="group_statement",
                        authority="group_report",
                        confidence=1,
                        excerpt=source.content,
                        created_at=datetime.now(UTC),
                    )
                    for identity in ids
                ],
            )
        assert (
            len(
                await read_self_initiative_facts(
                    database,
                    canonical_conversation_id=source.canonical_conversation_id,
                    fact_ids=ids,
                )
            )
            == 140
        )

        sqlalchemy_events.listen(database.engine.sync_engine, "before_cursor_execute", capture)
        try:
            for _ in range(3):
                await host.tick()
            scene = await host._scene(source.canonical_conversation_id)
            original = host._sessions[(scene.conversation_id, scene.generation)].controller
            host._sessions.clear()
            restored = await host._session(scene)
            assert restored.controller is not original
            for _ in range(3):
                await host.tick()
            assert not queries
            assert not host._observer.calls
            assert all(
                run.trigger_kind == "intrinsic" for run in await host.repository.list_active()
            )
        finally:
            sqlalchemy_events.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    finally:
        await host.close()
