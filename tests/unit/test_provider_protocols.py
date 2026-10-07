"""Wire-level parity and private checkpoint recovery, without paid calls."""

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError
from tests.conftest import build_harness, make_settings

# P10: explicit backend/Invocation fixture; original behavioral assertions retained.
from tests.support.agent_backend import StubAgentBackend

from qq_ai_bot.application.lifecycle import LifecycleRegistry
from qq_ai_bot.application.modules.model_runtime import ModelRuntimeModule
from qq_ai_bot.deployment_setup.service import build_model_profiles
from qq_ai_bot.domain.messages import (
    ChatImage,
    ChatMessage,
    ChatRequest,
    ChatTool,
    FunctionCallOutput,
    ModelResponseStatus,
    NativeToolDefinition,
    NativeToolStatus,
    NativeToolType,
    ProviderContinuation,
    ReasoningEffort,
)
from qq_ai_bot.llm.anthropic_messages import AnthropicMessagesProvider
from qq_ai_bot.llm.base import (
    LLMInvalidRequestError,
    LLMInvalidResponseError,
    LLMMalformedFunctionCallError,
    LLMUnsupportedFeatureError,
)
from qq_ai_bot.llm.gemini import GeminiProvider
from qq_ai_bot.llm.openai_compatible import OpenAICompatibleProvider
from qq_ai_bot.llm.vendor_policy import CHAT_VENDORS, ChatWireOptions
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
from qq_ai_bot.model_runtime.profiles import (
    ModelProfileCatalog,
    load_model_profile_catalog,
    parse_model_profile_catalog,
)
from qq_ai_bot.model_runtime.routes import ModelRouter
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.runtime.work_journal import decode_transcript, encode_transcript
from qq_ai_bot.services.agent_runner import AgentRuntime
from qq_ai_bot.services.native_tool_binder import NativeToolBinder
from qq_ai_bot.services.turn_transcript import TurnTranscript
from qq_ai_bot.web.models import WebMode


def request():
    return ChatRequest(
        messages=(ChatMessage("system", "fixed"), ChatMessage("user", "task")),
        model="thinking-model",
        thinking_enabled=True,
        reasoning_effort=ReasoningEffort.LOW,
        max_output_tokens=8192,
        temperature=0.7,
        tools=(ChatTool("inspect", "Read evidence", {"type": "object", "properties": {}}),),
        tool_choice="auto",
    )


def provider(kind, client, **kwargs):
    return kind(
        base_url="https://wire.invalid/v1",
        api_key="synthetic-key",
        timeout_seconds=1,
        max_retries=0,
        client=client,
        **kwargs,
    )


@pytest.mark.parametrize("vendor", sorted(CHAT_VENDORS))
async def test_vendor_client_and_named_tool_contract(vendor):
    profile = ModelProfile(
        id="main",
        provider=vendor,
        protocol=ModelProtocol.CHAT_COMPLETIONS,
        base_url="https://wire.invalid/v1",
        api_key_env="SYNTHETIC_KEY",
        model="thinking-model",
        timeout_seconds=1,
        max_retries=0,
        default_temperature=0.7,
        default_max_output_tokens=8192,
        capabilities={ModelCapability.REASONING, ModelCapability.TOOLS},
    )
    pool = ModelClientPool(secret_overrides={"SYNTHETIC_KEY": "synthetic"})
    try:
        adapter = pool.get(profile)
        assert isinstance(adapter, OpenAICompatibleProvider)
        payload = adapter._build_payload(replace(request(), tool_choice="inspect"))
        assert payload["tools"][0]["function"]["name"] == "inspect"
        assert "temperature" not in payload
        if vendor == "deepseek":
            assert "tool_choice" not in payload
        else:
            assert payload["tool_choice"] == {"type": "function", "function": {"name": "inspect"}}
        if vendor in {"openai", "azure_openai"}:
            assert payload["max_completion_tokens"] == 8192 and "thinking" not in payload
        elif vendor == "qwen":
            assert payload["enable_thinking"] is True and "reasoning_effort" not in payload
        elif vendor in {"deepseek", "moonshot", "doubao", "zhipu"}:
            assert payload["thinking"] == {"type": "enabled"}
        elif vendor == "minimax":
            assert payload["reasoning_split"] is True and "reasoning_effort" not in payload
        elif vendor == "openrouter":
            assert payload["reasoning"]["effort"] == "low"
    finally:
        await pool.close()


@pytest.mark.parametrize(
    "kind", [OpenAICompatibleProvider, AnthropicMessagesProvider, GeminiProvider]
)
async def test_signed_tool_result_and_redirect_survive_journal(kind):
    wires = []

    def transport(req):
        wires.append(json.loads(req.content))
        if kind is AnthropicMessagesProvider:
            body = {
                "stop_reason": "tool_use",
                "content": [
                    {"type": "thinking", "thinking": "private", "signature": "signed"},
                    {"type": "tool_use", "id": "call-1", "name": "inspect", "input": {}},
                ],
                "usage": {
                    "input_tokens": 10,
                    "cache_read_input_tokens": 3,
                    "cache_creation_input_tokens": 2,
                    "cache_creation": {
                        "ephemeral_5m_input_tokens": 2,
                        "ephemeral_1h_input_tokens": 0,
                    },
                    "output_tokens": 4,
                },
            }
        elif kind is GeminiProvider:
            body = {
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {
                            "role": "model",
                            "parts": [
                                {
                                    "functionCall": {"name": "inspect", "args": {}},
                                    "thoughtSignature": "signed",
                                }
                            ],
                        },
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 15,
                    "candidatesTokenCount": 4,
                    "thoughtsTokenCount": 2,
                    "totalTokenCount": 21,
                },
            }
        else:
            body = {
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "reasoning_details": [
                                {"type": "reasoning.encrypted", "data": "signed"}
                            ],
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "inspect",
                                        "arguments": "{}",
                                    },
                                }
                            ],
                        },
                    }
                ]
            }
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(
        base_url="https://wire.invalid/v1/", transport=httpx.MockTransport(transport)
    ) as client:
        adapter = provider(kind, client)
        original = request()
        answer = await adapter.complete(original)
        assert answer.continuation is not None
        transcript = TurnTranscript(original.messages)
        transcript.accept(answer.continuation)
        transcript.append_result(answer.tool_calls[0].id, '{"ok": true}')
        transcript.append(ChatMessage("user", "redirect after receipt"))
        encoded = json.loads(json.dumps(encode_transcript(transcript)))
        restored = decode_transcript(encoded).request()
        assert restored == transcript.request()
        await adapter.complete(
            replace(
                original,
                messages=restored.messages,
                continuation=restored.continuation,
                continuation_items=restored.items,
            )
        )
        sequence_key = "contents" if kind is GeminiProvider else "messages"
        old_prefix = wires[0][sequence_key]
        replay_prefix = wires[1][sequence_key][: len(old_prefix)]
        if kind is AnthropicMessagesProvider:
            # A moving cache breakpoint changes metadata, not the replayed
            # content or signed thinking/tool blocks.
            def without_cache_control(value):
                if isinstance(value, list):
                    return [without_cache_control(item) for item in value]
                if isinstance(value, dict):
                    return {
                        key: without_cache_control(item)
                        for key, item in value.items()
                        if key != "cache_control"
                    }
                return value

            assert without_cache_control(replay_prefix) == without_cache_control(old_prefix)
        else:
            assert replay_prefix == old_prefix
        tail_text = json.dumps(wires[1][sequence_key], ensure_ascii=False)
        assert tail_text.index('"signed"') < tail_text.index("ok") < tail_text.index("redirect")
        assert "_call_ids" not in tail_text
        assert wires[1]["tools"] == wires[0]["tools"]
        if kind is AnthropicMessagesProvider:
            assert answer.prompt_tokens == 15 and answer.total_tokens == 19
            assert answer.cached_prompt_tokens == 3
            assert answer.cache_creation_input_tokens == 2
            assert answer.cache_creation_5m_input_tokens == 2
            assert answer.cache_creation_1h_input_tokens == 0
        elif kind is GeminiProvider:
            assert answer.completion_tokens == 6 and answer.reasoning_tokens == 2


@pytest.mark.parametrize(
    "kind", [OpenAICompatibleProvider, AnthropicMessagesProvider, GeminiProvider]
)
async def test_image_and_json_schema_reach_wire(kind):
    async with httpx.AsyncClient() as client:
        adapter = provider(kind, client)
        original = replace(
            request(),
            tools=(),
            messages=(
                ChatMessage(
                    "user",
                    "image",
                    images=(ChatImage("data:image/png;base64,aW1hZ2U="),),
                ),
            ),
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "answer",
                    "strict": True,
                    "schema": {"type": "object"},
                },
            },
        )
        payload = adapter._build_payload(original)
        assert "aW1hZ2U=" in json.dumps(payload)
        assert "schema" in json.dumps(payload).lower()
        with pytest.raises(LLMInvalidRequestError):
            adapter._build_payload(
                replace(original, messages=(replace(original.messages[0], role="assistant"),))
            )


@pytest.mark.parametrize("reason", ["length", "stop"])
async def test_chat_reasoning_citations_and_usage(reason):
    def transport(req):
        return httpx.Response(
            200,
            json={
                "id": "req-1",
                "choices": [
                    {
                        "finish_reason": reason,
                        "message": {
                            "content": [{"type": "text", "text": "answer"}],
                            "reasoning_content": "private",
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "url_citation": {
                                        "url": "https://example.com/source",
                                        "title": "Source",
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                    "prompt_tokens_details": {"cached_tokens": 4},
                    "completion_tokens_details": {"reasoning_tokens": 3},
                },
            },
        )

    async with httpx.AsyncClient(
        base_url="https://wire.invalid/v1/", transport=httpx.MockTransport(transport)
    ) as client:
        result = await provider(OpenAICompatibleProvider, client).complete(request())
        assert result.content == "answer" and result.reasoning_content == "private"
        assert result.citations[0].url == "https://example.com/source"
        assert result.cached_prompt_tokens == 4 and result.reasoning_tokens == 3
        assert result.status is (
            ModelResponseStatus.INCOMPLETE if reason == "length" else ModelResponseStatus.COMPLETED
        )


async def test_chat_never_ignores_unsupported_native_or_foreign_state():
    async with httpx.AsyncClient() as client:
        adapter = provider(OpenAICompatibleProvider, client)
        native = (NativeToolDefinition(NativeToolType.WEB_SEARCH),)
        with pytest.raises(LLMUnsupportedFeatureError):
            adapter._build_payload(replace(request(), native_tools=native))
        with pytest.raises(LLMInvalidRequestError):
            adapter._build_payload(
                replace(
                    request(),
                    continuation=ProviderContinuation(
                        "openai",
                        "responses",
                        (),
                    ),
                )
            )
        adapter = provider(
            OpenAICompatibleProvider,
            client,
            provider_name="openai",
            options=ChatWireOptions(native_web_search=True),
        )
        payload = adapter._build_payload(replace(request(), tools=(), native_tools=native))
        assert payload["web_search_options"] == {} and payload["max_completion_tokens"] == 8192
        with pytest.raises(LLMUnsupportedFeatureError):
            adapter._build_payload(replace(request(), native_tools=native))


@pytest.mark.parametrize(
    "message",
    [
        {"content": "text", "tool_calls": [{"function": {"name": "inspect", "arguments": "{}"}}]},
        {
            "content": "text",
            "tool_calls": [{"id": "same", "function": {"name": "x", "arguments": "{}"}}] * 2,
        },
    ],
)
async def test_chat_rejects_malformed_or_duplicate_calls(message):
    async with httpx.AsyncClient() as client:
        adapter = provider(OpenAICompatibleProvider, client)
        with pytest.raises(LLMInvalidResponseError):
            adapter._parse(httpx.Response(200, json={"choices": [{"message": message}]}), request())


async def test_mistral_thinking_chunks_are_private_and_replay_losslessly():
    async with httpx.AsyncClient() as client:
        adapter = provider(OpenAICompatibleProvider, client, provider_name="mistral")
        original = request()
        assert adapter._build_payload(original)["reasoning_effort"] == "high"
        chunks = [
            {"type": "thinking", "thinking": [{"type": "text", "text": "private"}]},
            {"type": "text", "text": "answer"},
        ]
        answer = adapter._parse(
            httpx.Response(
                200, json={"choices": [{"finish_reason": "stop", "message": {"content": chunks}}]}
            ),
            original,
        )
        assert answer.content == "answer" and answer.reasoning_content == "private"
        payload = adapter._build_payload(replace(original, continuation=answer.continuation))
        assert payload["messages"][-1]["content"] == chunks
        with pytest.raises(LLMUnsupportedFeatureError):
            adapter._build_payload(replace(original, reasoning_effort=ReasoningEffort.MAX))


@pytest.mark.parametrize(
    "protocol,vendor",
    [
        ("chat_completions", "qwen"),
        ("responses", "openai"),
        ("anthropic_messages", "anthropic"),
        ("gemini", "gemini"),
    ],
)
def test_setup_generates_valid_vendor_catalog(tmp_path, protocol, vendor):
    path = tmp_path / "profiles.toml"
    path.write_text(
        build_model_profiles(main_protocol=protocol, main_provider=vendor, flash_enabled=False),
        encoding="utf-8",
    )
    catalog = load_model_profile_catalog(
        path,
        environment={"LLM_BASE_URL": "https://wire.invalid/v1", "LLM_MODEL": "thinking-model"},
    )
    main = catalog.profiles[
        catalog.routes[
            next(task for task in catalog.routes if task.value == "chat_agent")
        ].profile_id
    ]
    assert main.provider == vendor and main.protocol.value == protocol
    if protocol == "responses":
        assert catalog.profiles["self_reflection"].provider == vendor


def test_setup_names_secondary_connection_by_task_role():
    document = build_model_profiles(
        main_protocol="responses", main_provider="deepseek", flash_enabled=True
    )
    assert "[profiles.background_tasks]" in document
    assert "[profiles.primary_agent]" in document
    assert 'chat_agent = "primary_agent"' in document
    assert 'memory_extraction = "background_tasks"' in document
    assert "[profiles.flash]" not in document
    assert "[profiles.pro]" not in document


def test_multi_vendor_example_loads_without_reading_secrets():
    catalog = load_model_profile_catalog(
        Path("config/model_profiles.providers.example.toml"),
        environment={
            name: "https://wire.invalid/v1" if name.endswith("BASE_URL") else "thinking-model"
            for name in (
                "ANTHROPIC_BASE_URL",
                "ANTHROPIC_MODEL",
                "GEMINI_BASE_URL",
                "GEMINI_MODEL",
                "LLM_FLASH_BASE_URL",
                "LLM_FLASH_MODEL",
            )
        },
    )
    assert {profile.provider for profile in catalog.profiles.values()} == {
        "anthropic",
        "qwen",
        "gemini",
    }
    assert len({profile.api_key_env for profile in catalog.profiles.values()}) == 3


@pytest.mark.parametrize(
    "kind", [OpenAICompatibleProvider, AnthropicMessagesProvider, GeminiProvider]
)
@pytest.mark.parametrize("empty", [False, True])
async def test_truncated_tool_call_recovers_without_executing(database, kind, empty):
    wires = []

    def transport(req):
        wires.append(json.loads(req.content))
        first = len(wires) == 1
        if kind is AnthropicMessagesProvider:
            body = {
                "stop_reason": "max_tokens" if first else "end_turn",
                "content": [
                    {"type": "tool_use", "id": "unfinished", "name": "inspect", "input": {}}
                    if first
                    else {"type": "text", "text": "done"}
                ],
            }
        elif kind is GeminiProvider:
            body = {
                "candidates": [
                    {
                        "finishReason": "MAX_TOKENS" if first else "STOP",
                        "content": {
                            "role": "model",
                            "parts": [
                                {
                                    "functionCall": {
                                        "name": "inspect",
                                        "args": {},
                                        "id": "unfinished",
                                    },
                                    "thoughtSignature": "signed",
                                }
                                if first
                                else {"text": "done"}
                            ],
                        },
                    }
                ]
            }
        else:
            body = {
                "choices": [
                    {
                        "finish_reason": "length" if first else "stop",
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "unfinished",
                                    "type": "function",
                                    "function": {"name": "inspect", "arguments": "{}"},
                                }
                            ],
                        }
                        if first
                        else {"content": "done"},
                    }
                ]
            }
        if first and empty:
            if kind is AnthropicMessagesProvider:
                body["content"] = []
            elif kind is GeminiProvider:
                body["candidates"][0]["content"] = {"role": "model"}
            else:
                body["choices"][0]["message"] = {"content": None}
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(
        base_url="https://wire.invalid/v1/", transport=httpx.MockTransport(transport)
    ) as client:
        harness = build_harness(database, make_settings(database.url), provider(kind, client))
        chat = harness.processor._chat
        tools = request().tools
        execute = AsyncMock(side_effect=AssertionError("truncated calls must not execute"))
        backend = StubAgentBackend(
            definitions=lambda *args, **kwargs: tools,
            execute_call=execute,
            finalize=lambda text, runtime: text,
        )
        runtime = AgentRuntime(
            origin=TurnOrigin.SCHEDULED_AUTOMATION,
            actor_user_id="10001",
            actor_is_superuser=False,
            delegated_authority=None,
            conversation_key="truncated",
            current_group_id=None,
            bot_user_id="80001",
            gateway=None,
            runtime_config=await chat._runtime_config.snapshot(),
            current_time=chat._time.current_default(),
            allowed_capabilities=frozenset(),
            max_tool_calls=2,
            max_model_requests=2,
            fixed_tools=tools,
        )
        result = await chat.runtime.runner.run(
            (ChatMessage("system", "fixed"), ChatMessage("user", "inspect")), runtime, backend
        )
        assert result.text == "done" and result.model_requests == 2
        execute.assert_not_called()
    assert wires[0]["tools"] == wires[1]["tools"]
    if not empty:
        assert "provider_response_incomplete" in json.dumps(wires[1])
        assert "unfinished" in json.dumps(wires[1])


async def test_deepseek_chat_dsml_uses_only_declared_tools():
    markup = (
        '<｜｜DSML｜｜tool_calls><｜｜DSML｜｜invoke name="inspect">'
        '<｜｜DSML｜｜parameter name="query" string="true">evidence</｜｜DSML｜｜parameter>'
        "</｜｜DSML｜｜invoke></｜｜DSML｜｜tool_calls>"
    )
    async with httpx.AsyncClient() as client:
        adapter = provider(OpenAICompatibleProvider, client, provider_name="deepseek")
        response = httpx.Response(
            200,
            json={
                "id": "stable-response",
                "choices": [{"finish_reason": "stop", "message": {"content": markup}}],
            },
        )
        result = adapter._parse(response, request())
        assert result.content == "" and result.tool_calls[0].function.name == "inspect"
        assert json.loads(result.tool_calls[0].function.arguments) == {"query": "evidence"}
        assert adapter._parse(response, request()).tool_calls == result.tool_calls
        with pytest.raises(LLMInvalidResponseError):
            adapter._parse(response, replace(request(), tools=()))


def test_responses_revision_ignores_empty_new_defaults_and_canonicalizes_sets():
    profile = ModelProfile(
        id="main",
        provider="deepseek",
        protocol=ModelProtocol.RESPONSES,
        base_url="https://wire.invalid",
        api_key_env="UNUSED",
        model="thinking-model",
        timeout_seconds=1,
        max_retries=0,
        default_temperature=0.7,
        default_max_output_tokens=8192,
        capabilities={ModelCapability.REASONING, ModelCapability.TOOLS},
    )
    routes = {task: ModelRoute(task=task, profile_id="main") for task in ModelTask}
    executor = TaskModelExecutor(
        router=ModelRouter(ModelProfileCatalog(profiles={"main": profile}, routes=routes)),
        pool=ModelClientPool(),
    )
    legacy = {
        "route": routes[ModelTask.CHAT_AGENT].model_dump(mode="json"),
        "profile": profile.model_dump(
            mode="json",
            exclude={
                "wire_options",
                "headers",
                "max_output_tokens_limit",
                "search_mode",
                "max_input_tokens",
                "context_window_tokens",
            },
        ),
    }
    legacy["profile"]["capabilities"] = sorted(legacy["profile"]["capabilities"])
    legacy["route"]["required_capabilities"] = sorted(legacy["route"]["required_capabilities"])
    expected = hashlib.sha256(
        json.dumps(
            legacy, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        ).encode()
    ).hexdigest()
    assert executor.profile_revision(ModelTask.CHAT_AGENT) == expected
    capacity_only = profile.model_copy(
        update={"max_input_tokens": 131072, "context_window_tokens": 262144}
    )
    capacity_executor = TaskModelExecutor(
        router=ModelRouter(ModelProfileCatalog(profiles={"main": capacity_only}, routes=routes)),
        pool=ModelClientPool(),
    )
    assert capacity_executor.profile_revision(ModelTask.CHAT_AGENT) == expected
    changed = profile.model_copy(update={"headers": {"x-custom-feature": "enabled"}})
    alternate = TaskModelExecutor(
        router=ModelRouter(ModelProfileCatalog(profiles={"main": changed}, routes=routes)),
        pool=ModelClientPool(),
    )
    assert alternate.profile_revision(ModelTask.CHAT_AGENT) != expected


@pytest.mark.parametrize("image_location", ["messages", "continuation_items"])
async def test_image_input_capability_covers_continuation_delta(image_location):
    profile = ModelProfile(
        id="text-only",
        provider="fake",
        protocol=ModelProtocol.RESPONSES,
        model="text-only",
        timeout_seconds=1,
        max_retries=0,
        default_temperature=0,
        default_max_output_tokens=100,
        capabilities={ModelCapability.REASONING, ModelCapability.TOOLS},
    )
    executor = TaskModelExecutor(
        router=ModelRouter(
            ModelProfileCatalog(
                profiles={profile.id: profile},
                routes={task: ModelRoute(task=task, profile_id=profile.id) for task in ModelTask},
            )
        ),
        pool=ModelClientPool(),
    )
    image = ChatMessage("user", "look", images=(ChatImage("data:image/png;base64,aW1hZ2U="),))
    payload = {"messages": (ChatMessage("user", "hello"),), image_location: (image,)}
    with pytest.raises(ValueError, match="does not support: image_input"):
        await executor.execute(
            ModelTask.CHAT_AGENT,
            ChatRequest(**payload),
        )


async def test_gemini_parallel_receipts_keep_signature_and_call_order():
    async with httpx.AsyncClient() as client:
        adapter = provider(GeminiProvider, client)
        original = request()
        parts = [
            {
                "functionCall": {"name": "inspect", "args": {"index": index}},
                **({"thoughtSignature": "signed"} if index == 1 else {}),
            }
            for index in (1, 2)
        ]
        answer = adapter._parse(
            httpx.Response(
                200, json={"candidates": [{"finishReason": "STOP", "content": {"parts": parts}}]}
            ),
            original,
        )
        transcript = TurnTranscript(original.messages)
        transcript.accept(answer.continuation)
        for call in answer.tool_calls:
            transcript.append_result(call.id, call.function.arguments)
        sequence = transcript.request()
        payload = adapter._build_payload(
            replace(original, continuation=sequence.continuation, continuation_items=sequence.items)
        )
        assert payload["contents"][-2]["parts"] == parts
        receipts = payload["contents"][-1]["parts"]
        assert [
            json.loads(part["functionResponse"]["response"]["output"])["index"] for part in receipts
        ] == [1, 2]
        assert all("_call_ids" not in item for item in payload["contents"])


@pytest.mark.parametrize(
    "effort", [ReasoningEffort.LOW, ReasoningEffort.MEDIUM, ReasoningEffort.HIGH]
)
async def test_gemini_38_sends_selected_thinking_level_without_fixed_budget(effort):
    async with httpx.AsyncClient() as client:
        adapter = provider(GeminiProvider, client)
        payload = adapter._build_payload(
            replace(request(), model="gemini-3.8-flash", reasoning_effort=effort)
        )
        assert payload["generationConfig"]["thinkingConfig"] == {"thinkingLevel": effort.value}
        budget_adapter = provider(
            GeminiProvider, client, options=ChatWireOptions(reasoning="budget")
        )
        with pytest.raises(LLMUnsupportedFeatureError, match="thinkingLevel"):
            budget_adapter._build_payload(
                replace(request(), model="gemini-3.8-flash", reasoning_effort=effort)
            )


def test_gemini_38_profile_rejects_fixed_budget_mode_before_save():
    with pytest.raises(ValidationError, match="thinkingLevel"):
        ModelProfile(
            id="gemini",
            provider="gemini",
            protocol=ModelProtocol.GEMINI,
            base_url="https://generativelanguage.googleapis.com/v1beta",
            api_key_env="GEMINI_KEY",
            model="gemini-3.8-flash",
            timeout_seconds=120,
            max_retries=0,
            default_temperature=0.7,
            default_max_output_tokens=8192,
            capabilities=frozenset({ModelCapability.REASONING}),
            wire_options=ChatWireOptions(reasoning="budget"),
        )


async def test_gemini_38_flash_native_wire_and_usage_without_paid_call():
    async with httpx.AsyncClient() as client:
        adapter = provider(GeminiProvider, client)
        original = replace(
            request(), model="gemini-3.8-flash", reasoning_effort=ReasoningEffort.MEDIUM
        )
        payload = adapter._build_payload(original)
        assert adapter._path(original) == "models/gemini-3.8-flash:generateContent"
        assert adapter._request_headers()["x-goog-api-key"] == "synthetic-key"
        assert payload["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "medium"}
        assert "thinkingBudget" not in payload["generationConfig"]["thinkingConfig"]
        assert "temperature" not in payload["generationConfig"]
        assert payload["tools"][0]["functionDeclarations"][0]["name"] == "inspect"
        answer = adapter._parse(
            httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "finishReason": "STOP",
                            "content": {
                                "role": "model",
                                "parts": [
                                    {
                                        "functionCall": {
                                            "name": "inspect",
                                            "args": {},
                                            "id": "call-38",
                                        },
                                        "thoughtSignature": "opaque-38",
                                    }
                                ],
                            },
                        }
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 200,
                        "cachedContentTokenCount": 120,
                        "candidatesTokenCount": 10,
                        "thoughtsTokenCount": 5,
                        "totalTokenCount": 215,
                    },
                },
            ),
            original,
        )
        assert answer.tool_calls[0].id == "call-38"
        assert answer.cached_prompt_tokens == 120
        assert answer.total_tokens == 215
        transcript = TurnTranscript(original.messages)
        transcript.accept(answer.continuation)
        transcript.append_result("call-38", "{}")
        sequence = transcript.request()
        replay = adapter._build_payload(
            replace(original, continuation=sequence.continuation, continuation_items=sequence.items)
        )
        assert replay["contents"][-2]["parts"][0]["thoughtSignature"] == "opaque-38"
        assert replay["contents"][-1]["parts"][0]["functionResponse"]["id"] == "call-38"


async def test_gemini_native_search_is_profile_scoped_and_preserves_functions():
    native = (NativeToolDefinition(NativeToolType.WEB_SEARCH),)
    binder = NativeToolBinder()
    kwargs = {
        "protocol": ModelProtocol.GEMINI,
        "allowed_capabilities": frozenset({"web_search"}),
        "web_mode": WebMode.TAVILY,
        "web_was_used": False,
        "search_mode": ModelSearchMode.BOTH,
    }
    assert binder.bind(capabilities=frozenset({ModelCapability.TOOLS}), **kwargs) == ()
    assert (
        binder.bind(
            capabilities=frozenset({ModelCapability.TOOLS, ModelCapability.NATIVE_WEB_SEARCH}),
            **kwargs,
        )
        == native
    )
    async with httpx.AsyncClient() as client:
        adapter = provider(GeminiProvider, client)
        configured = replace(request(), native_tools=native)
        payload = adapter._build_payload(configured)
        assert payload["tools"] == [
            {
                "functionDeclarations": [
                    {
                        "name": "inspect",
                        "description": "Read evidence",
                        "parametersJsonSchema": {"type": "object", "properties": {}},
                    }
                ]
            },
            {"googleSearch": {}},
        ]
        assert payload["toolConfig"] == {
            "functionCallingConfig": {"mode": "VALIDATED"},
            "includeServerSideToolInvocations": True,
        }
        answer = adapter._parse(
            httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "finishReason": "STOP",
                            "content": {"role": "model", "parts": [{"text": "grounded answer"}]},
                            "groundingMetadata": {
                                "webSearchQueries": ["example search"],
                                "groundingChunks": [
                                    {
                                        "web": {
                                            "uri": "https://example.org/source",
                                            "title": "Source",
                                        }
                                    },
                                ],
                            },
                        }
                    ],
                },
            ),
            configured,
        )
        assert answer.native_tool_events[0].query == "example search"
        assert answer.citations[0].url == "https://example.org/source"


@pytest.mark.parametrize("mode", [WebMode.NATIVE, WebMode.TAVILY, WebMode.BOTH])
def test_claude_native_search_excludes_external_search_functions(mode):
    binder = NativeToolBinder()
    kwargs = {
        "protocol": ModelProtocol.ANTHROPIC_MESSAGES,
        "allowed_capabilities": frozenset({"web_search"}),
        "web_mode": mode,
        "search_mode": ModelSearchMode.NATIVE,
    }
    native_capabilities = frozenset({ModelCapability.TOOLS, ModelCapability.NATIVE_WEB_SEARCH})
    assert binder.bind(capabilities=native_capabilities, web_was_used=False, **kwargs) == (
        NativeToolDefinition(NativeToolType.WEB_SEARCH),
    )
    assert binder.excluded_function_names(capabilities=native_capabilities, **kwargs) == frozenset(
        {"web_search", "read_webpage"}
    )
    assert (
        binder.excluded_function_names(
            capabilities=frozenset({ModelCapability.TOOLS}),
            protocol=kwargs["protocol"],
            allowed_capabilities=kwargs["allowed_capabilities"],
            web_mode=mode,
        )
        == frozenset()
    )
    assert (
        binder.excluded_function_names(
            capabilities=native_capabilities,
            protocol=ModelProtocol.GEMINI,
            allowed_capabilities=kwargs["allowed_capabilities"],
            web_mode=mode,
            search_mode=ModelSearchMode.BOTH,
        )
        == frozenset()
    )


@pytest.mark.parametrize("has_functions", [False, True])
async def test_claude_cache_breakpoint_follows_final_native_tool(has_functions):
    async with httpx.AsyncClient() as client:
        adapter = provider(AnthropicMessagesProvider, client)
        original = request()
        if not has_functions:
            original = replace(original, tools=())
        payload = adapter._build_payload(
            replace(original, native_tools=(NativeToolDefinition(NativeToolType.WEB_SEARCH),))
        )
        assert payload["system"][-1]["cache_control"] == {"type": "ephemeral"}
        assert payload["tools"][-1]["type"] == "web_search_20250305"
        assert payload["tools"][-1]["cache_control"] == {"type": "ephemeral"}
        assert all("cache_control" not in tool for tool in payload["tools"][:-1])
        assert payload["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
        assert "cache_control" not in payload


async def test_claude_conversation_cache_moves_after_tool_receipt_without_touching_checkpoint():
    async with httpx.AsyncClient() as client:
        adapter = provider(AnthropicMessagesProvider, client)
        original = request()
        first = adapter._build_payload(original)
        checkpoint = ProviderContinuation(
            provider="anthropic",
            protocol="anthropic_messages",
            payload=(
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "private", "signature": "signed"},
                        {"type": "tool_use", "id": "call-1", "name": "inspect", "input": {}},
                    ],
                },
            ),
        )
        later = adapter._build_payload(
            replace(
                original,
                continuation=checkpoint,
                continuation_items=(FunctionCallOutput("call-1", "result"),),
            )
        )
        assert first["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
        assert "cache_control" not in later["messages"][0]["content"][0]
        assert later["messages"][1]["content"] == list(checkpoint.payload[0]["content"])
        assert later["messages"][-1]["content"] == [
            {
                "type": "tool_result",
                "tool_use_id": "call-1",
                "content": "result",
                "cache_control": {"type": "ephemeral"},
            }
        ]
        assert "cache_control" not in later


@pytest.mark.parametrize(
    ("usage_extra", "expected"),
    [
        ({}, None),
        (
            {
                "cache_creation_input_tokens": 0,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 0,
                    "ephemeral_1h_input_tokens": 0,
                },
            },
            0,
        ),
    ],
)
async def test_claude_cache_creation_usage_keeps_missing_distinct_from_zero(usage_extra, expected):
    async with httpx.AsyncClient() as client:
        adapter = provider(AnthropicMessagesProvider, client)
        body = {
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "done"}],
            "usage": {
                "input_tokens": 10,
                "cache_read_input_tokens": 0,
                "output_tokens": 2,
                **usage_extra,
            },
        }
        answer = adapter._parse(httpx.Response(200, json=body), request())
        expected_input = 10 if expected == 0 else None
        expected_total = 12 if expected == 0 else None
        assert answer.prompt_tokens == expected_input
        assert answer.total_tokens == expected_total
        assert adapter._usage_diagnostics(body)["usage"]["prompt_tokens"] == expected_input
        assert adapter._usage_diagnostics(body)["usage"]["total_tokens"] == expected_total
        assert answer.cache_creation_input_tokens is expected
        assert adapter._usage_diagnostics(body)["usage"]["cache_creation_input_tokens"] is expected
        assert answer.cache_creation_5m_input_tokens is expected
        assert answer.cache_creation_1h_input_tokens is expected
        assert (
            adapter._usage_diagnostics(body)["usage"]["cache_creation_5m_input_tokens"] is expected
        )


async def test_claude_missing_cache_read_does_not_infer_total_input_or_failure_usage():
    async with httpx.AsyncClient() as client:
        adapter = provider(AnthropicMessagesProvider, client)
        usage = {
            "input_tokens": 10,
            "cache_creation_input_tokens": 5,
            "output_tokens": 2,
        }
        completed = adapter._parse(
            httpx.Response(
                200,
                json={
                    "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": "done"}],
                    "usage": usage,
                },
            ),
            request(),
        )
        assert completed.prompt_tokens is None
        assert completed.total_tokens is None
        assert completed.cached_prompt_tokens is None
        assert completed.cache_creation_input_tokens == 5
        with pytest.raises(LLMInvalidResponseError) as rejected:
            adapter._parse(
                httpx.Response(
                    200,
                    json={
                        "stop_reason": "refusal",
                        "content": [{"type": "text", "text": "private refusal"}],
                        "usage": usage,
                    },
                ),
                request(),
            )
        assert rejected.value.diagnostics == {
            "usage": {
                "prompt_tokens": None,
                "completion_tokens": 2,
                "total_tokens": None,
                "cached_prompt_tokens": None,
                "cache_creation_input_tokens": 5,
                "cache_creation_5m_input_tokens": None,
                "cache_creation_1h_input_tokens": None,
            }
        }


async def test_claude_cache_creation_mismatch_is_reported_without_losing_usage(caplog):
    async with httpx.AsyncClient() as client:
        adapter = provider(AnthropicMessagesProvider, client)
        body = {
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "done"}],
            "usage": {
                "input_tokens": 10,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 5,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 2,
                    "ephemeral_1h_input_tokens": 1,
                },
                "output_tokens": 2,
            },
        }
        answer = adapter._parse(httpx.Response(200, json=body), request())
        assert answer.cache_creation_input_tokens == 5
        assert answer.cache_creation_5m_input_tokens == 2
        assert answer.cache_creation_1h_input_tokens == 1
        assert "claude_cache_creation_breakdown_mismatch" in caplog.text


@pytest.mark.parametrize(
    "protocol,mode,expected",
    [
        (ModelProtocol.GEMINI, WebMode.NATIVE, True),
        (ModelProtocol.GEMINI, WebMode.BOTH, True),
        (ModelProtocol.GEMINI, WebMode.TAVILY, False),
        (ModelProtocol.ANTHROPIC_MESSAGES, WebMode.NATIVE, True),
        (ModelProtocol.ANTHROPIC_MESSAGES, WebMode.BOTH, False),
        (ModelProtocol.ANTHROPIC_MESSAGES, WebMode.TAVILY, False),
    ],
)
def test_legacy_search_mode_keeps_global_provider_choice(protocol, mode, expected):
    binder = NativeToolBinder()
    capabilities = frozenset({ModelCapability.TOOLS, ModelCapability.NATIVE_WEB_SEARCH})
    bound = binder.bind(
        protocol=protocol,
        capabilities=capabilities,
        allowed_capabilities=frozenset({"web_search"}),
        web_mode=mode,
        web_was_used=False,
        search_mode=None,
    )
    assert bool(bound) is expected


def test_responses_native_tool_requires_declared_capability_and_per_connection_mode():
    binder = NativeToolBinder()
    kwargs = {
        "protocol": ModelProtocol.RESPONSES,
        "allowed_capabilities": frozenset({"web_search"}),
        "web_mode": WebMode.BOTH,
        "web_was_used": False,
    }
    local_only = frozenset({ModelCapability.REASONING, ModelCapability.TOOLS})
    native = local_only | {ModelCapability.NATIVE_WEB_SEARCH}
    assert binder.bind(capabilities=local_only, search_mode=ModelSearchMode.BOTH, **kwargs) == ()
    assert binder.bind(capabilities=native, search_mode=ModelSearchMode.EXTERNAL, **kwargs) == ()
    assert binder.bind(capabilities=native, search_mode=ModelSearchMode.BOTH, **kwargs) == (
        NativeToolDefinition(NativeToolType.WEB_SEARCH),
    )


async def test_gemini_native_search_preserves_server_tool_context_across_function_receipt():
    wires = []

    def transport(req):
        wires.append(json.loads(req.content))
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {
                            "role": "model",
                            "parts": [
                                {
                                    "toolCall": {
                                        "toolType": "GOOGLE_SEARCH_WEB",
                                        "id": "search-1",
                                        "args": {"queries": ["today's weather"]},
                                    },
                                    "thoughtSignature": "signed-search",
                                },
                                {
                                    "toolResponse": {
                                        "toolType": "GOOGLE_SEARCH_WEB",
                                        "id": "search-1",
                                        "response": {"search_suggestions": "rain"},
                                    },
                                    "thoughtSignature": "signed-result",
                                },
                                {
                                    "functionCall": {
                                        "name": "inspect",
                                        "id": "function-1",
                                        "args": {},
                                    },
                                    "thoughtSignature": "signed-function",
                                },
                            ],
                        },
                    }
                ],
                "usageMetadata": {"promptTokenCount": 20, "candidatesTokenCount": 5},
            },
        )

    async with httpx.AsyncClient(
        base_url="https://wire.invalid/v1/", transport=httpx.MockTransport(transport)
    ) as client:
        adapter = provider(GeminiProvider, client)
        original = replace(
            request(), native_tools=(NativeToolDefinition(NativeToolType.WEB_SEARCH),)
        )
        answer = await adapter.complete(original)
        assert answer.native_tool_events[0].call_id == "search-1"
        assert answer.native_tool_events[0].query == "today's weather"
        assert answer.tool_calls[0].id == "function-1"
        assert answer.continuation is not None
        await adapter.complete(
            replace(
                original,
                continuation=answer.continuation,
                continuation_items=(FunctionCallOutput("function-1", '{"ok": true}'),),
            )
        )
        replay = json.dumps(wires[1]["contents"], ensure_ascii=False)
        assert replay.index("signed-search") < replay.index("signed-result")
        assert replay.index("signed-result") < replay.index("signed-function")
        assert replay.index("signed-function") < replay.index("functionResponse")


async def test_claude_native_search_pause_preserves_encrypted_result_and_aggregates_usage():
    wires = []
    first_blocks = [
        {
            "type": "server_tool_use",
            "id": "srvtoolu_1",
            "name": "web_search",
            "input": {"query": "recent launch"},
        }
    ]

    def transport(req):
        wires.append(json.loads(req.content))
        if len(wires) == 1:
            return httpx.Response(
                200,
                json={
                    "id": "msg-first",
                    "stop_reason": "pause_turn",
                    "content": first_blocks,
                    "usage": {
                        "input_tokens": 10,
                        "cache_read_input_tokens": 3,
                        "cache_creation_input_tokens": 2,
                        "cache_creation": {
                            "ephemeral_5m_input_tokens": 2,
                            "ephemeral_1h_input_tokens": 0,
                        },
                        "output_tokens": 1,
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "msg-second",
                "stop_reason": "end_turn",
                "content": [
                    {
                        "type": "web_search_tool_result",
                        "tool_use_id": "srvtoolu_1",
                        "content": [
                            {
                                "type": "web_search_result",
                                "url": "https://example.org/launch",
                                "title": "Launch",
                                "encrypted_content": "opaque-result",
                            }
                        ],
                    },
                    {
                        "type": "text",
                        "text": "Launched today.",
                        "citations": [
                            {
                                "type": "web_search_result_location",
                                "url": "https://example.org/launch",
                                "title": "Launch",
                                "encrypted_index": "opaque-index",
                            }
                        ],
                    },
                ],
                "usage": {
                    "input_tokens": 2,
                    "cache_read_input_tokens": 5,
                    "cache_creation_input_tokens": 0,
                    "cache_creation": {
                        "ephemeral_5m_input_tokens": 0,
                        "ephemeral_1h_input_tokens": 0,
                    },
                    "output_tokens": 4,
                },
            },
        )

    async with httpx.AsyncClient(
        base_url="https://wire.invalid/v1/", transport=httpx.MockTransport(transport)
    ) as client:
        from qq_ai_bot.model_runtime.request_accounting import (
            ProviderAttemptCounter,
            current_provider_attempts,
        )

        adapter = provider(AnthropicMessagesProvider, client)
        original = replace(
            request(), native_tools=(NativeToolDefinition(NativeToolType.WEB_SEARCH),)
        )
        attempts = ProviderAttemptCounter()
        token = current_provider_attempts.set(attempts)
        try:
            answer = await adapter.complete(original)
        finally:
            current_provider_attempts.reset(token)
        assert len(wires) == 2
        assert attempts.requests == 2
        assert attempts.unknown_usage_requests == 0
        assert wires[0]["tools"][-1] == {
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": 5,
            "cache_control": {"type": "ephemeral"},
        }
        assert wires[1]["messages"][-1] == {"role": "assistant", "content": first_blocks}
        assert answer.content == "Launched today."
        assert answer.prompt_tokens == 22
        assert answer.cached_prompt_tokens == 8
        assert answer.cache_creation_input_tokens == 2
        assert answer.cache_creation_5m_input_tokens == 2
        assert answer.cache_creation_1h_input_tokens == 0
        assert answer.completion_tokens == 5
        assert answer.total_tokens == 27
        assert answer.native_tool_events[0].call_id == "srvtoolu_1"
        assert answer.native_tool_events[0].query == "recent launch"
        assert answer.citations[0].url == "https://example.org/launch"
        assert answer.continuation is not None
        assert (
            answer.continuation.payload[-1]["content"][0]["content"][0]["encrypted_content"]
            == "opaque-result"
        )


async def test_claude_native_search_error_is_not_reported_as_completed():
    async with httpx.AsyncClient() as client:
        adapter = provider(AnthropicMessagesProvider, client)
        configured = replace(
            request(), native_tools=(NativeToolDefinition(NativeToolType.WEB_SEARCH),)
        )
        answer = adapter._parse(
            httpx.Response(
                200,
                json={
                    "stop_reason": "end_turn",
                    "content": [
                        {
                            "type": "server_tool_use",
                            "id": "srvtoolu_2",
                            "name": "web_search",
                            "input": {"query": "news"},
                        },
                        {
                            "type": "web_search_tool_result",
                            "tool_use_id": "srvtoolu_2",
                            "content": {
                                "type": "web_search_tool_result_error",
                                "error_code": "unavailable",
                            },
                        },
                        {"type": "text", "text": "Search failed."},
                    ],
                },
            ),
            configured,
        )
        assert answer.native_tool_events[0].status is NativeToolStatus.FAILED
        assert answer.native_tool_events[0].error_category == "unavailable"


async def test_rejected_provider_response_keeps_only_numeric_usage_diagnostics():
    async with httpx.AsyncClient() as client:
        gemini = provider(GeminiProvider, client)
        with pytest.raises(LLMInvalidResponseError) as gemini_error:
            gemini._parse(
                httpx.Response(
                    200,
                    json={
                        "candidates": [{"finishReason": "SAFETY", "content": {"parts": []}}],
                        "usageMetadata": {
                            "promptTokenCount": 12,
                            "candidatesTokenCount": 3,
                            "thoughtsTokenCount": 2,
                            "totalTokenCount": 17,
                            "cachedContentTokenCount": 4,
                        },
                        "secret": "private response body",
                    },
                ),
                request(),
            )
        assert gemini_error.value.diagnostics == {
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 5,
                "total_tokens": 17,
                "cached_prompt_tokens": 4,
            }
        }
        claude = provider(AnthropicMessagesProvider, client)
        with pytest.raises(LLMInvalidResponseError) as claude_error:
            claude._parse(
                httpx.Response(
                    200,
                    json={
                        "stop_reason": "refusal",
                        "content": [{"type": "text", "text": "private response body"}],
                        "usage": {
                            "input_tokens": 7,
                            "cache_read_input_tokens": 3,
                            "cache_creation_input_tokens": 1,
                            "cache_creation": {
                                "ephemeral_5m_input_tokens": 0,
                                "ephemeral_1h_input_tokens": 1,
                            },
                            "output_tokens": 2,
                        },
                    },
                ),
                request(),
            )
        assert claude_error.value.diagnostics == {
            "usage": {
                "prompt_tokens": 11,
                "completion_tokens": 2,
                "total_tokens": 13,
                "cached_prompt_tokens": 3,
                "cache_creation_input_tokens": 1,
                "cache_creation_5m_input_tokens": 0,
                "cache_creation_1h_input_tokens": 1,
            }
        }


async def test_claude_paused_search_failure_reports_prior_usage_without_content():
    count = 0
    wires = []

    def transport(req):
        nonlocal count
        count += 1
        wires.append(json.loads(req.content))
        if count == 1:
            return httpx.Response(
                200,
                json={
                    "stop_reason": "pause_turn",
                    "content": [
                        {
                            "type": "server_tool_use",
                            "id": "srvtoolu_3",
                            "name": "web_search",
                            "input": {"query": "private query"},
                        }
                    ],
                    "usage": {"input_tokens": 5, "output_tokens": 1},
                },
            )
        if count == 2:
            return httpx.Response(
                200,
                json={
                    "stop_reason": "refusal",
                    "content": [{"type": "text", "text": "private refusal"}],
                    "usage": {"input_tokens": 2, "output_tokens": 3},
                },
            )
        return httpx.Response(
            200,
            json={
                "stop_reason": "end_turn",
                "content": [
                    {
                        "type": "web_search_tool_result",
                        "tool_use_id": "srvtoolu_3",
                        "content": [],
                    },
                    {"type": "text", "text": "Recovered without new search."},
                ],
                "usage": {"input_tokens": 1, "output_tokens": 2},
            },
        )

    async with httpx.AsyncClient(
        base_url="https://wire.invalid/v1/", transport=httpx.MockTransport(transport)
    ) as client:
        claude = provider(AnthropicMessagesProvider, client)
        configured = replace(
            request(), native_tools=(NativeToolDefinition(NativeToolType.WEB_SEARCH),)
        )
        paused = await claude.complete(configured)
        assert paused.status is ModelResponseStatus.INCOMPLETE
        assert paused.incomplete_reason == "pause_turn"
        assert paused.native_tool_events[0].status is NativeToolStatus.SEARCHING
        assert paused.native_tool_events[0].call_id == "srvtoolu_3"
        assert paused.prompt_tokens is None
        assert paused.completion_tokens == 4
        assert paused.total_tokens is None
        assert paused.continuation is not None
        assert "private refusal" not in str(paused.continuation)
        recovered = await claude.complete(replace(configured, continuation=paused.continuation))
        assert recovered.status is ModelResponseStatus.COMPLETED
        assert recovered.native_tool_events[0].call_id == "srvtoolu_3"
        assert len(wires) == 3
        assert len(wires[2]["messages"]) == 2
        assert wires[2]["messages"][-1]["content"][0]["id"] == "srvtoolu_3"
        assert (
            sum(
                block.get("type") == "server_tool_use"
                for message in wires[2]["messages"]
                for block in message["content"]
            )
            == 1
        )


@pytest.mark.parametrize("missing_on_first", [True, False])
@pytest.mark.parametrize(
    "missing_field", ["cache_read_input_tokens", "cache_creation_input_tokens"]
)
async def test_claude_paused_search_failure_does_not_merge_partial_cache_as_known(
    missing_on_first,
    missing_field,
):
    count = 0

    def transport(_request):
        nonlocal count
        count += 1
        usage = {
            "input_tokens": 10,
            "cache_creation_input_tokens": 2,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 2,
                "ephemeral_1h_input_tokens": 0,
            },
            "output_tokens": 1,
        }
        usage["cache_read_input_tokens"] = 3
        if missing_on_first == (count == 1):
            usage.pop(missing_field)
        if count == 1:
            return httpx.Response(
                200,
                json={
                    "stop_reason": "pause_turn",
                    "content": [
                        {
                            "type": "server_tool_use",
                            "id": "srvtoolu_4",
                            "name": "web_search",
                            "input": {"query": "public query"},
                        }
                    ],
                    "usage": usage,
                },
            )
        return httpx.Response(400, json={"error": {"type": "bad_request"}, "usage": usage})

    async with httpx.AsyncClient(
        base_url="https://wire.invalid/v1/", transport=httpx.MockTransport(transport)
    ) as client:
        claude = provider(AnthropicMessagesProvider, client)
        configured = replace(
            request(), native_tools=(NativeToolDefinition(NativeToolType.WEB_SEARCH),)
        )
        answer = await claude.complete(configured)
        assert count == 2
        assert answer.incomplete_reason == "pause_turn"
        assert answer.prompt_tokens == 15
        assert answer.cached_prompt_tokens is None
        assert answer.cache_creation_input_tokens is None
        assert answer.cache_creation_5m_input_tokens is None


def test_profile_rejects_native_search_claim_for_unsupported_protocol():
    with pytest.raises(ValidationError, match="native web search is unavailable"):
        ModelProfile(
            id="deepseek",
            provider="deepseek",
            protocol=ModelProtocol.RESPONSES,
            base_url="https://api.deepseek.com/v1",
            api_key_env="DEEPSEEK_KEY",
            model="deepseek-test",
            timeout_seconds=30,
            max_retries=1,
            default_temperature=0.7,
            default_max_output_tokens=4096,
            capabilities=frozenset(
                {
                    ModelCapability.REASONING,
                    ModelCapability.TOOLS,
                    ModelCapability.NATIVE_WEB_SEARCH,
                }
            ),
        )


def test_search_mode_requires_real_native_capability_and_rejects_claude_both():
    common = dict(
        id="claude-search",
        provider="anthropic",
        protocol=ModelProtocol.ANTHROPIC_MESSAGES,
        base_url="https://api.anthropic.com",
        api_key_env="CLAUDE_KEY",
        model="claude-test",
        timeout_seconds=30,
        max_retries=1,
        default_temperature=0.7,
        default_max_output_tokens=4096,
    )
    with pytest.raises(ValidationError, match="requires native_web_search"):
        ModelProfile(
            **common,
            search_mode=ModelSearchMode.NATIVE,
            capabilities=frozenset({ModelCapability.REASONING, ModelCapability.TOOLS}),
        )
    with pytest.raises(ValidationError, match="cannot combine"):
        ModelProfile(
            **common,
            search_mode=ModelSearchMode.BOTH,
            capabilities=frozenset(
                {
                    ModelCapability.REASONING,
                    ModelCapability.TOOLS,
                    ModelCapability.NATIVE_WEB_SEARCH,
                }
            ),
        )
    native = ModelProfile(
        **common,
        search_mode=ModelSearchMode.NATIVE,
        capabilities=frozenset(
            {
                ModelCapability.REASONING,
                ModelCapability.TOOLS,
                ModelCapability.NATIVE_WEB_SEARCH,
            }
        ),
    )
    assert native.model_dump(mode="json")["search_mode"] == "native"


def test_search_mode_survives_profile_toml_load():
    routes = "\n".join(f'{task.value} = "gemini"' for task in ModelTask)
    document = f"""schema_version = 3
[profiles.gemini]
provider = "gemini"
protocol = "gemini"
base_url = "https://generativelanguage.googleapis.com/v1beta"
api_key_env = "GEMINI_KEY"
model = "gemini-3.8-flash"
timeout_seconds = 120
max_retries = 1
default_temperature = 0.7
default_max_output_tokens = 8192
search_mode = "both"
capabilities = ["reasoning", "tools", "structured_output", "native_web_search"]
[routes]
{routes}
"""
    catalog = parse_model_profile_catalog(document)
    assert catalog.profiles["gemini"].search_mode is ModelSearchMode.BOTH


@pytest.mark.parametrize(
    "vendor,protocol",
    [
        ("qwen", "chat_completions"),
        ("anthropic", "anthropic_messages"),
        ("gemini", "gemini"),
        ("fake", "chat_completions"),
    ],
)
async def test_runtime_module_compatibility_uses_declared_vendor(
    database, tmp_path, vendor, protocol
):
    settings = make_settings(
        database.url,
        llm_provider=vendor,
        llm_base_url="https://wire.invalid/v1",
        llm_api_key="synthetic-key",
        llm_model="thinking-model",
        model_profiles_file=tmp_path / "absent.toml",
    )
    import json

    from qq_ai_bot.model_runtime.models import ModelTask

    settings.model_profiles_file.write_text(
        "schema_version = 3\n[profiles.main]\nprovider = "
        + json.dumps(vendor)
        + "\nprotocol = "
        + json.dumps(protocol)
        + '\nbase_url_env = "LLM_BASE_URL"\nmodel_env = "LLM_MODEL"\napi_key_env = "LLM_API_KEY"\n'
        "timeout_seconds = 60\nmax_retries = 1\ndefault_temperature = 0.7\n"
        "default_max_output_tokens = 2048\n"
        'capabilities = ["tools", "structured_output", "reasoning"]\n[routes]\n'
        + "".join(f'{task.value} = "main"\n' for task in ModelTask),
        encoding="utf-8",
    )
    bundle = ModelRuntimeModule(
        settings.model_runtime, database, lifecycle=LifecycleRegistry()
    ).build()
    try:
        assert getattr(bundle.chat_provider, "provider_name", "fake") == vendor
        assert bundle.executor.protocol(ModelTask.CHAT_AGENT).value == protocol
        assert bundle.chat_provider is bundle.clients.get(bundle.profiles.profiles["main"])
        assert not bundle.clients._injected_profiles
        if vendor == "qwen":
            payload = bundle.chat_provider._build_payload(request())
            assert payload["enable_thinking"] is True and "reasoning_effort" not in payload
    finally:
        await bundle.executor.close()


@pytest.mark.parametrize(
    "vendor,protocol,options",
    [
        ("openai", "responses", {"send_temperature": True}),
        ("anthropic", "anthropic_messages", {"send_temperature": True}),
        ("gemini", "gemini", {"token_field": "max_tokens"}),
        ("qwen", "chat_completions", {"reasoning": "gemini"}),
    ],
)
def test_configuration_never_ignores_options_from_another_protocol(vendor, protocol, options):
    with pytest.raises(ValidationError):
        ModelProfile(
            id="main",
            provider=vendor,
            protocol=protocol,
            base_url="https://wire.invalid",
            api_key_env="UNUSED",
            model="thinking-model",
            timeout_seconds=1,
            max_retries=0,
            default_temperature=0.7,
            default_max_output_tokens=8192,
            capabilities={ModelCapability.REASONING},
            wire_options=options,
        )


@pytest.mark.parametrize("parts", [[{"text": ""}], [], [{"text": " \n"}]])
async def test_gemini_confirmed_malformed_local_call_is_typed_and_keeps_numeric_usage(parts):
    from copy import deepcopy

    async with httpx.AsyncClient() as client:
        adapter = provider(GeminiProvider, client)
        configured = replace(
            request(),
            continuation=ProviderContinuation(
                "gemini",
                "gemini",
                ({"role": "model", "parts": [{"text": "prior", "thoughtSignature": "signature"}]},),
            ),
        )
        original = deepcopy(configured)
        with pytest.raises(LLMMalformedFunctionCallError) as caught:
            adapter._parse(
                httpx.Response(
                    200,
                    json={
                        "candidates": [
                            {
                                "index": 0,
                                "finishReason": "MALFORMED_FUNCTION_CALL",
                                "finishMessage": "private malformed call and arguments",
                                "content": {"role": "model", "parts": parts},
                            }
                        ],
                        "usageMetadata": {
                            "promptTokenCount": 120,
                            "candidatesTokenCount": 0,
                            "thoughtsTokenCount": 12,
                            "totalTokenCount": 132,
                            "cachedContentTokenCount": 80,
                        },
                    },
                ),
                configured,
            )
        assert isinstance(caught.value, LLMInvalidResponseError)
        assert caught.value.diagnostics == {
            "usage": {
                "prompt_tokens": 120,
                "completion_tokens": 12,
                "total_tokens": 132,
                "cached_prompt_tokens": 80,
            }
        }
        assert "private malformed" not in str(caught.value)
        assert "finishMessage" not in repr(caught.value.diagnostics)
        assert configured == original  # No partial output appended to signed history.


@pytest.mark.parametrize(
    "candidate",
    [
        {"content": {"parts": [{"functionCall": {"name": "send_message", "args": {}}}]}},
        {"content": {"parts": [{"toolCall": {"id": "search", "toolType": "GOOGLE_SEARCH"}}]}},
        {"content": {"parts": [{"toolResponse": {"id": "search"}}]}},
        {"content": {"parts": [{"text": "", "functionCall": {"name": "inspect"}}]}},
        {"content": {"parts": [{"text": "visible content"}]}},
        {"content": {"parts": [{"text": "reasoning", "thought": True}]}},
        {"content": {"parts": [{"text": "", "thoughtSignature": "signature"}]}},
        {"content": {"parts": [{"inlineData": {"mimeType": "image/png", "data": "private"}}]}},
        {"content": {"parts": ["invalid part"]}},
        {"content": {"parts": [{"text": None}]}},
        {"content": {"parts": [{"text": 0}]}},
        {"content": {"parts": [{}]}},
        {"content": {"parts": None}},
        {"content": {"role": "user", "parts": []}},
        {"content": {}},
        {"content": None},
        {"index": "0", "content": {"parts": []}},
        {"index": 1, "content": {"parts": []}},
        {"content": {"parts": []}, "groundingMetadata": {"webSearchQueries": ["query"]}},
        {"content": {"parts": []}, "safetyRatings": [{"blocked": True}]},
        {"content": {"parts": []}, "unknownToolEffect": True},
    ],
)
async def test_gemini_malformed_marker_with_ambiguous_output_remains_fatal(candidate):
    async with httpx.AsyncClient() as client:
        adapter = provider(GeminiProvider, client)
        with pytest.raises(LLMInvalidResponseError) as caught:
            adapter._parse(
                httpx.Response(
                    200,
                    json={"candidates": [{"finishReason": "MALFORMED_FUNCTION_CALL", **candidate}]},
                ),
                request(),
            )
        assert type(caught.value) is LLMInvalidResponseError


@pytest.mark.parametrize(
    "variant",
    ["native_tools", "multiple", "safety", "block", "error", "unknown_effect", "invalid_json"],
)
async def test_gemini_only_exact_safe_malformed_marker_can_be_recovered(variant):
    body = {
        "candidates": [
            {"finishReason": "MALFORMED_FUNCTION_CALL", "content": {"parts": [{"text": ""}]}}
        ]
    }
    configured = request()
    if variant == "native_tools":
        configured = replace(
            configured, native_tools=(NativeToolDefinition(NativeToolType.WEB_SEARCH),)
        )
    elif variant == "multiple":
        body["candidates"] *= 2
    elif variant == "safety":
        body["candidates"][0]["finishReason"] = "SAFETY"
    elif variant == "block":
        body["promptFeedback"] = {"blockReason": "SAFETY"}
    elif variant == "error":
        body["error"] = {"status": "INTERNAL"}
    elif variant == "unknown_effect":
        body["toolResponse"] = {"id": "possibly-executed"}
    async with httpx.AsyncClient() as client:
        adapter = provider(GeminiProvider, client)
        response = (
            httpx.Response(200, content=b"invalid JSON")
            if variant == "invalid_json"
            else httpx.Response(200, json=body)
        )
        with pytest.raises(LLMInvalidResponseError) as caught:
            adapter._parse(response, configured)
        assert type(caught.value) is LLMInvalidResponseError
