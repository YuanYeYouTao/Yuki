"""Standard Responses transport controls and provider-separated continuations."""

import json
from dataclasses import replace

import httpx
import pytest

from qq_ai_bot.domain.messages import ChatMessage, ChatRequest, NativeToolDefinition, NativeToolType
from qq_ai_bot.llm.base import LLMInvalidRequestError, LLMUnsupportedFeatureError
from qq_ai_bot.llm.deepseek_responses import DeepSeekResponsesProvider
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


async def standard_responses_cases():
    for provider_name in ("openai", "openai_compatible"):
        captured = []

        def transport(request, captured=captured):
            body = json.loads(request.content)
            captured.append(body)
            assert request.url.path == "/responses"
            assert body["store"] is False
            assert body["include"] == ["reasoning.encrypted_content"]
            assert body["tools"] == [{"type": "web_search"}]
            assert body["tool_choice"] == ("auto" if len(captured) == 1 else "none")
            return httpx.Response(
                200,
                json={
                    "id": f"resp-{len(captured)}",
                    "status": "completed",
                    "output": [
                        {
                            "id": f"reason-{len(captured)}",
                            "type": "reasoning",
                            "summary": [],
                            "encrypted_content": "opaque",
                        },
                        {
                            "id": f"msg-{len(captured)}",
                            "type": "message",
                            "role": "assistant",
                            "status": "completed",
                            "content": [{"type": "output_text", "text": "done"}],
                        },
                    ],
                },
            )

        profile = ModelProfile(
            id="standard",
            provider=provider_name,
            protocol=ModelProtocol.RESPONSES,
            base_url="https://responses.example",
            api_key_env="TEST_ONLY",
            model="test-model",
            timeout_seconds=1,
            max_retries=0,
            default_temperature=0.5,
            default_max_output_tokens=100,
            capabilities=frozenset(ModelCapability),
        )
        pool = ModelClientPool(secret_overrides={"TEST_ONLY": "local-test"})
        executor = TaskModelExecutor(
            router=ModelRouter(
                ModelProfileCatalog(
                    profiles={profile.id: profile},
                    routes={
                        task: ModelRoute(task=task, profile_id=profile.id) for task in ModelTask
                    },
                )
            ),
            pool=pool,
        )
        try:
            adapter = pool.get(profile)
            assert isinstance(adapter, OpenAIResponsesProvider)
            assert pool.get(profile) is adapter
            async with httpx.AsyncClient(
                base_url=profile.base_url, transport=httpx.MockTransport(transport)
            ) as client:
                adapter._client = client
                request = ChatRequest(
                    model=profile.model,
                    messages=(ChatMessage(role="user", content="test"),),
                    native_tools=(NativeToolDefinition(type=NativeToolType.WEB_SEARCH),),
                    tool_choice="auto",
                )
                first = await executor.execute(ModelTask.CHAT_AGENT, request)
                assert first.continuation is not None
                assert first.continuation.provider == provider_name
                await executor.execute(
                    ModelTask.CHAT_AGENT,
                    replace(request, continuation=first.continuation, tool_choice="none"),
                )
                assert captured[1]["input"][: len(captured[0]["input"])] == captured[0]["input"]
                assert any(
                    item.get("encrypted_content") == "opaque" for item in captured[1]["input"]
                )
                assert captured[0]["tools"] == captured[1]["tools"]
                with pytest.raises(LLMInvalidRequestError):
                    DeepSeekResponsesProvider._continuation_items(first.continuation)
                with pytest.raises(LLMInvalidRequestError):
                    adapter._continuation_items(replace(first.continuation, provider="deepseek"))
                unsupported = DeepSeekResponsesProvider(
                    base_url=profile.base_url,
                    api_key="local-test",
                    timeout_seconds=1,
                    max_retries=0,
                    client=client,
                )
                with pytest.raises(LLMInvalidRequestError, match="cannot disable"):
                    await unsupported.complete(replace(request, tool_choice="none"))
                assert len(captured) == 2
                # Raw calls cannot bypass the effective native capability by
                # skipping AgentRunner's native binder or advertising a stale bit.
                for unavailable in (
                    profile.model_copy(update={"provider": "deepseek"}),
                    profile.model_copy(
                        update={
                            "capabilities": profile.capabilities
                            - {
                                ModelCapability.NATIVE_WEB_SEARCH,
                            }
                        }
                    ),
                ):
                    denied = TaskModelExecutor(
                        router=ModelRouter(
                            ModelProfileCatalog(
                                profiles={profile.id: unavailable},
                                routes={
                                    task: ModelRoute(task=task, profile_id=profile.id)
                                    for task in ModelTask
                                },
                            )
                        ),
                        pool=pool,
                    )
                    with pytest.raises(
                        LLMUnsupportedFeatureError, match="effective model contract"
                    ):
                        await denied.execute(ModelTask.CHAT_AGENT, request)
                    assert len(captured) == 2
        finally:
            await pool.close()
