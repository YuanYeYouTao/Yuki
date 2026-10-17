"""Main maintenance reuses actual input and durable paid responses."""

from dataclasses import replace

import pytest
from tests.support.work_compaction_capacity_helpers import _runtime, _session

from qq_ai_bot.domain.messages import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ChatTool,
    ModelResponseStatus,
    ToolCall,
    ToolFunction,
)
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.model_runtime.capacity import ModelCapacity, estimate_request_tokens
from qq_ai_bot.model_runtime.models import ModelExecutionPriority
from qq_ai_bot.runtime.work_journal import JournalUnavailable
from qq_ai_bot.runtime.work_repository import WorkConflict
from qq_ai_bot.runtime.work_session import WorkSession
from qq_ai_bot.services.context_compaction import CompactReceipt, execution_compact_plan
from qq_ai_bot.services.turn_execution import TurnExecution
from qq_ai_bot.services.turn_transcript import TurnTranscript


async def _public_context(database, control, initial):
    from qq_ai_bot.conversation.frozen_fragments import FrozenFragments
    from qq_ai_bot.conversation.projections import PromptProjectionRepository
    from qq_ai_bot.domain.conversations import ConversationScope
    from qq_ai_bot.persistence.event_repository import EventLedgerRepository
    from qq_ai_bot.services.context_compaction import PublicCompactContext

    version, events = await EventLedgerRepository(database).read_scope_context(
        ConversationScope.group("80001", "20001"),
        limit=100,
    )
    fragments = FrozenFragments.load([]).extend_history(
        tuple(((event.id,), ChatMessage("user", event.content)) for event in events),
        (),
    )

    async def adopt(summary, coverage, retained):
        from qq_ai_bot.services.context_compaction import render_context_checkpoint

        context.messages = (
            initial[0],
            render_context_checkpoint(summary, coverage),
            *retained.messages(),
        )
        context.fragments = retained
        return (*context.messages, initial[-1])

    context = PublicCompactContext(
        (initial[0], *fragments.messages()),
        1,
        fragments,
        version,
        "test-public",
        PromptProjectionRepository(database),
        adopt,
    )
    return context


async def test_public_prefix_excludes_current_and_private_material(database, tmp_path):
    from qq_ai_bot.services.context_compaction import public_compact_plan

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

    import qq_ai_bot.services.context_compaction as compaction
    from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
    from qq_ai_bot.conversation.observation_models import ContextObservationModel

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
    async with database.sessions() as reader:
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
    from qq_ai_bot.domain.conversations import ConversationScope
    from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork

    return await ScopedEventLedgerUnitOfWork(database).append(
        scope=ConversationScope.group("80001", "20001"),
        platform_message_id="new-public-event",
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

    from qq_ai_bot.conversation.observation_models import ContextObservationModel

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
    async with database.sessions() as reader:
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
    import qq_ai_bot.services.context_compaction as compaction

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
    from qq_ai_bot.web.models import WebMode

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

    from qq_ai_bot.domain.conversations import ConversationScope
    from qq_ai_bot.services.main_agent_contract import MainAgentContract
    from qq_ai_bot.workspace.short_state import ShortState
    from qq_ai_bot.workspace.store import WorkspaceStore

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
    await harness.scoped_events.append(
        scope=scope,
        platform_message_id="prior-public",
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
    from qq_ai_bot.services.context_compaction import CompactReceipt

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
    database, tmp_path, monkeypatch, phase
):
    control, _, initial = await _session(database, tmp_path)
    context = await _public_context(database, control, initial)
    initial = (*context.messages, initial[-1])
    provider = FakeLLMProvider(lambda request: "The paid private checkpoint.")
    runner, runtime = await _runtime(database, control, initial, provider)
    runtime = replace(
        runtime,
        work_control=None,
        public_compaction=context,
        execution_id="event:e-cold-review",
        auxiliary_requests=[0],
    )
    execution = TurnExecution(runner, initial, runtime, None)
    execution.state.transcript.append(ChatMessage("user", "old activation material"))
    await execution._finalize_initial_layout()
    execution.state.transcript.append(ChatMessage("assistant", "old investigation " * 100))
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
    await _append_public(database, "New public correction after private paid response.")
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
    assert text.count("The paid private checkpoint.") == 1
    assert "New public correction" in text
    assert "fresh activation material" in text
    assert "old activation material" not in text


@pytest.mark.asyncio
async def test_work_p_response_keeps_fresh_extras(database, tmp_path, monkeypatch):
    import qq_ai_bot.services.context_compaction as compaction

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
