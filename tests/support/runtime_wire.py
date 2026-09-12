"""Exercise the real provider serializers around deterministic runtime fixtures."""

import json
from dataclasses import asdict

import httpx

from qq_ai_bot.llm.deepseek_responses import DeepSeekResponsesProvider
from qq_ai_bot.llm.openai_compatible import OpenAICompatibleProvider
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


def install_wire(chat, fake, protocol):
    captured = []

    def transport(request):
        captured.append(json.loads(request.content))
        response = fake._responder(fake.requests[-1])
        if protocol == "responses":
            output = [
                {
                    "type": "function_call",
                    "id": c.id,
                    "call_id": c.id,
                    "name": c.function.name,
                    "arguments": c.function.arguments,
                    "status": "completed",
                }
                for c in response.tool_calls
            ]
            if not output:
                output = [
                    {
                        "type": "message",
                        "id": f"msg-{len(captured)}",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": response.content}],
                    }
                ]
            payload = {"id": f"r-{len(captured)}", "status": "completed", "output": output}
        else:
            message = {"role": "assistant", "content": response.content}
            if response.tool_calls:
                message["tool_calls"] = [asdict(c) for c in response.tool_calls]
            payload = {
                "choices": [
                    {
                        "message": message,
                        "finish_reason": "tool_calls" if response.tool_calls else "stop",
                    }
                ]
            }
        return httpx.Response(200, json=payload)

    client = httpx.AsyncClient(
        base_url="https://runtime.example", transport=httpx.MockTransport(transport)
    )
    provider_type = (
        DeepSeekResponsesProvider if protocol == "responses" else OpenAICompatibleProvider
    )
    provider = provider_type(
        base_url="https://runtime.example",
        api_key="test",
        timeout_seconds=2,
        max_retries=0,
        client=client,
    )

    async def complete(request):
        fake.requests.append(request)
        return await provider.complete(request)

    fake.complete = complete
    profile = ModelProfile(
        id="runtime-wire",
        provider="deepseek",
        protocol=ModelProtocol(protocol),
        base_url="https://runtime.example",
        api_key_env="UNUSED",
        model="deepseek-v4-flash",
        timeout_seconds=2,
        max_retries=0,
        default_temperature=0.5,
        default_max_output_tokens=1024,
        capabilities=frozenset(ModelCapability),
    )
    models = TaskModelExecutor(
        router=ModelRouter(
            ModelProfileCatalog(
                profiles={profile.id: profile},
                routes={task: ModelRoute(task=task, profile_id=profile.id) for task in ModelTask},
            )
        ),
        pool=ModelClientPool(injected_profiles={profile.id: fake}),
    )
    chat._agent_runner._models = models
    chat._models = models
    return client, captured
