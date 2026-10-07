"""Native ordinary tails retain public facts and deterministic delivery receipts."""

import json
from unittest.mock import AsyncMock

import httpx
import pytest
from tests.conftest import build_harness, make_settings

# P10: explicit backend/Invocation fixture; original behavioral assertions retained.
from tests.support.agent_backend import StubAgentBackend

from qq_ai_bot.domain.messages import ChatMessage, ChatTool
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.llm.gemini import GeminiProvider
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
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.services.agent_runner import AgentRuntime


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["gemini", "openai_responses"])
async def test_native_public_mirror_and_receipts_survive_empty_summary_without_work(
    database, protocol, caplog
):
    harness = build_harness(
        database,
        make_settings(
            database.url,
            context_window_tokens=65536,
            context_compaction_window_tokens=8192,
            context_compaction_output_tokens=1024,
            web_mode="native",
        ),
        FakeLLMProvider(),
    )
    runner = harness.processor._chat.runtime.runner
    fixed = (ChatTool("send_message", "Send a message", {"type": "object"}),)
    initial = (ChatMessage("system", "fixed contract"), ChatMessage("user", "inspect and report"))
    requests, bodies, summary_sources = [], [], []
    main_calls = 0
    text = "Native public findings. " + "x" * 40000
    calls = ("delivered-call", "unknown-call")

    def transport(request):
        nonlocal main_calls
        bodies.append(json.loads(request.content))
        current = requests[-1]
        if current.structured_output:
            summary_sources.append(json.loads(current.messages[-1].content))
            # An empty summary covers nothing; the original public tail must
            # survive if its complete replacement would not reduce capacity.
            content = json.dumps({"facts": [], "pending": [], "next_steps": []})
        else:
            main_calls += 1
            content = text if main_calls == 1 else "Finished the current review."
        first = not current.structured_output and main_calls == 1
        if protocol == "gemini":
            parts = [{"text": content}]
            if first:
                parts.extend(
                    {
                        "functionCall": {
                            "id": identity,
                            "name": "send_message",
                            "args": {"text": identity},
                        },
                        "thoughtSignature": "private-signature",
                    }
                    for identity in calls
                )
            candidate = {"finishReason": "STOP", "content": {"role": "model", "parts": parts}}
            if first:
                candidate["groundingMetadata"] = {
                    "webSearchQueries": ["public query"],
                    "groundingChunks": [
                        {"web": {"uri": "https://example.org/source", "title": "Public source"}}
                    ],
                }
            response = {"candidates": [candidate]}
        else:
            annotations = (
                [
                    {
                        "type": "url_citation",
                        "url": "https://example.org/source",
                        "title": "Public source",
                    }
                ]
                if first
                else []
            )
            output = [
                {
                    "id": "native-message",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {"type": "output_text", "text": content, "annotations": annotations}
                    ],
                }
            ]
            if first:
                output.insert(
                    0,
                    {
                        "id": "native-search",
                        "type": "web_search_call",
                        "status": "completed",
                        "action": {"type": "search", "query": "public query"},
                    },
                )
                output.extend(
                    {
                        "id": identity,
                        "type": "function_call",
                        "call_id": identity,
                        "name": "send_message",
                        "arguments": json.dumps({"text": identity}),
                        "status": "completed",
                    }
                    for identity in calls
                )
            response = {"id": f"response-{len(bodies)}", "status": "completed", "output": output}
        return httpx.Response(200, json=response)

    async with httpx.AsyncClient(
        base_url="https://native.invalid", transport=httpx.MockTransport(transport)
    ) as client:
        provider_type = GeminiProvider if protocol == "gemini" else OpenAIResponsesProvider
        provider = provider_type(
            base_url="https://native.invalid",
            api_key="synthetic",
            timeout_seconds=2,
            max_retries=0,
            client=client,
        )
        complete = provider.complete

        async def record(request):
            requests.append(request)
            return await complete(request)

        provider.complete = record
        profile = ModelProfile(
            id="native-public-wire",
            provider="gemini" if protocol == "gemini" else "openai",
            protocol=ModelProtocol.GEMINI if protocol == "gemini" else ModelProtocol.RESPONSES,
            base_url="https://native.invalid",
            api_key_env="UNUSED",
            model="gemini-3.8-flash" if protocol == "gemini" else "gpt-synthetic",
            timeout_seconds=2,
            max_retries=0,
            default_temperature=0.5,
            default_max_output_tokens=1024,
            capabilities=frozenset(ModelCapability),
        )
        runner._models = TaskModelExecutor(
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

        async def execute(invocation):
            arguments = invocation.call.function.arguments
            identity = json.loads(arguments)["text"]
            return json.dumps(
                {
                    "ok": identity == calls[0],
                    "data": {
                        "status": "succeeded" if identity == calls[0] else "unknown",
                        "uncertain": identity != calls[0],
                        "target": {"kind": "space", "id": "internal-space"},
                    },
                }
            )

        execute_mock = AsyncMock(side_effect=execute)
        backend = StubAgentBackend(
            definitions=lambda *args, **kwargs: fixed,
            execute_call=execute_mock,
            is_side_effecting=lambda *args: True,
            parallel_safe=lambda *args: False,
            exhausted=lambda *args: "",
            finalize=lambda content, runtime: content,
        )
        runtime = AgentRuntime(
            origin=TurnOrigin.USER_MESSAGE,
            actor_user_id="10001",
            actor_is_superuser=False,
            delegated_authority=None,
            conversation_key="ordinary-native-public",
            current_group_id=None,
            bot_user_id="80001",
            gateway=None,
            runtime_config=await harness.processor._chat._runtime_config.snapshot(),
            current_time=harness.processor._chat._time.current_default(),
            allowed_capabilities=frozenset({"web"}),
            max_tool_calls=8,
            max_model_requests=8,
            fixed_tools=fixed,
        )
        result = await runner.run(initial, runtime, backend)
    assert result.model_requests == len(requests) == 3
    assert execute_mock.await_count == 2 and runtime.work_control is None
    assert len(summary_sources) == 1
    records = {item["ref"]: json.loads(item["text"]) for item in summary_sources[0]["records"]}
    mirror = records["observation:0"]
    assert mirror["content"] == text
    assert {item["id"] for item in mirror["tool_calls"]} == set(calls)
    assert {item["call_id"] for item in mirror["results"]} == set(calls)
    assert mirror["citations"][0]["url"] == "https://example.org/source"
    assert mirror["native_tool_events"][0]["status"] == "completed"
    assert "private-signature" not in json.dumps(summary_sources)
    final = requests[-1]
    assert final.messages[: len(initial)] == initial and final.tools == requests[0].tools
    assert final.continuation is not None
    # The rejected candidate retains the actual original provider continuation,
    # including public native facts and both already-executed tool receipts.
    encoded = json.dumps(bodies[-1], ensure_ascii=False)
    assert text in encoded
    for call_id in calls:
        assert call_id in encoded
    assert "succeeded" in encoded and "unknown" in encoded
