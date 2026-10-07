"""Offline real adapters/real executor/real SQL aggregate; no sockets or credentials."""

import json

import httpx

from qq_ai_bot.domain.messages import ChatMessage, ChatRequest
from qq_ai_bot.llm.anthropic_messages import AnthropicMessagesProvider
from qq_ai_bot.llm.deepseek_responses import DeepSeekResponsesProvider
from qq_ai_bot.llm.gemini import GeminiProvider
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
from qq_ai_bot.model_runtime.repository import ModelInvocationRepository
from qq_ai_bot.model_runtime.routes import ModelRouter
from qq_ai_bot.persistence.control_activity_query import ControlActivityQueryAdapter
from qq_ai_bot.persistence.database import Database

KINDS = {
    "chat": (OpenAICompatibleProvider, ModelProtocol.CHAT_COMPLETIONS, "openai"),
    "responses": (DeepSeekResponsesProvider, ModelProtocol.RESPONSES, "deepseek"),
    "claude": (AnthropicMessagesProvider, ModelProtocol.ANTHROPIC_MESSAGES, "anthropic"),
    "gemini": (GeminiProvider, ModelProtocol.GEMINI, "gemini"),
}


def body(kind, cache=60, total=100, pause=False):
    if kind == "chat":
        return {
            "choices": [
                {"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
            ],
            "usage": {
                "prompt_tokens": total,
                "completion_tokens": 10,
                "total_tokens": total + 10,
                **(
                    {"prompt_tokens_details": {"cached_tokens": cache}} if cache is not None else {}
                ),
            },
        }
    if kind == "responses":
        return {
            "id": "synthetic",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "ok"}],
                }
            ],
            "usage": {
                "input_tokens": total,
                "output_tokens": 10,
                "total_tokens": total + 10,
                **({"input_tokens_details": {"cached_tokens": cache}} if cache is not None else {}),
            },
        }
    if kind == "gemini":
        return {
            "candidates": [
                {"content": {"role": "model", "parts": [{"text": "ok"}]}, "finishReason": "STOP"}
            ],
            "usageMetadata": {
                "promptTokenCount": total,
                "candidatesTokenCount": 10,
                "totalTokenCount": total + 10,
                **({"cachedContentTokenCount": cache} if cache is not None else {}),
            },
        }
    return {
        "id": "synthetic",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "pause_turn" if pause else "end_turn",
        "usage": {
            "input_tokens": total - (cache or 0) - 20,
            "output_tokens": 10,
            "cache_creation_input_tokens": 20,
            "cache_creation": {"ephemeral_5m_input_tokens": 15, "ephemeral_1h_input_tokens": 5},
            **({"cache_read_input_tokens": cache} if cache is not None else {}),
        },
    }


class Capture:
    def __init__(self):
        self.records = []

    async def record(self, **kw):
        self.records.append(kw)


async def invoke(kind, sequence, retries=0):
    cls, protocol, vendor = KINDS[kind]
    wires = []
    capture = Capture()

    def transport(req):
        wires.append({"path": req.url.path, "body": json.loads(req.content)})
        status, payload = sequence[len(wires) - 1]
        return httpx.Response(status, json=payload)

    async with httpx.AsyncClient(
        base_url="https://synthetic.invalid/", transport=httpx.MockTransport(transport)
    ) as client:
        kwargs = {"provider_name": "openai"} if kind == "chat" else {}
        provider = cls(
            base_url="https://synthetic.invalid/",
            api_key="synthetic-placeholder",
            timeout_seconds=1,
            max_retries=retries,
            client=client,
            **kwargs,
        )
        profile = ModelProfile(
            id="audit",
            provider=vendor,
            protocol=protocol,
            model="synthetic-model",
            base_url="https://synthetic.invalid/",
            api_key_env="UNUSED",
            timeout_seconds=1,
            max_retries=retries,
            default_temperature=0.1,
            default_max_output_tokens=100,
            capabilities=frozenset(ModelCapability) - {ModelCapability.NATIVE_WEB_SEARCH},
        )
        executor = TaskModelExecutor(
            router=ModelRouter(
                ModelProfileCatalog(
                    profiles={"audit": profile},
                    routes={t: ModelRoute(task=t, profile_id="audit") for t in ModelTask},
                )
            ),
            pool=ModelClientPool(injected_profiles={"audit": provider}),
            invocations=capture,
        )
        error = None
        try:
            await executor.execute(
                ModelTask.CHAT_AGENT,
                ChatRequest(messages=(ChatMessage("user", "synthetic audit"),)),
            )
        except Exception as exc:
            error = type(exc).__name__
        finally:
            await executor.close()
    return {
        "synthetic_responses": sequence,
        "physical_mock_requests": len(wires),
        "record": capture.records[-1] if capture.records else None,
        "error": error,
        "wire_modes": [{"path": x["path"], "stream": x["body"].get("stream")} for x in wires],
    }


async def sql_summary(records):
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.create_schema()
    repo = ModelInvocationRepository(db)
    for rec in records:
        await repo.record(**rec)
    summary = await ControlActivityQueryAdapter(
        db.sessions, workspace=None
    ).read_model_usage_summary("24h")
    await db.engine.dispose()
    return summary.fields
