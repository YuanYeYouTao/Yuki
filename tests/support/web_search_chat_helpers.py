"""End-to-end controlled web search and backend source display tests."""

from __future__ import annotations

import json

import httpx

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
