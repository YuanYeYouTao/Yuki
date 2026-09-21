"""End-to-end controlled web search and backend source display tests."""

from __future__ import annotations

import json

import pytest
from tests.conftest import MemorySender, build_harness, make_settings
from tests.fakes import FakeWebSearchProvider
from tests.support.fixed_contract_fixture import bind_main_contract

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import (
    ChatRequest,
    ChatResponse,
    CitationOrigin,
    InboundMessage,
    NativeToolDefinition,
    NativeToolEvent,
    NativeToolStatus,
    NativeToolType,
    OutboundMessage,
    OutboundSendReceipt,
    ResponseCitation,
    SenderIdentity,
    ToolCall,
    ToolFunction,
)
from qq_ai_bot.llm.base import LLMProvider
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.web_repository import WebSearchSourceRepository
from qq_ai_bot.web.base import WebSearchError
from qq_ai_bot.web.models import WebMode, WebSearchResponse, WebSearchSource


def event(
    text: str,
    *,
    message_id: str,
    user_id: str = "1001",
    group_id: str | None = None,
) -> InboundMessage:
    return InboundMessage(
        message_id=message_id,
        bot_user_id="8000",
        event_type="message:test",
        scope_type=ScopeType.GROUP if group_id else ScopeType.PRIVATE,
        sender=SenderIdentity(user_id=user_id, nickname=f"用户{user_id}"),
        text=text,
        raw_text=text,
        group_id=group_id,
        mentions_bot=group_id is not None,
        segments=({"type": "text", "data": {"text": text}},),
    )


def _request_missing_tool(request: ChatRequest, name: str) -> ChatResponse | None:
    if name in {tool.name for tool in request.tools}:
        return None
    return ChatResponse(
        content="",
        latency_seconds=0,
        tool_calls=(
            ToolCall(
                id=f"request-{name}",
                function=ToolFunction(
                    name="request_tools",
                    arguments=json.dumps(
                        {"query": name, "max_results": 2},
                        ensure_ascii=False,
                    ),
                ),
            ),
        ),
    )


def web_response() -> WebSearchResponse:
    return WebSearchResponse(
        query="最新 DeepSeek 更新",
        sources=(
            WebSearchSource(
                source_id="source-1",
                title="DeepSeek 官方更新",
                url="https://example.com/deepseek-update",
                domain="example.com",
                snippet="官方发布了新版本。",
                relevant_content="官方发布了新版本，并改进了工具调用。",
                provider_score=0.95,
            ),
        ),
        provider_request_id="request-1",
        latency_seconds=0.1,
    )


class WebToolLLM(LLMProvider):
    """Issue web_search, then summarize its structured result."""

    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []

    async def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        missing = _request_missing_tool(request, "web_search")
        if missing is not None:
            return missing
        last = request.messages[-1]
        if last.role != "tool" or "loaded_tools" in (last.content or ""):
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id=f"web-{len(self.requests)}",
                        function=ToolFunction(
                            name="web_search",
                            arguments=json.dumps(
                                {"query": "最新 DeepSeek 更新", "topic": "news"},
                                ensure_ascii=False,
                            ),
                        ),
                    ),
                ),
            )
        result = json.loads(last.content or "{}")
        if result.get("ok"):
            assert result["evidence_state"]["source"] == "web_tool"
            assert result["evidence_state"]["query_status"] == "success"
            assert result["evidence_state"]["source_refs"] == ["source-1"]
            assert result["evidence_state"]["delivery"] == "staged"
        if not result.get("ok"):
            return ChatResponse(content="联网查询暂时失败，请稍后再试。", latency_seconds=0)
        return ChatResponse(content="", latency_seconds=0)


class ToolGatewaySender(MemorySender):
    """Record whether a forbidden post-web OneBot action executes."""

    def __init__(self) -> None:
        super().__init__()
        self.api_calls: list[tuple[str, dict[str, object]]] = []

    async def call_api(self, action: str, params: dict[str, object]) -> object:
        self.api_calls.append((action, params))
        return {"status": "ok"}

    async def send(self, message: OutboundMessage) -> OutboundSendReceipt:
        return await super().send(message)


class WebThenOneBotLLM(LLMProvider):
    """Use an authorized OneBot action after a web lookup."""

    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []
        self._called_onebot = False
        self._web_called = False

    async def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        names = {tool.name for tool in request.tools}
        last = request.messages[-1]
        if last.role != "tool":
            self._web_called = False
        missing = _request_missing_tool(request, "web_search")
        if missing is not None:
            return missing
        if not self._web_called:
            self._web_called = True
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id="web-first",
                        function=ToolFunction(
                            name="web_search",
                            arguments='{"query":"测试网页提示词注入"}',
                        ),
                    ),
                ),
            )
        if "call_onebot_api" not in names:
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id="request-onebot",
                        function=ToolFunction(
                            name="request_tools",
                            arguments=json.dumps({"query": "call_onebot_api", "max_results": 1}),
                        ),
                    ),
                ),
            )
        if not self._called_onebot:
            self._called_onebot = True
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id="authorized-onebot",
                        function=ToolFunction(
                            name="call_onebot_api",
                            arguments=(
                                '{"action":"send_private_msg",'
                                '"params":{"user_id":"12345678","message":"授权发送"}}'
                            ),
                        ),
                    ),
                ),
            )
        return ChatResponse(content="", latency_seconds=0)


class RepeatedWebToolLLM(LLMProvider):
    """Request four web calls so the backend-enforced limit is observable."""

    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []

    async def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        if len(self.requests) <= 4:
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id=f"web-repeat-{len(self.requests)}",
                        function=ToolFunction(
                            name="web_search",
                            arguments=json.dumps({"query": f"搜索 {len(self.requests)}"}),
                        ),
                    ),
                ),
            )
        assert "web_tool_limit_exceeded" in (request.messages[-1].content or "")
        return ChatResponse(content="", latency_seconds=0)


class NativeWebLLM(LLMProvider):
    """Return provider-native events without fabricating a local Function Call."""

    async def complete(self, request: ChatRequest) -> ChatResponse:
        del request
        return ChatResponse(
            content="",
            latency_seconds=0,
            native_tool_events=(
                NativeToolEvent(
                    tool_type=NativeToolType.WEB_SEARCH,
                    call_id="native-search",
                    status=NativeToolStatus.COMPLETED,
                    action_type="search",
                    query="public docs",
                ),
                NativeToolEvent(
                    tool_type=NativeToolType.WEB_SEARCH,
                    call_id="native-open",
                    status=NativeToolStatus.COMPLETED,
                    action_type="open_page",
                    url="https://example.com/native-docs#ws_call_id=test",
                ),
            ),
            citations=(
                ResponseCitation(
                    url="https://example.com/native-docs",
                    title="Native docs",
                    origin=CitationOrigin.ANNOTATION,
                ),
            ),
        )


class NativeSourceFailureThenTavilyLLM(LLMProvider):
    """Use Tavily immediately when the profile cannot expose native tools."""

    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []

    async def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        missing = _request_missing_tool(request, "web_search")
        if missing is not None:
            return missing
        last = request.messages[-1]
        if last.role != "tool" or "loaded_tools" in (last.content or ""):
            assert "web_search" in {tool.name for tool in request.tools}
            assert not request.native_tools
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id="tavily-direct",
                        function=ToolFunction(
                            name="web_search",
                            arguments='{"query":"最新 DeepSeek 更新"}',
                        ),
                    ),
                ),
            )
        assert request.messages[-1].role == "tool"
        return ChatResponse(content="", latency_seconds=0)


class DomainRoutedTavilyLLM(LLMProvider):
    """Use Tavily immediately when an explicit URL matches a routing rule."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.requests: list[ChatRequest] = []

    async def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        missing = _request_missing_tool(request, "read_webpage")
        if missing is not None:
            return missing
        last = request.messages[-1]
        if last.role != "tool" or "loaded_tools" in (last.content or ""):
            assert not request.native_tools
            assert "read_webpage" in {tool.name for tool in request.tools}
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id="domain-routed-read",
                        function=ToolFunction(
                            name="read_webpage",
                            arguments=json.dumps(
                                {"url": self.url, "question": "这个项目是什么"},
                                ensure_ascii=False,
                            ),
                        ),
                    ),
                ),
            )
        payload = json.loads(request.messages[-1].content or "{}")
        assert payload["ok"] is True
        return ChatResponse(content="", latency_seconds=0)


class TargetMissThenTavilyLLM(LLMProvider):
    """Read through Tavily when native tools are unavailable for the profile."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.requests: list[ChatRequest] = []

    async def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        missing = _request_missing_tool(request, "read_webpage")
        if missing is not None:
            return missing
        last = request.messages[-1]
        if last.role != "tool" or "loaded_tools" in (last.content or ""):
            assert not request.native_tools
            assert "read_webpage" in {tool.name for tool in request.tools}
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id="target-miss-read",
                        function=ToolFunction(
                            name="read_webpage",
                            arguments=json.dumps({"url": self.url}, ensure_ascii=False),
                        ),
                    ),
                ),
            )
        return ChatResponse(content="", latency_seconds=0)


def web_settings(database: Database):
    return make_settings(
        database.url,
        web_enabled=True,
        web_mode=WebMode.TAVILY,
        tavily_api_key="test-placeholder",
    )


@pytest.mark.asyncio
async def test_native_web_sources_are_persisted_without_implicit_rendering(
    database: Database,
) -> None:
    settings = make_settings(
        database.url,
        web_enabled=False,
        web_mode=WebMode.NATIVE,
        tavily_api_key="",
    )
    harness = build_harness(database, settings, NativeWebLLM())
    sender = MemorySender()

    result = await harness.processor.handle(
        event("请联网确认并附上来源。", message_id="native-visible"),
        sender,
    )

    assert result.sent_messages == 0
    assert not sender.messages
    source = await harness.ledger.find_by_platform_message(
        bot_user_id="8000", platform_message_id="native-visible"
    )
    assert source is not None
    stored = await WebSearchSourceRepository(database).for_trigger(
        conversation_key="bot:8000:private:1001",
        trigger_event_id=source.id,
    )
    assert [source.url for source in stored] == ["https://example.com/native-docs"]


@pytest.mark.asyncio
async def test_chat_completions_profile_can_request_tavily_without_native(
    database: Database,
    tmp_path,
) -> None:
    settings = make_settings(
        database.url,
        web_enabled=False,
        web_mode=WebMode.BOTH,
        tavily_api_key="test-placeholder",
        tooling_first_round_pin_ids_csv="",
    )
    llm = NativeSourceFailureThenTavilyLLM()
    harness = build_harness(
        database,
        settings,
        llm,
        web_provider=FakeWebSearchProvider(response=web_response()),
    )
    bind_main_contract(harness, tmp_path)
    sender = MemorySender()

    result = await harness.processor.handle(
        event("请联网确认并附上来源。", message_id="native-fallback-visible"),
        sender,
    )

    assert result.sent_messages == 0
    assert not sender.messages
    assert len(llm.requests) == 2
    assert llm.requests[0].tools == llm.requests[1].tools


@pytest.mark.asyncio
async def test_domain_text_does_not_fabricate_a_deployment_route(
    database: Database,
    tmp_path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO")
    target_url = "https://github.com/YuanYeYouTao/Yuki-QQbot"
    source = WebSearchSource(
        source_id="github-yuki",
        title="Yuki-QQbot",
        url=target_url,
        domain="github.com",
        snippet="Yuki QQ bot repository",
        relevant_content="Yuki-QQbot is a QQ AI Agent project.",
    )
    settings = make_settings(
        database.url,
        web_enabled=False,
        web_mode=WebMode.BOTH,
        tavily_api_key="test-placeholder",
        tooling_first_round_pin_ids_csv="",
    )
    llm = DomainRoutedTavilyLLM(target_url)
    web = FakeWebSearchProvider(extracted={target_url: source})
    harness = build_harness(database, settings, llm, web_provider=web)
    bind_main_contract(harness, tmp_path)
    sender = MemorySender()

    result = await harness.processor.handle(
        event(f"请读取 {target_url} 并告诉我这个项目是什么。", message_id="domain-route"),
        sender,
    )

    assert result.reason == "chat"
    assert not sender.messages
    assert web.extract_requests == [(target_url, "这个项目是什么")]
    assert len(llm.requests) == 2
    assert llm.requests[0].tools == llm.requests[1].tools
    assert '"web_mode": "both"' in caplog.text
    assert "web_route_selected" not in caplog.text
    assert "reason=domain_rule" not in caplog.text


@pytest.mark.asyncio
async def test_tavily_keyword_does_not_fabricate_a_deployment_route(
    database: Database,
    tmp_path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO")
    settings = make_settings(
        database.url,
        web_enabled=False,
        web_mode=WebMode.BOTH,
        tavily_api_key="test-placeholder",
        tooling_first_round_pin_ids_csv="",
    )
    llm = WebToolLLM()
    web = FakeWebSearchProvider(response=web_response())
    harness = build_harness(database, settings, llm, web_provider=web)
    bind_main_contract(harness, tmp_path)
    sender = MemorySender()

    result = await harness.processor.handle(
        event("Tavily搜索立党的最新推文", message_id="tavily-keyword-route"),
        sender,
    )

    assert result.reason == "chat"
    assert len(web.search_requests) == 1
    assert len(llm.requests) == 2
    assert llm.requests[0].tools == llm.requests[1].tools
    assert not llm.requests[0].native_tools
    assert "web_search" in {tool.name for tool in llm.requests[0].tools}
    assert "web_search" in {tool.name for tool in llm.requests[1].tools}
    assert '"web_mode": "both"' in caplog.text
    assert "web_route_selected" not in caplog.text
    assert "reason=user_override" not in caplog.text


@pytest.mark.asyncio
async def test_chat_completions_url_read_uses_read_webpage(
    database: Database,
    tmp_path,
) -> None:
    target_url = "https://docs.example.org/required-page"
    source = WebSearchSource(
        source_id="required-page",
        title="Required page",
        url=target_url,
        domain="docs.example.org",
        snippet="Requested content",
        relevant_content="The requested page content.",
    )
    settings = make_settings(
        database.url,
        web_enabled=False,
        web_mode=WebMode.BOTH,
        tavily_api_key="test-placeholder",
        tooling_first_round_pin_ids_csv="",
    )
    llm = TargetMissThenTavilyLLM(target_url)
    web = FakeWebSearchProvider(extracted={target_url: source})
    harness = build_harness(database, settings, llm, web_provider=web)
    bind_main_contract(harness, tmp_path)
    sender = MemorySender()

    result = await harness.processor.handle(
        event(f"读取 {target_url} 并总结。", message_id="target-miss-route"),
        sender,
    )

    assert result.reason == "chat"
    assert not sender.messages
    assert web.extract_requests == [(target_url, "读取用户指定的网页")]
    assert len(llm.requests) == 2
    assert llm.requests[0].tools == llm.requests[1].tools
    first_names = {tool.name for tool in llm.requests[0].tools}
    assert "read_webpage" in first_names
    assert "web_search" in first_names
    assert "read_webpage" in {tool.name for tool in llm.requests[1].tools}
    assert not llm.requests[0].native_tools


@pytest.mark.asyncio
async def test_web_result_observation_stays_redacted_without_implicit_delivery(
    database: Database,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO", logger="qq_ai_bot.services.evidence_observation")
    llm = WebToolLLM()
    web = FakeWebSearchProvider(response=web_response())
    harness = build_harness(database, web_settings(database), llm, web_provider=web)
    sender = MemorySender()

    result = await harness.processor.handle(
        event("最近 DeepSeek 有什么更新？", message_id="web-hidden"),
        sender,
    )

    assert result.reason == "chat"
    assert not sender.messages
    assert web.search_requests[0].query == "最新 DeepSeek 更新"
    observations = [
        json.loads(record.getMessage().removeprefix("agent_evidence "))
        for record in caplog.records
        if record.name == "qq_ai_bot.services.evidence_observation"
    ]
    assert any(
        item["phase"] == "tool_result_staged" and item["tool"] == "web_search" and item["ok"]
        for item in observations
    )
    assert any(
        item["phase"] == "response_received" and item["confirmed_prior_results"] == 1
        for item in observations
    )
    assert len({item["correlation_id"] for item in observations}) == 1
    serialized = json.dumps(observations, ensure_ascii=False)
    assert "最新 DeepSeek 更新" not in serialized
    assert "example.com" not in serialized


@pytest.mark.asyncio
async def test_web_failure_is_returned_to_llm_for_a_natural_answer(database: Database) -> None:
    llm = WebToolLLM()
    harness = build_harness(
        database,
        web_settings(database),
        llm,
        web_provider=FakeWebSearchProvider(
            error=WebSearchError("provider_unavailable", "联网服务暂不可用")
        ),
    )
    sender = MemorySender()

    result = await harness.processor.handle(
        event("查询最新消息", message_id="web-failure"),
        sender,
    )

    assert result.reason == "llm_failure"
    assert sender.messages[0].text == "AI 服务暂时不可用，请稍后重试。"


@pytest.mark.asyncio
async def test_web_lookup_can_be_followed_by_superuser_onebot_tool(
    database: Database, tmp_path
) -> None:
    llm = WebThenOneBotLLM()
    harness = build_harness(
        database,
        web_settings(database),
        llm,
        web_provider=FakeWebSearchProvider(response=web_response()),
    )
    bind_main_contract(harness, tmp_path)
    sender = ToolGatewaySender()

    result = await harness.processor.handle(
        event("联网查看后回答", message_id="web-admin", user_id="9000"),
        sender,
    )

    assert result.reason == "chat"
    assert sender.api_calls == [
        ("send_private_msg", {"user_id": "12345678", "message": "授权发送"})
    ]
    assert not sender.messages


@pytest.mark.asyncio
async def test_each_turn_executes_at_most_three_web_tools(database: Database) -> None:
    llm = RepeatedWebToolLLM()
    web = FakeWebSearchProvider(response=web_response())
    harness = build_harness(database, web_settings(database), llm, web_provider=web)
    sender = MemorySender()

    result = await harness.processor.handle(
        event("做一个复杂联网研究", message_id="web-limit"),
        sender,
    )

    assert result.reason == "chat"
    assert len(web.search_requests) == 3
    assert not sender.messages


def _native_first_settings(database: Database):
    return make_settings(
        database.url,
        web_enabled=True,
        web_mode=WebMode.BOTH,
        tavily_api_key="test-placeholder",
        tooling_first_round_pin_ids_csv="",
    )


@pytest.mark.asyncio
async def test_spoken_search_phrase_exposes_web_search_in_native_first_mode(
    database: Database,
) -> None:
    llm = FakeLLMProvider()
    harness = build_harness(
        database,
        _native_first_settings(database),
        llm,
        web_provider=FakeWebSearchProvider(response=web_response()),
    )
    result = await harness.processor.handle(
        event("这个说法你搜下", message_id="spoken-search"),
        MemorySender(),
    )
    assert result.reason == "llm_failure"
    assert llm.requests
    first = llm.requests[0]
    assert "web_search" not in {tool.name for tool in first.tools}
    assert not first.native_tools


@pytest.mark.asyncio
async def test_mixed_tools_stay_visible_and_missing_native_sources_do_not_restart(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qq_ai_bot.services.native_tool_binder import NativeToolBinder

    monkeypatch.setattr(
        NativeToolBinder,
        "bind",
        lambda self, **kwargs: (NativeToolDefinition(type=NativeToolType.WEB_SEARCH),),
    )

    class MissingSourcesLLM(FakeLLMProvider):
        async def complete(self, request: ChatRequest) -> ChatResponse:
            self.requests.append(request)
            return ChatResponse(
                content="",
                latency_seconds=0,
                native_tool_events=(
                    NativeToolEvent(
                        tool_type=NativeToolType.WEB_SEARCH,
                        call_id="failed-native",
                        status=NativeToolStatus.FAILED,
                        action_type="search",
                    ),
                ),
            )

    llm = MissingSourcesLLM()
    web = FakeWebSearchProvider(response=web_response())
    harness = build_harness(
        database,
        make_settings(
            database.url,
            web_enabled=True,
            web_mode=WebMode.BOTH,
            tavily_api_key="test-placeholder",
        ),
        llm,
        web_provider=web,
    )
    result = await harness.processor.handle(
        event("请搜索最新公告并附上来源", message_id="failed-native-no-restart"),
        MemorySender(),
    )
    assert result.reason == "chat"
    assert len(llm.requests) == 1
    assert not web.search_requests
    first_names = {tool.name for tool in llm.requests[0].tools}
    assert "web_search" in first_names
    assert llm.requests[0].native_tools


@pytest.mark.asyncio
async def test_native_first_public_url_does_not_pin_read_webpage(
    database: Database,
) -> None:
    llm = FakeLLMProvider()
    harness = build_harness(
        database,
        _native_first_settings(database),
        llm,
        web_provider=FakeWebSearchProvider(response=web_response()),
    )
    result = await harness.processor.handle(
        event("https://docs.example.org/required-page", message_id="url-pin"),
        MemorySender(),
    )
    assert result.reason == "llm_failure"
    assert llm.requests
    first = llm.requests[0]
    names = {tool.name for tool in first.tools}
    assert "read_webpage" not in names
    assert "web_search" not in names
    assert not first.native_tools
