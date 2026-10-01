"""Capacity replacement preserves original input and paired execution facts."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from tests.conftest import build_harness, make_settings
from tests.support.social_identity_cases import social_env

from qq_ai_bot.domain.messages import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ChatTool,
    NativeToolEvent,
    NativeToolStatus,
    NativeToolType,
    ProviderContinuation,
    ResponseCitation,
    ToolCall,
    ToolFunction,
)
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.model_runtime.capacity import ModelCapacity, estimate_request_tokens
from qq_ai_bot.model_runtime.models import ModelExecutionPriority
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_repository import WorkCapacityError, WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import journal
from qq_ai_bot.runtime.work_session import WorkSession
from qq_ai_bot.services.agent_runner import AgentRunner, AgentRuntime
from qq_ai_bot.services.concurrency import ConcurrencyManager
from qq_ai_bot.services.turn_transcript import TurnTranscript


async def _session(database, tmp_path):
    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    lease = await repository.acquire(env.context.conversation_id, 1)

    async def validate():
        assert await repository.valid(lease)

    control = WorkControl(repository, lease, "capacity-test", {"trigger_event_id": 1}, validate)
    control.current = await repository.accept(
        lease, source_key="capacity-test", source={}, goal="prepare an artifact"
    )
    task = ChatMessage("user", "Prepare the artifact and preserve the original instructions.")
    initial = (ChatMessage("system", "fixed contract"), task)
    session = WorkSession(control, "capacity-contract")
    control.session = session
    await session.restore(TurnTranscript(initial), compaction_brief=task)
    return control, session, initial


async def _snapshot(database, work_id):
    async with database.sessions() as reader:
        return dict(
            (await reader.execute(select(journal).where(journal.c.work_id == work_id)))
            .mappings()
            .one()
        )


def _grow(transcript):
    # Old, replaceable history must sit outside the retained recent raw suffix.
    for _ in range(20):
        transcript.append(ChatMessage("assistant", "Completed investigation. " * 500))
    for _ in range(16):
        transcript.append(ChatMessage("assistant", "Recent completed check."))


async def _runtime(database, control, initial, provider, **settings):
    harness = build_harness(database, make_settings(database.url, **settings), provider)
    chat = harness.processor._chat
    runtime = AgentRuntime(
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id="10001",
        actor_is_superuser=False,
        delegated_authority=None,
        conversation_key="capacity-test",
        current_group_id=None,
        bot_user_id="80001",
        gateway=None,
        runtime_config=await chat._runtime_config.snapshot(),
        current_time=chat._time.current_default(),
        allowed_capabilities=frozenset(),
        max_tool_calls=8,
        max_model_requests=1,
        work_control=control,
        compaction_brief=initial[-1],
    )
    return chat.runtime.runner, runtime


@pytest.mark.asyncio
async def test_old_negative_steer_survives_three_compactions_and_two_restarts(database, tmp_path):
    control, session, initial = await _session(database, tmp_path)
    original_work_id = control.current["id"]
    constraint = "Do not publish a release or resend any delivered artifact."
    identity = await control.repository.enqueue(
        control.lease.conversation_id,
        1,
        "event:old-negative-steer",
        kind="message",
        event_id=1,
        work_id=control.current["id"],
        ready=False,
    )
    assert await control.repository.prepare_input(identity, {"text": constraint})
    for message in await control.take_inputs("steer-attempt"):
        session.transcript.append(message)
    await session.save("paired")
    await control.repository.consume(control.lease, "steer-attempt")

    chains = {session.transcript.chain_id}
    for index in range(3):
        _grow(session.transcript)
        candidate = await session.compact("Checks completed; continue preparing the artifact.")
        capsule = json.loads(candidate.request().messages[-1].content)
        assert constraint not in capsule["summary"]
        assert capsule["task_inputs"] == [
            {
                "input_id": identity,
                "event_id": 1,
                "source_key": "event:old-negative-steer",
                "text": constraint,
            }
        ]
        assert candidate.chain_id not in chains
        chains.add(candidate.chain_id)
        assert session.input_ids == [identity]
        assert control.current["id"] == original_work_id
        if index < 2:
            session = WorkSession(control, "capacity-contract")
            control.session = session
            restored = await session.restore(TurnTranscript(initial), compaction_brief=initial[-1])
            assert restored.request() == candidate.request()
            assert constraint in await session.summary_source()
    await control.repository.release(control.lease)


@pytest.mark.asyncio
async def test_native_public_call_and_result_are_paired_after_compaction_and_restart(
    database, tmp_path
):
    control, _, initial = await _session(database, tmp_path)
    call = ToolCall("original-call", ToolFunction("read_probe", '{"path":"report"}'))
    output = json.dumps({"ok": True, "data": {"text": "original evidence"}})
    provider = FakeLLMProvider(
        lambda _: ChatResponse(
            "Read the original evidence",
            0,
            tool_calls=(call,),
            reasoning_content="private reasoning must stay private",
            continuation=ProviderContinuation(
                "gemini", "gemini", "test", {"opaque_signature": "never summarize this signature"}
            ),
            citations=(ResponseCitation("https://example.org/evidence", "Evidence"),),
            native_tool_events=(
                NativeToolEvent(
                    NativeToolType.WEB_SEARCH, "native-search", NativeToolStatus.COMPLETED
                ),
            ),
        )
    )
    runner, runtime = await _runtime(database, control, initial, provider)
    definition = ChatTool("read_probe", "Read evidence", {"type": "object"})
    backend = SimpleNamespace(
        definitions=lambda *args, **kwargs: (definition,),
        execute=AsyncMock(return_value=output),
        begin_batch=lambda *args: None,
        is_side_effecting=lambda *args: False,
        parallel_safe=lambda *args: False,
        exhausted=lambda *args: "",
    )
    await runner.run(initial, runtime, backend)
    backend.execute.assert_awaited_once()
    session = control.session
    _grow(session.transcript)
    candidate = await session.compact("Continue from the successful read.")
    capsule = json.loads(candidate.request().messages[-1].content)
    round_record = capsule["recent_tool_rounds"][0]
    assert round_record["tool_calls"][0]["id"] == call.id
    assert round_record["results"] == [
        {
            "call_id": call.id,
            "name": "read_probe",
            "arguments": call.function.arguments,
            "output": output,
            "executed": True,
        }
    ]
    assert round_record["citations"][0]["url"] == "https://example.org/evidence"
    assert round_record["native_tool_events"][0]["call_id"] == "native-search"
    assert "never summarize this signature" not in candidate.request().messages[-1].content
    assert "private reasoning must stay private" not in candidate.request().messages[-1].content
    restored = WorkSession(control, session.contract)
    await restored.restore(TurnTranscript(initial), compaction_brief=initial[-1])
    assert restored.transcript.request() == candidate.request()
    assert restored.progress["retained_tool_rounds"] == capsule["recent_tool_rounds"]
    backend.execute.assert_awaited_once()
    await control.repository.release(control.lease)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["invalid_summary", "no_improvement", "fixed_tools"])
async def test_rejected_candidate_keeps_original_paired_checkpoint(database, tmp_path, failure):
    control, session, _ = await _session(database, tmp_path)
    if failure == "fixed_tools":
        _grow(session.transcript)
    await session.save("paired")
    snapshot = await _snapshot(database, control.current["id"])
    original = session.transcript.request()
    template = None
    if failure == "fixed_tools":
        template = ChatRequest(
            messages=original.messages,
            tools=(ChatTool("large_fixed_contract", "x" * 120000, {"type": "object"}),),
        )
    error = ValueError if failure == "invalid_summary" else WorkCapacityError
    with pytest.raises(error):
        await session.compact(
            " " if failure == "invalid_summary" else "Valid summary",
            target_tokens=20000,
            request_template=template,
        )
    assert session.transcript.request() == original
    assert await _snapshot(database, control.current["id"]) == snapshot
    restored = WorkSession(control, session.contract)
    await restored.restore(TurnTranscript((ChatMessage("user", "fresh wakeup"),)))
    assert restored.transcript.request() == original
    await control.repository.release(control.lease)


@pytest.mark.asyncio
async def test_auxiliary_output_reservation_rejects_source_before_dispatch(database, tmp_path):
    control, session, initial = await _session(database, tmp_path)
    session.transcript.append(ChatMessage("assistant", "x" * 120000))
    await session.save("paired")
    snapshot = await _snapshot(database, control.current["id"])
    _, runtime = await _runtime(
        database,
        control,
        initial,
        FakeLLMProvider(),
        work_compaction_max_output_tokens=32768,
        work_context_window_tokens=65536,
    )
    capacity = ModelCapacity(context_tokens=65536, output_tokens=4096)
    executor = SimpleNamespace(capacity=lambda _: capacity, execute=AsyncMock())
    runner = AgentRunner(executor, ConcurrencyManager(1))
    main_request = ChatRequest(
        messages=session.transcript.request().messages, max_output_tokens=4096
    )
    assert estimate_request_tokens(main_request) < capacity.input_budget(65536, output_tokens=4096)
    with pytest.raises(WorkCapacityError, match="work_compaction_source_capacity"):
        await runner._compact_work(
            runtime,
            ModelExecutionPriority.FOREGROUND,
            capacity.input_budget(65536, output_tokens=4096),
            main_request,
        )
    executor.execute.assert_not_awaited()
    assert await _snapshot(database, control.current["id"]) == snapshot
    row = await control.repository.get(control.current["id"])
    assert row["model_requests"] == 0 and row["tool_calls"] == 0
    await control.repository.release(control.lease)


@pytest.mark.asyncio
async def test_tool_dense_source_compacts_without_duplicate_outputs(database, tmp_path):
    control, session, initial = await _session(database, tmp_path)
    for index in range(70):
        call = ToolCall(f"read-{index}", ToolFunction("read_file", '{"path":"report"}'))
        output = f"evidence-{index}:" + "x" * 4500
        session.transcript.append(ChatMessage("assistant", "Read evidence", tool_calls=(call,)))
        session.transcript.append_result(call.id, output)
        session.progress.setdefault("model_observations", []).append(
            {
                "sequence": index,
                "content": "Read evidence",
                "tool_calls": [
                    {
                        "id": call.id,
                        "function": {"name": "read_file", "arguments": call.function.arguments},
                    }
                ],
                "results": [{"call_id": call.id, "output": output, "executed": True}],
            }
        )
    await session.save("paired")
    runner, runtime = await _runtime(database, control, initial, FakeLLMProvider())
    main = ChatRequest(messages=session.transcript.request().messages, max_output_tokens=8192)
    assert 108800 < estimate_request_tokens(main) < 128000
    auxiliary = AsyncMock(return_value=ChatResponse("Read evidence; continue original task.", 0))
    runner._models = SimpleNamespace(capacity=lambda _: ModelCapacity(), execute=auxiliary)
    candidate = await runner._compact_work(runtime, ModelExecutionPriority.FOREGROUND, 128000, main)
    request = auxiliary.call_args.args[1]
    assert estimate_request_tokens(request) < 128000
    assert request.messages[-1].content.count("evidence-0:") == 1
    assert candidate.chain_id != main.request_chain_id
    assert estimate_request_tokens(ChatRequest(messages=candidate.request().messages)) < 64000
    await control.repository.release(control.lease)
