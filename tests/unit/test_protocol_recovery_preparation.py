"""Exact private recovery chooses its original read set before building fresh history."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from tests.conftest import build_harness, make_settings
from tests.support.work_compaction import summary_json
from tests.unit.test_semantic_participation_host import _event_and_route
from tests.unit.test_work_compaction_capacity import _grow, _runtime
from tests.unit.test_work_journal_source_retry import _change, _session

from qq_ai_bot.conversation.observation_models import ContextObservationModel
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.domain.messages import (
    InboundMessage,
    ProviderContinuation,
    SenderIdentity,
    ToolCall,
    ToolFunction,
)
from qq_ai_bot.execution_trace.db_models import ExecutionTraceStateModel
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.model_runtime.models import ModelExecutionPriority
from qq_ai_bot.persistence.event_repository import EventLedgerRepository
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.runtime.context_preparation import (
    ContextPreparationMode,
    context_preparation_mode,
    prepare_context,
    protocol_recovery_preparation,
    select_protocol_recovery,
)
from qq_ai_bot.runtime.subagent_repository import SubagentRepository
from qq_ai_bot.runtime.work_activation import current_work_control
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_journal import decode_transcript
from qq_ai_bot.runtime.work_repository import WorkConflict
from qq_ai_bot.runtime.work_session import WorkSession
from qq_ai_bot.runtime.work_source_guard import WorkSourceGuard
from qq_ai_bot.services.context_assembler import ContextAssembler
from qq_ai_bot.services.main_agent_contract import MainAgentContract
from qq_ai_bot.services.turn_transcript import TurnTranscript
from qq_ai_bot.time.models import TimeContext
from qq_ai_bot.workspace.short_state import ShortState
from qq_ai_bot.workspace.store import WorkspaceStore


async def as_child(control, original):
    children = SubagentRepository(control.repository)
    identity = await children.start(
        control.lease,
        control.current["id"],
        "child-case",
        {"goal": "original child", "output_kind": "answer"},
    )
    lease = await children.acquire(identity)
    record = await control.repository.get(identity)
    child = WorkControl(
        control.repository, lease, "child-source", json.loads(record["source_json"]), AsyncMock()
    )
    child.current = record
    child.session = WorkSession(child, "fixed-contract")
    await child.session.restore(TurnTranscript(original.transcript.request().messages))
    child.session.source_guard = WorkSourceGuard.restore(original.source_guard.snapshot())
    return child, child.session


@pytest.mark.parametrize("kind", ["dispatched", "pause", "staging", "child", "delivery"])
async def test_actual_journal_selects_exact_before_assembler_reads_history(database, kind):
    control, session, selected, _ = await _session(database)
    if kind == "child":
        control, session = await as_child(control, session)
    session.transcript.accept(
        ProviderContinuation(
            provider="gemini",
            protocol="gemini",
            payload={
                "contents": [
                    {
                        "role": "model",
                        "parts": [{"thoughtSignature": "original-signature", "text": "private"}],
                    }
                ]
            },
        )
    )
    if kind == "pause":
        session.progress["provider_pause_replay"] = True
    if kind == "staging":
        session.progress["compaction_staging"] = {"cursor": 1}
    await session.save(
        "dispatched" if kind == "dispatched" else "delivery" if kind == "delivery" else "paired"
    )
    assembler = ContextAssembler.__new__(ContextAssembler)
    assembler._ledger = EventLedgerRepository(database)
    now = datetime.now(UTC)
    assembler._time = SimpleNamespace(current_default=lambda: TimeContext(now, now, "UTC"))
    assembler._rollups = SimpleNamespace(
        load_prompt_snapshot=AsyncMock(side_effect=AssertionError("fresh history must not be read"))
    )
    scope = ConversationScope.group(selected.bot_user_id, selected.group_id)
    inbound = SimpleNamespace(scope=lambda: scope)

    async def builder():
        return await assembler.assemble_plugin(
            inbound=inbound,
            content="new",
            metadata={},
            current_time=TimeContext(now, now, "UTC"),
            read_history=True,
            projection_scope="plugin",
            runtime=None,
        )

    result = await prepare_context(builder, control, recovery_contract="fixed-contract")
    assert result.recovery_protocol
    assert result.read_version == session.source_guard.version
    assert result.read_version.visible_event_ids == session.source_guard.version.visible_event_ids
    assert result.history_messages == () and result.metadata_payload == {}
    assert result.current_message.content == ""
    assembler._rollups.load_prompt_snapshot.assert_not_awaited()
    loaded = await session.journal.load(control.lease, control.current["id"], "fixed-contract")
    assert "original-signature" in loaded.record["payload_json"]
    assert protocol_recovery_preparation.get() is None
    assert context_preparation_mode.get() is ContextPreparationMode.FOREGROUND


@pytest.mark.parametrize("kind", ["paired", "contract", "legacy_guard", "source"])
async def test_normal_business_or_changed_contract_builds_fresh(database, kind):
    control, session, selected, _ = await _session(database)
    if kind == "legacy_guard":
        session.source_guard = None
    await session.save("paired" if kind == "paired" else "dispatched")
    if kind == "source":
        await _change(database, selected)
    builder = AsyncMock(return_value="fresh-context")
    assert (
        await prepare_context(
            builder,
            control,
            recovery_contract="changed" if kind == "contract" else "fixed-contract",
        )
        == "fresh-context"
    )
    builder.assert_awaited_once()
    assert protocol_recovery_preparation.get() is None


async def test_child_real_deleted_source_cannot_be_certified_by_fresh_history(database):
    control, session, selected, _ = await _session(database)
    control, session = await as_child(control, session)
    await session.save("dispatched")
    await _change(database, selected)
    builder = AsyncMock(return_value="must not be read")
    with pytest.raises(WorkConflict, match="work_source_changed"):
        await prepare_context(builder, control, recovery_contract="fixed-contract")
    builder.assert_not_awaited()
    assert protocol_recovery_preparation.get() is None
    assert context_preparation_mode.get() is ContextPreparationMode.FOREGROUND


async def test_missing_contract_does_not_claim_exact_recovery(database):
    control, session, _, _ = await _session(database)
    await session.save("dispatched")
    assert await select_protocol_recovery(control, None) is None


async def test_unobserved_edit_preserves_actual_private_pause_across_reopen(database):
    control, session, _selected, unselected = await _session(database)
    session.transcript.accept(
        ProviderContinuation(
            provider="gemini",
            protocol="gemini",
            payload={
                "contents": [
                    {
                        "role": "model",
                        "parts": [{"thoughtSignature": "same-private-signature", "text": "tail"}],
                    }
                ]
            },
        )
    )
    session.progress["provider_pause_replay"] = True
    await session.save("paired")
    original = session.transcript.request()
    chain = session.transcript.chain_id
    original_guard = session.source_guard.snapshot()
    await _change(database, unselected)
    await database.engine.dispose()
    # Without an actual persisted read set and control, legacy callers remain strict.
    legacy = await session.journal.load(control.lease, control.current["id"], session.contract)
    assert legacy.reason == "source_changed" and legacy.record is None
    selected = await select_protocol_recovery(control, "fixed-contract")
    assert selected is not None
    assert selected.snapshot.reason == "resume"
    restored = decode_transcript(json.loads(selected.snapshot.record["payload_json"])["transcript"])
    assert restored.request() == original
    assert restored.chain_id == chain
    assert selected.guard.snapshot() == original_guard
    control.session = recovered = WorkSession(control, "fixed-contract")
    actual = await recovered.restore(TurnTranscript(()))
    assert actual.request() == original
    assert actual.chain_id == chain and recovered.uses_recovery_transcript
    assert recovered.progress["provider_pause_replay"]


@pytest.mark.parametrize("change", ["selected", "deleted", "privacy", "legacy"])
async def test_original_guard_does_not_excuse_changed_private_sources(database, change):
    control, session, selected, unselected = await _session(database)
    session.progress["provider_pause_replay"] = True
    if change == "legacy":
        session.source_guard = None
    await session.save("paired")
    if change == "selected":
        await _change(database, selected)
    elif change == "deleted":
        async with database.sessions() as writer, writer.begin():
            await writer.execute(delete(ChatEventModel).where(ChatEventModel.id == selected.id))
    elif change == "privacy":
        # An unrelated edit exercises the load scalar boundary; privacy invalidates
        # the saved selected fingerprint even though its actual events are unchanged.
        await _change(database, unselected)
        async with database.sessions() as writer, writer.begin():
            state = await writer.get(ExecutionTraceStateModel, 1)
            if state is None:
                writer.add(ExecutionTraceStateModel(id=1, privacy_generation=1))
            else:
                state.privacy_generation += 1
    else:
        await _change(database, unselected)
    loaded = await session.journal.load(
        control.lease, control.current["id"], session.contract, source_control=control
    )
    assert loaded.reason == "source_changed" and loaded.record is None
    assert await select_protocol_recovery(control, "fixed-contract") is None


async def test_valid_selected_sources_do_not_override_fixed_contract_boundary(database):
    control, session, _selected, unselected = await _session(database)
    session.progress["provider_pause_replay"] = True
    await session.save("paired")
    await _change(database, unselected)
    loaded = await session.journal.load(
        control.lease, control.current["id"], "new-contract", source_control=control
    )
    assert loaded.reason == "contract_changed" and loaded.record is None
    assert await select_protocol_recovery(control, "new-contract") is None


@pytest.mark.parametrize("change", ["edited", "deleted"])
async def test_saved_steer_source_is_checked_before_session_restore(database, change):
    control, session, _selected, steer = await _session(database)
    session.event_ids.append(steer.id)
    assert await session.source_guard.check(control)
    assert steer.id in session.source_guard.additional_events
    session.progress["provider_pause_replay"] = True
    await session.save("paired")
    # Fresh activation has not restored its private transcript/event list yet.
    control.session = None
    if change == "edited":
        await _change(database, steer)
    else:
        async with database.sessions() as writer, writer.begin():
            await writer.execute(delete(ChatEventModel).where(ChatEventModel.id == steer.id))
    loaded = await session.journal.load(
        control.lease, control.current["id"], session.contract, source_control=control
    )
    assert loaded.reason == "source_changed" and loaded.record is None
    assert await select_protocol_recovery(control, "fixed-contract") is None


@pytest.mark.parametrize("changed", [False, True])
async def test_dispatch_observations_extend_only_unchanged_original_guard(database, changed):
    control, session, selected, _unselected = await _session(database)
    guard = session.source_guard
    assert selected.author_person_id
    guard.version = replace(
        guard.version,
        observation_actor_id=selected.author_person_id,
        observation_read_scope="main",
    )
    before = guard.snapshot()
    identity = str(uuid4())
    async with database.sessions() as writer, writer.begin():
        writer.add(
            ContextObservationModel(
                id=identity,
                conversation_id=control.lease.conversation_id,
                generation=control.lease.generation,
                actor_id=selected.author_person_id,
                read_scope="main",
                source_key="snapshot:" + identity,
                version=1,
                payload_json='{"context":{"profile":"dispatch snapshot"}}',
                created_at=datetime.now(UTC),
            )
        )
    if changed:
        await _change(database, selected)
    assert await guard.check(control, observation_sources=((identity, 1),)) is not changed
    if changed:
        assert guard.snapshot() == before
    else:
        assert guard.version.observation_sources == ((identity, 1),)
        assert guard.fingerprint != before["fingerprint"]
        await session.save("paired")
        control.session = None
        async with database.sessions() as writer, writer.begin():
            await writer.execute(
                delete(ContextObservationModel).where(ContextObservationModel.id == identity)
            )
        loaded = await session.journal.load(
            control.lease, control.current["id"], session.contract, source_control=control
        )
        assert loaded.reason == "source_changed" and loaded.record is None


async def test_response_paid_stage_prepares_current_main_history_before_retiring_private_tail(
    database, tmp_path, monkeypatch
):
    """The real preparation/composer must supply H, not the exact-replay placeholder."""
    control, session, selected, _ = await _session(database)
    provider = FakeLLMProvider(
        lambda request: (
            summary_json(request.messages[-1].content)
            if request.structured_output
            else "Continue the original task."
        )
    )
    harness = build_harness(
        database, make_settings(database.url, runtime_work_enabled=True), provider
    )
    chat = harness.processor._chat
    runner = chat.runtime.runner
    runner.main_contract = MainAgentContract(chat, ShortState(WorkspaceStore(tmp_path / "main")))
    config = await chat._runtime_config.snapshot()
    inbound = InboundMessage(
        message_id=selected.platform_message_id,
        source_event_id=selected.id,
        event_type="message",
        scope_type=selected.scope_type,
        sender=SenderIdentity(selected.sender_user_id),
        text=selected.content,
        bot_user_id=selected.bot_user_id,
        group_id=selected.group_id,
        person_id=selected.author_person_id,
        conversation_id=selected.canonical_conversation_id,
    )

    async def assemble():
        return await chat._context_assembler.assemble_plugin(
            inbound=inbound,
            content="Continue the current Work.",
            metadata={},
            current_time=chat._time.current_default(),
            read_history=True,
            projection_scope="main",
            runtime=config,
        )

    token = current_work_control.set(control)
    try:
        first_context = await assemble()
        first = await chat.runtime.main_turns.compose(
            inbound=inbound,
            context=first_context,
            runtime=config,
            visual_observation=None,
            visual_failure=False,
        )
        definitions = await runner.main_contract.definitions()
        session.contract = runner.work_contract(config, first.messages, definitions)
        session.transcript = TurnTranscript(first.messages)
        session.compaction_anchor = TurnTranscript(first.messages)
        session.source_guard = WorkSourceGuard(first.read_version)
        assert await session.source_guard.check(control)
        _grow(session.transcript)
        await session.save("paired")
        _, runtime = await _runtime(database, control, first.messages, provider)
        runtime = replace(runtime, runtime_config=config, fixed_tools=definitions)
        from qq_ai_bot.domain.messages import ChatMessage, ChatRequest

        main = ChatRequest(messages=session.transcript.request().messages, tools=definitions)
        save = session.save

        async def fail_candidate(phase, *args, **kwargs):
            if phase == "paired" and session.compaction_ready_summary is None:
                raise WorkConflict("candidate_publish_failed")
            await save(phase, *args, **kwargs)

        monkeypatch.setattr(session, "save", fail_candidate)
        with pytest.raises(WorkConflict):
            await runner._compact_work(runtime, ModelExecutionPriority.FOREGROUND, 128000, main)
        call = ToolCall("recorded-call", ToolFunction("read_probe", "{}"))
        session.transcript.append(
            ChatMessage("assistant", "Complete original response", tool_calls=(call,))
        )
        await save("response", (call,))
        invoke = AsyncMock(return_value='{"ok":true,"data":"Original recorded effect"}')
        await session.execute(call, invoke)
        ledger = EventLedgerRepository(database)
        ambient_text = "Ambient group history after the paid candidate."
        steer_text = "Directed input after the saved original response."
        await _event_and_route(database, ledger, content=ambient_text)
        steer = await _event_and_route(database, ledger, content=steer_text)
        pending = await control.repository.enqueue(
            control.lease.conversation_id,
            1,
            "main-response-paid-steer",
            kind="message",
            event_id=steer.id,
            work_id=control.current["id"],
            ready=False,
        )
        assert await control.repository.prepare_input(pending, {"text": steer_text})
        await control.repository.release(control.lease)
        lease = await control.repository.acquire(control.lease.conversation_id, 1)
        resumed = WorkControl(
            control.repository, lease, control.source_key, control.source, AsyncMock()
        )
        resumed.current = await control.repository.get(control.current["id"])
        current_work_control.set(resumed)
        context = await prepare_context(assemble, resumed, recovery_contract=session.contract)
        assert not context.recovery_protocol
        assert ambient_text in "\n".join(
            message.content or "" for message in context.history_messages
        )
        current = await chat.runtime.main_turns.compose(
            inbound=inbound,
            context=context,
            runtime=config,
            visual_observation=None,
            visual_failure=False,
        )
        runtime = replace(
            runtime,
            work_control=resumed,
            max_model_requests=1,
            visible_event_ids=current.visible_event_ids,
            before_model_request=chat._context_validator(
                current.read_version, commit_projection=current.commit_projection
            ),
        )
        await chat.runtime.main_turns.run(current.messages, runtime, None)
        assert len(provider.requests) == 2
        wire = provider.requests[-1]
        assert not wire.structured_output
        body = "\n".join(message.content or "" for message in wire.messages)
        assert body.count(ambient_text) == body.count(steer_text) == 1
        assert "compaction_staging" not in resumed.session.progress
        assert resumed.session.progress["task_material"]["paid_observations"]
        invoke.assert_awaited_once()
        from qq_ai_bot.runtime.work_schema_v1 import inputs

        async with database.sessions() as reader:
            assert (
                await reader.scalar(select(inputs.c.state).where(inputs.c.id == pending))
                == "consumed"
            )
        await control.repository.release(lease)
    finally:
        current_work_control.reset(token)
