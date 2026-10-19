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
from tests.support.agent_backend import StubAgentBackend
from tests.support.semantic_participation_host_helpers import _event_and_route
from tests.support.work_compaction_capacity_helpers import _grow, _runtime
from tests.support.work_session import WorkSession, invoke_tool
from tests.unit.test_work_journal_source_retry import _change, _session

from yuki.conversation.observation_models import ContextObservationModel
from yuki.domain.conversations import ConversationScope
from yuki.domain.messages import (
    ChatMessage,
    InboundMessage,
    ProviderContinuation,
    SenderIdentity,
    ToolCall,
    ToolFunction,
)
from yuki.execution_trace.db_models import ExecutionTraceStateModel
from yuki.llm.fake import FakeLLMProvider
from yuki.model_runtime.models import ModelExecutionPriority
from yuki.persistence.event_repository import EventLedgerRepository
from yuki.persistence.models import ChatEventModel
from yuki.runtime.context_preparation import (
    prepare_context,
    protocol_recovery_preparation,
    select_protocol_recovery,
)
from yuki.runtime.subagent_repository import SubagentRepository
from yuki.runtime.work_activation import current_work_control
from yuki.runtime.work_control import WorkControl
from yuki.runtime.work_journal import decode_transcript
from yuki.runtime.work_repository import WorkConflict
from yuki.runtime.work_schema_v1 import effects
from yuki.runtime.work_source_guard import WorkSourceGuard
from yuki.services.context_assembler import ContextAssembler
from yuki.services.main_agent_contract import MainAgentContract
from yuki.services.turn_transcript import TurnTranscript
from yuki.workspace.short_state import ShortState
from yuki.workspace.store import WorkspaceStore


async def as_child(control, original):
    control.repository.database.subagents_enabled = True
    children = SubagentRepository(control.repository)
    identity = await children.start(
        control.lease,
        control.current["id"],
        "child-case",
        {"goal": "original child"},
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


@pytest.mark.parametrize("kind", ["pause", "child", "paired_native", "paired_native_root"])
async def test_actual_journal_selects_exact_before_assembler_reads_history(database, kind):
    control, session, selected, _ = await _session(database)
    if kind in {"child", "paired_native"}:
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
    if kind != "child":
        if kind == "pause":
            session.progress["provider_pause_replay"] = True
        await session.save("paired")
    else:
        call = ToolCall("pending", ToolFunction("workspace_read", "{}"))
        session.transcript.append(ChatMessage("assistant", tool_calls=(call,)))
        await session.save("response", (call,))
    assembler = ContextAssembler.__new__(ContextAssembler)
    assembler._ledger = EventLedgerRepository(database)
    assembler._scopes = SimpleNamespace(
        load_prompt_snapshot=AsyncMock(side_effect=AssertionError("fresh history must not be read"))
    )
    scope = ConversationScope.group(selected.bot_user_id, selected.group_id)

    async def builder():
        return await assembler.assemble_plugin(
            identity=scope,
            content="new",
            metadata={},
            read_history=True,
            runtime=None,
        )

    result = await prepare_context(builder, control, recovery_contract="fixed-contract")
    assert result.recovery_protocol
    assert result.read_version == session.source_guard.version
    assert result.read_version.visible_event_ids == session.source_guard.version.visible_event_ids
    assert result.history_messages == () and result.metadata_payload == {}
    assert result.current_message.content == ""
    assembler._scopes.load_prompt_snapshot.assert_not_awaited()
    loaded = await session.journal.load(control.lease, control.current["id"], "fixed-contract")
    assert "original-signature" in loaded.record["payload_json"]
    assert protocol_recovery_preparation.get() is None


@pytest.mark.parametrize(
    "kind", ["paired", "child", "contract", "legacy_guard", "source", "legacy_staging"]
)
async def test_normal_business_or_changed_contract_builds_fresh(database, kind):
    control, session, selected, _ = await _session(database)
    if kind == "child":
        control, session = await as_child(control, session)
    if kind == "legacy_guard":
        session.source_guard = None
    if kind == "legacy_staging":
        session.progress["compaction_staging"] = {"cursor": 1}
    await session.save("paired")
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
    call = ToolCall("pending", ToolFunction("workspace_read", "{}"))
    session.transcript.append(ChatMessage("assistant", tool_calls=(call,)))
    await session.save("response", (call,))
    await _change(database, selected)
    builder = AsyncMock(return_value="must not be read")
    with pytest.raises(WorkConflict, match="work_source_changed"):
        await prepare_context(builder, control, recovery_contract="fixed-contract")
    builder.assert_not_awaited()
    assert protocol_recovery_preparation.get() is None


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


@pytest.mark.parametrize("guarded", [False, True])
@pytest.mark.parametrize("receipt_state", ["accepted", "unknown"])
async def test_changed_context_uses_original_receipts_without_replaying_calls(
    database, tmp_path, monkeypatch, guarded, receipt_state
):
    control, session, selected, _ = await _session(database)
    identity = control.current["id"]
    if not guarded:
        session.source_guard = None
    session.transcript.accept(
        ProviderContinuation(
            provider="gemini", protocol="gemini", payload={"private": "retired-signature"}
        )
    )
    old_key = session.call_key("final-1")
    await control.repository.prepare_effect(control.lease, identity, old_key, "final")
    await control.repository.record_effect(
        old_key, receipt_state, {"transport_accepted": True} if receipt_state == "accepted" else {}
    )
    await control.repository.checkpoint(control.lease, identity, None, models=1, messages=2)
    control.current = await control.repository.get(identity)
    await session.save("paired")
    await _change(database, selected)
    legacy = await session.journal.load(control.lease, identity, session.contract)
    assert legacy.reason == "source_changed" and legacy.record is None
    assert await select_protocol_recovery(control, "fixed-contract") is None
    builder = AsyncMock(return_value="current legal context")
    assert await prepare_context(builder, control, recovery_contract="fixed-contract") == (
        "current legal context"
    )
    builder.assert_awaited_once()

    provider = FakeLLMProvider(lambda _request: "current answer from retained receipts")
    initial = (ChatMessage("system", "fixed contract"), ChatMessage("user", "current legal input"))
    runner, runtime = await _runtime(database, control, initial, provider)
    monkeypatch.setattr(runner, "work_contract", lambda *_args, **_kwargs: "fixed-contract")
    execute = AsyncMock(side_effect=AssertionError("legacy plan must not dispatch"))
    result = await runner.run(
        initial,
        runtime,
        StubAgentBackend(
            execute_call=execute,
            finalize=lambda content, _runtime: content,
        ),
    )
    await control.settle(pending_inputs=False)
    assert len(provider.requests) == 1
    rendered = repr(provider.requests[0])
    assert "current legal input" in rendered and old_key in rendered
    assert "retired-signature" not in rendered
    assert "never sent old draft" not in rendered
    execute.assert_not_awaited()
    assert control.current["id"] == identity
    assert control.current["model_requests"] == 2 and control.current["sent_messages"] == 2
    assert control.current["state"] == "completed"
    assert result.model_requests == 1
    async with database.sessions() as reader:
        original = (
            await reader.execute(
                select(effects.c.effect_key, effects.c.state).where(effects.c.work_id == identity)
            )
        ).all()
    assert original == [(old_key, receipt_state)]


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
async def test_private_observations_extend_only_unchanged_original_guard(database, changed):
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
        session.progress["provider_pause_replay"] = True
        await session.save("paired")
        control.session = None
        async with database.sessions() as writer, writer.begin():
            await writer.execute(
                delete(ContextObservationModel).where(ContextObservationModel.id == identity)
            )
        loaded = await session.journal.load(
            control.lease, control.current["id"], session.contract, source_control=control
        )
        # Private source deletion leaves the public revision and original journal intact.
        # Exact recovery still checks that original source before building fresh history.
        assert loaded.reason == "resume" and loaded.record is not None
        builder = AsyncMock(return_value="fresh history cannot replace the deleted original")
        with pytest.raises(WorkConflict, match="work_source_changed"):
            await prepare_context(builder, control, recovery_contract=session.contract)
        builder.assert_not_awaited()


async def test_paid_current_compact_recovery_prepares_latest_public_history(
    database, tmp_path, monkeypatch
):
    """The real preparation/composer must supply H, not the exact-replay placeholder."""
    control, session, selected, _ = await _session(database)
    provider = FakeLLMProvider(lambda request: "Continue the original task with verified findings.")
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
            identity=inbound.scope(),
            content="Continue the current Work.",
            metadata={},
            read_history=True,
            runtime=config,
        )

    token = current_work_control.set(control)
    try:
        first_context = await assemble()
        first = await chat.runtime.main_turns.compose(
            inbound=inbound,
            context=first_context,
            runtime=config,
        )
        definitions = await runner.main_contract.definitions()
        session.contract = runner.work_contract(config, first.messages, definitions)
        session.transcript = TurnTranscript(first.messages)
        session.source_guard = WorkSourceGuard(first.read_version)
        assert await session.source_guard.check(control)
        call = ToolCall("recorded-call", ToolFunction("read_probe", "{}"))
        session.transcript.append(ChatMessage("assistant", "Original probe", tool_calls=(call,)))
        invoke = AsyncMock(return_value='{"ok":true,"data":"Original recorded effect"}')
        result = await invoke_tool(session, call, invoke)
        session.transcript.append_result(call.id, result)
        _grow(session.transcript)
        await session.save("paired")
        _, runtime = await _runtime(database, control, first.messages, provider)
        runtime = replace(
            runtime, runtime_config=config, fixed_tools=definitions, auxiliary_requests=[0]
        )
        from yuki.domain.messages import ChatRequest
        from yuki.services.turn_execution import TurnExecution

        main = ChatRequest(messages=session.transcript.request().messages, tools=definitions)
        execution = TurnExecution(runner, first.messages, runtime, None)
        execution.state.transcript = session.transcript

        async def fail_candidate():
            raise WorkConflict("candidate_publish_failed")

        monkeypatch.setattr(session, "finish_context_compaction", fail_candidate)
        with pytest.raises(WorkConflict):
            await execution.compact_current(0, main, ModelExecutionPriority.FOREGROUND, 128000)
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
        assert "context_compaction" not in resumed.session.progress
        assert (
            resumed.session.progress["task_material"]["execution_checkpoint"]
            == "Continue the original task with verified findings."
        )
        invoke.assert_awaited_once()
        from yuki.runtime.work_schema_v1 import inputs

        async with database.sessions() as reader:
            assert (
                await reader.scalar(select(inputs.c.state).where(inputs.c.id == pending))
                == "consumed"
            )
        await control.repository.release(lease)
    finally:
        current_work_control.reset(token)


async def test_pending_receipt_settled_after_exact_selection_keeps_original_context_once(database):
    control, session, _selected, _ = await _session(database)
    control, session = await as_child(control, session)
    call = ToolCall("paid-pending", ToolFunction("workspace_read", "{}"))
    session.transcript.append(ChatMessage("assistant", tool_calls=(call,)))
    await session.save("response", (call,))
    chain = session.transcript.chain_id

    async def prepare_exact():
        # Another lawful observer can settle the original call after selection.
        await invoke_tool(
            session, call, AsyncMock(return_value='{"ok":true}'), side_effecting=False
        )
        return TurnTranscript(())

    initial = await prepare_context(prepare_exact, control, recovery_contract="fixed-contract")
    resumed = WorkSession(control, "fixed-contract")
    actual = await resumed.restore(initial)
    assert resumed.exact_recovery and resumed.uses_recovery_transcript
    assert actual.chain_id == chain
    assert any(message.role == "tool" for message in actual.request().messages)
    assert not await resumed.rebase_business(TurnTranscript((ChatMessage("user", "fresh"),)))
    fresh = TurnTranscript((ChatMessage("user", "current lawful public input"),))
    builder = AsyncMock(return_value=fresh)
    initial = await prepare_context(builder, control, recovery_contract="fixed-contract")
    again = WorkSession(control, "fixed-contract")
    actual = await again.restore(initial)
    assert not again.exact_recovery and not again.uses_recovery_transcript
    assert actual.chain_id == fresh.chain_id != chain
    assert not any(
        message.role == "tool" or message.tool_calls for message in actual.request().messages
    )


@pytest.mark.parametrize("state", ["accepted", "unknown", "failed"])
async def test_settled_or_unknown_original_effect_does_not_force_child_exact_recovery(
    database, state
):
    control, session, _selected, _ = await _session(database)
    control, session = await as_child(control, session)
    call = ToolCall("original", ToolFunction("workspace_read", "{}"))
    session.transcript.append(ChatMessage("assistant", tool_calls=(call,)))
    await session.save("response", (call,))
    key = session.call_key(call.id)
    await control.repository.prepare_effect(control.lease, control.current["id"], key, "tool")
    await control.repository.record_effect(key, state, {"result": "original evidence"})
    builder = AsyncMock(return_value="fresh public context")
    assert (
        await prepare_context(builder, control, recovery_contract="fixed-contract")
        == "fresh public context"
    )
    builder.assert_awaited_once()
