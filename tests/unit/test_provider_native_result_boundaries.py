"""Paid native results and ambiguous local IDs survive without automatic replay."""

import json
import sqlite3
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy.exc import OperationalError
from tests.conftest import build_harness, make_settings
from tests.support.social_identity_cases import social_env

from qq_ai_bot.domain.messages import (
    ChatMessage,
    ChatRequest,
    ChatTool,
    ModelResponseStatus,
    NativeToolDefinition,
    NativeToolStatus,
    NativeToolType,
    ProviderContinuation,
    ToolCall,
    ToolFunction,
)
from qq_ai_bot.llm.anthropic_messages import AnthropicMessagesProvider
from qq_ai_bot.llm.base import LLMUnavailableError
from qq_ai_bot.llm.deepseek_responses import DeepSeekResponsesProvider
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.llm.gemini import GeminiProvider
from qq_ai_bot.llm.openai_compatible import OpenAICompatibleProvider
from qq_ai_bot.llm.openai_responses import OpenAIResponsesProvider
from qq_ai_bot.llm.vendor_policy import ChatWireOptions
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
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_journal import decode_transcript, encode_transcript
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.runtime.work_session import WorkSession
from qq_ai_bot.services.agent_runner import AgentRuntime
from qq_ai_bot.services.turn_transcript import TurnTranscript

KINDS = [
    AnthropicMessagesProvider,
    GeminiProvider,
    OpenAIResponsesProvider,
    DeepSeekResponsesProvider,
]


def empty_native_reply(kind):
    if kind is OpenAICompatibleProvider:
        return {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": "",
                        "reasoning_details": [
                            {"type": "reasoning.encrypted", "data": "original-opaque"}
                        ],
                    },
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
        }
    body = native_reply(kind)
    if kind is AnthropicMessagesProvider:
        body["content"] = body["content"][:1]
    elif kind is GeminiProvider:
        body["candidates"][0]["content"]["parts"] = [
            {"text": "private", "thought": True, "thoughtSignature": "original-signature"}
        ]
        body["candidates"][0].pop("groundingMetadata")
    else:
        body["output"] = body["output"][:1]
    return body


def native_reply(kind, *, duplicate=False, status="completed"):
    if kind is AnthropicMessagesProvider:
        body = {
            "stop_reason": "end_turn",
            "content": [
                {"type": "thinking", "thinking": "private", "signature": "original-signature"},
                {
                    "type": "server_tool_use",
                    "id": "search-original",
                    "name": "web_search",
                    "input": {"query": "audit"},
                },
                {
                    "type": "web_search_tool_result",
                    "tool_use_id": "search-original",
                    "content": [
                        {
                            "type": "web_search_result",
                            "url": "https://example.org/source",
                            "title": "Source",
                            "encrypted_content": "opaque-source",
                        }
                    ],
                },
            ],
            "usage": {
                "input_tokens": 10,
                "output_tokens": 3,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        }
        if duplicate:
            body["content"].extend(
                {
                    "type": "tool_use",
                    "id": "duplicate",
                    "name": "write_probe",
                    "input": {"value": i},
                }
                for i in range(2)
            )
        return body
    if kind is GeminiProvider:
        body = {
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {
                        "role": "model",
                        "parts": [
                            {
                                "toolCall": {
                                    "id": "search-original",
                                    "toolType": "GOOGLE_SEARCH_WEB",
                                    "args": {"queries": ["audit"]},
                                },
                                "thoughtSignature": "original-signature",
                            },
                            {
                                "toolResponse": {
                                    "id": "search-original",
                                    "response": {"source": "https://example.org/source"},
                                }
                            },
                        ],
                    },
                    "groundingMetadata": {
                        "groundingChunks": [
                            {"web": {"uri": "https://example.org/source", "title": "Source"}}
                        ]
                    },
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 10,
                "candidatesTokenCount": 3,
                "totalTokenCount": 13,
            },
        }
        if duplicate:
            body["candidates"][0]["content"]["parts"].extend(
                {
                    "functionCall": {
                        "id": "duplicate",
                        "name": "write_probe",
                        "args": {"value": i},
                    },
                    "thoughtSignature": f"call-signature-{i}",
                }
                for i in range(2)
            )
        return body
    output = [
        {
            "type": "reasoning",
            "id": "reason-original",
            "summary": [],
            "encrypted_content": "original-opaque",
        },
        {
            "type": "web_search_call",
            "id": "search-original",
            "status": status,
            "action": {"type": "search", "query": "audit"},
        },
    ]
    if duplicate:
        output.extend(
            {
                "type": "function_call",
                "id": f"fc-{i}",
                "call_id": "duplicate",
                "name": "write_probe",
                "arguments": json.dumps({"value": i}),
            }
            for i in range(2)
        )
    return {
        "status": "completed",
        "output": output,
        "usage": {"input_tokens": 10, "output_tokens": 3, "total_tokens": 13},
    }


@pytest.mark.parametrize("kind", KINDS)
async def test_native_only_complete_keeps_real_checkpoint_usage_and_http_shape(kind):
    wire = []

    def transport(req):
        wire.append(json.loads(req.content))
        return httpx.Response(200, json=native_reply(kind))

    async with httpx.AsyncClient(
        base_url="https://wire.invalid/v1/", transport=httpx.MockTransport(transport)
    ) as client:
        provider = kind(
            base_url="https://wire.invalid/v1",
            api_key="synthetic",
            timeout_seconds=1,
            max_retries=3,
            client=client,
        )
        request = ChatRequest(
            messages=(ChatMessage("system", "fixed"), ChatMessage("user", "audit")),
            model="synthetic",
            native_tools=(NativeToolDefinition(NativeToolType.WEB_SEARCH),),
        )
        response = await provider.complete(request)
        assert len(wire) == 1
        assert response.content == "" and response.tool_calls == ()
        assert response.status is ModelResponseStatus.COMPLETED
        assert response.total_tokens == 13
        assert response.native_tool_events[0].call_id == "search-original"
        assert response.native_tool_events[0].status is NativeToolStatus.COMPLETED
        assert response.continuation is not None
        transcript = TurnTranscript(request.messages)
        transcript.accept(response.continuation)
        restored = decode_transcript(encode_transcript(transcript)).request()
        replay = replace(
            request,
            messages=restored.messages,
            continuation=restored.continuation,
            continuation_items=restored.items,
        )
        payload = provider._build_payload(replay)
        assert "search-original" in json.dumps(payload)
        assert (
            "original-signature"
            if kind in (GeminiProvider, AnthropicMessagesProvider)
            else "original-opaque"
        ) in json.dumps(payload)
        assert payload == provider._build_payload(replay)
        assert wire[0].get("tools") == payload.get("tools")


@pytest.mark.parametrize("kind", [OpenAIResponsesProvider, DeepSeekResponsesProvider])
@pytest.mark.parametrize("with_native", [False, True])
@pytest.mark.parametrize("same_arguments", [False, True])
async def test_duplicate_local_ids_are_not_executable_and_keep_native_result(
    kind, with_native, same_arguments
):
    body = native_reply(kind, duplicate=True)
    if not with_native:
        body["output"] = [item for item in body["output"] if item["type"] != "web_search_call"]
    if same_arguments:
        body["output"][-1]["arguments"] = body["output"][-2]["arguments"]
    async with httpx.AsyncClient(
        base_url="https://wire.invalid/",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body)),
    ) as client:
        provider = kind(
            base_url="https://wire.invalid",
            api_key="synthetic",
            timeout_seconds=1,
            max_retries=0,
            client=client,
        )
        response = await provider.complete(
            ChatRequest(messages=(ChatMessage("user", "audit"),), model="synthetic")
        )
    assert response.tool_calls == ()
    assert response.status is ModelResponseStatus.INCOMPLETE
    assert response.incomplete_reason == "duplicate_tool_call_id"
    assert response.total_tokens == 13
    if with_native:
        assert response.native_tool_events[0].call_id == "search-original"
    else:
        assert response.native_tool_events == ()
    calls = [item for item in response.continuation.payload if item["type"] == "function_call"]
    assert len(calls) == 2 and calls[0]["call_id"] == calls[1]["call_id"]
    assert (calls[0]["arguments"] == calls[1]["arguments"]) is same_arguments


@pytest.mark.parametrize("kind", [AnthropicMessagesProvider, GeminiProvider])
@pytest.mark.parametrize("same_arguments", [False, True])
@pytest.mark.parametrize("signed", [False, True])
async def test_native_duplicate_claude_gemini_preserves_paid_raw_state(
    kind, same_arguments, signed
):
    body = native_reply(kind, duplicate=True)
    if kind is AnthropicMessagesProvider:
        blocks = body["content"]
        if not signed:
            blocks.pop(0)
        calls = [block for block in blocks if block["type"] == "tool_use"]
        if same_arguments:
            calls[1]["input"] = calls[0]["input"].copy()
    else:
        blocks = body["candidates"][0]["content"]["parts"]
        if not signed:
            for block in blocks:
                block.pop("thoughtSignature", None)
        calls = [block["functionCall"] for block in blocks if "functionCall" in block]
        if same_arguments:
            calls[1]["args"] = calls[0]["args"].copy()
    wire = []

    def transport(request):
        wire.append(json.loads(request.content))
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(
        base_url="https://wire.invalid/", transport=httpx.MockTransport(transport)
    ) as client:
        provider = kind(
            base_url="https://wire.invalid",
            api_key="synthetic",
            timeout_seconds=1,
            max_retries=3,
            client=client,
        )
        response = await provider.complete(
            ChatRequest(
                messages=(ChatMessage("user", "audit"),),
                model="synthetic",
                tools=(ChatTool("write_probe", "write", {"type": "object"}),),
                native_tools=(NativeToolDefinition(NativeToolType.WEB_SEARCH),),
            )
        )
    assert len(wire) == 1
    assert response.status is ModelResponseStatus.INCOMPLETE
    assert response.incomplete_reason == "duplicate_tool_call_id"
    assert response.tool_calls == () and response.total_tokens == 13
    assert response.native_tool_events[0].call_id == "search-original"
    assert response.continuation is not None
    # The whole server response remains in protocol order. No duplicate call
    # is collapsed, and absent signatures are not fabricated on receipt.
    saved = response.continuation.payload[-1]
    assert saved["content" if kind is AnthropicMessagesProvider else "parts"] == blocks
    transcript = TurnTranscript((ChatMessage("user", "audit"),))
    transcript.accept(response.continuation)
    assert decode_transcript(encode_transcript(transcript)).continuation == response.continuation


@pytest.mark.parametrize(
    "kind,duplicate,missing_event",
    [(kind, False, False) for kind in KINDS]
    + [(kind, True, False) for kind in KINDS]
    + [
        (kind, False, True)
        for kind in (
            AnthropicMessagesProvider,
            GeminiProvider,
            OpenAIResponsesProvider,
            OpenAICompatibleProvider,
        )
    ],
)
@pytest.mark.parametrize("checkpoint_failure", [False, True])
async def test_runner_suspends_paid_native_boundary_without_requeue_or_local_execution(
    database, tmp_path, monkeypatch, kind, duplicate, checkpoint_failure, missing_event
):
    if checkpoint_failure:
        original_save = WorkSession.save

        async def fail_received_checkpoint(self, phase, *args, **kwargs):
            if phase == "paired" and self.progress.get("model_observations"):
                error = sqlite3.OperationalError("database is locked")
                error.sqlite_errorcode = sqlite3.SQLITE_BUSY
                raise OperationalError("INSERT", {}, error)
            return await original_save(self, phase, *args, **kwargs)

        monkeypatch.setattr(WorkSession, "save", fail_received_checkpoint)
    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    lease = await repository.acquire(env.context.conversation_id, 1)

    async def validate():
        assert await repository.valid(lease)

    control = WorkControl(repository, lease, "native-boundary", {"trigger_event_id": 1}, validate)
    control.current = await repository.accept(
        lease, source_key="native-boundary", source=control.source, goal="audit sources"
    )
    fixed = (
        ()
        if kind is OpenAICompatibleProvider
        else (ChatTool("write_probe", "Authorized write", {"type": "object"}),)
    )
    harness = build_harness(database, make_settings(database.url), FakeLLMProvider())
    chat = harness.processor._chat
    wire = []

    def transport(req):
        wire.append(json.loads(req.content))
        return httpx.Response(
            200,
            json=empty_native_reply(kind)
            if missing_event
            else native_reply(kind, duplicate=duplicate),
        )

    async with httpx.AsyncClient(
        base_url="https://wire.invalid/", transport=httpx.MockTransport(transport)
    ) as client:
        provider = kind(
            base_url="https://wire.invalid",
            api_key="synthetic",
            timeout_seconds=1,
            max_retries=3,
            client=client,
            **(
                {"options": ChatWireOptions(native_web_search=True)}
                if kind is OpenAICompatibleProvider
                else {}
            ),
        )
        protocol = (
            ModelProtocol.ANTHROPIC_MESSAGES
            if kind is AnthropicMessagesProvider
            else ModelProtocol.GEMINI
            if kind is GeminiProvider
            else ModelProtocol.CHAT_COMPLETIONS
            if kind is OpenAICompatibleProvider
            else ModelProtocol.RESPONSES
        )
        profile = ModelProfile(
            id="native-boundary",
            provider=provider.provider_name,
            protocol=protocol,
            base_url="https://wire.invalid",
            api_key_env="UNUSED",
            model="synthetic",
            timeout_seconds=1,
            max_retries=3,
            default_max_output_tokens=8192,
            default_temperature=0.5,
            wire_options=ChatWireOptions(native_web_search=True)
            if kind is OpenAICompatibleProvider
            else None,
            search_mode=ModelSearchMode.EXTERNAL
            if kind is DeepSeekResponsesProvider
            else ModelSearchMode.NATIVE,
            capabilities=frozenset(
                {
                    ModelCapability.TOOLS,
                    ModelCapability.NATIVE_WEB_SEARCH,
                    ModelCapability.REASONING,
                }
            )
            - ({ModelCapability.NATIVE_WEB_SEARCH} if kind is DeepSeekResponsesProvider else set())
            - ({ModelCapability.TOOLS} if kind is OpenAICompatibleProvider else set()),
        )
        chat.runtime.runner._models = TaskModelExecutor(
            router=ModelRouter(
                ModelProfileCatalog(
                    profiles={profile.id: profile},
                    routes={
                        task: ModelRoute(task=task, profile_id=profile.id) for task in ModelTask
                    },
                )
            ),
            pool=ModelClientPool(injected_profiles={profile.id: provider}),
        )
        backend = type("Backend", (), {})()
        backend.definitions = lambda *args, **kwargs: fixed
        backend.execute = AsyncMock(side_effect=AssertionError("ambiguous calls must not execute"))
        runtime = AgentRuntime(
            origin=TurnOrigin.USER_MESSAGE,
            actor_user_id="1001",
            actor_is_superuser=False,
            delegated_authority=None,
            conversation_key="native-boundary",
            current_group_id=None,
            bot_user_id="9999",
            gateway=None,
            runtime_config=await chat._runtime_config.snapshot(),
            current_time=chat._time.current_default(),
            allowed_capabilities=frozenset({"web"}),
            max_tool_calls=8,
            max_model_requests=8,
            fixed_tools=fixed,
            work_control=control,
            compaction_brief=ChatMessage("user", "audit sources"),
        )
        result = await chat.runtime.runner.run(
            (ChatMessage("system", "fixed"), runtime.compaction_brief), runtime, backend
        )
    assert len(wire) == 1
    backend.execute.assert_not_awaited()
    assert result.work_state == "suspended"
    assert not result.outcome.failure.retryable
    assert result.outcome.failure.code == (
        "LLMInvalidResponseError" if duplicate else "LLMNativeToolError"
    )
    persisted = await repository.get(control.current["id"])
    assert (
        persisted["state"] == "suspended"
        and persisted["model_requests"] == 1
        and persisted["tool_calls"] == 0
    )
    checkpoint = await control.session.journal.load(
        lease, persisted["id"], control.session.contract
    )
    assert checkpoint.pending_calls == ()
    if checkpoint_failure:
        assert result.outcome.failure.diagnostics["checkpoint_saved"] is False
        assert checkpoint.record["phase"] == "dispatched"
        # Only the charged dispatch is durable when publishing its result fails.
        # Recovery suspends instead of treating this as a replayable database plan.
        await repository.release(lease)
        return
    assert checkpoint.record["phase"] == "paired"
    saved = decode_transcript(json.loads(checkpoint.record["payload_json"])["transcript"])
    assert saved.continuation is not None
    encoded = json.dumps(encode_transcript(saved))
    if not missing_event:
        assert "search-original" in encoded
    assert (
        "original-signature"
        if kind in (GeminiProvider, AnthropicMessagesProvider)
        else "original-opaque"
    ) in encoded
    events = control.session.progress["model_observations"][-1]["native_tool_events"]
    assert events == [] if missing_event else events[0]["call_id"] == "search-original"
    await repository.release(lease)


@pytest.mark.parametrize(
    "kind,native_requested",
    [
        (AnthropicMessagesProvider, True),
        (GeminiProvider, True),
        (OpenAIResponsesProvider, True),
        (OpenAICompatibleProvider, True),
        (GeminiProvider, False),
        (OpenAIResponsesProvider, False),
    ],
)
@pytest.mark.parametrize(
    "failure",
    ["timeout", "disconnected", "predispatch", "authentication", "unavailable_with_usage"],
)
async def test_native_transport_unknown_suspends_original_work_without_automatic_replay(
    database, tmp_path, kind, failure, native_requested
):
    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    lease = await repository.acquire(env.context.conversation_id, 1)

    async def validate():
        assert await repository.valid(lease)

    control = WorkControl(repository, lease, "native-transport", {}, validate)
    control.current = await repository.accept(
        lease, source_key="native-transport", source=control.source, goal="audit sources"
    )
    fixed = (
        ()
        if kind is OpenAICompatibleProvider
        else (ChatTool("read_fixture", "read", {"type": "object"}),)
    )
    harness = build_harness(database, make_settings(database.url), FakeLLMProvider())
    chat = harness.processor._chat
    wire = []

    def transport(request):
        wire.append(json.loads(request.content))
        if failure == "authentication":
            return httpx.Response(401, json={"error": "synthetic unauthorized"})
        if failure == "unavailable_with_usage":
            body = (
                native_reply(kind)
                if kind is not OpenAICompatibleProvider
                else empty_native_reply(kind)
            )
            body["error"] = {"message": "synthetic server failure"}
            return httpx.Response(503, json=body)
        if failure == "timeout":
            raise httpx.ReadTimeout("fixture timeout", request=request)
        raise httpx.ConnectError("fixture disconnected", request=request)

    async with httpx.AsyncClient(
        base_url="https://wire.invalid/", transport=httpx.MockTransport(transport)
    ) as client:
        options = (
            ChatWireOptions(native_web_search=True) if kind is OpenAICompatibleProvider else None
        )
        provider = kind(
            base_url="https://wire.invalid",
            api_key="synthetic",
            timeout_seconds=1,
            max_retries=3 if native_requested else 0,
            client=client,
            **({"options": options} if options else {}),
        )
        protocol = (
            ModelProtocol.ANTHROPIC_MESSAGES
            if kind is AnthropicMessagesProvider
            else ModelProtocol.GEMINI
            if kind is GeminiProvider
            else ModelProtocol.CHAT_COMPLETIONS
            if kind is OpenAICompatibleProvider
            else ModelProtocol.RESPONSES
        )
        profile = ModelProfile(
            id="native-transport",
            provider=provider.provider_name,
            protocol=protocol,
            base_url="https://wire.invalid",
            api_key_env="UNUSED",
            model="synthetic",
            timeout_seconds=1,
            max_retries=3,
            default_max_output_tokens=8192,
            default_temperature=0.5,
            wire_options=options,
            search_mode=ModelSearchMode.NATIVE if native_requested else ModelSearchMode.EXTERNAL,
            capabilities=frozenset(
                {
                    ModelCapability.TOOLS,
                    ModelCapability.NATIVE_WEB_SEARCH,
                    ModelCapability.REASONING,
                }
            )
            - ({ModelCapability.TOOLS} if kind is OpenAICompatibleProvider else set()),
        )
        chat.runtime.runner._models = TaskModelExecutor(
            router=ModelRouter(
                ModelProfileCatalog(
                    profiles={profile.id: profile},
                    routes={
                        task: ModelRoute(task=task, profile_id=profile.id) for task in ModelTask
                    },
                )
            ),
            pool=ModelClientPool(injected_profiles={profile.id: provider}),
        )
        backend = type("Backend", (), {})()
        backend.definitions = lambda *_args, **_kwargs: fixed
        backend.execute = AsyncMock(side_effect=AssertionError("no tool execution"))
        before = (
            AsyncMock(side_effect=LLMUnavailableError("admission unavailable"))
            if failure == "predispatch"
            else None
        )
        runtime = AgentRuntime(
            origin=TurnOrigin.USER_MESSAGE,
            actor_user_id="1001",
            actor_is_superuser=False,
            delegated_authority=None,
            conversation_key="native-transport",
            current_group_id=None,
            bot_user_id="9999",
            gateway=None,
            runtime_config=await chat._runtime_config.snapshot(),
            current_time=chat._time.current_default(),
            allowed_capabilities=frozenset({"web"}),
            max_tool_calls=8,
            max_model_requests=8,
            fixed_tools=fixed,
            work_control=control,
            compaction_brief=ChatMessage("user", "audit sources"),
            before_model_request=before,
        )
        result = await chat.runtime.runner.run(
            (ChatMessage("system", "fixed"), runtime.compaction_brief), runtime, backend
        )
    backend.execute.assert_not_awaited()
    persisted = await repository.get(control.current["id"])
    assert persisted["tool_calls"] == 0
    if failure == "predispatch":
        assert not wire and persisted["model_requests"] == 0
        assert result.work_state == "queued" and result.outcome.failure.retryable
        assert result.outcome.failure.code == "LLMUnavailableError"
        assert result.outcome.failure.diagnostics.get("physical_request_count", 0) == 0
    else:
        assert len(wire) == 1 and persisted["model_requests"] == 1
        if not native_requested and failure != "authentication":
            assert result.work_state == "queued" and persisted["state"] == "queued"
            assert result.outcome.failure.retryable
            assert result.outcome.failure.code == (
                "LLMTimeoutError" if failure == "timeout" else "LLMUnavailableError"
            )
            assert result.outcome.failure.diagnostics["physical_request_count"] == 1
            await repository.release(lease)
            return
        assert result.work_state == "suspended" and persisted["state"] == "suspended"
        assert not result.outcome.failure.retryable
        assert result.outcome.failure.code == (
            "LLMAuthenticationError" if failure == "authentication" else "LLMNativeToolError"
        )
        assert result.outcome.failure.diagnostics["physical_request_count"] == 1
        assert result.outcome.failure.diagnostics["unknown_usage_request_count"] == (
            0 if failure == "unavailable_with_usage" else 1
        )
        if failure == "unavailable_with_usage":
            assert result.outcome.failure.diagnostics["usage"]["total_tokens"] == 13
            assert result.outcome.failure.diagnostics["http_status"] == 503
            assert len(result.outcome.failure.diagnostics["body_sha256"]) == 64
        checkpoint = await control.session.journal.load(
            lease, persisted["id"], control.session.contract
        )
        assert checkpoint.record["phase"] == "dispatched"
        assert checkpoint.pending_calls == ()
    await repository.release(lease)


@pytest.mark.parametrize("kind", [OpenAICompatibleProvider, *KINDS])
@pytest.mark.parametrize(
    "output,failed",
    [
        (
            json.dumps(
                {"ok": False, "error_code": "tool_input_validation_failed", "executed": False}
            ),
            True,
        ),
        (
            json.dumps(
                {
                    "ok": True,
                    "process": {"succeeded": False, "exit_code": 127},
                    "data": {"status": "failed"},
                    "mutation_committed": True,
                }
            ),
            True,
        ),
        (json.dumps({"ok": True, "data": {"output": "failed: example text"}}), False),
        (
            json.dumps(
                {
                    "ok": True,
                    "data": {"exit_code": 127},
                    "uncertain": True,
                    "mutation_committed": None,
                }
            ),
            False,
        ),
        ("failed: this is only returned prose", False),
    ],
)
async def test_real_http_failure_projection_preserves_transport_and_effect_facts(
    kind, output, failed
):
    wire = []

    def transport(req):
        wire.append(json.loads(req.content))
        if kind is AnthropicMessagesProvider:
            body = {"stop_reason": "end_turn", "content": [{"type": "text", "text": "observed"}]}
        elif kind is GeminiProvider:
            body = {
                "candidates": [
                    {"finishReason": "STOP", "content": {"parts": [{"text": "observed"}]}}
                ]
            }
        elif kind is OpenAICompatibleProvider:
            body = {"choices": [{"finish_reason": "stop", "message": {"content": "observed"}}]}
        else:
            body = {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "id": "answer",
                        "content": [{"type": "output_text", "text": "observed"}],
                    }
                ],
            }
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(
        base_url="https://wire.invalid/", transport=httpx.MockTransport(transport)
    ) as client:
        adapter = kind(
            base_url="https://wire.invalid",
            api_key="synthetic",
            timeout_seconds=1,
            max_retries=0,
            client=client,
        )
        initial = (ChatMessage("system", "fixed"), ChatMessage("user", "inspect"))
        call = ToolCall("original-call", ToolFunction("terminal_read", "{}"))
        transcript = TurnTranscript(initial)
        if kind is OpenAICompatibleProvider:
            transcript.append(ChatMessage("assistant", tool_calls=(call,)))
        else:
            if kind is AnthropicMessagesProvider:
                native = (
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "thinking",
                                "thinking": "private",
                                "signature": "original-signature",
                            },
                            {
                                "type": "tool_use",
                                "id": call.id,
                                "name": call.function.name,
                                "input": {},
                            },
                        ],
                    },
                )
            elif kind is GeminiProvider:
                native = (
                    {
                        "role": "model",
                        "parts": [
                            {
                                "functionCall": {
                                    "name": call.function.name,
                                    "args": {},
                                    "id": call.id,
                                },
                                "thoughtSignature": "original-signature",
                            }
                        ],
                    },
                )
            else:
                native = (
                    {"type": "reasoning", "id": "reason", "encrypted_content": "original-opaque"},
                    {
                        "type": "function_call",
                        "id": "fc-original",
                        "call_id": call.id,
                        "name": call.function.name,
                        "arguments": "{}",
                    },
                )
            transcript.accept(
                ProviderContinuation(
                    adapter.provider_name,
                    adapter.protocol
                    if kind in (GeminiProvider, AnthropicMessagesProvider)
                    else "responses",
                    native,
                )
            )
        transcript.append_result(call.id, output)
        restored = decode_transcript(encode_transcript(transcript)).request()
        await adapter.complete(
            ChatRequest(
                messages=restored.messages,
                model="synthetic",
                continuation=restored.continuation,
                continuation_items=restored.items,
            )
        )
    payload = wire[0]
    if kind is GeminiProvider:
        value = payload["contents"][-1]["parts"][0]["functionResponse"]
        assert value["id"] == call.id
        assert value["response"] == {"error" if failed else "output": output}
    elif kind is AnthropicMessagesProvider:
        value = payload["messages"][-1]["content"][0]
        assert value["tool_use_id"] == call.id
        assert value["content"] == output
        assert value.get("is_error", False) is failed
    elif kind is OpenAICompatibleProvider:
        assert payload["messages"][-1] == {
            "role": "tool",
            "content": output,
            "tool_call_id": call.id,
        }
    else:
        value = payload["input"][-1]
        assert value == {"type": "function_call_output", "call_id": call.id, "output": output}
    if kind is not OpenAICompatibleProvider:
        assert (
            "original-signature"
            if kind in (GeminiProvider, AnthropicMessagesProvider)
            else "original-opaque"
        ) in json.dumps(payload)
