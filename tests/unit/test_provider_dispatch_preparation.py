"""One physical fence/decoded body and an owned normalized capacity preparation."""

import asyncio
from dataclasses import replace

import httpx
import pytest

from yuki.domain.messages import ChatMessage, ChatRequest, ChatResponse
from yuki.llm.anthropic_messages import AnthropicMessagesProvider
from yuki.llm.base import LLMError, LLMProvider, LLMUnsupportedFeatureError
from yuki.llm.fake import FakeLLMProvider
from yuki.llm.gemini import GeminiProvider
from yuki.llm.openai_compatible import OpenAICompatibleProvider
from yuki.llm.openai_responses import OpenAIResponsesProvider
from yuki.model_runtime import executor as executor_module
from yuki.model_runtime.dispatch_guard import check_model_dispatch, model_dispatch_guard
from yuki.model_runtime.executor import TaskModelExecutor
from yuki.model_runtime.models import ModelProfile, ModelRoute, ModelTask
from yuki.model_runtime.pool import ModelClientPool
from yuki.model_runtime.profiles import ModelProfileCatalog
from yuki.model_runtime.request_accounting import after_provider_request
from yuki.model_runtime.routes import ModelRouter


def catalog(**changes):
    profile = ModelProfile(
        id="main",
        provider="fake",
        model="offline",
        timeout_seconds=1,
        max_retries=0,
        default_temperature=0.7,
        default_max_output_tokens=128,
        **changes,
    )
    return ModelProfileCatalog(
        profiles={profile.id: profile},
        routes={task: ModelRoute(task=task, profile_id=profile.id) for task in ModelTask},
    )


def executor(provider, *, profiles=None, **kwargs):
    return TaskModelExecutor(
        router=ModelRouter(profiles or catalog()),
        pool=ModelClientPool(injected_profiles={"main": provider}),
        **kwargs,
    )


CHAT_BODY = {
    "choices": [{"message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
}


@pytest.mark.parametrize(
    ("kind", "body"),
    [
        (OpenAICompatibleProvider, CHAT_BODY),
        (
            OpenAIResponsesProvider,
            {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {"type": "output_text", "text": "done"},
                        ],
                    }
                ],
                "usage": {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
            },
        ),
        (
            AnthropicMessagesProvider,
            {
                "content": [{"type": "text", "text": "done"}],
                "stop_reason": "end_turn",
                "usage": {
                    "input_tokens": 4,
                    "output_tokens": 2,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
            },
        ),
        (
            GeminiProvider,
            {
                "candidates": [
                    {
                        "content": {"role": "model", "parts": [{"text": "done"}]},
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 4,
                    "candidatesTokenCount": 2,
                    "totalTokenCount": 6,
                },
            },
        ),
    ],
)
@pytest.mark.parametrize("finish_hook", [False, True])
async def test_success_body_decoded_once_for_usage_parser_and_optional_hook(
    monkeypatch, kind, body, finish_hook
):
    decoded = []
    original = httpx.Response.json

    def decode(response, **kwargs):
        decoded.append(response)
        return original(response, **kwargs)

    monkeypatch.setattr(httpx.Response, "json", decode)
    finished = []

    async def finish(status, output):
        finished.append((status, output))

    async with httpx.AsyncClient(
        base_url="https://offline.invalid",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)),
    ) as client:
        provider = kind(
            base_url="https://offline.invalid",
            api_key="synthetic",
            timeout_seconds=1,
            max_retries=0,
            client=client,
        )
        token = after_provider_request.set(finish if finish_hook else None)
        try:
            result = await provider.complete(
                ChatRequest((ChatMessage("user", "original input"),), model="offline")
            )
        finally:
            after_provider_request.reset(token)
    assert result.content == "done"
    assert (result.prompt_tokens, result.completion_tokens, result.total_tokens) == (4, 2, 6)
    assert len(decoded) == 1
    assert bool(finished) is finish_hook
    if finish_hook:
        assert finished[0][1] == 2


async def test_retries_have_one_fresh_guard_per_physical_attempt():
    physical, guards, encodings = [], [], []

    class CountingProvider(OpenAICompatibleProvider):
        def _build_payload(self, request):
            payload = super()._build_payload(request)
            encodings.append(payload)
            return payload

    def transport(request):
        physical.append(request)
        return (
            httpx.Response(503, json={})
            if len(physical) == 1
            else httpx.Response(200, json=CHAT_BODY)
        )

    async def check():
        guards.append(len(physical))

    async with httpx.AsyncClient(
        base_url="https://offline.invalid", transport=httpx.MockTransport(transport)
    ) as client:
        provider = CountingProvider(
            base_url="https://offline.invalid",
            api_key="synthetic",
            timeout_seconds=1,
            max_retries=1,
            client=client,
        )
        models = executor(provider)
        try:
            with model_dispatch_guard(check):
                result = await models.execute(
                    ModelTask.CHAT_AGENT, ChatRequest((ChatMessage("user", "x"),))
                )
        finally:
            await models.close()
    assert result.content == "done"
    assert guards == [0, 1]
    assert len(physical) == 2
    assert physical[0].content == physical[1].content
    assert len(encodings) == 1


async def test_revocation_during_retry_prevents_the_next_physical_request():
    physical = []

    def transport(request):
        physical.append(request)
        return httpx.Response(503, json={})

    async def check():
        if physical:
            raise LLMError("source revoked after first attempt")

    async with httpx.AsyncClient(
        base_url="https://offline.invalid", transport=httpx.MockTransport(transport)
    ) as client:
        provider = OpenAICompatibleProvider(
            base_url="https://offline.invalid",
            api_key="synthetic",
            timeout_seconds=1,
            max_retries=1,
            client=client,
        )
        models = executor(provider)
        try:
            with model_dispatch_guard(check), pytest.raises(LLMError, match="source revoked"):
                await models.execute(ModelTask.CHAT_AGENT, ChatRequest((ChatMessage("user", "x"),)))
        finally:
            await models.close()
    assert len(physical) == 1


async def test_non_http_provider_rechecks_after_slot_admission():
    started, proceed = asyncio.Event(), asyncio.Event()
    requests = []

    class BlockingProvider(LLMProvider):
        async def complete(self, request):
            await check_model_dispatch()
            requests.append(request)
            started.set()
            await proceed.wait()
            return ChatResponse("done", 0)

    models = executor(BlockingProvider(), max_concurrency=1)
    first = asyncio.create_task(models.execute(ModelTask.CHAT_AGENT, ChatRequest(())))
    await started.wait()
    revoked = False

    async def check():
        if revoked:
            raise LLMError("queued source revoked")

    async def second():
        with model_dispatch_guard(check):
            return await models.execute(ModelTask.CHAT_AGENT, ChatRequest(()))

    queued = asyncio.create_task(second())
    await asyncio.sleep(0)
    revoked = True
    proceed.set()
    try:
        assert (await first).content == "done"
        with pytest.raises(LLMError, match="queued source revoked"):
            await queued
        assert len(requests) == 1
    finally:
        await models.close()


async def test_prepared_capacity_is_reused_only_for_its_exact_request_and_profile(monkeypatch):
    estimates = []
    original = executor_module.estimate_request_tokens

    def measure(request):
        estimates.append(request)
        return original(request)

    monkeypatch.setattr(executor_module, "estimate_request_tokens", measure)
    provider = FakeLLMProvider(lambda request: "done")
    models = executor(provider, profiles=catalog(max_input_tokens=1000))
    prepared = models.prepare_capacity(
        ModelTask.CHAT_AGENT, ChatRequest((ChatMessage("user", "x"),))
    )
    try:
        await models.execute(ModelTask.CHAT_AGENT, prepared.request, prepared_capacity=prepared)
        assert estimates == [prepared.request]
        changed = replace(prepared.request, messages=(ChatMessage("user", "x" * 6000),))
        with pytest.raises(LLMUnsupportedFeatureError, match="input capacity"):
            await models.execute(ModelTask.CHAT_AGENT, changed, prepared_capacity=prepared)
        assert estimates[-1] == changed
        assert len(provider.requests) == 1
        models.apply_catalog(
            catalog(max_input_tokens=1), ModelClientPool(injected_profiles={"main": provider})
        )
        with pytest.raises(LLMUnsupportedFeatureError, match="input capacity"):
            await models.execute(ModelTask.CHAT_AGENT, prepared.request, prepared_capacity=prepared)
        assert len(estimates) == 3
    finally:
        await models.close()
