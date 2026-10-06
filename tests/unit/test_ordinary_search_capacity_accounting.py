"""Ordinary Gemini search archives its full source before returning a manifest."""

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
from qq_ai_bot.domain.messages import ChatResponse
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.llm.gemini import GeminiProvider
from qq_ai_bot.model_runtime.capacity import estimate_request_tokens, estimate_text_tokens
from qq_ai_bot.persistence.models import ChatEventModel, WebSearchRunModel, WebSearchSourceModel
from qq_ai_bot.runtime.work_schema_v1 import work
from qq_ai_bot.tool_results.access import ArtifactAccess
from qq_ai_bot.tool_results.artifacts import ToolArtifactRepository
from qq_ai_bot.web.models import WebSearchResponse, WebSearchSource


def legacy_estimate(request):
    # The removed asdict policy charged host-only metadata and empty optional fields.
    return estimate_text_tokens(json.dumps(asdict(request), ensure_ascii=False, default=str))


@pytest.mark.asyncio
@pytest.mark.parametrize("large_result", [False, True])
async def test_ordinary_search_keeps_large_sources_external_without_capacity_stop(
    database, tmp_path, large_result
):
    env = await social_env(database, tmp_path)
    body = "搜" * 10915 if large_result else "搜" * 2500 + "a" * 8415
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
    chat._tool_artifacts = ToolArtifactRepository(
        database, tmp_path / "research", retention_seconds=60
    )
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

    observed = fake.requests  # Actual normalized physical provider requests.
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
    assert legacy_estimate(observed[1]) > estimate_request_tokens(observed[1])
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
    # Pure serialization of the actual second request, without another HTTP.
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
    assert candidate_outcome["artifact_handle"]
    assert candidate_outcome["available_operations"] == ["inspect", "get", "search"]
    assert "data" not in candidate_outcome
    access = ArtifactAccess(
        env.context.conversation_id,
        1,
        env.person,
        read_scope=json.dumps(
            {"memory": [], "plugin_id": None, "delegation_id": None}, sort_keys=True
        ),
    )
    original = await chat._tool_artifacts.read(
        candidate_outcome["artifact_handle"],
        limit=100000,
        access=access,
    )
    assert original is not None and original["next_offset"] is None
    archived = json.loads(original["content"])
    assert archived["data"]["sources"][0]["url"] == source.url
    assert archived["data"]["sources"][0]["relevant_content"] == body
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
        assert projection.revision >= 1 and projection.invalidated_reason is None
        assert "test_current_material" not in projection.payload_json
        assert "signature-search" not in projection.payload_json

    # Both source sizes are legitimate research data. The new early archive
    # removes their prompt residency before a continuation can exceed capacity;
    # true hard-overflow stopping remains covered by no_work_capacity_status.
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
    assert call["functionCall"]["id"] == "search" and call["thoughtSignature"] == "signature-search"
    receipt = next(part["functionResponse"] for _, part in parts if "functionResponse" in part)
    assert receipt["id"] == "search" and receipt["name"] == "web_search"
    assert json.loads(receipt["response"]["output"]) == candidate_outcome
