"""Ordinary paid summaries preserve the chat selection and delivered facts."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from tests.conftest import MemorySender, build_harness, make_settings
from tests.support.fixed_contract_fixture import bind_main_contract
from tests.support.social_identity_cases import social_env
from tests.unit.test_commands_and_chat import inbound
from tests.unit.test_work_reporting_runner_gemini_wire import content_parts, gemini_wire

from qq_ai_bot.domain.messages import ChatMessage, ChatRequest, ChatResponse, ToolCall, ToolFunction
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.model_runtime.capacity import estimate_request_tokens
from qq_ai_bot.model_runtime.models import ModelCapability, StructuredOutputMode
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.runtime.work_repository import WorkCapacityError
from qq_ai_bot.runtime.work_schema_v1 import work
from qq_ai_bot.services.ordinary_compaction import compact_ordinary, summarize_records
from qq_ai_bot.services.turn_transcript import TurnTranscript


def summary_response(request):
    source = json.loads(request.messages[-1].content)
    return ChatResponse(
        json.dumps(
            {
                "facts": [{"text": "已核合成资料", "refs": source["source_refs"]}],
                "pending": [],
                "next_steps": [],
            }
        ),
        0,
    )


@pytest.mark.asyncio
async def test_paid_pages_reconstruct_complete_single_large_json_and_keep_previous_refs():
    records = [
        ("record:0", json.dumps({"text": "原件😀" * 3000}, ensure_ascii=False)),
        ("record:1", json.dumps({"data": "later record"})),
    ]
    requests = []

    async def execute(request):
        requests.append(request)
        return summary_response(request)

    result = await summarize_records(
        records,
        main_request=ChatRequest(messages=()),
        structured_mode=StructuredOutputMode.JSON_SCHEMA,
        summary_budget=1800,
        output_tokens=1024,
        prepare=lambda item: item,
        execute=execute,
    )
    assert len(requests) > 2
    reconstructed = {ref: "" for ref, _ in records}
    for index, request in enumerate(requests):
        assert estimate_request_tokens(request) <= 1800
        assert request.tools == request.native_tools == ()
        source = json.loads(request.messages[-1].content)
        for piece in source["records"]:
            reconstructed[piece["ref"]] += piece["text"]
        if index:
            assert source["previous_summary"]["facts"]
            assert "record:0" in source["source_refs"]
    assert reconstructed == dict(records)
    assert set(result["facts"][0]["refs"]) == {"record:0", "record:1"}
    assert len({request.request_chain_id for request in requests}) == len(requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["refs", "refs_fenced", "structure", "tool"])
async def test_invalid_paid_summary_preserves_original_paired_transcript(invalid):
    initial = (ChatMessage("system", "fixed contract"), ChatMessage("user", "original request"))
    transcript = TurnTranscript(initial)
    call = ToolCall("read-1", ToolFunction("read", "{}"))
    transcript.append(ChatMessage("assistant", "", tool_calls=(call,)))
    transcript.append_result(call.id, json.dumps({"ok": True, "data": "原件" * 2000}))
    before = transcript.request()
    old_chain = transcript.chain_id
    calls = []

    async def execute(request):
        calls.append(request)
        if invalid == "structure":
            return ChatResponse("{}", 0)
        if invalid == "tool":
            return ChatResponse("", 0, tool_calls=(call,))
        content = json.dumps(
            {
                "facts": [{"text": "cannot cite an absent source", "refs": ["record:unknown"]}],
                "pending": [],
                "next_steps": [],
            }
        )
        return ChatResponse(f"```json\n{content}\n```" if invalid == "refs_fenced" else content, 0)

    with pytest.raises(WorkCapacityError, match=r"ordinary_compaction_(invalid|incomplete)"):
        await compact_ordinary(
            initial,
            transcript,
            main_request=ChatRequest(messages=before.messages, continuation_items=before.items),
            structured_mode=StructuredOutputMode.JSON_SCHEMA,
            summary_budget=20000,
            input_budget=100000,
            output_tokens=1024,
            prepare=lambda item: item,
            execute=execute,
            evidence=[],
        )
    assert len(calls) == 1
    assert transcript.request() == before
    assert transcript.chain_id == old_chain


@pytest.mark.asyncio
async def test_impossible_summary_input_makes_no_paid_request():
    calls = []

    async def execute(request):
        calls.append(request)
        return summary_response(request)

    with pytest.raises(WorkCapacityError, match="ordinary_compaction_source_capacity"):
        await summarize_records(
            [("record:0", "material")],
            main_request=ChatRequest(messages=()),
            structured_mode=StructuredOutputMode.JSON_SCHEMA,
            summary_budget=1,
            output_tokens=1024,
            prepare=lambda item: item,
            execute=execute,
        )
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("shape", "valid"),
    [
        ("json_fence", True),
        ("bare_fence", True),
        ("prefix_explanation", False),
        ("suffix_explanation", False),
        ("multiple_json", False),
        ("truncated", False),
    ],
)
async def test_paid_summary_unwraps_only_entire_single_fence_then_keeps_strict_contract(
    shape, valid
):
    content = json.dumps(
        {"facts": [{"text": "source fact", "refs": ["record:0"]}], "pending": [], "next_steps": []}
    )
    outputs = {
        "json_fence": f"```json\n{content}\n```",
        "bare_fence": f"```\n{content}\n```",
        "prefix_explanation": f"explanation\n```json\n{content}\n```",
        "suffix_explanation": f"```json\n{content}\n```\nexplanation",
        "multiple_json": f"{content}\n{content}",
        "truncated": f"```json\n{content[:-2]}",
    }
    calls = []

    async def execute(request):
        calls.append(request)
        return ChatResponse(outputs[shape], 0)

    arguments = dict(
        main_request=ChatRequest(messages=()),
        structured_mode=StructuredOutputMode.JSON_SCHEMA,
        summary_budget=20000,
        output_tokens=1024,
        prepare=lambda item: item,
        execute=execute,
    )
    if valid:
        result = await summarize_records([("record:0", "source")], **arguments)
        assert result["facts"] == [{"text": "source fact", "refs": ["record:0"]}]
    else:
        with pytest.raises(WorkCapacityError, match="ordinary_compaction_invalid_structure"):
            await summarize_records([("record:0", "source")], **arguments)
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "send_count"),
    [("valid", 1), ("invalid", 1), ("budget", 1), ("valid", 2), ("invalid_repeat", 2)],
)
async def test_real_ordinary_send_paid_compaction_continues_without_work_or_resend(
    database, tmp_path, monkeypatch, mode, send_count
):
    env = await social_env(database, tmp_path)
    main_calls = []
    summary_calls = []

    def respond(request):
        if request.structured_output:
            summary_calls.append(request)
            if mode in {"invalid", "invalid_repeat"}:
                return ChatResponse(
                    json.dumps(
                        {
                            "facts": [{"text": "invalid", "refs": ["missing:source"]}],
                            "pending": [],
                            "next_steps": [],
                        }
                    ),
                    0,
                )
            return summary_response(request)
        main_calls.append(request)
        if len(main_calls) <= send_count:
            final_send = (
                len(main_calls) == 1 if mode == "invalid_repeat" else len(main_calls) == send_count
            )
            call_id = "only-send" if len(main_calls) == 1 else "second-send"
            text = "阶段结果已发送" if len(main_calls) == 1 else "第二阶段结果已发送"
            return ChatResponse(
                "临时分析资料。" * 6500 if final_send else "第一阶段。",
                0,
                tool_calls=(
                    ToolCall(call_id, ToolFunction("send_message", json.dumps({"text": text}))),
                ),
            )
        assert any(
            "ordinary_working_summary" in str(item.content) for item in request.messages
        ) == (mode == "valid")
        return ChatResponse("阶段结果已发送，结束本轮。", 0)

    provider = FakeLLMProvider(respond)
    harness = build_harness(
        database,
        make_settings(
            database.url,
            runtime_work_enabled=True,
            enabled_groups_csv="20001",
            context_window_tokens=524288,
            context_compaction_window_tokens=22000,
            context_compaction_output_tokens=1024,
            agent_max_model_requests=2 if mode == "budget" else 12,
        ),
        provider,
    )
    bind_main_contract(harness, tmp_path)
    chat = harness.processor._chat
    chat._tools.social_service = env.service
    client, captured = gemini_wire(SimpleNamespace(provider=provider, runner=chat.runtime.runner))
    executor = chat.runtime.runner._models
    catalog = executor._router.catalog
    profile = next(iter(catalog.profiles.values()))
    catalog.profiles[profile.id] = profile.model_copy(
        update={"capabilities": profile.capabilities | {ModelCapability.STRUCTURED_OUTPUT}}
    )
    chat._models = chat.runtime.runner._models
    completed_runs = []
    original_run = chat.runtime.runner.run

    async def run(messages, runtime, backend):
        result = await original_run(messages, runtime, backend)
        completed_runs.append(result)
        return result

    monkeypatch.setattr(chat.runtime.runner, "run", run)
    message = replace(
        inbound(
            "检查资料并报告阶段结果",
            message_id="ordinary-compaction",
            user_id="10001",
            group_id="20001",
            mentions_bot=True,
        ),
        bot_user_id="80001",
        conversation_id=env.context.conversation_id,
        legacy_conversation_key="bot:80001:group:20001",
        person_id=env.person,
        space_id=env.space,
        presence_id=env.presence,
    )
    sender = MemorySender()
    try:
        result = await harness.processor.handle(message, sender)
    finally:
        await client.aclose()
    assert result.reason == "chat"
    assert len(main_calls) == send_count + 1
    assert len(summary_calls) == (0 if mode == "budget" else 1)
    assert completed_runs[0].model_requests == len(main_calls) + len(summary_calls)
    assert completed_runs[0].tool_calls_used == send_count
    assert not sender.messages
    assert sum(action == "send_group_msg" for action, _ in env.bot.calls) == send_count
    assert (main_calls[-1].request_chain_id != main_calls[0].request_chain_id) == (mode == "valid")
    assert main_calls[-1].messages[: len(main_calls[0].messages)] == main_calls[0].messages
    assert main_calls[-1].tools == main_calls[0].tools
    assert main_calls[0].tools == await chat.runtime.runner.main_contract.definitions()
    if mode == "valid":
        capsule = json.loads(
            next(
                item.content
                for item in main_calls[-1].messages
                if "ordinary_working_summary" in str(item.content)
            )
        )
        assert capsule["execution_evidence"]
        assert any(item.get("delivered_message") for item in capsule["execution_evidence"])
        assert (
            sum(bool(item.get("delivered_message")) for item in capsule["execution_evidence"])
            == send_count
        )
    first, final = captured[0], captured[-1]
    for field in ("systemInstruction", "tools", "toolConfig", "generationConfig"):
        assert first[field] == final[field]
    assert content_parts(final)[: len(content_parts(first))] == content_parts(first)
    assert ("signature-only-send" not in json.dumps(final)) == (mode == "valid")
    assert all(not request.tools for request in summary_calls)
    async with database.sessions() as reader:
        assert not (await reader.execute(select(work))).all()
        outgoing = (
            await reader.scalars(
                select(ChatEventModel).where(
                    ChatEventModel.canonical_conversation_id == env.context.conversation_id,
                    ChatEventModel.direction == "outbound",
                )
            )
        ).all()
    assert len(outgoing) == send_count
    assert [item.content for item in outgoing] == ["阶段结果已发送", "第二阶段结果已发送"][
        :send_count
    ]
