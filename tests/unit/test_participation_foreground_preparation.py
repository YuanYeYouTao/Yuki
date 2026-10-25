"""Ordinary preparation preserves the existing owner, lifecycle and saved feedback."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import update
from tests.support.canonical_identity import ensure_person
from tests.support.event_seed import seed_event
from tests.support.semantic_participation_host_helpers import _event_and_route, _host
from tests.unit.test_participation_ordinary_feedback import ordinary_expression
from tests.unit.test_participation_pipeline_retirement import _relationship
from yuki_participation.participation import ParticipationCheckpoint
from yuki_participation.self_report import SelfDelta
from yuki_participation.snapshot import thaw

from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.domain.conversations import ScopeType
from yuki.domain.messages import ChatResponse
from yuki.persistence.models import ChatEventModel


def _runtime(host, item, source):
    host.validate_turn_snapshot = AsyncMock(return_value=True)
    return SimpleNamespace(
        turn_snapshot=SimpleNamespace(
            conversation_id=item.scene.conversation_id, generation=item.scene.generation
        ),
        effective_trigger_event_id=source.id,
        visible_event_ids=frozenset(),
    )


async def _hint(host, runtime):
    await host.observe_main_response(
        runtime,
        1,
        ChatResponse("", 0, provider_request_id="local-quiet"),
        SelfDelta(engage="quiet"),
    )


async def test_unrelated_scope_prepares_and_binds_while_another_reads_config(database, tmp_path):
    host, _ = await _host(database, tmp_path, observer=False)
    first = await _event_and_route(database, host.ledger, group="2001")
    other = await _event_and_route(database, host.ledger, group="2002")
    first_scene = await host._scene(first.canonical_conversation_id)
    entered, release = asyncio.Event(), asyncio.Event()
    snapshot = host.runtime_config.snapshot

    async def delayed(**kwargs):
        if kwargs.get("group_id") == first_scene.space_id:
            entered.set()
            await release.wait()
        return await snapshot(**kwargs)

    host.runtime_config.snapshot = delayed
    task = asyncio.create_task(host.binding_for_event(first.id))
    try:
        await entered.wait()
        result = await asyncio.wait_for(host.binding_for_event(other.id), 2)
        assert result.event_id == other.id
        assert result.conversation_id == other.canonical_conversation_id
        assert not task.done()
        release.set()
        assert (await task).event_id == first.id
        assert len(host._sessions) == 2
    finally:
        release.set()
        await task
        await host.close()


async def test_prepared_continuation_sees_quiet_applied_before_its_owner_application(
    database, tmp_path
):
    host, item, original, _, _ = await _relationship(database, tmp_path)
    source = await _event_and_route(database, host.ledger, content="not yet selected input")
    entered, release = asyncio.Event(), asyncio.Event()
    snapshot = host.runtime_config.snapshot

    async def delayed(**kwargs):
        prepared = await snapshot(**kwargs)
        entered.set()
        await release.wait()
        return prepared

    host.runtime_config.snapshot = delayed
    task = asyncio.create_task(host.continuation_for_event(source.id))
    try:
        await entered.wait()
        # A prepared read is not permission to reuse the previous participation
        # state. Quiet still applies to the live owner before this input binds.
        await host.observe_main_response(
            _runtime(host, item, original),
            2,
            ChatResponse("", 0, provider_request_id="local-later-quiet"),
            SelfDelta(engage="quiet"),
        )
        release.set()
        assert await task is None
        checkpoint = ParticipationCheckpoint.model_validate(
            item.controller.state.host_checkpoint["participation_v1"]
        )
        assert next(iter(checkpoint.units.values())).hint.delta.engage == "quiet"
        assert f"event:{source.id}" not in item.controller.state.consumed
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await host.close()


@pytest.mark.parametrize("change", ["reset", "close"])
async def test_preparation_wait_cannot_restore_a_retired_or_closed_owner(
    database, tmp_path, change
):
    host, _ = await _host(database, tmp_path, observer=False)
    source = await _event_and_route(database, host.ledger)
    entered, release = asyncio.Event(), asyncio.Event()
    snapshot = host.runtime_config.snapshot

    async def delayed(**kwargs):
        prepared = await snapshot(**kwargs)
        entered.set()
        await release.wait()
        return prepared

    host.runtime_config.snapshot = delayed
    task = asyncio.create_task(host.binding_for_event(source.id))
    try:
        await entered.wait()
        if change == "close":
            await host.close()
        else:
            async with database.immediate_session() as writer:
                await writer.execute(
                    update(CanonicalConversationModel)
                    .where(CanonicalConversationModel.id == source.canonical_conversation_id)
                    .values(generation=2, starts_after_event_id=source.id)
                )
        release.set()
        assert await task is None
        assert not host._sessions
    finally:
        release.set()
        await task
        await host.close()


async def test_concurrent_cold_hints_restore_one_owner_and_do_not_resurrect_after_close(
    database, tmp_path
):
    host, item, source, _, _ = await ordinary_expression(database, tmp_path, send=False)
    runtime = _runtime(host, item, source)
    item.observation.queue.pending.clear()
    await host._save(item)
    host._sessions.clear()
    try:
        await asyncio.gather(_hint(host, runtime), _hint(host, runtime))
        assert len(host._sessions) == 1
        restored = host._sessions[(item.scene.conversation_id, item.scene.generation)]
        assert restored.pins == 0
        checkpoint = ParticipationCheckpoint.model_validate(
            restored.controller.state.host_checkpoint["participation_v1"]
        )
        unit = next(iter(checkpoint.units.values()))
        assert unit.hint.delta.engage == "quiet" and unit.hint.response_id == "local-quiet"
        assert not restored.observation.queue.pending
        assert not host._observer.calls
        await host.close()
        await _hint(host, runtime)
        assert host._sessions == {} and host._store is None
    finally:
        await host.close()


@pytest.mark.parametrize("change", ["content", "withdrawn", "generation"])
async def test_warm_hint_rechecks_real_admission_source_before_applying(database, tmp_path, change):
    host, item, source, _, _ = await ordinary_expression(database, tmp_path, send=False)
    runtime = _runtime(host, item, source)
    try:
        async with database.immediate_session() as writer:
            if change == "generation":
                await writer.execute(
                    update(CanonicalConversationModel)
                    .where(CanonicalConversationModel.id == source.canonical_conversation_id)
                    .values(generation=2, starts_after_event_id=source.id)
                )
            else:
                await writer.execute(
                    update(ChatEventModel)
                    .where(ChatEventModel.id == source.id)
                    .values(
                        **(
                            {"content": "the original source changed"}
                            if change == "content"
                            else {
                                "suppression_status": "duplicate",
                                "utterance_fingerprint": "f" * 64,
                            }
                        )
                    )
                )
        await _hint(host, runtime)
        checkpoint = ParticipationCheckpoint.model_validate(
            item.controller.state.host_checkpoint["participation_v1"]
        )
        assert all(unit.hint is None for unit in checkpoint.units.values())
        assert item.pins == 0
    finally:
        await host.close()


async def test_hint_cancellation_keeps_actual_saved_revision_and_quiet_before_propagating(
    database, tmp_path
):
    host, item, source, _, _ = await ordinary_expression(database, tmp_path, send=False)
    runtime = _runtime(host, item, source)
    item.observation.queue.pending.clear()
    committed, acknowledge = asyncio.Event(), asyncio.Event()
    save = host._store.save

    async def delayed(payload, *, expected_revision):
        revision = await save(payload, expected_revision=expected_revision)
        committed.set()
        await acknowledge.wait()
        return revision

    host._store.save = delayed
    task = asyncio.create_task(_hint(host, runtime))
    try:
        await committed.wait()
        assert item.pins == 1
        task.cancel()
        acknowledge.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert item.pins == 0
        loaded_revision, progress, body = await host._store.load(item.scene.scope)
        assert item.revision == loaded_revision
        assert (
            thaw(item.scene.scope, progress, body).model_dump_json()
            == item.controller.state.model_dump_json()
        )
        host._sessions.clear()
        restored = await host._session(item.scene)
        checkpoint = ParticipationCheckpoint.model_validate(
            restored.controller.state.host_checkpoint["participation_v1"]
        )
        assert next(iter(checkpoint.units.values())).hint.delta.engage == "quiet"
        assert not restored.observation.queue.pending
        assert not host._observer.calls
    finally:
        acknowledge.set()
        await asyncio.gather(task, return_exceptions=True)
        await host.close()


async def test_warm_hint_recovers_committed_input_when_admission_feedback_was_not_applied(
    database, tmp_path
):
    host, item, _, _, _ = await _relationship(database, tmp_path)
    try:
        source = await _event_and_route(database, host.ledger, content="committed continuation")
        binding, reason = await host.continuation_for_event(source.id)
        assert reason == "continuation"
        prepared = host.ordinary.prepare(source, item.scene.generation, 2, binding, reason=reason)
        assert await host.ordinary.commit(prepared)
        # The real admission survived but derived feedback failed. The live
        # Controller still has only the previous input, so a hint must recover
        # this admitted input locally rather than silently lose the report.
        runtime = _runtime(host, item, source)
        before = ParticipationCheckpoint.model_validate(
            item.controller.state.host_checkpoint["participation_v1"]
        )
        assert all(
            unit.input_ref.event_id != f"event:{source.id}" for unit in before.units.values()
        )
        await _hint(host, runtime)
        checkpoint = ParticipationCheckpoint.model_validate(
            item.controller.state.host_checkpoint["participation_v1"]
        )
        unit = next(iter(checkpoint.units.values()))
        assert unit.input_ref.event_id == f"event:{source.id}"
        assert unit.hint.delta.engage == "quiet"
        assert unit.hint.run_ref == prepared.admission.activation_id
        assert unit.hint.source == unit.input_ref
        assert item.controller.state.events[unit.input_ref.event_id].source_order == source.id
        assert unit.continuation_refs == (unit.input_ref,)
        await _hint(host, runtime)
        await host.on_ordinary_admitted(binding, prepared.admission)
        assert (
            ParticipationCheckpoint.model_validate(
                item.controller.state.host_checkpoint["participation_v1"]
            )
            == checkpoint
        )
    finally:
        await host.close()


async def test_binding_hydrates_unseen_confirmed_self_quote_before_freezing_its_unit(
    database, tmp_path, monkeypatch
):
    host, item, original, own, _ = await ordinary_expression(database, tmp_path)
    try:
        assert f"event:{own.id}" not in item.controller.state.events
        # A send ACK exists, but no tick has hydrated its real public source.
        await host._save(item)
        async with database.immediate_session() as writer:
            await ensure_person(writer, "1002")
        focus, _ = await seed_event(
            host.ledger,
            bot_user_id="8000",
            platform_message_id=str(uuid4()),
            scope_type=ScopeType.GROUP,
            sender_user_id="1002",
            direction="inbound",
            content="reply to the confirmed SELF message before another tick",
            group_id=original.group_id,
            reply_to_event_id=own.id,
        )
        binding = await host.binding_for_event(focus.id)
        projected = item.controller.state.events[f"event:{focus.id}"]
        anchor = item.controller.state.events[f"event:{own.id}"]
        assert binding.unit_key == anchor.thread == f"event:{original.id}"
        assert projected.reply_to == anchor.ref
        assert projected.thread == anchor.thread
        assert not projected.unit_ambiguous and projected.unit_options == ()

        # Contrast the proposed deletion against the same saved owner and DB.
        # Receipts alone establish expression, but cannot invent the SELF ref.
        host._sessions.clear()
        without_history = await host._session(item.scene)

        async def skip_hydration(_item, *args, runtime=None, **kwargs):
            return runtime

        with monkeypatch.context() as patch:
            patch.setattr(host, "_hydrate", skip_hydration)
            reduced = await host.binding_for_event(focus.id)
        incomplete = without_history.controller.state.events[f"event:{focus.id}"]
        assert reduced.unit_key == f"event:{focus.id}" != binding.unit_key
        assert incomplete.reply_to is None and incomplete.unit_ambiguous
        # Once projected, the same source revision is immutable; a later tick
        # cannot repair this by silently rewriting the already frozen focus.
        await host._hydrate(without_history)
        assert host._event(focus, without_history) is incomplete
    finally:
        await host.close()
