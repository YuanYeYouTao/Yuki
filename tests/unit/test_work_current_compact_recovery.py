"""Current Main compact retains original input, receipt and paid-budget facts."""

import json
from dataclasses import asdict, replace

import pytest
from tests.support.compact_delivery_evidence import (
    capture_compact_facts,
)
from tests.support.compact_delivery_evidence import (
    compact_evidence as capture_compact_evidence,
)
from tests.support.work_compaction_capacity_helpers import _runtime, _session
from tests.support.work_session import WorkSession, invoke_tool

from yuki.domain.messages import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ModelResponseStatus,
    ToolCall,
    ToolFunction,
)
from yuki.llm.fake import FakeLLMProvider
from yuki.model_runtime.capacity import estimate_request_tokens
from yuki.model_runtime.models import ModelExecutionPriority
from yuki.runtime.work_repository import WorkCapacityError
from yuki.services.context_compaction import EXECUTION_COMPACT_INSTRUCTION
from yuki.services.turn_execution import TurnExecution
from yuki.services.turn_transcript import TurnTranscript


@pytest.fixture
def compact_evidence(request):
    with capture_compact_evidence(request) as evidence:
        yield evidence


async def _paid_root(database, tmp_path):
    control, session, _ = await _session(database, tmp_path)
    initial = session.transcript.request().messages
    transcript = session.transcript
    transcript.append(ChatMessage("user", "a real public message"))
    transcript.append(ChatMessage("assistant", "a real reply"))
    await control.repository.checkpoint(
        control.lease, control.current["id"], None, models=3, tools=1
    )
    control.current = await control.repository.get(control.current["id"])
    return control, session, transcript, initial


@pytest.mark.parametrize("retained_inputs", [0, 2])
@pytest.mark.parametrize("provider_incomplete", [False, True])
async def test_current_compact_retains_original_inputs_and_business_resume(
    database, tmp_path, retained_inputs, provider_incomplete
):
    control, first, transcript, initial = await _paid_root(database, tmp_path)
    identity = control.current["id"]
    original_source = control.current["source_json"]
    first.progress["task_material"] = {
        "directives": [{"text": "keep the original restriction", "refs": ["goal"]}]
    }
    original_request = await first._original_request()
    input_ids = []
    for index in range(19):
        text = f"original requirement {index}: " + "specific requirement " * 100
        input_id = await control.repository.enqueue(
            control.lease.conversation_id,
            control.lease.generation,
            f"current-compact-input:{index}",
            kind="message",
            work_id=identity,
            ready=False,
        )
        assert await control.repository.prepare_input(input_id, {"text": text})
        input_ids.append(input_id)
        transcript.append(ChatMessage("user", text))
    await control.repository.stage(control.lease, input_ids, "original-inputs")
    await control.repository.consume(control.lease, "original-inputs")
    await first.save("paired")
    original = transcript.request()
    checkpoint = "Keep the original restriction and all uncompleted requirements."
    provider = FakeLLMProvider(
        lambda request: ChatResponse(
            content=checkpoint,
            latency_seconds=0,
            status=ModelResponseStatus.INCOMPLETE
            if provider_incomplete
            else ModelResponseStatus.COMPLETED,
            incomplete_reason="max_output_tokens" if provider_incomplete else None,
        )
    )
    runner, runtime = await _runtime(database, control, initial, provider)
    execution = TurnExecution(runner, initial, runtime, None)
    execution.state.transcript = transcript
    main = ChatRequest(messages=original.messages, max_output_tokens=8192)
    boundary = len(original.messages) - retained_inputs
    paid_prefix = (
        *original.messages[:boundary],
        ChatMessage("user", EXECUTION_COMPACT_INSTRUCTION),
    )
    # Leave room for the real current-Work scope appended to the instruction;
    # another full requirement still does not fit this partial-prefix case.
    budget = (
        estimate_request_tokens(runner._capacity_request(replace(main, messages=paid_prefix))) + 256
    )
    if provider_incomplete and estimate_request_tokens(runner._capacity_request(main)) > budget:
        with pytest.raises(WorkCapacityError, match="model_request_capacity"):
            await execution.compact_current(0, main, ModelExecutionPriority.FOREGROUND, budget)
    else:
        candidate = await execution.compact_current(
            0, main, ModelExecutionPriority.FOREGROUND, budget
        )
        if provider_incomplete:
            assert candidate == main
        else:
            assert candidate.messages[: len(initial)] == initial
            if retained_inputs:
                assert candidate.messages[-retained_inputs:] == original.messages[-retained_inputs:]
    assert len(provider.requests) == 1
    assert provider.requests[0].messages[:-1] == paid_prefix[:-1]
    assert provider.requests[0].messages[-1].content.startswith(EXECUTION_COMPACT_INSTRUCTION)
    assert provider.requests[0].tools == main.tools
    if provider_incomplete:
        assert first.transcript.request() == original
        assert "execution_checkpoint" not in first.progress["task_material"]
    else:
        assert first.progress["task_material"]["execution_checkpoint"] == checkpoint
    resumed = WorkSession(control, first.contract)
    control.session = resumed
    restored = await resumed.restore(TurnTranscript((ChatMessage("user", "fresh business input"),)))
    material = json.loads(restored.request().messages[-1].content)
    assert material["original_request"] == original_request
    assert material["task_material"]["directives"][0]["text"] == "keep the original restriction"
    for index in range(19):
        assert restored.request().messages[-1].content.count(f"original requirement {index}:") == 1
    assert len(provider.requests) == 1
    saved = await control.repository.get(identity)
    assert saved["goal"] == "prepare an artifact"
    assert saved["source_json"] == original_source
    assert (saved["model_requests"], saved["tool_calls"]) == (4, 1)
    await control.repository.release(control.lease)


async def test_incomplete_compact_keeps_original_paired_work_and_paid_budget(database, tmp_path):
    control, session, transcript, initial = await _paid_root(database, tmp_path)
    before = transcript.request()
    provider = FakeLLMProvider(
        lambda request: ChatResponse(
            content="Incomplete paid checkpoint",
            latency_seconds=0,
            status=ModelResponseStatus.INCOMPLETE,
            incomplete_reason="max_output_tokens",
        )
    )
    runner, runtime = await _runtime(database, control, initial, provider)
    execution = TurnExecution(runner, initial, runtime, None)
    execution.state.transcript = transcript
    main = ChatRequest(messages=before.messages, max_output_tokens=8192)
    assert (
        await execution.compact_current(0, main, ModelExecutionPriority.FOREGROUND, 128000) == main
    )
    assert session.transcript.request() == before
    assert session.transcript.chain_id == transcript.chain_id
    assert len(provider.requests) == 1
    saved = await control.repository.get(control.current["id"])
    assert saved["goal"] == "prepare an artifact"
    assert (saved["model_requests"], saved["tool_calls"]) == (4, 1)
    await control.repository.release(control.lease)


async def test_current_compact_selects_a_fitting_prefix_without_splitting_tool_pairs(
    database, tmp_path
):
    control, session, transcript, initial = await _paid_root(database, tmp_path)
    for index in range(60):
        transcript.append(ChatMessage("assistant", f"Record {index}: " + "中文 observation " * 120))
    call = ToolCall("original-paired-call", ToolFunction("terminal_exec", "{}"))
    transcript.append(ChatMessage("assistant", "", tool_calls=(call,)))
    invoked = []

    async def invoke():
        invoked.append(call.id)
        return json.dumps({"ok": True, "data": "original real result " * 1000})

    await session.save("response", (call,))
    result = await invoke_tool(session, call, invoke)
    original_call_key = session.call_key(call.id)
    transcript.append_result(call.id, result)
    await session.save("paired")
    original = transcript.request()
    provider = FakeLLMProvider(
        lambda request: "Verified prior records; keep the original tool receipt."
    )
    runner, runtime = await _runtime(database, control, initial, provider)
    execution = TurnExecution(runner, initial, runtime, None)
    execution.state.transcript = transcript
    main = ChatRequest(messages=original.messages, max_output_tokens=8192)
    # The real capacity fits the declaration but not its result. The paid
    # prefix must stop before the complete pair, never in its middle.
    halfway = (*original.messages[:-1], ChatMessage("user", EXECUTION_COMPACT_INSTRUCTION))
    budget = (
        estimate_request_tokens(runner._capacity_request(replace(main, messages=halfway))) + 256
    )
    candidate = await execution.compact_current(0, main, ModelExecutionPriority.FOREGROUND, budget)
    assert len(provider.requests) == 1
    assert provider.requests[0].messages[:-1] == original.messages[:-2]
    assert estimate_request_tokens(runner._capacity_request(provider.requests[0])) <= budget
    assert not any(message.tool_calls for message in provider.requests[0].messages)
    assert candidate.messages[-2:] == original.messages[-2:]
    assert candidate.messages[-2].tool_calls == (call,)
    assert candidate.messages[-1].tool_call_id == call.id
    assert await session.journal.effect_result(original_call_key) == result
    assert invoked == [call.id]
    assert (await control.repository.get(control.current["id"]))["model_requests"] == 4
    await control.repository.release(control.lease)


@pytest.mark.parametrize("input_budget", [None, 128000], ids=["unknown", "finite"])
@pytest.mark.parametrize("rebase_before_compact", [False, True], ids=["paired", "portable"])
async def test_current_compact_keeps_unobserved_original_receipt_for_next_main(
    database, tmp_path, input_budget, rebase_before_compact, compact_evidence
):
    """A fitting paid prefix cannot consume Main's original, still-unseen result."""
    control, session, initial = await _session(database, tmp_path)
    call = ToolCall("original-unobserved-call", ToolFunction("read_probe", '{"path":"proof"}'))
    session.transcript.append(ChatMessage("assistant", "", tool_calls=(call,)))
    invoked = []

    async def invoke():
        invoked.append(call.id)
        return json.dumps(
            {
                "ok": True,
                "data": {
                    "status": "succeeded",
                    "operation_id": original_key,
                    "proof": "original-unobserved-result",
                },
            }
        )

    await session.save("response", (call,))
    original_key = session.call_key(call.id)
    result = await invoke_tool(session, call, invoke)
    session.transcript.append_result(call.id, result)
    await session.save("paired")
    identity = control.current["id"]
    original_source = control.current["source_json"]
    directory, evidence = compact_evidence
    if rebase_before_compact:
        await database.engine.dispose()
        restored = WorkSession(control, session.contract)
        control.session = restored
        await restored.restore(TurnTranscript(initial))
        session = restored
        assert "work_unobserved_tool_round" in str(session.transcript.request().messages)
        assert original_key in str(session.transcript.request().messages)
        evidence["first_portable_restore"] = asdict(session.transcript.request())
        await session.save("paired")
        await database.engine.dispose()
        restored = WorkSession(control, session.contract)
        control.session = restored
        await restored.restore(TurnTranscript(initial))
        session = restored
        evidence["second_portable_restore"] = asdict(session.transcript.request())

    provider = FakeLLMProvider(
        lambda request: (
            "Private progress checkpoint."
            if "Host 上下文压缩要求" in (request.messages[-1].content or "")
            else "Original result received; no tool replay is needed."
        )
    )
    runner, runtime = await _runtime(database, control, initial, provider)
    runtime = replace(runtime, max_model_requests=None, auxiliary_requests=[0])
    execution = TurnExecution(runner, initial, runtime, None)
    execution.state.transcript = session.transcript
    evidence.update(
        input_budget=input_budget,
        rebase_before_compact=rebase_before_compact,
        original_work=identity,
        original_source=original_source,
        original_call=asdict(call),
        original_effect_key=original_key,
        original_result=result,
    )
    try:
        await execution.compact_current(
            0,
            ChatRequest(messages=session.transcript.request().messages),
            ModelExecutionPriority.FOREGROUND,
            input_budget,
            trigger_tokens=1 if rebase_before_compact else None,
        )
        evidence["compacted_transcript"] = asdict(session.transcript.request())
        await execution.request(0)
        evidence["normal_request"] = asdict(provider.requests[-1])
        body = json.dumps(asdict(provider.requests[-1]), ensure_ascii=False)
        assert call.id in body
        assert "original-unobserved-result" in body
        assert json.loads(result) == json.loads(await session.journal.effect_result(original_key))
        assert original_key in body
        assert invoked == [call.id]
        saved = await control.repository.get(identity)
        evidence["saved_work"] = saved
        assert saved["source_json"] == original_source
        assert saved["model_requests"] == (1 if rebase_before_compact else 2)
        assert saved["tool_calls"] == 1
    finally:
        evidence["requests"] = [asdict(request) for request in provider.requests]
        evidence["invoked"] = invoked
        evidence["original_effect"] = await session.journal.effect_result(original_key)
        await capture_compact_facts(database, directory, evidence)
        await control.repository.release(control.lease)


@pytest.mark.parametrize("partial_keys", [False, True], ids=["missing-key", "partial-known-keys"])
async def test_legacy_public_compact_restore_does_not_invent_original_effect_key(
    database, tmp_path, partial_keys, compact_evidence
):
    """59ad paired P retained raw calls after changing chain and clearing observations."""
    from tests.unit.test_current_context_compaction import _public_context

    control, session, initial = await _session(database, tmp_path)
    context = await _public_context(database, control, initial)
    initial = (*context.messages, initial[-1])
    session.transcript = TurnTranscript(initial)
    call = ToolCall("legacy-original-call", ToolFunction("read_probe", '{"path":"original"}'))
    known_call = ToolCall("legacy-known-call", ToolFunction("read_probe", '{"path":"known"}'))
    calls = (call, known_call) if partial_keys else (call,)
    session.transcript.append(ChatMessage("assistant", "", tool_calls=calls))
    invoked = []

    async def invoke():
        invoked.append(call.id)
        return json.dumps(
            {
                "ok": True,
                "data": {
                    "status": "succeeded",
                    "operation_id": original_key,
                    "proof": "legacy-original-result",
                },
            }
        )

    await session.save("response", calls)
    original_key = session.call_key(call.id)
    result = await invoke_tool(session, call, invoke)
    session.transcript.append_result(call.id, result)
    known_key = session.call_key(known_call.id)
    known_result = json.dumps({"ok": True, "data": "original-known-key-result"})
    if partial_keys:

        async def invoke_known():
            invoked.append(known_call.id)
            return known_result

        assert await invoke_tool(session, known_call, invoke_known) == known_result
        session.transcript.append_result(known_call.id, known_result)
    await session.save("paired")
    provider = FakeLLMProvider(
        lambda request: (
            "Paid public checkpoint."
            if "Host 会话上下文压缩要求" in (request.messages[-1].content or "")
            else "Original receipt received."
        )
    )
    runner, runtime = await _runtime(database, control, initial, provider)
    runtime = replace(
        runtime,
        public_compaction=context,
        compact_focus="",
        auxiliary_requests=[0],
        max_model_requests=None,
    )
    execution = TurnExecution(runner, initial, runtime, None)
    execution.state.transcript = session.transcript
    directory, evidence = compact_evidence
    evidence.update(
        original_call=asdict(call),
        original_key=original_key,
        original_result=result,
        partial_keys=partial_keys,
        known_key=known_key if partial_keys else None,
    )
    try:
        await execution.compact_current(
            0,
            ChatRequest(messages=session.transcript.request().messages),
            ModelExecutionPriority.FOREGROUND,
            128000,
        )
        assert session.progress["chain_links"][-1]["reason"] == "compact"
        assert original_key != session.call_key(call.id)
        # This is the old persisted representation, not an untrusted user tag.
        # 59ad finished P by clearing the Host observations but keeping raw pairs.
        if partial_keys:
            # Keep one actual Host-proved key while the other original key is
            # absent. Missing proof in one item cannot erase its neighbour's fact.
            observations = session.progress["model_observations"]
            for item in observations[-1]["results"]:
                if item["call_id"] == call.id:
                    item.pop("effect_key", None)
        else:
            session.progress.pop("model_observations", None)
        await session.save("paired")
        evidence["legacy_paired_transcript"] = asdict(session.transcript.request())
        evidence["legacy_paired_progress"] = dict(session.progress)
        await database.engine.dispose()
        restored = WorkSession(control, session.contract)
        control.session = restored
        await restored.restore(TurnTranscript((*context.messages, initial[-1])))
        resumed = TurnExecution(
            runner,
            (*context.messages, initial[-1]),
            replace(runtime, compact_focus=None),
            None,
        )
        resumed.state.transcript = restored.transcript
        await resumed.request(0)
        normal = provider.requests[-1]
        portable = next(
            json.loads(message.content)
            for message in normal.messages
            if "legacy-original-result" in (message.content or "")
        )
        evidence["normal_request"] = asdict(normal)
        original = portable["calls"][0]
        assert original["call_id"] == call.id
        assert original["arguments"] == call.function.arguments
        assert original["result"] == result
        assert "effect_key" not in original
        assert json.loads(original["result"])["data"]["operation_id"] == original_key
        if partial_keys:
            known = portable["calls"][1]
            assert known["call_id"] == known_call.id
            assert known["arguments"] == known_call.function.arguments
            assert known["result"] == known_result
            assert known["effect_key"] == known_key
            assert await session.journal.effect_result(known_key) == known_result
        assert await session.journal.effect_result(original_key) == result
        assert invoked == [item.id for item in calls]
        assert len(provider.requests) == 2
    finally:
        evidence["requests"] = [asdict(request) for request in provider.requests]
        evidence["original_effect"] = await session.journal.effect_result(original_key)
        evidence["invoked"] = invoked
        await capture_compact_facts(database, directory, evidence)
        await control.repository.release(control.lease)
