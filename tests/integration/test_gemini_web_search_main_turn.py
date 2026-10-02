"""Exercise Gemini search through the real main Agent entry and tool loop."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from tests.conftest import MemorySender, build_harness, make_settings
from tests.fakes import FakeWebSearchProvider
from tests.support.fixed_contract_fixture import bind_main_contract
from tests.support.social_identity_cases import social_env

from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.domain.messages import (
    InboundMessage,
    OutboundMessage,
    ReasoningEffort,
    SenderIdentity,
)
from qq_ai_bot.llm.gemini import GeminiProvider
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.web_repository import WebSearchSourceRepository
from qq_ai_bot.web.bridge_state import BridgeState
from qq_ai_bot.web.gemini_bridge import GeminiSearchBridge
from qq_ai_bot.web.models import WebMode, WebSearchSource

SOURCE_URL = "https://docs.python.org/3/library/asyncio-task.html"
QUESTION = "请联网查 Python 官方文档，说明 TaskGroup 与 gather 的失败处理差异，并附来源。"
SEARCH_CALL_ID = "test-search-call-1"
SEND_CALL_ID = "test-send-call-2"


def _gemini_response(*parts: dict, response_id: str, grounding: dict | None = None) -> dict:
    candidate: dict = {"finishReason": "STOP", "content": {"role": "model", "parts": list(parts)}}
    if grounding is not None:
        candidate["groundingMetadata"] = grounding
    return {
        "responseId": response_id,
        "candidates": [candidate],
        "usageMetadata": {
            "promptTokenCount": 180,
            "candidatesTokenCount": 30,
            "totalTokenCount": 210,
        },
    }


@pytest.mark.asyncio
async def test_gemini_search_bridge_main_turn_with_trusted_receipt(database: Database, tmp_path):
    """The protocol mock controls model choice; all Yuki entry/tool code is real."""
    identity = await social_env(database, tmp_path)
    main_wires: list[dict] = []
    search_wires: list[dict] = []
    send_receipt: dict = {}

    def main_response(request: httpx.Request) -> httpx.Response:
        wire = json.loads(request.content)
        main_wires.append(wire)
        declarations = wire["tools"][0]["functionDeclarations"]
        names = [item["name"] for item in declarations]
        assert "web_search" in names and "send_message" in names
        assert "request_tools" not in names
        assert "googleSearch" not in json.dumps(wire["tools"])
        assert wire["toolConfig"]["functionCallingConfig"]["mode"] == "AUTO"
        assert all(item["role"] in {"user", "model"} for item in wire["contents"])
        if len(main_wires) == 1:
            assert QUESTION in json.dumps(wire["contents"], ensure_ascii=False)
            return httpx.Response(
                200,
                json=_gemini_response(
                    {
                        "functionCall": {
                            "name": "web_search",
                            "args": {
                                "query": "Python asyncio TaskGroup gather failure official docs"
                            },
                            "id": SEARCH_CALL_ID,
                        }
                    },
                    response_id="main-search-response",
                ),
            )
        receipts = [
            part["functionResponse"]
            for item in wire["contents"]
            for part in item["parts"]
            if "functionResponse" in part
        ]
        assert receipts[0]["id"] == SEARCH_CALL_ID
        assert receipts[0]["name"] == "web_search"
        search_result = json.loads(receipts[0]["response"]["output"])
        assert search_result["ok"] is True, search_result
        assert search_result["evidence_state"]["query_status"] == "success"
        assert search_result["evidence_state"]["source_refs"]
        assert search_result["data"]["sources"][0]["url"] == SOURCE_URL
        assert "invented.example" not in json.dumps(search_result)
        if len(main_wires) == 2:
            assert [item["name"] for item in declarations] == [
                item["name"] for item in main_wires[0]["tools"][0]["functionDeclarations"]
            ]
            return httpx.Response(
                200,
                json=_gemini_response(
                    {
                        "functionCall": {
                            "name": "send_message",
                            "args": {
                                "text": (
                                    "按官方文档，TaskGroup 会取消同组其余任务；"
                                    f"gather 默认不会这样做。来源：{SOURCE_URL}"
                                )
                            },
                            "id": SEND_CALL_ID,
                        }
                    },
                    response_id="main-send-response",
                ),
            )
        assert receipts[1]["id"] == SEND_CALL_ID
        assert receipts[1]["name"] == "send_message"
        send_receipt.update(json.loads(receipts[1]["response"]["output"]))
        return httpx.Response(
            200, json=_gemini_response({"text": "已完成"}, response_id="main-final")
        )

    def search_response(request: httpx.Request) -> httpx.Response:
        wire = json.loads(request.content)
        search_wires.append(wire)
        assert wire["tools"] == [{"googleSearch": {}}]
        assert "functionDeclarations" not in json.dumps(wire)
        assert QUESTION not in json.dumps(wire, ensure_ascii=False)
        return httpx.Response(
            200,
            json=_gemini_response(
                {"text": "未经验证的正文 URL：https://invented.example/unsupported"},
                response_id="search-bridge-response",
                grounding={
                    "webSearchQueries": ["Python asyncio TaskGroup gather failure official docs"],
                    "groundingChunks": [
                        {"web": {"uri": SOURCE_URL, "title": "Python asyncio docs"}}
                    ],
                },
            ),
        )

    main_client = httpx.AsyncClient(
        base_url="https://example.invalid/v1beta/", transport=httpx.MockTransport(main_response)
    )
    search_client = httpx.AsyncClient(
        base_url="https://example.invalid/v1beta/", transport=httpx.MockTransport(search_response)
    )
    main = GeminiProvider(
        base_url="https://example.invalid/v1beta/",
        api_key="test-only",
        timeout_seconds=5,
        max_retries=0,
        client=main_client,
    )
    search_model = GeminiProvider(
        base_url="https://example.invalid/v1beta/",
        api_key="test-only",
        timeout_seconds=5,
        max_retries=0,
        client=search_client,
    )
    page = WebSearchSource(
        "python-docs",
        "Python asyncio docs",
        SOURCE_URL,
        "docs.python.org",
        "TaskGroup cancels remaining tasks after a task fails.",
        "Official asyncio task documentation.",
    )
    fallback = FakeWebSearchProvider(extracted={SOURCE_URL: page})
    bridge = GeminiSearchBridge(
        profile=SimpleNamespace(
            id="test-gemini",
            provider="gemini",
            model="gemini-3.8-flash",
            base_url="https://example.invalid/v1beta/",
            default_max_output_tokens=2048,
            max_output_tokens_limit=None,
            thinking_enabled=True,
            reasoning_effort=ReasoningEffort.LOW,
            wire_options=None,
        ),
        credential="test-only",
        provider=search_model,
        state=BridgeState(tmp_path / "bridge-cache.db"),
        fallback=fallback,
    )
    settings = make_settings(
        database.url,
        llm_model="gemini-3.8-flash",
        enabled_groups_csv="20001",
        web_enabled=False,
        web_mode=WebMode.BOTH,
        tavily_api_key="test-only",
    )
    harness = build_harness(database, settings, main, web_provider=bridge)
    bind_main_contract(harness, tmp_path)
    sender = MemorySender()

    class IsolatedSocialService:
        def __init__(self):
            self.database = database
            self.context = None

        async def execute(self, name, arguments, context):
            self.context = context
            assert name == "send_message"
            receipt = await sender.send(OutboundMessage(text=arguments["text"]))
            return {
                "status": "succeeded",
                "sent_messages": 1,
                "platform_reference": receipt.platform_message_id,
            }

    social = IsolatedSocialService()
    harness.processor._chat._tools.social_service = social
    inbound = InboundMessage(
        message_id="isolated-search",
        event_type="message:test",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity("10001"),
        text=QUESTION,
        raw_text=QUESTION,
        bot_user_id="80001",
        group_id="20001",
        mentions_bot=True,
        segments=({"type": "text", "data": {"text": QUESTION}},),
        conversation_id=identity.context.conversation_id,
        legacy_conversation_key=ConversationScope.group("80001", "20001").key,
        person_id=identity.person,
        space_id=identity.space,
        presence_id=identity.presence,
    )
    try:
        result = await harness.processor.handle(inbound, sender)
    finally:
        await bridge.close()
        await main.close()
        await main_client.aclose()
        await search_client.aclose()

    assert result.reason == "chat"
    assert len(main_wires) == 3
    assert len(search_wires) == 1
    assert not fallback.search_requests
    assert fallback.extract_requests == [
        (SOURCE_URL, "Python asyncio TaskGroup gather failure official docs")
    ]
    assert len(sender.messages) == 1, send_receipt
    assert SOURCE_URL in sender.messages[0].text
    assert not any(action.startswith("send_") for action, _ in identity.bot.calls)
    source_event = await harness.ledger.find_by_platform_message(
        bot_user_id="80001", platform_message_id="isolated-search"
    )
    assert source_event is not None
    assert social.context.trigger_event_id == source_event.id
    assert social.context.origin == "social_tool"
    assert social.context.call_id == SEND_CALL_ID
    assert social.context.actor.user_id == "10001"
    assert social.context.actor.event_id == source_event.id
    assert social.context.actor.source_key == f"event:{source_event.id}"
    stored = await WebSearchSourceRepository(database).for_trigger(
        conversation_key=ConversationScope.group("80001", "20001").key,
        trigger_event_id=source_event.id,
    )
    assert [item.url for item in stored] == [SOURCE_URL]
