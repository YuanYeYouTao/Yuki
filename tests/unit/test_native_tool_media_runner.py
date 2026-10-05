"""Select tool pixels through the real Runner and inspect actual HTTP payloads."""

import json
from unittest.mock import AsyncMock

import httpx
import pytest
from tests.conftest import build_harness, make_settings
from tests.support.agent_backend import StubAgentBackend

from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.capabilities.media import MediaResultText
from qq_ai_bot.capabilities.results import (
    ToolExecutionResult,
    ToolResultBudgeter,
    normalize_legacy_result,
)
from qq_ai_bot.domain.messages import ChatImage, ChatMessage, ChatTool, ToolCall, ToolFunction
from qq_ai_bot.llm.anthropic_messages import AnthropicMessagesProvider
from qq_ai_bot.llm.deepseek_responses import DeepSeekResponsesProvider
from qq_ai_bot.llm.gemini import GeminiProvider
from qq_ai_bot.llm.openai_compatible import OpenAICompatibleProvider
from qq_ai_bot.llm.openai_responses import OpenAIResponsesProvider
from qq_ai_bot.model_runtime.executor import TaskModelExecutor
from qq_ai_bot.model_runtime.models import (
    ModelCapability,
    ModelProfile,
    ModelProtocol,
    ModelRoute,
    ModelTask,
)
from qq_ai_bot.model_runtime.pool import ModelClientPool
from qq_ai_bot.model_runtime.profiles import ModelProfileCatalog
from qq_ai_bot.model_runtime.routes import ModelRouter
from qq_ai_bot.services.agent_runner import AgentRuntime
from qq_ai_bot.services.turn_transcript import TurnTranscript

PIXELS = "data:image/png;base64,aW1hZ2U="
IMAGE = ChatImage(PIXELS, source="workspace", artifact_id="immutable", version="fixed")


class Backend(StubAgentBackend):
    media_max_bytes = 16_777_216

    def __init__(self):
        self.executed = []
        self.validated = []

    def definitions(self, runtime, **kwargs):
        return (ChatTool("inspect", "Select file pixels", {"type": "object"}),)

    def begin_batch(self, *args):
        pass

    def parallel_safe(self, *args):
        return True

    def is_side_effecting(self, *args):
        return False

    # Test branch uses the typed original-call boundary.
    def counts_toward_limit(self, *_args):
        return True

    async def execute_call(self, invocation):
        self.executed.append(invocation.call.function.name)
        return MediaResultText('{"ok":true,"data":{"mode":"native_image"}}', (IMAGE,))

    async def validate_images(self, images, runtime):
        self.validated.append(images)

    def finalize(self, text, runtime):
        return text

    def exhausted(self, runtime):
        return "exhausted"


KINDS = [
    (
        OpenAICompatibleProvider,
        ModelProtocol.CHAT_COMPLETIONS,
        "openai",
        '"type": "image_url"',
        '"role": "tool"',
    ),
    (
        DeepSeekResponsesProvider,
        ModelProtocol.RESPONSES,
        "deepseek",
        "input_image",
        "function_call_output",
    ),
    (
        OpenAIResponsesProvider,
        ModelProtocol.RESPONSES,
        "openai",
        "input_image",
        "function_call_output",
    ),
    (GeminiProvider, ModelProtocol.GEMINI, "gemini", "inlineData", "functionResponse"),
    (
        AnthropicMessagesProvider,
        ModelProtocol.ANTHROPIC_MESSAGES,
        "anthropic",
        '"type": "image"',
        "tool_result",
    ),
]


@pytest.mark.parametrize("kind,protocol,vendor,image_marker,result_marker", KINDS)
async def test_tool_pixels_enter_original_main_model_after_all_paired_receipts(
    database,
    kind,
    protocol,
    vendor,
    image_marker,
    result_marker,
):
    wires = []

    def transport(request):
        wires.append(json.loads(request.content))
        first = len(wires) == 1
        calls = [{"id": identity, "name": "inspect", "args": {}} for identity in ("a", "b")]
        if protocol == ModelProtocol.GEMINI:
            parts = (
                [{"functionCall": call, "thoughtSignature": "signed"} for call in calls]
                if first
                else [{"text": "done"}]
            )
            body = {
                "candidates": [
                    {"finishReason": "STOP", "content": {"role": "model", "parts": parts}}
                ]
            }
        elif protocol == ModelProtocol.ANTHROPIC_MESSAGES:
            content = (
                [
                    {"type": "tool_use", "id": call["id"], "name": call["name"], "input": {}}
                    for call in calls
                ]
                if first
                else [{"type": "text", "text": "done"}]
            )
            body = {
                "id": "message",
                "type": "message",
                "role": "assistant",
                "stop_reason": "tool_use" if first else "end_turn",
                "content": content,
                "usage": {"input_tokens": 10, "output_tokens": 2},
            }
        elif protocol == ModelProtocol.RESPONSES:
            output = (
                [
                    {
                        "type": "function_call",
                        "id": call["id"],
                        "call_id": call["id"],
                        "name": "inspect",
                        "arguments": "{}",
                        "status": "completed",
                    }
                    for call in calls
                ]
                if first
                else [
                    {
                        "type": "message",
                        "id": "done",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": "done"}],
                    }
                ]
            )
            body = {"id": f"response-{len(wires)}", "status": "completed", "output": output}
        else:
            message = {"role": "assistant", "content": "" if first else "done"}
            if first:
                message["tool_calls"] = [
                    {
                        "id": call["id"],
                        "type": "function",
                        "function": {"name": "inspect", "arguments": "{}"},
                    }
                    for call in calls
                ]
            body = {
                "choices": [
                    {"message": message, "finish_reason": "tool_calls" if first else "stop"}
                ]
            }
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(
        base_url="https://wire.invalid", transport=httpx.MockTransport(transport)
    ) as client:
        kwargs = {"provider_name": vendor} if kind is OpenAICompatibleProvider else {}
        provider = kind(
            base_url="https://wire.invalid",
            api_key="synthetic",
            timeout_seconds=1,
            max_retries=0,
            client=client,
            **kwargs,
        )
        chat = build_harness(database, make_settings(database.url), provider).processor._chat
        profile = ModelProfile(
            id="main",
            provider=vendor,
            protocol=protocol,
            base_url="https://wire.invalid",
            api_key_env="UNUSED",
            model="main-image-model",
            default_temperature=0.5,
            default_max_output_tokens=512,
            timeout_seconds=1,
            max_retries=0,
            capabilities=frozenset(ModelCapability) - {ModelCapability.NATIVE_WEB_SEARCH},
        )
        chat.runtime.runner._models = TaskModelExecutor(
            router=ModelRouter(
                ModelProfileCatalog(
                    profiles={"main": profile},
                    routes={task: ModelRoute(task=task, profile_id="main") for task in ModelTask},
                )
            ),
            pool=ModelClientPool(injected_profiles={"main": provider}),
        )
        runtime = AgentRuntime(
            origin=TurnOrigin.USER_MESSAGE,
            actor_user_id="synthetic",
            actor_is_superuser=False,
            delegated_authority=None,
            conversation_key="synthetic",
            current_group_id=None,
            bot_user_id="synthetic",
            gateway=None,
            runtime_config=await chat._runtime_config.snapshot(),
            current_time=chat._time.current_default(),
            allowed_capabilities=frozenset(),
            max_tool_calls=4,
            max_model_requests=2,
        )
        backend = Backend()
        result = await chat.runtime.runner.run(
            (ChatMessage("user", "read the selected file"),), runtime, backend
        )
    assert result.text == "done" and len(wires) == 2
    assert backend.executed == ["inspect"]  # Same batch alias keeps its pixels.
    assert backend.validated == [(IMAGE,)]
    assert wires[0]["tools"] == wires[1]["tools"]
    encoded = json.dumps(wires[1])
    assert encoded.count(image_marker) == 1
    assert encoded.rfind(result_marker) < encoded.find(image_marker)
    assert "native_image" in encoded
    if protocol == ModelProtocol.GEMINI:
        assert "signed" in encoded


async def test_media_carrier_never_enters_public_json_or_budget_accounting():
    original = MediaResultText('{"ok":true,"data":{"mode":"native_image"}}', (IMAGE,))
    outcome = normalize_legacy_result(original, provider_id="core", tool_name="inspect")
    assert outcome.images == (IMAGE,)
    rendered = await ToolResultBudgeter(max_characters=128).render(outcome)
    assert rendered.text.images == (IMAGE,)
    assert "base64" not in rendered.text and "images" not in outcome.model_payload()
    assert PIXELS not in repr(ToolExecutionResult(ok=True, images=(IMAGE,)))


async def test_image_archive_failure_preserves_successful_remote_effect():
    store = type(
        "Store", (), {"write_media_artifact": AsyncMock(side_effect=ValueError("denied"))}
    )()
    original = ToolExecutionResult(
        ok=True, mutation_committed=True, images=(IMAGE,), provider_id="mcp", tool_name="screenshot"
    )
    result = await ToolResultBudgeter(max_characters=1000, artifacts=store).render(original)
    value = json.loads(result.text)
    assert value["ok"] is True and value["mutation_committed"] is True
    assert value["metadata"]["media_read"] is False
    assert value["metadata"]["media_error"] == "media_artifact_unavailable"
    assert result.text.images == ()


async def test_capability_and_whole_request_budget_fail_explicitly(database):
    from qq_ai_bot.llm.fake import FakeLLMProvider

    chat = build_harness(database, make_settings(database.url), FakeLLMProvider()).processor._chat
    runner = chat.runtime.runner
    config = await chat._runtime_config.snapshot()
    runtime = type("Runtime", (), {"runtime_config": config})()
    call = ToolCall("one", ToolFunction("inspect", "{}"))
    original = MediaResultText('{"ok":true}', (IMAGE,))
    runner._models.capabilities = lambda task: frozenset()
    batch = runner._budget_tool_media(
        ((call, original, True),), TurnTranscript(()), runtime, Backend()
    )
    assert json.loads(batch[0][1])["error"] == "image_capability_unavailable"
    runner._models.capabilities = lambda task: frozenset({ModelCapability.IMAGE_INPUT})
    backend = Backend()
    backend.media_max_bytes = 1
    batch = runner._budget_tool_media(
        ((call, original, True),), TurnTranscript(()), runtime, backend
    )
    assert json.loads(batch[0][1])["error"] == "media_request_budget_exceeded"


async def test_previously_dispatched_pixels_are_still_guarded_in_opaque_continuation():
    from qq_ai_bot.domain.messages import ProviderContinuation
    from qq_ai_bot.llm.base import LLMError
    from qq_ai_bot.services.agent_runner import AgentRunner

    transcript = TurnTranscript((ChatMessage("user", "read"),))
    transcript.accept(ProviderContinuation("gemini", "gemini", (), "main"))
    transcript.append_tool_media((("one", MediaResultText("receipt", (IMAGE,))),))
    transcript.accept(ProviderContinuation("gemini", "gemini", (), "main"))
    assert not transcript.request().items
    backend = Backend()
    backend.validate_images = AsyncMock(side_effect=LLMError("source_deleted"))
    with pytest.raises(LLMError, match="source_deleted"):
        await AgentRunner._validate_tool_media(backend, None, transcript.request(), (IMAGE,))


async def test_restored_tool_pixels_and_new_input_share_dispatch_budget(database):
    from dataclasses import replace

    from qq_ai_bot.llm.fake import FakeLLMProvider
    from qq_ai_bot.runtime.work_repository import WorkCapacityError
    from qq_ai_bot.services.agent_runner import AgentRunner

    chat = build_harness(database, make_settings(database.url), FakeLLMProvider()).processor._chat
    config = await chat._runtime_config.snapshot()
    runtime = type("Runtime", (), {"runtime_config": config})()
    restored = replace(IMAGE, source="tool", tool_handle="accepted-original")
    current = replace(IMAGE, source="current", source_event_id=2)
    backend = Backend()
    backend.media_max_bytes = len(restored.data_url)
    AgentRunner._check_request_media_budget((restored,), runtime, backend)
    with pytest.raises(WorkCapacityError, match="media_request_budget_exceeded"):
        AgentRunner._check_request_media_budget((restored, current), runtime, backend)


async def test_worker_wrapper_retains_private_pixels_and_delegates_source_guard():
    from types import SimpleNamespace

    from qq_ai_bot.capabilities.invocation import direct_invocations
    from qq_ai_bot.llm.base import LLMError
    from qq_ai_bot.services.subagent_execution import WorkerBackend

    backend = Backend()
    backend.media_max_bytes = 123
    worker = WorkerBackend(backend, frozenset({"inspect"}))
    runtime = SimpleNamespace(work_control=None)
    invocation = direct_invocations((ToolCall("image", ToolFunction("inspect", "{}")),), runtime)[0]
    result = await worker.execute_call(invocation)
    assert result.images == (IMAGE,) and worker.media_max_bytes == 123
    await worker.validate_images(result.images, runtime)
    assert backend.validated == [(IMAGE,)]
    backend.validate_images = AsyncMock(side_effect=LLMError("source_deleted"))
    with pytest.raises(LLMError, match="source_deleted"):
        await worker.validate_images(result.images, runtime)
    denied = direct_invocations((ToolCall("denied", ToolFunction("undeclared", "{}")),), runtime)[0]
    assert json.loads(await worker.execute_call(denied))["error"] == "worker_tool_not_declared"
    assert backend.executed == ["inspect"]
