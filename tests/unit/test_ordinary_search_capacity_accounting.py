"""Ordinary Gemini search counts model payload, while retaining its original facts."""

import json
from dataclasses import asdict, replace
from itertools import pairwise
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from tests.conftest import MemorySender, build_harness, make_settings
from tests.fakes import FakeWebSearchProvider
from tests.support.fixed_contract_fixture import bind_main_contract
from tests.support.social_identity_cases import social_env
from tests.unit.test_commands_and_chat import inbound
from tests.unit.test_work_reporting_runner import response, tool
from tests.unit.test_work_reporting_runner_gemini_wire import content_parts, gemini_wire

from qq_ai_bot.conversation.projection_models import PromptProjectionModel
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.domain.messages import ChatMessage, ChatRequest, ChatResponse
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.llm.gemini import GeminiProvider
from qq_ai_bot.model_runtime.capacity import estimate_request_tokens, estimate_text_tokens
from qq_ai_bot.persistence.models import ChatEventModel, WebSearchRunModel, WebSearchSourceModel
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_schema_v1 import work
from qq_ai_bot.web.models import WebSearchResponse, WebSearchSource


def legacy_estimate(request):
    # The removed asdict policy charged host-only metadata and empty optional fields.
    return estimate_text_tokens(json.dumps(asdict(request), ensure_ascii=False, default=str))


@pytest.mark.asyncio
@pytest.mark.parametrize("genuine_overflow", [False, True])
async def test_ordinary_search_continues_with_payload_accounting_but_keeps_real_hard_stop(
    database, tmp_path, monkeypatch, genuine_overflow
):
    env = await social_env(database, tmp_path)
    body = "搜" * 10915 if genuine_overflow else "搜" * 2500 + "a" * 8415
    source = WebSearchSource(
        "eta-source",
        "eta evidence",
        "https://example.com/eta",
        "example.com",
        "verified source",
        body,
    )
    web = FakeWebSearchProvider(response=WebSearchResponse("eta", (source,), "search-1", 0))
    scripted = iter(
        [
            response(tool("web_search", {"query": "eta"}, "search")),
            response(
                tool("send_message", {"text": "查到 eta 的资料。https://example.com/eta"}, "answer")
            ),
            ChatResponse("已发送。", 0),
        ]
    )
    fake = FakeLLMProvider(lambda _: next(scripted))
    harness = build_harness(
        database,
        make_settings(
            database.url,
            runtime_work_enabled=True,
            enabled_groups_csv="20001",
            web_mode="tavily",
            tavily_api_key="synthetic",
            context_window_tokens=96000,
        ),
        fake,
        web_provider=web,
    )
    bind_main_contract(harness, tmp_path)
    chat = harness.processor._chat
    chat._tools.social_service = env.service
    test_case = SimpleNamespace(provider=fake, runner=chat.runtime.runner)
    client, captured = gemini_wire(test_case)
    chat._models = chat.runtime.runner._models

    # Real ledger rows produce many small ordinary history messages, each of which
    # the old estimator expanded into a dataclass containing unused fields.
    for index in range(240):
        await harness.scoped_events.append(
            scope=ConversationScope.group("80001", "20001"),
            platform_message_id=f"small-history-{index}",
            sender_user_id="10001" if index % 2 == 0 else "80001",
            direction="inbound" if index % 2 == 0 else "outbound",
            content=f"small message {index}",
        )

    prepared = []
    original_run = chat.runtime.main_turns._run_prepared
    original_state = WorkControl.runtime_state

    async def run_prepared(messages, runtime, backend):
        prepared.append((messages, runtime))
        return await original_run(messages, runtime, backend)

    async def runtime_state(control):
        state = {**await original_state(control), "test_current_material": ""}
        messages, runtime = prepared[-1]
        content = "[运行状态资料，不增加任何权限] " + json.dumps(
            state, ensure_ascii=False, separators=(",", ":")
        )
        request = ChatRequest(
            messages=(*messages, ChatMessage("user", content)),
            model=runtime.runtime_config.llm.model or "fake",
            temperature=runtime.runtime_config.llm.temperature,
            max_output_tokens=runtime.runtime_config.llm.max_output_tokens,
            thinking_enabled=runtime.runtime_config.llm.thinking_enabled,
            tools=await chat.runtime.runner.main_contract.definitions(),
            tool_choice="auto",
        )
        # Calibrate only fixture material, not the window, guard or estimator:
        # reproduce an admitted old ~88.7k request at the same 96k ceiling.
        baseline = legacy_estimate(request)
        assert baseline < 88700
        state["test_current_material"] = "x" * ((88700 - baseline) * 3)
        return state

    monkeypatch.setattr(chat.runtime.main_turns, "_run_prepared", run_prepared)
    monkeypatch.setattr(WorkControl, "runtime_state", runtime_state)
    observed = []

    def measure(request):
        observed.append(request)
        return estimate_request_tokens(request)

    monkeypatch.setattr("qq_ai_bot.services.agent_runner.estimate_request_tokens", measure)
    message = replace(
        inbound(
            "搜索一下 eta",
            message_id="ordinary-search-capacity",
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

    assert len(web.search_requests) == 1 and not web.extract_requests
    assert 88600 <= legacy_estimate(observed[0]) <= 88900
    assert legacy_estimate(observed[1]) > 96000
    assert estimate_request_tokens(observed[0]) < 96000
    assert len(observed[0].messages) >= 200
    assert legacy_estimate(observed[0]) - estimate_request_tokens(observed[0]) > 10000
    assert len(observed[1].continuation_items) > 0
    assert observed[1].request_chain_id == observed[0].request_chain_id
    serializer = GeminiProvider(
        base_url="https://gemini.invalid",
        api_key="synthetic",
        timeout_seconds=2,
        max_retries=0,
        client=client,
    )
    # This is pure serialization of the rejected candidate, without another HTTP.
    candidate_payload = serializer._build_payload(observed[1])
    candidate_wire_tokens = estimate_text_tokens(json.dumps(candidate_payload, ensure_ascii=False))
    candidate_parts = content_parts(candidate_payload)
    candidate_call = next(part for _, part in candidate_parts if "functionCall" in part)
    assert candidate_call["thoughtSignature"] == "signature-search"
    candidate_receipt = next(
        part["functionResponse"] for _, part in candidate_parts if "functionResponse" in part
    )
    assert candidate_receipt["id"] == "search" and candidate_receipt["name"] == "web_search"
    candidate_outcome = json.loads(candidate_receipt["response"]["output"])
    assert candidate_outcome["ok"] is True
    assert candidate_outcome["data"]["sources"][0]["url"] == source.url
    assert candidate_outcome["data"]["sources"][0]["relevant_content"] == body
    for earlier, later in pairwise(captured):
        for field in ("systemInstruction", "tools", "toolConfig", "generationConfig"):
            assert later[field] == earlier[field]
        previous = content_parts(earlier)
        assert content_parts(later)[: len(previous)] == previous

    async with database.sessions() as reader:
        assert not (await reader.execute(select(work))).all()
        search_run = (await reader.scalars(select(WebSearchRunModel))).one()
        saved_source = (await reader.scalars(select(WebSearchSourceModel))).one()
        trigger = await reader.get(ChatEventModel, search_run.trigger_event_id)
        projection = (await reader.scalars(select(PromptProjectionModel))).one()
        assert trigger.content == "搜索一下 eta"
        assert search_run.canonical_conversation_id == env.context.conversation_id
        assert saved_source.url == source.url and saved_source.run_id == search_run.id
        assert projection.revision == 1 and projection.invalidated_reason is None
        assert "test_current_material" in projection.payload_json
        assert "signature-search" not in projection.payload_json

    if genuine_overflow:
        assert result.reason == "capacity_failure"
        assert estimate_request_tokens(observed[1]) > 96000
        assert candidate_wire_tokens > 96000
        assert len(captured) == 1 and len(observed) == 2
        assert not any(action == "send_group_msg" for action, _ in env.bot.calls)
        assert len(sender.messages) == 1 and "未完整完成" in sender.messages[0].text
    else:
        assert result.reason == "chat" and len(captured) == len(observed) == 3
        assert candidate_wire_tokens < 96000
        assert candidate_payload == captured[1]
        assert all(estimate_request_tokens(request) <= 96000 for request in observed)
        assert all(
            estimate_text_tokens(json.dumps(payload, ensure_ascii=False)) < 96000
            for payload in captured
        )
        assert sum(action == "send_group_msg" for action, _ in env.bot.calls) == 1
        parts = content_parts(captured[1])
        call = next(part for _, part in parts if "functionCall" in part)
        assert (
            call["functionCall"]["id"] == "search"
            and call["thoughtSignature"] == "signature-search"
        )
        receipt = next(part["functionResponse"] for _, part in parts if "functionResponse" in part)
        assert receipt["id"] == "search" and receipt["name"] == "web_search"
        outcome = json.loads(receipt["response"]["output"])
        assert outcome["ok"] is True
        assert outcome["data"]["sources"][0]["url"] == source.url
        assert outcome["data"]["sources"][0]["relevant_content"] == body
