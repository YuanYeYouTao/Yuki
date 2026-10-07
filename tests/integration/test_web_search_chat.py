"""End-to-end controlled web search and backend source display tests."""

from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest
from sqlalchemy import select
from tests.conftest import MemorySender, build_harness, make_settings
from tests.fakes import FakeWebSearchProvider
from tests.support.fixed_contract_fixture import bind_main_contract
from tests.support.social_identity_cases import social_env

from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.domain.messages import (
    ChatRequest,
    ChatResponse,
    InboundMessage,
    OutboundMessage,
    OutboundSendReceipt,
    SenderIdentity,
    ToolCall,
    ToolFunction,
)
from qq_ai_bot.llm.base import LLMProvider
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.llm.openai_responses import OpenAIResponsesProvider
from qq_ai_bot.model_runtime.executor import TaskModelExecutor
from qq_ai_bot.model_runtime.models import (
    ModelCapability,
    ModelProfile,
    ModelProtocol,
    ModelRoute,
    ModelSearchMode,
    ModelTask,
)
from qq_ai_bot.model_runtime.pool import ModelClientPool
from qq_ai_bot.model_runtime.profiles import ModelProfileCatalog
from qq_ai_bot.model_runtime.routes import ModelRouter
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.web_repository import WebSearchSourceRepository
from qq_ai_bot.social.db_models import SocialOperationModel
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


def _assert_declared_tool(request: ChatRequest, name: str) -> None:
    assert name in {tool.name for tool in request.tools}


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


def install_native_response_wire(harness, responses, *, search_mode=ModelSearchMode.BOTH):
    """Use an actual explicit Responses profile and capture the serialized HTTP."""
    captured = []

    def transport(request):
        captured.append(json.loads(request.content))
        assert len(captured) <= len(responses), "native work must not be implicitly replayed"
        return httpx.Response(200, json=responses[len(captured) - 1])

    client = httpx.AsyncClient(
        base_url="https://wire.invalid/", transport=httpx.MockTransport(transport)
    )
    provider = OpenAIResponsesProvider(
        base_url="https://wire.invalid",
        api_key="synthetic",
        timeout_seconds=1,
        max_retries=3,
        client=client,
    )
    profile = ModelProfile(
        id="native-web-wire",
        provider="openai",
        protocol=ModelProtocol.RESPONSES,
        base_url="https://wire.invalid",
        api_key_env="UNUSED",
        model="synthetic",
        timeout_seconds=1,
        max_retries=3,
        default_temperature=0.5,
        default_max_output_tokens=8192,
        search_mode=search_mode,
        capabilities=frozenset(
            {ModelCapability.TOOLS, ModelCapability.NATIVE_WEB_SEARCH, ModelCapability.REASONING}
        ),
    )
    models = TaskModelExecutor(
        router=ModelRouter(
            ModelProfileCatalog(
                profiles={profile.id: profile},
                routes={task: ModelRoute(task=task, profile_id=profile.id) for task in ModelTask},
            )
        ),
        pool=ModelClientPool(injected_profiles={profile.id: provider}),
    )
    chat = harness.processor._chat
    chat.runtime.runner._models = chat._models = models
    return client, captured


def native_response(*items):
    return {
        "status": "completed",
        "output": list(items),
        "usage": {"input_tokens": 10, "output_tokens": 3, "total_tokens": 13},
    }


def explicit_native_final(text="NO_REPLY", *, annotations=()):
    return {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text, "annotations": list(annotations)}],
    }


class WebToolLLM(LLMProvider):
    """Issue web_search, then summarize its structured result."""

    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []

    async def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        _assert_declared_tool(request, "web_search")
        last = request.messages[-1]
        if last.role != "tool":
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
        self._accepted_work = False
        self._looked_up = False

    async def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        names = {tool.name for tool in request.tools}
        last = request.messages[-1]
        if last.role != "tool":
            self._web_called = False
        _assert_declared_tool(request, "web_search")
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
        # The same authorized OneBot binding now has a Code Mode-only model
        # surface. Query its unchanged schema, accept Work, then invoke it once.
        assert "call_onebot_api" not in names
        if not self._looked_up:
            self._looked_up = True
            return ChatResponse(
                "",
                0,
                tool_calls=(
                    ToolCall(
                        "lookup-onebot", ToolFunction("lookup_tools", '{"name":"call_onebot_api"}')
                    ),
                ),
            )
        if not self._accepted_work:
            self._accepted_work = True
            return ChatResponse(
                "",
                0,
                tool_calls=(
                    ToolCall(
                        "accept-onebot",
                        ToolFunction(
                            "task_control",
                            json.dumps(
                                {
                                    "action": "accept",
                                    "goal": "联网后执行已授权发送",
                                    "output_kind": "state_change",
                                    "reporting": "quiet",
                                }
                            ),
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
                            name="execute_code",
                            arguments=json.dumps(
                                {
                                    "code": "r = await yuki_call_onebot_api("
                                    "{'action':'send_private_msg',"
                                    "'params':{'user_id':'12345678','message':'授权发送'}})\n"
                                    "{'ok': r['ok']}"
                                }
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


class NativeSourceFailureThenTavilyLLM(LLMProvider):
    """Use Tavily immediately when the profile cannot expose native tools."""

    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []

    async def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        _assert_declared_tool(request, "web_search")
        last = request.messages[-1]
        if last.role != "tool":
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
        _assert_declared_tool(request, "read_webpage")
        last = request.messages[-1]
        if last.role != "tool":
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
        _assert_declared_tool(request, "read_webpage")
        last = request.messages[-1]
        if last.role != "tool":
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
        web_enabled=True,
        web_mode=WebMode.NATIVE,
        tavily_api_key="",
    )
    harness = build_harness(database, settings, FakeLLMProvider())
    client, wire = install_native_response_wire(
        harness,
        [
            native_response(
                {
                    "type": "web_search_call",
                    "id": "native-search",
                    "status": "completed",
                    "action": {"type": "search", "query": "public docs"},
                },
                # An explicit decision to remain silent is a final model output;
                # a native-only response with no final is covered by the stop test.
                explicit_native_final(
                    annotations=(
                        {
                            "type": "url_citation",
                            "url": "https://example.com/native-docs",
                            "title": "Native docs",
                        },
                    )
                ),
            )
        ],
        search_mode=ModelSearchMode.NATIVE,
    )
    sender = MemorySender()

    try:
        result = await harness.processor.handle(
            event("请联网确认并附上来源。", message_id="native-visible"),
            sender,
        )
    finally:
        await client.aclose()

    assert result.reason == "chat" and len(wire) == 1
    assert result.sent_messages == 0
    assert not sender.messages
    assert any(item["type"] == "web_search" for item in wire[0]["tools"])
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

    assert result.reason == "agent_output_failure"
    # The fake receives the search failure but never calls send_message. Its
    # unsent final response is not a model-provider availability failure.
    assert sender.messages[0].text == "这次回复没有发出，请稍后重试。"


@pytest.mark.asyncio
async def test_web_lookup_can_be_followed_by_superuser_onebot_tool(
    database: Database, tmp_path
) -> None:
    llm = WebThenOneBotLLM()
    import hashlib

    from tests.support.codemode_cases import BINARY, BINDING

    if not BINDING or not BINARY.is_file():
        pytest.skip("OneBot tiered calling syntax requires the pinned Monty worker/binding")
    settings = web_settings(database).model_copy(
        update={
            "runtime_work_enabled": True,
            "code_mode_worker_path": BINARY,
            "code_mode_worker_sha256": hashlib.sha256(BINARY.read_bytes()).hexdigest(),
        }
    )
    harness = build_harness(
        database,
        settings,
        llm,
        web_provider=FakeWebSearchProvider(response=web_response()),
    )
    bind_main_contract(harness, tmp_path)
    harness.processor._chat.runtime.runner.code_mode_settings = settings
    sender = ToolGatewaySender()

    # Code Mode needs a real canonical Work source; the earlier direct-only
    # fixture used an anonymous legacy event. Preserve the same superuser and
    # gateway action while binding its genuine private conversation identity.
    from dataclasses import replace

    from sqlalchemy import select

    from qq_ai_bot.conversation.rollup.models import RollupPolicyConfig
    from qq_ai_bot.domain.conversations import ConversationScope
    from qq_ai_bot.persistence.models import ChatEventModel
    from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork

    writer = ScopedEventLedgerUnitOfWork(database, config=RollupPolicyConfig())
    await writer.append(
        scope=ConversationScope.private("8000", "9000"),
        platform_message_id="web-seed",
        sender_user_id="9000",
        direction="inbound",
        content="canonical web context",
    )
    async with database.sessions() as session:
        seed = await session.scalar(select(ChatEventModel))
        inbound = replace(
            event("联网查看后回答", message_id="web-admin", user_id="9000"),
            person_id=seed.author_person_id,
            presence_id=seed.ingress_presence_id,
            conversation_id=seed.canonical_conversation_id,
            legacy_conversation_key=ConversationScope.private("8000", "9000").key,
        )

    result = await harness.processor.handle(
        inbound,
        sender,
    )

    assert result.reason == "chat"
    assert sender.api_calls == [
        ("send_private_msg", {"user_id": "12345678", "message": "授权发送"})
    ], [(m.tool_call_id, m.content) for m in llm.requests[-1].messages if m.role == "tool"]
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
    assert result.reason == "agent_output_failure"
    assert llm.requests
    first = llm.requests[0]
    assert "web_search" in {tool.name for tool in first.tools}
    assert not first.native_tools


@pytest.mark.asyncio
async def test_mixed_tools_stay_visible_and_missing_native_sources_do_not_restart(
    database: Database, tmp_path
) -> None:
    web = FakeWebSearchProvider(response=web_response())
    harness = build_harness(
        database,
        make_settings(
            database.url,
            web_enabled=True,
            web_mode=WebMode.BOTH,
            tavily_api_key="test-placeholder",
        ),
        FakeLLMProvider(),
        web_provider=web,
    )
    bind_main_contract(harness, tmp_path)
    client, wire = install_native_response_wire(
        harness,
        [
            native_response(
                {
                    "type": "web_search_call",
                    "id": "failed-native",
                    "status": "failed",
                    "action": {"type": "search", "query": "announcement"},
                },
            )
        ],
    )
    sender = MemorySender()
    try:
        result = await harness.processor.handle(
            event("请搜索最新公告并附上来源", message_id="failed-native-no-restart"),
            sender,
        )
    finally:
        await client.aclose()
    # Native failure with no final or local calls cannot be silently called a
    # successful chat, and cannot cause another paid native request or fallback.
    assert result.reason == "llm_failure" and result.sent_messages == 1
    assert [message.text for message in sender.messages] == ["模型未能完成这次回复，请稍后重试。"]
    assert len(wire) == 1
    assert not web.search_requests
    first_names = {tool.get("name") for tool in wire[0]["tools"] if tool["type"] == "function"}
    assert "web_search" in first_names
    assert any(tool["type"] == "web_search" for tool in wire[0]["tools"])
    source = await harness.ledger.find_by_platform_message(
        bot_user_id="8000", platform_message_id="failed-native-no-restart"
    )
    assert source is not None
    assert (
        await WebSearchSourceRepository(database).for_trigger(
            conversation_key="bot:8000:private:1001", trigger_event_id=source.id
        )
        == ()
    )


@pytest.mark.asyncio
async def test_native_failure_with_explicit_local_fallback_keeps_protocol_and_send_receipts(
    database: Database, tmp_path
) -> None:
    env = await social_env(database, tmp_path)
    web = FakeWebSearchProvider(response=web_response())
    harness = build_harness(
        database,
        make_settings(
            database.url,
            enabled_groups_csv="20001",
            web_enabled=True,
            web_mode=WebMode.BOTH,
            tavily_api_key="test-placeholder",
        ),
        FakeLLMProvider(),
        web_provider=web,
    )
    chat = harness.processor._chat
    chat._tools.social_service = env.service
    env.service.runtime_config = chat._runtime_config
    bind_main_contract(harness, tmp_path)
    client, wire = install_native_response_wire(
        harness,
        [
            native_response(
                {
                    "type": "reasoning",
                    "id": "original-reason",
                    "summary": [],
                    "encrypted_content": "original-opaque",
                },
                {
                    "type": "web_search_call",
                    "id": "failed-native",
                    "status": "failed",
                    "action": {"type": "search", "query": "latest announcement"},
                },
                {
                    "type": "function_call",
                    "id": "fc-local",
                    "call_id": "local-fallback",
                    "name": "web_search",
                    "arguments": '{"query":"最新 DeepSeek 更新"}',
                },
            ),
            native_response(
                {
                    "type": "function_call",
                    "id": "fc-send",
                    "call_id": "public-send",
                    "name": "send_message",
                    "arguments": '{"text":"已查到本地联网来源，原生搜索未成功。"}',
                }
            ),
            native_response(explicit_native_final()),
        ],
    )
    sender = MemorySender()
    inbound = replace(
        event(
            "请搜索最新公告并附上来源",
            message_id="native-local-explicit-send",
            user_id="10001",
            group_id="20001",
        ),
        bot_user_id="80001",
        conversation_id=env.context.conversation_id,
        legacy_conversation_key=ConversationScope.group("80001", "20001").key,
        person_id=env.person,
        space_id=env.space,
        presence_id=env.presence,
    )
    try:
        result = await harness.processor.handle(inbound, sender)
    finally:
        await client.aclose()
    assert result.reason == "chat" and result.sent_messages == 1 and not sender.messages
    assert len(wire) == 3 and len(web.search_requests) == 1
    assert all(item["tools"] == wire[0]["tools"] for item in wire)
    assert {tool.get("name") for tool in wire[0]["tools"] if tool["type"] == "function"} >= {
        "web_search",
        "send_message",
    }
    assert any(tool["type"] == "web_search" for tool in wire[0]["tools"])
    second = wire[1]["input"]
    assert any(item.get("id") == "failed-native" and item["status"] == "failed" for item in second)
    assert any(item.get("encrypted_content") == "original-opaque" for item in second)
    local_results = [
        item
        for item in second
        if item.get("type") == "function_call_output" and item["call_id"] == "local-fallback"
    ]
    assert len(local_results) == 1 and json.loads(local_results[0]["output"])["ok"] is True
    third = wire[2]["input"]
    send_results = [
        item
        for item in third
        if item.get("type") == "function_call_output" and item["call_id"] == "public-send"
    ]
    assert len(send_results) == 1
    receipt = json.loads(send_results[0]["output"])
    assert receipt["ok"] is True and receipt["data"]["status"] == "succeeded", receipt
    actual_sends = [
        (action, params) for action, params in env.bot.calls if action.startswith("send_")
    ]
    assert len(actual_sends) == 1 and actual_sends[0][0] == "send_group_msg"
    async with database.sessions() as reader:
        statuses = list(await reader.scalars(select(SocialOperationModel.status).limit(4)))
    assert statuses == ["succeeded"]
    source = await harness.ledger.find_by_platform_message(
        bot_user_id="80001", platform_message_id="native-local-explicit-send"
    )
    assert source is not None
    stored = await WebSearchSourceRepository(database).for_trigger(
        conversation_key="bot:80001:group:20001",
        trigger_event_id=source.id,
    )
    assert [item.url for item in stored] == ["https://example.com/deepseek-update"]


@pytest.mark.asyncio
async def test_public_url_keeps_stable_external_web_tools(
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
    assert result.reason == "agent_output_failure"
    assert llm.requests
    first = llm.requests[0]
    names = {tool.name for tool in first.tools}
    assert "read_webpage" in names
    assert "web_search" in names
    assert not first.native_tools
