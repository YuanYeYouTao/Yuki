"""Main maintenance reuses actual input and durable paid responses."""

import json
from dataclasses import asdict, replace

import pytest
from tests.support.compact_delivery_evidence import (
    capture_compact_facts,
)
from tests.support.compact_delivery_evidence import (
    compact_evidence as capture_compact_evidence,
)
from tests.support.event_seed import seed_scoped_event
from tests.support.work_compaction_capacity_helpers import _runtime, _session

from yuki.domain.messages import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ChatTool,
    ModelResponseStatus,
    ToolCall,
    ToolFunction,
)
from yuki.llm.fake import FakeLLMProvider
from yuki.model_runtime.capacity import ModelCapacity, estimate_request_tokens
from yuki.model_runtime.models import ModelExecutionPriority
from yuki.runtime.work_journal import JournalUnavailable
from yuki.runtime.work_repository import WorkConflict
from yuki.runtime.work_session import WorkSession
from yuki.services.context_compaction import CompactReceipt, execution_compact_plan
from yuki.services.turn_execution import TurnExecution
from yuki.services.turn_transcript import TurnTranscript


@pytest.fixture
def compact_evidence(request):
    with capture_compact_evidence(request) as evidence:
        yield evidence


async def _public_context(database, control, initial):
    from yuki.conversation.frozen_fragments import FrozenFragments
    from yuki.conversation.projections import PromptProjectionRepository
    from yuki.domain.conversations import ConversationScope
    from yuki.persistence.event_repository import EventLedgerRepository
    from yuki.services.context_compaction import PublicCompactContext, render_context_checkpoint

    version, events = await EventLedgerRepository(database).read_scope_context(
        ConversationScope.group("80001", "20001"),
        limit=100,
    )
    repository = PromptProjectionRepository(database)
    checkpoint = await repository.read_checkpoint(version.conversation_id)
    coverage = checkpoint.selected_summary_coverage if checkpoint is not None else 0
    prefix = (
        (initial[0], render_context_checkpoint(checkpoint.selected_summary_text, coverage))
        if checkpoint is not None and checkpoint.selected_summary_text
        else (initial[0],)
    )
    fragments = FrozenFragments.load([]).extend_history(
        tuple(
            ((event.id,), ChatMessage("user", event.content))
            for event in events
            if event.id > coverage
        ),
        (),
    )

    async def adopt(summary, coverage, retained):
        from yuki.services.context_compaction import render_context_checkpoint

        context.messages = (
            initial[0],
            render_context_checkpoint(summary, coverage),
            *retained.messages(),
        )
        context.fragments = retained
        return (*context.messages, initial[-1])

    context = PublicCompactContext(
        (*prefix, *fragments.messages()),
        1,
        fragments,
        version,
        "test-public",
        repository,
        adopt,
    )
    return context


async def test_public_prefix_excludes_current_and_private_material(database, tmp_path):
    from yuki.services.context_compaction import public_compact_plan

    control, _, initial = await _session(database, tmp_path)
    context = await _public_context(database, control, initial)
    initial = (*context.messages, ChatMessage("user", "private runtime and trigger"))
    transcript = TurnTranscript(initial)
    transcript.append(ChatMessage("assistant", "private tool finding"))
    request = ChatRequest(messages=transcript.request().messages)
    plan = public_compact_plan(
        context,
        transcript,
        request,
        initial=initial,
        input_budget=10000,
        prepare=lambda request: request,
        focus="decisions",
    )
    assert plan.request.messages[:-1] == context.messages
    assert "decisions" in plan.request.messages[-1].content
    assert not any("private" in (message.content or "") for message in plan.request.messages)
    assert plan.retained == (ChatMessage("assistant", "private tool finding"),)


async def test_ordinary_paid_response_recovers_without_rebuy_or_source_mutation(
    database,
    tmp_path,
    monkeypatch,
):
    from sqlalchemy import func, select

    import yuki.services.context_compaction as compaction
    from yuki.conversation.canonical_db_models import CanonicalConversationModel
    from yuki.conversation.observation_models import ContextObservationModel

    control, _, initial = await _session(database, tmp_path)
    context = await _public_context(database, control, initial)
    initial = (*context.messages, ChatMessage("user", "private runtime and trigger"))
    provider = FakeLLMProvider(lambda request: "Public decision checkpoint.")
    runner, runtime = await _runtime(database, control, initial, provider)
    runtime = replace(
        runtime,
        work_control=None,
        public_compaction=context,
        execution_id="event:paid-test",
        compact_focus="",
        auxiliary_requests=[0],
    )
    execution = TurnExecution(runner, initial, runtime, None)
    request = ChatRequest(messages=initial, max_output_tokens=1024)
    publish = compaction.public_compact_publication

    async def fail_publication(*args, **kwargs):
        raise WorkConflict("publication_failed")

    monkeypatch.setattr(compaction, "public_compact_publication", fail_publication)
    with pytest.raises(WorkConflict, match="publication_failed"):
        await execution.compact_current(0, request, ModelExecutionPriority.FOREGROUND, 10000)
    monkeypatch.setattr(compaction, "public_compact_publication", publish)
    recovered = TurnExecution(runner, initial, runtime, None)
    result = await recovered.restore_compact_receipt()
    assert result.text == "已压缩当前会话的公共上下文。"
    assert len(provider.requests) == 1
    assert not any(
        "private" in (message.content or "") for message in provider.requests[0].messages
    )
    snapshot = await context.repository.read(context.version.conversation_id)
    assert snapshot.selected_summary_kind == "compact"
    assert snapshot.selected_summary_text == "Public decision checkpoint."
    async with database.read_session() as reader:
        owner = await reader.get(CanonicalConversationModel, context.version.conversation_id)
        assert owner.prompt_source_revision == context.version.prompt_source_revision
        assert await reader.scalar(select(func.count()).select_from(ContextObservationModel)) == 3
    again = TurnExecution(runner, initial, runtime, None)
    await again.restore_compact_receipt()
    assert len(provider.requests) == 1


async def test_short_work_does_not_buy_summary_at_soft_watermark(database, tmp_path):
    control, _, initial = await _session(database, tmp_path)
    initial = (*initial[:-1], ChatMessage("user", "public history " * 11000), initial[-1])
    provider = FakeLLMProvider(lambda request: "finished")
    runner, runtime = await _runtime(database, control, initial, provider)
    runtime = replace(runtime, max_model_requests=3)
    result = await runner.run(initial, runtime, None)
    assert result.text == "finished"
    assert len(provider.requests) == 1
    assert provider.requests[0].messages[:2] == initial[:2]


async def _append_public(database, text):
    from yuki.domain.conversations import ConversationScope
    from yuki.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork

    return await seed_scoped_event(
        ScopedEventLedgerUnitOfWork(database),
        scope=ConversationScope.group("80001", "20001"),
        origin_key="new-public-event",
        sender_user_id="10001",
        direction="inbound",
        content=text,
    )


async def test_ordinary_cold_compact_keeps_new_history_and_fresh_runtime_material(
    database, tmp_path
):
    control, _, initial = await _session(database, tmp_path)
    context = await _public_context(database, control, initial)
    initial = (*context.messages, initial[-1])
    provider = FakeLLMProvider(lambda request: "Public paid checkpoint.")
    runner, runtime = await _runtime(database, control, initial, provider)
    runtime = replace(
        runtime,
        work_control=None,
        public_compaction=context,
        execution_id="event:cold-public",
        compact_focus="",
        auxiliary_requests=[0],
    )
    execution = TurnExecution(runner, initial, runtime, None)
    execution.state.transcript.append(ChatMessage("user", "old activation material"))
    await execution._finalize_initial_layout()
    await execution.compact_current(
        0,
        ChatRequest(messages=execution.state.transcript.request().messages),
        ModelExecutionPriority.FOREGROUND,
        10000,
    )
    await _append_public(database, "A new public correction after the paid request.")
    fresh = await _public_context(database, control, initial)
    fresh_initial = (*fresh.messages, initial[-1])
    recovered = TurnExecution(
        runner, fresh_initial, replace(runtime, public_compaction=fresh), None
    )
    recovered.state.transcript.append(ChatMessage("user", "fresh activation material"))
    await recovered.restore_compact_receipt()
    text = "\n".join(
        message.content or "" for message in recovered.state.transcript.request().messages
    )
    assert "A new public correction" in text
    assert "Public paid checkpoint." in text
    assert "fresh activation material" in text and "old activation material" not in text
    assert len(provider.requests) == 1


async def test_privacy_erases_compact_body_without_authorising_repurchase(database, tmp_path):
    from sqlalchemy import select, text

    from yuki.conversation.observation_models import ContextObservationModel

    control, _, initial = await _session(database, tmp_path)
    context = await _public_context(database, control, initial)
    initial = (*context.messages, initial[-1])
    provider = FakeLLMProvider(lambda request: "Private paid-body erasure sentinel.")
    runner, runtime = await _runtime(database, control, initial, provider)
    runtime = replace(
        runtime,
        work_control=None,
        public_compaction=context,
        execution_id="event:privacy-compact",
        compact_focus="",
        auxiliary_requests=[0],
    )
    execution = TurnExecution(runner, initial, runtime, None)
    await execution.compact_current(
        0, ChatRequest(messages=initial), ModelExecutionPriority.FOREGROUND, 10000
    )
    async with database.immediate_session() as writer:
        await writer.execute(
            text(
                "INSERT INTO execution_trace_state(id,privacy_generation) VALUES(1,1) "
                "ON CONFLICT(id) DO UPDATE SET privacy_generation=1"
            )
        )
    async with database.read_session() as reader:
        rows = (await reader.scalars(select(ContextObservationModel))).all()
        assert len(rows) == 3
        assert all(row.payload_json == '{"phase":"invalidated"}' for row in rows)
        assert all(row.actor_id == row.read_scope == "" for row in rows)
        assert all(row.parent_sources_json == "[]" and row.summary_view_key is None for row in rows)
    recovered = TurnExecution(runner, initial, runtime, None)
    with pytest.raises(WorkConflict, match="context_compaction_source_changed"):
        await recovered.restore_compact_receipt()
    assert len(provider.requests) == 1


async def test_work_cold_public_compact_adopts_paid_prefix_before_business_rebase(
    database, tmp_path, monkeypatch
):
    import yuki.services.context_compaction as compaction

    control, session, initial = await _session(database, tmp_path)
    context = await _public_context(database, control, initial)
    initial = (*context.messages, initial[-1])
    session.transcript = TurnTranscript(initial)
    provider = FakeLLMProvider(lambda request: "Paid Work public checkpoint.")
    runner, runtime = await _runtime(database, control, initial, provider)
    runtime = replace(runtime, public_compaction=context, compact_focus="", auxiliary_requests=[0])
    execution = TurnExecution(runner, initial, runtime, None)
    execution.state.transcript = session.transcript
    publish = compaction.public_compact_publication

    async def fail_publication(*args, **kwargs):
        raise WorkConflict("publication_failed")

    monkeypatch.setattr(compaction, "public_compact_publication", fail_publication)
    with pytest.raises(WorkConflict, match="publication_failed"):
        await execution.compact_current(
            0, ChatRequest(messages=initial), ModelExecutionPriority.FOREGROUND, 10000
        )
    await _append_public(database, "Fresh history after Work compact payment.")
    fresh = await _public_context(database, control, initial)
    monkeypatch.setattr(compaction, "public_compact_publication", publish)
    restored = WorkSession(control, session.contract)
    control.session = restored
    await restored.restore(TurnTranscript((*fresh.messages, initial[-1])), public_compaction=fresh)
    text = "\n".join(message.content or "" for message in restored.transcript.request().messages)
    assert "Paid Work public checkpoint." in text
    assert "Fresh history after Work compact payment." in text
    assert len(provider.requests) == 1
    assert (await control.repository.get(control.current["id"]))["model_requests"] == 1
    assert "context_compaction" not in restored.progress


def test_compact_prefix_retains_real_tool_pairs_and_main_settings():
    initial = (ChatMessage("system", "same system"), ChatMessage("user", "original goal"))
    transcript = TurnTranscript(initial)
    call = ToolCall("read-1", ToolFunction("read_probe", "{}"))
    transcript.append(ChatMessage("assistant", "", tool_calls=(call,)))
    transcript.append_result(call.id, "real result " * 100)
    main = ChatRequest(
        messages=transcript.request().messages,
        tools=(ChatTool("read_probe", "Read", {"type": "object"}),),
        tool_choice="auto",
        provider_session_id="same-session",
        max_output_tokens=1024,
    )
    plan = execution_compact_plan(
        transcript,
        initial,
        main,
        input_budget=10000,
        prepare=lambda request: request,
    )
    assert plan.request.messages[:-1] == main.messages
    assert plan.request.tools == main.tools
    assert plan.request.provider_session_id == main.provider_session_id
    assert plan.request.max_output_tokens == main.max_output_tokens
    assert plan.request.messages[-2].tool_call_id == call.id


async def test_actual_overflow_compacts_paired_prefix_and_keeps_fitting_tail(
    database, tmp_path, monkeypatch
):
    from yuki.web.models import WebMode

    control, session, initial = await _session(database, tmp_path)
    for index in range(30):
        session.transcript.append(ChatMessage("assistant", f"finding {index}: " + "result " * 400))
    provider = FakeLLMProvider(lambda request: "Verified findings. Continue original goal.")
    runner, runtime = await _runtime(database, control, initial, provider)
    monkeypatch.setattr(runner._models, "capacity", lambda task: ModelCapacity(context_tokens=8192))
    runtime = replace(
        runtime,
        max_model_requests=20,
        auxiliary_requests=[0],
        runtime_config=replace(
            runtime.runtime_config,
            llm=replace(runtime.runtime_config.llm, max_output_tokens=512),
        ),
    )
    execution = TurnExecution(runner, initial, runtime, None)
    execution.state.transcript = session.transcript
    prepared = await execution.prepare_request(0, (), WebMode.DISABLED)
    assert provider.requests
    assert all(
        "Host 上下文压缩要求" in request.messages[-1].content for request in provider.requests
    )
    assert estimate_request_tokens(runner._capacity_request(prepared.request)) <= 7680
    assert initial[-1] in prepared.request.messages
    assert runtime.auxiliary_requests[0] == len(provider.requests)


@pytest.mark.parametrize("crash_after_response", [False, True])
async def test_paid_compact_response_reused_after_publication_failure(
    database,
    tmp_path,
    monkeypatch,
    crash_after_response,
):
    control, session, initial = await _session(database, tmp_path)
    transcript = session.transcript
    transcript.append(ChatMessage("assistant", "old tool discoveries " * 1000))
    original = transcript.request().messages
    provider = FakeLLMProvider(lambda request: "Verified finding; continue original goal.")
    runner, runtime = await _runtime(database, control, initial, provider)
    runtime = replace(runtime, max_model_requests=3, auxiliary_requests=[0])
    execution = TurnExecution(runner, initial, runtime, None)
    execution.state.transcript = transcript
    request = ChatRequest(messages=original, max_output_tokens=1024)
    budget = estimate_request_tokens(request) + 1000
    if crash_after_response:

        async def fail_publish():
            raise WorkConflict("publication_failed")

        monkeypatch.setattr(session, "finish_context_compaction", fail_publish)
        with pytest.raises(WorkConflict, match="publication_failed"):
            await execution.compact_current(0, request, ModelExecutionPriority.FOREGROUND, budget)
        session = WorkSession(control, session.contract)
        control.session = session
        await session.restore(TurnTranscript(initial))
        candidate = session.transcript.request()
    else:
        compacted = await execution.compact_current(
            0,
            request,
            ModelExecutionPriority.FOREGROUND,
            budget,
        )
        candidate = execution.state.transcript.request()
        assert compacted.messages == candidate.messages
    assert len(provider.requests) == 1
    assert provider.requests[0].messages[:-1] == original
    assert session.progress["task_material"]["execution_checkpoint"].startswith("Verified finding")
    assert not any(
        "old tool discoveries" in (message.content or "") for message in candidate.messages
    )
    assert "context_compaction" not in session.progress
    saved = await control.repository.get(control.current["id"])
    assert saved["model_requests"] == 1


async def test_unknown_compact_dispatch_is_not_repeated(database, tmp_path):
    control, session, initial = await _session(database, tmp_path)
    await session.prepare_context_compaction(initial, ())
    await session.save("dispatched")
    restored = WorkSession(control, session.contract)
    control.session = restored
    with pytest.raises(JournalUnavailable, match="work_response_not_persisted"):
        await restored.restore(TurnTranscript(initial))


@pytest.mark.parametrize(
    "group_id,user_id,allowed",
    [(None, "1001", True), ("2001", "1001", False), ("2001", "9000", True)],
)
async def test_manual_compact_uses_public_main_and_existing_authority(
    database,
    tmp_path,
    group_id,
    user_id,
    allowed,
):
    from tests.conftest import MemorySender, build_harness, make_settings
    from tests.support.commands_and_chat_helpers import inbound

    from yuki.domain.conversations import ConversationScope
    from yuki.services.main_agent_contract import MainAgentContract
    from yuki.workspace.short_state import ShortState
    from yuki.workspace.store import WorkspaceStore

    provider = FakeLLMProvider(lambda request: "Keep the public decision.")
    harness = build_harness(database, make_settings(database.url), provider)
    chat = harness.processor._chat
    chat.runtime.runner.main_contract = MainAgentContract(
        chat,
        ShortState(WorkspaceStore(tmp_path / "manual-workspace")),
    )
    scope = (
        ConversationScope.group("9999", group_id)
        if group_id
        else ConversationScope.private("9999", user_id)
    )
    await seed_scoped_event(
        harness.scoped_events,
        scope=scope,
        origin_key="prior-public",
        sender_user_id=user_id,
        direction="inbound",
        content="public decision",
    )
    sender = MemorySender()
    result = await harness.processor.handle(
        inbound(
            "/ai compact decisions", message_id="manual-compact", user_id=user_id, group_id=group_id
        ),
        sender,
    )
    assert result.reason == "command_compact"
    assert len(provider.requests) == int(allowed)
    assert len(sender.messages) == 1
    if allowed:
        assert sender.messages[0].text == "已压缩当前会话的公共上下文。"
        messages = provider.requests[0].messages
        assert any("public decision" in (message.content or "") for message in messages)
        assert not any("/ai compact" in (message.content or "") for message in messages[1:])
        assert "decisions" in messages[-1].content
    else:
        assert "权限不足" in sender.messages[0].text


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["paired", "response"])
async def test_incomplete_ordinary_compact_restores_fresh_history_and_runtime_material(
    database, tmp_path, monkeypatch, phase
):
    from yuki.services.context_compaction import CompactReceipt

    control, _, initial = await _session(database, tmp_path)
    context = await _public_context(database, control, initial)
    initial = (*context.messages, initial[-1])
    provider = FakeLLMProvider(
        lambda request: ChatResponse(
            content="An incomplete paid checkpoint.",
            latency_seconds=0,
            status=ModelResponseStatus.INCOMPLETE,
            incomplete_reason="max_output_tokens",
        )
    )
    runner, runtime = await _runtime(database, control, initial, provider)
    runtime = replace(
        runtime,
        work_control=None,
        public_compaction=context,
        execution_id="event:invalid-cold-review",
        compact_focus="",
        auxiliary_requests=[0],
    )
    execution = TurnExecution(runner, initial, runtime, None)
    execution.state.transcript.append(ChatMessage("user", "old activation material"))
    await execution._finalize_initial_layout()
    request = ChatRequest(messages=execution.state.transcript.request().messages)
    if phase == "response":
        write = CompactReceipt.write

        async def fail_pairing(self, payload, **kwargs):
            if payload["phase"] == "paired":
                raise WorkConflict("pairing_failed")
            await write(self, payload, **kwargs)

        monkeypatch.setattr(CompactReceipt, "write", fail_pairing)
        with pytest.raises(WorkConflict, match="pairing_failed"):
            await execution.compact_current(0, request, ModelExecutionPriority.FOREGROUND, 10000)
        monkeypatch.setattr(CompactReceipt, "write", write)
    else:
        await execution.compact_current(0, request, ModelExecutionPriority.FOREGROUND, 10000)
    await _append_public(database, "New public correction after incomplete paid response.")
    fresh = await _public_context(database, control, initial)
    recovered = TurnExecution(
        runner, (*fresh.messages, initial[-1]), replace(runtime, public_compaction=fresh), None
    )
    recovered.state.transcript.append(ChatMessage("user", "fresh activation material"))
    await recovered.restore_compact_receipt()
    text = "\n".join(
        message.content or "" for message in recovered.state.transcript.request().messages
    )
    assert len(provider.requests) == 1
    assert "New public correction" in text
    assert "fresh activation material" in text
    assert "old activation material" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["paired", "response"])
async def test_ordinary_e_cold_keeps_checkpoint_and_fresh_material(
    database, tmp_path, monkeypatch, phase, compact_evidence
):
    control, _, initial = await _session(database, tmp_path)
    context = await _public_context(database, control, initial)
    initial = (*context.messages, initial[-1])
    provider = FakeLLMProvider(
        lambda request: (
            "The paid public checkpoint."
            if "Host 会话上下文压缩要求" in (request.messages[-1].content or "")
            else "The paid private checkpoint."
            if "Host 上下文压缩要求" in (request.messages[-1].content or "")
            else "Original ordinary result observed."
        )
    )
    runner, runtime = await _runtime(database, control, initial, provider)
    runtime = replace(
        runtime,
        work_control=None,
        public_compaction=context,
        execution_id="event:e-cold-review",
        auxiliary_requests=[0],
    )
    # A real existing public checkpoint leaves no new public fragment. This
    # remains the ordinary E path even when legal P is now preferred.
    public_execution = TurnExecution(runner, initial, replace(runtime, compact_focus=""), None)
    await public_execution.compact_current(
        0, ChatRequest(messages=initial), ModelExecutionPriority.FOREGROUND, 10000
    )
    initial = public_execution.initial_messages
    assert not context.fragments
    execution = TurnExecution(runner, initial, runtime, None)
    execution.state.transcript.append(ChatMessage("user", "old activation material"))
    await execution._finalize_initial_layout()
    execution.state.transcript.append(ChatMessage("assistant", "old investigation " * 100))
    call = ToolCall("ordinary-original-call", ToolFunction("read_probe", '{"path":"original"}'))
    result = json.dumps(
        {
            "ok": True,
            "data": {
                "status": "succeeded",
                "operation_id": "ordinary-original-operation",
                "event_id": 123,
                "proof": "ordinary-unobserved-result",
            },
        }
    )
    execution.state.transcript.append(ChatMessage("assistant", "", tool_calls=(call,)))
    execution.state.transcript.append_result(call.id, result)
    request = ChatRequest(messages=execution.state.transcript.request().messages)
    directory, evidence = compact_evidence
    evidence.update(phase=phase, original_call=asdict(call), original_result=result)
    if phase == "response":
        write = CompactReceipt.write

        async def fail_pairing(self, payload, **kwargs):
            if payload["phase"] == "paired":
                raise WorkConflict("pairing_failed")
            await write(self, payload, **kwargs)

        monkeypatch.setattr(CompactReceipt, "write", fail_pairing)
        with pytest.raises(WorkConflict, match="pairing_failed"):
            await execution.compact_current(0, request, ModelExecutionPriority.FOREGROUND, 10000)
        monkeypatch.setattr(CompactReceipt, "write", write)
    else:
        await execution.compact_current(0, request, ModelExecutionPriority.FOREGROUND, 10000)
    try:
        await _append_public(database, "New public correction after private paid response.")
        fresh = await _public_context(database, control, initial)
        recovered = TurnExecution(
            runner, (*fresh.messages, initial[-1]), replace(runtime, public_compaction=fresh), None
        )
        recovered.state.transcript.append(ChatMessage("user", "fresh activation material"))
        await recovered.restore_compact_receipt()
        evidence["restored_transcript"] = asdict(recovered.state.transcript.request())
        assert len(provider.requests) == 2
        await recovered.request(0)
        evidence["normal_request"] = asdict(provider.requests[-1])
        text = "\n".join(message.content or "" for message in provider.requests[-1].messages)
        assert text.count("The paid private checkpoint.") == 1
        assert "New public correction" in text
        assert "fresh activation material" in text
        assert "old activation material" not in text
        assert call.id in str(provider.requests[-1].messages)
        assert "ordinary-original-operation" in text
        assert "ordinary-unobserved-result" in text
        assert len(provider.requests) == 3
    finally:
        evidence["requests"] = [asdict(item) for item in provider.requests]
        await capture_compact_facts(database, directory, evidence)


@pytest.mark.asyncio
async def test_work_p_response_keeps_fresh_extras(database, tmp_path, monkeypatch):
    import yuki.services.context_compaction as compaction

    control, session, initial = await _session(database, tmp_path)
    context = await _public_context(database, control, initial)
    initial = (*context.messages, initial[-1])
    session.transcript = TurnTranscript(initial)
    provider = FakeLLMProvider(lambda request: "The paid public checkpoint.")
    runner, runtime = await _runtime(database, control, initial, provider)
    runtime = replace(runtime, public_compaction=context, compact_focus="", auxiliary_requests=[0])
    execution = TurnExecution(runner, initial, runtime, None)
    execution.state.transcript = session.transcript
    publish = compaction.public_compact_publication

    async def fail_publication(*args, **kwargs):
        raise WorkConflict("publication_failed")

    monkeypatch.setattr(compaction, "public_compact_publication", fail_publication)
    with pytest.raises(WorkConflict, match="publication_failed"):
        await execution.compact_current(
            0, ChatRequest(messages=initial), ModelExecutionPriority.FOREGROUND, 10000
        )
    await _append_public(database, "New public correction after Work paid response.")
    fresh = await _public_context(database, control, initial)
    monkeypatch.setattr(compaction, "public_compact_publication", publish)
    restored = WorkSession(control, session.contract)
    control.session = restored
    await restored.restore(
        TurnTranscript(
            (*fresh.messages, initial[-1], ChatMessage("user", "fresh activation material"))
        ),
        public_compaction=fresh,
    )
    text = "\n".join(message.content or "" for message in restored.transcript.request().messages)
    assert len(provider.requests) == 1
    assert text.count("The paid public checkpoint.") == 1
    assert "New public correction" in text
    assert text.count("fresh activation material") == 1
    assert "context_compaction" not in restored.progress


@pytest.mark.parametrize("phase", ["paired", "response"])
async def test_ordinary_native_cold_preserves_host_tool_proof_for_another_compact(
    database, tmp_path, monkeypatch, phase, compact_evidence
):
    import httpx
    from tests.support.agent_backend import StubAgentBackend

    from yuki.llm.gemini import GeminiProvider

    control, _, initial = await _session(database, tmp_path)
    context = await _public_context(database, control, initial)
    initial = (*context.messages, initial[-1])
    directory, evidence = compact_evidence

    def respond(request):
        evidence["requests"].append(asdict(request))
        if any(
            "Host 会话上下文压缩要求" in (getattr(item, "content", "") or "")
            for item in (*request.messages, *request.continuation_items)
        ):
            return "Existing public checkpoint."
        if any(
            "Host 上下文压缩要求" in (getattr(item, "content", "") or "")
            for item in (*request.messages, *request.continuation_items)
        ):
            return "Private discoveries checkpoint."
        return "The original tool result was received."

    provider = FakeLLMProvider(respond)
    runner, runtime = await _runtime(database, control, initial, provider)
    tools = (ChatTool("read_probe", "Read existing evidence", {"type": "object"}),)
    runtime = replace(
        runtime,
        work_control=None,
        public_compaction=context,
        execution_id="ordinary-native-cold-" + phase,
        fixed_tools=tools,
        max_model_requests=None,
        auxiliary_requests=[0],
    )
    public = TurnExecution(runner, initial, replace(runtime, compact_focus=""), None)
    public.state.fixed_definitions = tools
    await public.compact_current(
        0, ChatRequest(messages=initial, tools=tools), ModelExecutionPriority.FOREGROUND, 128000
    )
    initial = public.initial_messages
    assert not context.fragments
    invocations = []

    async def execute(invocation):
        invocations.append(invocation.identity.operation_id)
        return json.dumps(
            {
                "ok": True,
                "data": {
                    "operation_id": invocation.identity.operation_id,
                    "proof": "ordinary-native-original-result",
                },
            }
        )

    backend = StubAgentBackend(execute_call=execute, is_side_effecting=lambda *_: False)
    execution = TurnExecution(runner, initial, runtime, backend)
    execution.state.fixed_definitions = execution.state.definitions = tools
    native_wire = []

    def transport(request):
        native_wire.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {
                            "role": "model",
                            "parts": [
                                {
                                    "functionCall": {
                                        "name": "read_probe",
                                        "args": {"query": "original cold proof"},
                                    },
                                    "thoughtSignature": "ordinary-original-native-signature",
                                }
                            ],
                        },
                    }
                ]
            },
        )

    evidence.update(phase=phase, native_wire=native_wire, invocations=invocations)
    try:
        async with httpx.AsyncClient(
            base_url="https://wire.invalid/v1/", transport=httpx.MockTransport(transport)
        ) as client:
            native = GeminiProvider(
                base_url="https://wire.invalid/v1/",
                api_key="synthetic",
                timeout_seconds=1,
                max_retries=0,
                client=client,
            )
            response = await native.complete(
                ChatRequest(messages=initial, tools=tools, model="gemini-test")
            )
        assert response.continuation is not None
        await execution.observe_response(0, response, ())
        outcome = await execution.execute_tools(0, response)
        await execution.finish_tool_turn(0, response, outcome)
        original_observation = json.loads(json.dumps(execution.state.response_observation))
        evidence["original_host_observation"] = original_observation
        sequence = execution.state.transcript.request()
        request = ChatRequest(
            messages=sequence.messages,
            continuation=sequence.continuation,
            continuation_items=sequence.items,
            tools=tools,
        )
        write = CompactReceipt.write

        async def fail_pairing(self, payload, **kwargs):
            if payload["phase"] == "paired":
                raise WorkConflict("native_pairing_failed")
            await write(self, payload, **kwargs)

        if phase == "response":
            monkeypatch.setattr(CompactReceipt, "write", fail_pairing)
            with pytest.raises(WorkConflict, match="native_pairing_failed"):
                await execution.compact_current(0, request, ModelExecutionPriority.FOREGROUND, None)
            monkeypatch.setattr(CompactReceipt, "write", write)
        else:
            await execution.compact_current(0, request, ModelExecutionPriority.FOREGROUND, None)
        receipt = await CompactReceipt(context, runtime.execution_id, runtime.actor_user_id).read()
        evidence["paid_receipt"] = receipt
        assert receipt["phase"] == phase
        assert "ordinary-native-original-result" in str(receipt["retained"])
        await database.engine.dispose()
        fresh = await _public_context(database, control, initial)
        resumed = TurnExecution(
            runner,
            (*fresh.messages, initial[-1]),
            replace(runtime, public_compaction=fresh),
            backend,
        )
        resumed.state.fixed_definitions = resumed.state.definitions = tools
        await resumed.restore_compact_receipt()
        evidence["restored_host_observation"] = dict(resumed.state.response_observation)
        evidence["before_second_compact"] = asdict(resumed.state.transcript.request())
        before = len(provider.requests)
        await resumed.compact_current(
            0,
            ChatRequest(messages=resumed.state.transcript.request().messages, tools=tools),
            ModelExecutionPriority.FOREGROUND,
            128000,
        )
        evidence["second_compact_requests"] = [asdict(item) for item in provider.requests[before:]]
        await resumed.request(0)
        normal = provider.requests[-1]
        evidence["normal_request"] = asdict(normal)
        body = json.dumps(asdict(normal), ensure_ascii=False)
        assert response.tool_calls[0].id in body
        assert "ordinary-native-original-result" in body
        assert invocations[0] in body
        assert "ordinary-original-native-signature" not in body
        assert len(invocations) == 1
        assert resumed.state.response_observation != original_observation
    finally:
        evidence["requests"] = [asdict(item) for item in provider.requests]
        await capture_compact_facts(database, directory, evidence)
