"""Hermetic four-protocol wire fixtures migrated from the supplied correctness audit."""

import json

import httpx

from qq_ai_bot.llm.anthropic_messages import AnthropicMessagesProvider
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

KINDS = {
    "gemini": (GeminiProvider, "gemini", ModelProtocol.GEMINI),
    "anthropic": (AnthropicMessagesProvider, "anthropic", ModelProtocol.ANTHROPIC_MESSAGES),
    "chat": (OpenAICompatibleProvider, "openai", ModelProtocol.CHAT_COMPLETIONS),
    "responses": (OpenAIResponsesProvider, "openai", ModelProtocol.RESPONSES),
}


def body(kind, reply, index=0):
    if kind == "gemini":
        parts = ([{"text": reply.content}] if reply.content else []) + [
            {
                "functionCall": {
                    "id": c.id,
                    "name": c.function.name,
                    "args": json.loads(c.function.arguments),
                },
                "thoughtSignature": "signature-" + c.id,
            }
            for c in reply.tool_calls
        ]
        return {
            "candidates": [{"finishReason": "STOP", "content": {"role": "model", "parts": parts}}]
        }
    if kind == "anthropic":
        blocks = [{"type": "text", "text": reply.content}] if reply.content else []
        if reply.tool_calls:
            blocks += [
                {
                    "type": "thinking",
                    "thinking": "synthetic private",
                    "signature": "signature-" + reply.tool_calls[0].id,
                }
            ]
        blocks += [
            {
                "type": "tool_use",
                "id": c.id,
                "name": c.function.name,
                "input": json.loads(c.function.arguments),
            }
            for c in reply.tool_calls
        ]
        return {
            "id": f"a-{index}",
            "stop_reason": "tool_use" if reply.tool_calls else "end_turn",
            "content": blocks,
        }
    if kind == "chat":
        msg = {"role": "assistant", "content": reply.content or None}
        if reply.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.function.name, "arguments": c.function.arguments},
                }
                for c in reply.tool_calls
            ]
        return {
            "id": f"c-{index}",
            "choices": [
                {"finish_reason": "tool_calls" if reply.tool_calls else "stop", "message": msg}
            ],
        }
    out = [
        {
            "id": f"r-{index}",
            "type": "reasoning",
            "summary": [],
            "encrypted_content": f"signature-{index}",
        }
    ]
    out += [
        {
            "type": "function_call",
            "id": "fc-" + c.id,
            "call_id": c.id,
            "name": c.function.name,
            "arguments": c.function.arguments,
            "status": "completed",
        }
        for c in reply.tool_calls
    ]
    if reply.content:
        out += [
            {
                "type": "message",
                "id": f"m-{index}",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": reply.content, "annotations": []}],
            }
        ]
    return {"id": f"resp-{index}", "status": "completed", "output": out}


def wire(test_case, kind):
    captured = []

    def transport(req):
        captured.append(json.loads(req.content))
        return httpx.Response(
            200,
            json=body(
                kind, test_case.provider._responder(test_case.provider.requests[-1]), len(captured)
            ),
        )

    client = httpx.AsyncClient(
        base_url="https://wire.invalid", transport=httpx.MockTransport(transport)
    )
    cls, vendor, protocol = KINDS[kind]
    provider = cls(
        base_url="https://wire.invalid",
        api_key="synthetic",
        timeout_seconds=2,
        max_retries=0,
        client=client,
        **({"provider_name": "openai"} if kind == "chat" else {}),
    )

    async def complete(request):
        test_case.provider.requests.append(request)
        return await provider.complete(request)

    test_case.provider.complete = complete
    profile = ModelProfile(
        id="audit-" + kind,
        provider=vendor,
        protocol=protocol,
        base_url="https://wire.invalid",
        api_key_env="UNUSED",
        model="synthetic-model",
        timeout_seconds=2,
        max_retries=0,
        default_temperature=0.5,
        default_max_output_tokens=1024,
        capabilities=frozenset({ModelCapability.TOOLS, ModelCapability.REASONING}),
    )
    test_case.runner._models = TaskModelExecutor(
        router=ModelRouter(
            ModelProfileCatalog(
                profiles={profile.id: profile},
                routes={task: ModelRoute(task=task, profile_id=profile.id) for task in ModelTask},
            )
        ),
        pool=ModelClientPool(injected_profiles={profile.id: test_case.provider}),
    )
    return client, captured


def strip_cache(value):
    if isinstance(value, dict):
        return {k: strip_cache(v) for k, v in value.items() if k != "cache_control"}
    if isinstance(value, list):
        return [strip_cache(v) for v in value]
    return value


def parts(payload, kind):
    if kind == "gemini":
        return [(m["role"], p) for m in payload["contents"] for p in m["parts"]]
    if kind == "anthropic":
        return [(m["role"], strip_cache(p)) for m in payload["messages"] for p in m["content"]]
    return payload["input" if kind == "responses" else "messages"]


def settings(payload, kind):
    return {
        k: v
        for k, v in payload.items()
        if k != ("input" if kind == "responses" else "contents" if kind == "gemini" else "messages")
    }
