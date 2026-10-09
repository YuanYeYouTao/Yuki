"""Real journal round trips for anchors extended by public conversation deltas."""

import json

import pytest
from sqlalchemy import select, update
from tests.support.social_identity_cases import social_env
from tests.support.work_session import WorkSession, invoke_tool

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.domain.messages import ChatMessage, ToolCall, ToolFunction
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_journal import JournalUnavailable
from qq_ai_bot.runtime.work_repository import WorkConflict, WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import journal
from qq_ai_bot.services.turn_transcript import TurnTranscript


async def _staged_root(
    database, tmp_path, static_roles=("system",), *, stage=True, bind_access=False
):
    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    lease = await repository.acquire(env.context.conversation_id, 1)

    async def validate():
        assert await repository.valid(lease)

    control = WorkControl(repository, lease, "public-anchor", {"trigger_event_id": 1}, validate)
    if bind_access:
        from qq_ai_bot.tool_results.access import ArtifactAccess

        control.bind_context_access(ArtifactAccess(lease.conversation_id, 1, env.person))
    control.current = await repository.accept(
        lease,
        source_key=control.source_key,
        source=control.source,
        goal="finish the original audit",
    )
    initial = (
        *(ChatMessage(role, "fixed contract") for role in static_roles),
        ChatMessage("user", "original request"),
        ChatMessage("user", "current Work state"),
    )
    session = WorkSession(control, "same-contract")
    control.session = session
    transcript = await session.restore(TurnTranscript(initial), compaction_brief=initial[-2])
    tail = (ChatMessage("user", "a real public message"), ChatMessage("assistant", "a real reply"))
    for message in tail:
        transcript.append(message)
    await repository.checkpoint(lease, control.current["id"], None, models=3, tools=1)
    control.current = await repository.get(control.current["id"])
    if stage:
        await session.summary_source()
        await session.stage_compaction(None, retained_public=tail)
    return control, session, transcript, (*initial, *tail)


@pytest.mark.parametrize("static_roles", [("system",), ("system", "developer"), ("developer",)])
async def test_staged_public_assistant_tail_recovers_exact_anchor(database, tmp_path, static_roles):
    control, first, transcript, expected_anchor = await _staged_root(
        database, tmp_path, static_roles
    )
    identity = control.current["id"]
    original = await control.repository.get(identity)
    loaded = await first.journal.load(
        control.lease, identity, first.contract, source_control=control
    )
    assert loaded.reason == "resume"
    assert loaded.record["phase"] == "paired"
    payload = json.loads(loaded.record["payload_json"])
    assert payload["pending"] == []
    assert payload["metadata"]["compaction_anchor"]["items"][-1]["value"]["role"] == "assistant"

    resumed = WorkSession(control, first.contract)
    restored = await resumed.restore(TurnTranscript((ChatMessage("user", "new wakeup"),)))
    assert restored.chain_id == transcript.chain_id
    assert restored.request() == transcript.request()
    assert resumed.compaction_anchor.request().messages == expected_anchor
    assert resumed.progress["compaction_staging"] == first.progress["compaction_staging"]
    assert control.current["id"] == identity
    current = await control.repository.get(identity)
    assert (current["model_requests"], current["tool_calls"]) == (3, 1)
    assert current["source_json"] == original["source_json"]
    assert current["goal"] == original["goal"]
    await control.repository.release(control.lease)


async def test_public_tail_recovery_keeps_original_confirmed_effect(database, tmp_path):
    control, first, transcript, expected_anchor = await _staged_root(
        database, tmp_path, stage=False
    )
    call = ToolCall(id="original-call", function=ToolFunction("terminal_exec", "{}"))
    invocations = []

    async def invoke():
        invocations.append(call.id)
        return json.dumps({"ok": True, "data": {"run_id": "original-run", "pending": False}})

    transcript.append(ChatMessage("assistant", None, tool_calls=(call,)))
    await first.save("response", (call,))
    result = await invoke_tool(first, call, invoke)
    key = first.call_key(call.id)
    transcript.append_result(call.id, result)
    await first.save("paired")
    await first.summary_source()
    await first.stage_compaction(None, retained_public=expected_anchor[-2:])
    resumed = WorkSession(control, first.contract)
    restored = await resumed.restore(TurnTranscript((ChatMessage("user", "new wakeup"),)))
    assert invocations == [call.id]
    assert restored.request() == transcript.request()
    assert await resumed.journal.effect_state(key) == "accepted"
    assert await resumed.journal.effect_result(key) == result
    assert (await control.repository.get(control.current["id"]))["model_requests"] == 3
    await control.repository.release(control.lease)


@pytest.mark.parametrize(
    "damage",
    ["missing_value", "wrong_count", "boolean_count", "unknown_role", "nontext", "tool", "opaque"],
)
async def test_staged_anchor_real_corruption_still_blocks_recovery(database, tmp_path, damage):
    control, first, _, _ = await _staged_root(database, tmp_path)
    identity = control.current["id"]
    async with database.sessions() as reader:
        raw = await reader.scalar(
            select(journal.c.payload_json).where(journal.c.work_id == identity)
        )
    payload = await first.journal.objects.hydrate(json.loads(raw))
    anchor = payload["metadata"]["compaction_anchor"]
    last = anchor["items"][-1]
    if damage == "missing_value":
        del last["value"]
    elif damage == "wrong_count":
        anchor["messages_count"] -= 1
    elif damage == "boolean_count":
        anchor["messages_count"] = True
    elif damage == "unknown_role":
        last["value"]["role"] = "invalid"
    elif damage == "nontext":
        last["value"]["content"] = {"invalid": "not text"}
    elif damage == "tool":
        last["value"]["tool_calls"] = [
            {"id": "unpaired", "type": "function", "function": {"name": "exec", "arguments": "{}"}}
        ]
    else:
        last["value"]["response_item"] = {
            "provider": "gemini",
            "protocol": "gemini",
            "payload": {"private": "opaque"},
        }
    async with database.sessions() as writer, writer.begin():
        await writer.execute(
            update(journal)
            .where(journal.c.work_id == identity)
            .values(payload_json=json.dumps(payload))
        )
    with pytest.raises(JournalUnavailable, match="work_compaction_anchor_corrupt"):
        await WorkSession(control, first.contract).restore(
            TurnTranscript((ChatMessage("user", "new wakeup"),))
        )
    assert (await control.repository.get(identity))["model_requests"] == 3
    await control.repository.release(control.lease)


async def test_public_tail_anchor_does_not_bypass_generation_fence(database, tmp_path):
    control, first, _, _ = await _staged_root(database, tmp_path)
    async with database.sessions() as writer, writer.begin():
        await writer.execute(
            update(CanonicalConversationModel)
            .where(CanonicalConversationModel.id == control.lease.conversation_id)
            .values(generation=control.lease.generation + 1)
        )
    with pytest.raises(WorkConflict, match="work_source_generation_changed"):
        await WorkSession(control, first.contract).restore(
            TurnTranscript((ChatMessage("user", "new wakeup"),))
        )
    await control.repository.release(control.lease)


@pytest.mark.parametrize("include_directives", [False, True])
async def test_partial_summary_recovers_original_work_without_format_policies(
    database, tmp_path, include_directives
):
    control, first, original, _ = await _staged_root(database, tmp_path, stage=False)
    identity = control.current["id"]
    first.progress["task_material"] = {
        "directives": [{"id": "old-directive", "text": "old requirement", "refs": ["goal"]}]
    }
    source = json.loads(await first.summary_source())
    summary = {"pending": [{"text": "still checking", "refs": ["goal", "goal"], "future": True}]}
    if include_directives:
        record_ref = next(ref for ref in source["source_refs"] if ref.startswith("record:"))
        directive = {"text": "retain this observation", "refs": [record_ref]}
        summary.update(version=2, task_directives=[directive, directive])
    await first.compact(json.dumps(summary), ceiling_tokens=128000)
    assert first.transcript.chain_id != original.chain_id
    capsule = json.loads(first.transcript.request().messages[-1].content)
    assert capsule["summary"]["pending"][0]["refs"] == ["goal", "goal"]
    assert capsule["summary"]["completed"] == []
    assert capsule["task_material"]["raw_inputs_retained"]
    assert capsule["previous_protocol_ref"]
    resumed = WorkSession(control, first.contract)
    restored = await resumed.restore(TurnTranscript((ChatMessage("user", "new wakeup"),)))
    recovered = json.loads(restored.request().messages[-1].content)
    assert recovered["task_material"]["directives"] == capsule["task_material"]["directives"]
    assert any(item["id"] == "old-directive" for item in recovered["task_material"]["directives"])
    assert (
        recovered["task_material"]["paid_observations"]["pending"] == capsule["summary"]["pending"]
    )
    current = await control.repository.get(identity)
    assert current["goal"] == "finish the original audit"
    assert (current["model_requests"], current["tool_calls"]) == (3, 1)
    await control.repository.release(control.lease)


@pytest.mark.parametrize("covered_prefix", [0, 2])
@pytest.mark.parametrize("provider_incomplete", [False, True])
async def test_partial_summary_keeps_uncovered_inputs_across_paid_pages_and_business_resume(
    database, tmp_path, covered_prefix, provider_incomplete
):
    from tests.support.work_compaction_capacity_helpers import _runtime

    from qq_ai_bot.domain.messages import ChatRequest, ChatResponse, ModelResponseStatus
    from qq_ai_bot.llm.fake import FakeLLMProvider
    from qq_ai_bot.model_runtime.models import ModelExecutionPriority

    control, first, _, _ = await _staged_root(database, tmp_path, stage=False)
    identity = control.current["id"]
    first.progress["task_material"] = {
        "directives": [
            {"id": "old-directive", "text": "keep the original restriction", "refs": ["goal"]}
        ]
    }
    original_request = await first._original_request()
    input_ids = []
    for index in range(19):
        input_id = await control.repository.enqueue(
            control.lease.conversation_id,
            control.lease.generation,
            f"partial-summary-input:{index}",
            kind="message",
            work_id=identity,
            ready=False,
        )
        assert await control.repository.prepare_input(
            input_id, {"text": f"original requirement {index}"}
        )
        input_ids.append(input_id)
    await control.repository.stage(control.lease, input_ids, "original-inputs")
    await control.repository.consume(control.lease, "original-inputs")
    supplied_pages = []

    def respond(request):
        source = json.loads(request.messages[-1].content)
        supplied_pages.append([item["input_id"] for item in source["task_inputs"]])
        references = input_ids[:covered_prefix] if len(supplied_pages) == 1 else input_ids[16:]
        content = json.dumps(
            {
                "pending": [
                    {
                        "text": "still checking",
                        "refs": ["goal", *(f"input:{item}" for item in references)],
                    }
                ]
            }
        )
        return ChatResponse(
            content=content,
            latency_seconds=0,
            status=ModelResponseStatus.INCOMPLETE
            if provider_incomplete
            else ModelResponseStatus.COMPLETED,
            incomplete_reason="max_output_tokens" if provider_incomplete else None,
        )

    provider = FakeLLMProvider(respond)
    initial = first.compaction_anchor.request().messages
    runner, runtime = await _runtime(database, control, initial, provider)
    candidate = await runner._compact_work(
        runtime,
        ModelExecutionPriority.FOREGROUND,
        128000,
        ChatRequest(messages=first.transcript.request().messages, max_output_tokens=8192),
    )
    assert supplied_pages == [input_ids[:16], input_ids[16:]]
    capsule = json.loads(candidate.request().messages[-1].content)
    material = capsule["task_material"]
    assert material["covered_input_id"] == (input_ids[covered_prefix - 1] if covered_prefix else 0)
    assert {item["input_id"] for item in material["recent_inputs"]} == set(
        input_ids[covered_prefix:]
    )
    assert material["directives"][0]["text"] == "keep the original restriction"
    assert material["original_request_ref"] is None
    assert "paid_observations" not in material

    resumed = WorkSession(control, first.contract)
    control.session = resumed
    restored = await resumed.restore(TurnTranscript((ChatMessage("user", "fresh business input"),)))
    current_material = json.loads(restored.request().messages[-1].content)
    assert current_material["original_request"] == original_request
    assert current_material["original_inputs"] == []  # Already present once in task_material.
    assert current_material["task_material"]["recent_inputs"] == material["recent_inputs"]
    for index in range(covered_prefix, 19):
        assert restored.request().messages[-1].content.count(f'"original requirement {index}"') == 1
    assert (
        current_material["task_material"]["paid_observations"]["pending"]
        == capsule["summary"]["pending"]
    )
    saved = await control.repository.get(identity)
    assert (saved["model_requests"], saved["tool_calls"]) == (5, 1)
    await control.repository.release(control.lease)


async def test_truncated_json_summary_keeps_original_paired_work_and_paid_budget(
    database, tmp_path
):
    from tests.support.work_compaction_capacity_helpers import _runtime

    from qq_ai_bot.domain.messages import ChatRequest, ChatResponse, ModelResponseStatus
    from qq_ai_bot.llm.fake import FakeLLMProvider
    from qq_ai_bot.model_runtime.models import ModelExecutionPriority
    from qq_ai_bot.runtime.work_repository import WorkCapacityError

    control, first, original, _ = await _staged_root(database, tmp_path, stage=False)
    before = original.request()
    provider = FakeLLMProvider(
        lambda request: ChatResponse(
            content='{"pending":[',
            latency_seconds=0,
            status=ModelResponseStatus.INCOMPLETE,
            incomplete_reason="max_output_tokens",
        )
    )
    runner, runtime = await _runtime(
        database, control, first.compaction_anchor.request().messages, provider
    )
    with pytest.raises(WorkCapacityError, match="work_compaction_invalid_structure"):
        await runner._compact_work(
            runtime,
            ModelExecutionPriority.FOREGROUND,
            128000,
            ChatRequest(messages=first.transcript.request().messages, max_output_tokens=8192),
        )
    assert first.transcript.request() == before
    assert first.transcript.chain_id == original.chain_id
    assert len(provider.requests) == 1
    saved = await control.repository.get(control.current["id"])
    assert (saved["model_requests"], saved["tool_calls"]) == (4, 1)
    await control.repository.release(control.lease)


@pytest.mark.parametrize("old_index", [1, -1])
@pytest.mark.parametrize("clear_completed", [False, True])
async def test_second_compaction_keeps_paid_observation_on_its_original_chain(
    database, tmp_path, old_index, clear_completed
):
    control, first, transcript, _ = await _staged_root(database, tmp_path, stage=False)
    call = ToolCall(id="original-paid-call", function=ToolFunction("terminal_exec", "{}"))
    invocations = []

    async def invoke():
        invocations.append(call.id)
        return json.dumps({"ok": True, "data": {"run_id": "original-paid-run", "pending": False}})

    transcript.append(ChatMessage("assistant", None, tool_calls=(call,)))
    await first.save("response", (call,))
    result = await invoke_tool(first, call, invoke)
    key = first.call_key(call.id)
    transcript.append_result(call.id, result)
    for index in range(30):
        transcript.append(ChatMessage("assistant", f"original research record {index}"))
    await first.save("paired")
    source = json.loads(await first.summary_source())
    old_chain = source["chain_id"]
    index = source["record_source_indices"][old_index]
    original_ref = f"record:{old_chain}:{index}"
    assert original_ref in source["source_refs"]
    completed = [{"text": "finished the original check", "refs": [original_ref]}]
    artifacts = [{"text": "retained original finding", "refs": [original_ref]}]
    summary = json.dumps(
        {
            "pending": [{"text": "keep checking the original record", "refs": [original_ref]}],
            "completed": completed,
            "artifacts": artifacts,
        }
    )
    await first.compact(summary, ceiling_tokens=128000)

    resumed = WorkSession(control, first.contract)
    control.session = resumed
    restored = await resumed.restore(
        TurnTranscript(
            (ChatMessage("system", "fixed contract"), ChatMessage("user", "new business source"))
        )
    )
    assert restored.chain_id != old_chain
    source = json.loads(await resumed.summary_source(fits=lambda raw: len(raw) < 100000))
    assert original_ref in source["source_refs"]
    assert f"record:{restored.chain_id}:1" in source["source_refs"]
    assert (
        source["records"][source["record_source_indices"].index(1)]["content"]
        == "new business source"
    )
    assert original_ref != f"record:{restored.chain_id}:{index}"
    update = {"pending": [{"text": "the new pending check", "refs": [original_ref]}]}
    if clear_completed:
        update["completed"] = []
    summary = json.dumps(update)
    assert await resumed.next_summary_source(summary, fits=lambda raw: len(raw) < 100000) is None
    candidate = await resumed.compact(summary, ceiling_tokens=128000)
    capsule = json.loads(candidate.request().messages[-1].content)
    assert capsule["summary"]["pending"][0]["refs"] == [original_ref]
    assert capsule["summary"]["pending"][0]["text"] == "the new pending check"
    assert capsule["summary"]["completed"] == ([] if clear_completed else completed)
    assert capsule["summary"]["artifacts"] == artifacts
    assert "paid_observations" not in capsule["task_material"]
    assert invocations == [call.id]
    assert await resumed.journal.effect_state(key) == "accepted"
    assert await resumed.journal.effect_result(key) == result
    assert (await control.repository.get(control.current["id"]))["model_requests"] == 3
    restored_again = await WorkSession(control, first.contract).restore(
        TurnTranscript((ChatMessage("user", "another business wakeup"),))
    )
    paid = json.loads(restored_again.request().messages[-1].content)["task_material"][
        "paid_observations"
    ]
    assert paid["completed"] == capsule["summary"]["completed"]
    assert paid["artifacts"] == artifacts
    assert paid["pending"] == update["pending"]
    await control.repository.release(control.lease)


async def test_optional_note_does_not_reject_repeated_references_or_extra_metadata(
    database, tmp_path
):
    from qq_ai_bot.runtime.work_context_note import visible_context_note

    control, _, _, _ = await _staged_root(database, tmp_path, stage=False, bind_access=True)
    await control.update_context_note(
        {"version": 2, "facts": [{"text": "", "refs": ["goal", "goal"]}], "future": True},
        "original-note",
    )
    note = await visible_context_note(control)
    assert note["facts"] == [{"text": "", "refs": ["goal", "goal"]}]
    assert note["version"] == 2
    assert (await control.repository.get(control.current["id"]))["model_requests"] == 3
    await control.repository.release(control.lease)
