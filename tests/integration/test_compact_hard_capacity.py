"""Real preparation, paid journal and original tool facts at a hard capacity.

Before the fix, these cases distinguish the actual capacity facts:
* a completed checkpoint plus the mandatory unseen round/new input cannot fit,
  and all older execution source is already covered: preserve the paid result
  and fail, without purchasing that checkpoint again;
* older source remains, but the checkpoint prevents a fitting request from
  reaching any of it: preserve the paid result rather than summarizing itself;
* uncovered, already-observed execution source remains: legitimate successive
  batches may compact it until the real normal candidate fits, including real
  user input observed by the original normal tool-bearing response.

The model is scripted at its boundary. The original file read uses the real
InvocationService/Work journal, and prepare_request performs actual planning,
dispatch, response publication and capacity checks. Test assertions abort an
attempt to repurchase covered source; they are not a product request-count cap.
"""

import json
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest
from tests.support.compact_delivery_evidence import capture_compact_facts
from tests.support.compact_delivery_evidence import compact_evidence as evidence_capture
from tests.support.correctness_wire import wire
from tests.support.event_seed import seed_scoped_event
from tests.support.work_compaction_capacity_helpers import _runtime, _session
from tests.support.work_session import invoke_tool
from tests.unit.test_current_context_compaction import _public_context

from yuki.domain.conversations import ConversationScope
from yuki.domain.messages import ChatMessage, ChatResponse, ChatTool, ToolCall, ToolFunction
from yuki.llm.fake import FakeLLMProvider
from yuki.model_runtime.capacity import ModelCapacity, estimate_request_tokens
from yuki.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork
from yuki.runtime.work_journal import decode_transcript
from yuki.runtime.work_repository import WorkCapacityError
from yuki.services.context_compaction import (
    EXECUTION_COMPACT_INSTRUCTION,
    PUBLIC_COMPACT_INSTRUCTION,
)
from yuki.services.turn_execution import TurnExecution
from yuki.services.turn_transcript import TurnTranscript
from yuki.web.models import WebMode


@pytest.fixture
def compact_evidence(request):
    with evidence_capture(request) as captured:
        yield captured


@pytest.mark.parametrize(
    "case",
    [
        "checkpoint_cannot_fit",
        "checkpoint_blocks_remaining_source",
        "uncovered_observed_prefix",
        "uncovered_observed_user_prefix",
    ],
)
async def test_hard_capacity_preserves_paid_checkpoint_and_only_compacts_uncovered_source(
    database, tmp_path, monkeypatch, compact_evidence, case
):
    directory, evidence = compact_evidence
    control, session, initial = await _session(database, tmp_path)
    budget = 4096
    fully_covered = case == "checkpoint_cannot_fit"
    negative = case in {"checkpoint_cannot_fit", "checkpoint_blocks_remaining_source"}
    source_role = "user" if case == "uncovered_observed_user_prefix" else "assistant"
    old_segments = [
        f"COMPLETED_SEGMENT_{index} " + "oldfact " * (800 if fully_covered else 750)
        for index in range(1 if fully_covered else 4)
    ]
    original_text = "RAW_UNSEEN_REQUIRED " + "r" * (3000 if fully_covered else 100)
    source = tmp_path / "original-read.txt"
    source.write_text(original_text, encoding="utf-8")
    call = ToolCall(
        "original-required-round", ToolFunction("read_probe", json.dumps({"path": str(source)}))
    )
    for content in old_segments:
        session.transcript.append(ChatMessage(source_role, content))
    session.transcript.append(ChatMessage("assistant", "", tool_calls=(call,)))
    await session.save("response", (call,))
    original_key = session.call_key(call.id)
    actual_reads = []

    async def read_original():
        actual_reads.append(source.read_text(encoding="utf-8"))
        return json.dumps({"ok": True, "data": {"content": actual_reads[-1]}}, ensure_ascii=False)

    original_result = await invoke_tool(session, call, read_original, side_effecting=False)
    session.transcript.append_result(call.id, original_result)
    if fully_covered:
        session.transcript.append(ChatMessage("user", "NEW_INPUT_REQUIRED " + "n" * 5000))
    await session.save("paired")
    checkpoint = "PAID_VALID_CHECKPOINT " + "s" * 6000 if negative else None
    covered = set()

    def respond(request):
        evidence["requests"].append(
            {"estimated_tokens": estimate_request_tokens(request), "request": asdict(request)}
        )
        if negative:
            if len(provider.requests) > 1:
                text = "\n".join(item.content or "" for item in request.messages)
                assert all(segment not in text for segment in old_segments)
                assert checkpoint in text
                if not fully_covered:
                    retained = decode_transcript(
                        session.progress["context_compaction"]["retained"]
                    ).request()
                    retained_text = "\n".join(item.content or "" for item in retained.messages)
                    assert any(segment in retained_text for segment in old_segments)
                    evidence["uncovered_source_blocked_by_checkpoint"] = True
                    evidence["retained_at_blocked_frontier"] = asdict(retained)
                evidence["repeat_without_new_execution"] = True
                raise AssertionError("BOUNDED_PROBE: repurchased covered checkpoint/original round")
            response = checkpoint
        else:
            batch = {
                segment
                for segment in old_segments
                if any(
                    item.role == source_role and item.content == segment
                    for item in request.messages
                )
            }
            assert batch - covered, "Cannot buy another batch without uncovered execution source"
            covered.update(batch)
            response = "VERIFIED_CHECKPOINT: " + ",".join(
                segment.split(" ", 1)[0] for segment in sorted(covered)
            )
        evidence.setdefault("paid_responses", []).append(response)
        return response

    provider = FakeLLMProvider(respond)
    runner, runtime = await _runtime(database, control, initial, provider)
    monkeypatch.setattr(runner._models, "capacity", lambda _: ModelCapacity(input_tokens=budget))
    runtime = replace(
        runtime,
        max_model_requests=None,
        auxiliary_requests=[0],
        runtime_config=replace(
            runtime.runtime_config,
            llm=replace(runtime.runtime_config.llm, max_output_tokens=4096),
        ),
    )
    execution = TurnExecution(runner, initial, runtime, None)
    execution.state.transcript = session.transcript
    work_before = await control.repository.get(control.current["id"])
    evidence.update(
        case=case,
        hard_budget=budget,
        original_call=asdict(call),
        original_effect_key=original_key,
        original_result=original_result,
        old_segments=old_segments,
        model_requests_before=work_before["model_requests"],
        tool_calls_before=work_before["tool_calls"],
    )
    (directory / "original-read.txt").write_bytes(source.read_bytes())
    try:
        if negative:
            with pytest.raises(WorkCapacityError, match="model_request_capacity"):
                await execution.prepare_request(0, (), WebMode.DISABLED)
            assert len(provider.requests) == 1, (
                "The original paid checkpoint must not be repurchased"
            )
            assert session.progress["task_material"]["execution_checkpoint"] == checkpoint
        else:
            prepared = await execution.prepare_request(0, (), WebMode.DISABLED)
            evidence["normal_candidate"] = asdict(prepared.request)
            assert estimate_request_tokens(runner._capacity_request(prepared.request)) <= budget
            assert len(evidence["paid_responses"]) > 1
            receipt = next(
                item for item in prepared.request.messages if item.tool_call_id == call.id
            )
            assert receipt.content == original_result
        assert await session.journal.effect_result(original_key) == original_result
        assert await session.journal.effect_state(original_key) == "accepted"
        assert actual_reads == [original_text]
        saved = await control.repository.get(control.current["id"])
        assert saved["model_requests"] == work_before["model_requests"] + len(provider.requests)
        assert saved["tool_calls"] == work_before["tool_calls"]
    finally:
        evidence["auxiliary_requests"] = runtime.auxiliary_requests
        evidence["after_transcript"] = asdict(execution.state.transcript.request())
        evidence["original_receipt_after"] = await session.journal.effect_result(original_key)
        evidence["actual_reads"] = actual_reads
        await capture_compact_facts(database, directory, evidence)
        await control.repository.release(control.lease)


async def test_native_execution_compact_can_hand_over_to_newly_legal_public_compact(
    database, tmp_path, monkeypatch, compact_evidence
):
    """E legally retires native state; a now-legal P must get its chance to fit."""
    directory, evidence = compact_evidence
    control, session, original_initial = await _session(database, tmp_path)
    public_text = "REAL_PUBLIC_SOURCE " + "p" * 5500
    public_event = await seed_scoped_event(
        ScopedEventLedgerUnitOfWork(database),
        scope=ConversationScope.group("80001", "20001"),
        origin_key="native-public-source",
        sender_user_id="10001",
        direction="inbound",
        content=public_text,
    )
    public = await _public_context(database, control, original_initial)
    initial = (*public.messages, original_initial[-1])
    session.transcript = TurnTranscript(initial)
    source = tmp_path / "native-original-read.txt"
    source.write_text("ORIGINAL_NATIVE_RESULT " + "read result " * 50, encoding="utf-8")
    (directory / "native-original-read.txt").write_bytes(source.read_bytes())
    call = ToolCall(
        "native-original-read", ToolFunction("read_probe", json.dumps({"path": str(source)}))
    )
    new_input = "FRESH_UNPROCESSED_INPUT " + "n" * 4200
    private_summary = "PRIVATE_EXECUTION_CHECKPOINT " + "e" * 3000
    signature = "original-native-signature-" + "s" * 2400
    requested_original = False
    maintenance = []
    normals = []

    def respond(request):
        nonlocal requested_original
        contents = [
            getattr(item, "content", "") or ""
            for item in (*request.messages, *request.continuation_items)
        ]
        kind = (
            "E"
            if any(text.startswith(EXECUTION_COMPACT_INSTRUCTION) for text in contents)
            else "P"
            if any(text.startswith(PUBLIC_COMPACT_INSTRUCTION) for text in contents)
            else "normal"
        )
        evidence["requests"].append({"kind": kind, "request": asdict(request)})
        if kind in {"E", "P"}:
            assert kind not in maintenance, (
                "Do not repurchase a candidate to make this test terminate"
            )
            maintenance.append(kind)
            return ChatResponse(
                private_summary if kind == "E" else "PUBLIC_PAID_CHECKPOINT: decision retained.", 0
            )
        normals.append(request)
        if not requested_original:
            requested_original = True
            return ChatResponse("", 0, tool_calls=(call,))
        return ChatResponse("Original native receipt received by normal Main.", 0)

    provider = FakeLLMProvider(respond)
    runner, runtime = await _runtime(database, control, initial, provider)
    runtime = replace(
        runtime,
        max_model_requests=None,
        auxiliary_requests=[0],
        public_compaction=public,
        fixed_tools=(ChatTool("read_probe", "Read original file", {"type": "object"}),),
        runtime_config=replace(
            runtime.runtime_config,
            llm=replace(runtime.runtime_config.llm, max_output_tokens=4096),
            context=replace(runtime.runtime_config.context, compact_trigger_tokens=1000000),
        ),
    )
    import tests.support.correctness_wire as wire_support

    original_body = wire_support.body

    def response_body(kind, reply, index=0):
        payload = original_body(kind, reply, index)
        if kind == "gemini" and reply.tool_calls:
            payload["candidates"][0]["content"]["parts"][0]["thoughtSignature"] = signature
        return payload

    monkeypatch.setattr(wire_support, "body", response_body)
    client, payloads = wire(SimpleNamespace(provider=provider, runner=runner), "gemini")
    evidence["wire_requests"] = payloads
    monkeypatch.setattr(runner._models, "capacity", lambda _: ModelCapacity(input_tokens=4096))
    execution = TurnExecution(runner, initial, runtime, None)
    execution.state.transcript = session.transcript
    reads = []
    try:
        original_response = await execution.request(0)
        evidence["original_response"] = asdict(original_response)
        await execution.observe_response(0, original_response, ())
        assert original_response.continuation is not None
        assert signature in json.dumps(asdict(original_response.continuation))
        await session.save("response", original_response.tool_calls)
        original_key = session.call_key(call.id)

        async def read_original():
            reads.append(source.read_text(encoding="utf-8"))
            return json.dumps({"ok": True, "data": {"content": reads[-1]}})

        output = await invoke_tool(
            session, original_response.tool_calls[0], read_original, side_effecting=False
        )
        execution.state.transcript.append_result(call.id, output)
        execution.state.transcript.append(ChatMessage("user", new_input))
        await session.save("paired")
        evidence.update(
            original_effect_key=original_key,
            original_result=output,
            public_event_id=public_event.event.id,
            native_before=asdict(execution.state.transcript.request()),
        )
        result = await execution.request(1)
        evidence["normal_response"] = asdict(result)
        assert maintenance == ["E", "P"]
        normal = normals[-1]
        assert normal.continuation is None
        assert estimate_request_tokens(runner._capacity_request(normal)) <= 4096
        text = "\n".join(item.content or "" for item in normal.messages)
        portable = [
            json.loads(item.content)
            for item in normal.messages
            if (item.content or "").startswith("{")
        ]
        original_calls = [
            item
            for payload in portable
            if payload.get("kind") == "work_unobserved_tool_round"
            for item in payload["calls"]
            if item["call_id"] == call.id
        ]
        assert len(original_calls) == 1
        assert original_calls[0]["result"] == output
        assert original_calls[0]["effect_key"] == original_key
        assert json.loads(original_calls[0]["arguments"]) == json.loads(call.function.arguments)
        assert new_input in text and private_summary in text
        assert public_text not in text
        checkpoint = await public.repository.read_checkpoint(public.version.conversation_id)
        assert checkpoint.selected_summary_coverage == public_event.event.id
        assert checkpoint.selected_summary_text == "PUBLIC_PAID_CHECKPOINT: decision retained."
        e_wire, p_wire = payloads[1:3]
        assert signature in json.dumps(e_wire)
        assert signature not in json.dumps(p_wire)
        assert private_summary not in json.dumps(p_wire) and new_input not in json.dumps(p_wire)
        assert signature not in json.dumps(payloads[-1])
        assert await session.journal.effect_result(original_key) == output
        assert reads == [source.read_text(encoding="utf-8")]
    finally:
        evidence["maintenance"] = maintenance
        evidence["actual_reads"] = reads
        evidence["after_transcript"] = asdict(execution.state.transcript.request())
        await capture_compact_facts(database, directory, evidence)
        await control.repository.release(control.lease)
        await client.aclose()
