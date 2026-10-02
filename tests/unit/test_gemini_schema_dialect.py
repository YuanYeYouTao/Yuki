"""The Gemini Schema dialect is explicit, wire-only, and leaves Main intact."""

import copy
import hashlib
import json
from dataclasses import replace

import httpx
import pytest
from pydantic import ValidationError

from qq_ai_bot.domain.messages import ChatMessage, ChatRequest, ChatTool
from qq_ai_bot.llm.base import LLMUnsupportedFeatureError
from qq_ai_bot.llm.gemini import GeminiProvider
from qq_ai_bot.llm.gemini_schema import response_schema
from qq_ai_bot.llm.vendor_policy import ChatWireOptions, wire_options
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
from qq_ai_bot.runtime.work_compaction import CompactionSummary


def profile(protocol=ModelProtocol.GEMINI, options=None):
    return ModelProfile(
        id="main",
        provider="fake",
        protocol=protocol,
        model="gemini-3.8-flash",
        base_url="https://schema.invalid",
        api_key_env="UNUSED",
        timeout_seconds=2,
        max_retries=0,
        default_temperature=0.1,
        default_max_output_tokens=1024,
        capabilities=frozenset(
            {ModelCapability.REASONING, ModelCapability.TOOLS, ModelCapability.STRUCTURED_OUTPUT}
        ),
        wire_options=options,
    )


def executor(item):
    routes = {task: ModelRoute(task=task, profile_id=item.id) for task in ModelTask}
    return TaskModelExecutor(
        router=ModelRouter(ModelProfileCatalog(profiles={item.id: item}, routes=routes)),
        pool=ModelClientPool(),
    )


@pytest.mark.parametrize("protocol", list(ModelProtocol))
def test_dialect_setting_is_only_valid_for_gemini(protocol):
    options = ChatWireOptions(gemini_schema_format="response_schema")
    if protocol is ModelProtocol.GEMINI:
        assert profile(protocol, options).wire_options.gemini_schema_format == "response_schema"
    else:
        with pytest.raises(ValidationError):
            profile(protocol, options)


def test_unknown_dialect_is_rejected():
    with pytest.raises(ValidationError):
        ChatWireOptions(gemini_schema_format="detect_from_url")


def test_full_schema_expansion_is_wire_only_and_local_constraints_remain_strict():
    original = CompactionSummary.model_json_schema()
    before = copy.deepcopy(original)
    converted = response_schema(original)
    assert original == before
    assert "$defs" not in converted and "$ref" not in json.dumps(converted)
    assert set(converted["properties"]) == set(original["properties"])
    assert converted["required"] == original["required"]
    fact = converted["properties"]["task_directives"]["items"]
    assert fact["required"] == ["text", "refs"]
    assert fact["properties"]["refs"]["items"]["type"] == "string"
    assert "additionalProperties" not in fact and "minItems" not in fact["properties"]["refs"]
    bad = {
        "version": 1,
        "task_directives": [{"text": "x", "refs": []}],
        "superseded_directives": [],
        "input_dispositions": [],
        "completed": [],
        "pending": [],
        "failures": [],
        "artifacts": [],
        "next_steps": [],
    }
    with pytest.raises(ValidationError, match="too_short"):
        CompactionSummary.model_validate(bad)
    bad["task_directives"] = []
    bad["unknown"] = "x"
    with pytest.raises(ValidationError, match="extra_forbidden"):
        CompactionSummary.model_validate(bad)


@pytest.mark.parametrize(
    "schema",
    [
        {"$ref": "https://schema.invalid/type"},
        {"$ref": "#/$defs/missing"},
        {"$ref": "#"},
        {
            "$defs": {"A": {"type": "object", "properties": {"child": {"$ref": "#/$defs/A"}}}},
            "$ref": "#/$defs/A",
        },
        {"type": "object", "properties": {"x": False}},
        {"anyOf": [{"type": "string"}, {"type": "number"}]},
    ],
)
async def test_unrepresentable_schema_rejects_before_any_http(schema):
    captured = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: captured.append(request))
    ) as client:
        provider = GeminiProvider(
            base_url="https://schema.invalid",
            api_key="unused",
            client=client,
            timeout_seconds=2,
            max_retries=0,
            options=ChatWireOptions(gemini_schema_format="response_schema"),
        )
        request = ChatRequest(
            model="gemini-3.8-flash",
            messages=(ChatMessage("user", "synthetic"),),
            response_format={"type": "json_schema", "json_schema": {"schema": schema}},
        )
        with pytest.raises(LLMUnsupportedFeatureError):
            await provider.complete(request)
    assert captured == []


async def test_explicit_dialect_reaches_real_wire_without_altering_default_or_main():
    bodies = []
    schema = CompactionSummary.model_json_schema()
    original = copy.deepcopy(schema)

    def transport(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {"role": "model", "parts": [{"text": "ok"}]},
                    }
                ]
            },
        )

    async with httpx.AsyncClient(
        base_url="https://schema.invalid", transport=httpx.MockTransport(transport)
    ) as client:
        ordinary = GeminiProvider(
            base_url="https://schema.invalid",
            api_key="unused",
            client=client,
            timeout_seconds=2,
            max_retries=0,
        )
        explicit = GeminiProvider(
            base_url="https://schema.invalid",
            api_key="unused",
            client=client,
            timeout_seconds=2,
            max_retries=0,
            options=ChatWireOptions(gemini_schema_format="response_schema"),
        )
        request = ChatRequest(
            model="gemini-3.8-flash",
            messages=(ChatMessage("user", "synthetic"),),
            response_format={"type": "json_schema", "json_schema": {"schema": schema}},
        )
        await ordinary.complete(request)
        await explicit.complete(request)
        assert bodies[0]["generationConfig"]["responseJsonSchema"] == original
        assert "responseSchema" not in bodies[0]["generationConfig"]
        assert bodies[1]["generationConfig"]["responseSchema"] == response_schema(original)
        assert "responseJsonSchema" not in bodies[1]["generationConfig"]
        main = replace(
            request,
            response_format=None,
            tools=(ChatTool("read", "Read", {"type": "object", "properties": {}}),),
            tool_choice="auto",
        )
        await ordinary.complete(main)
        await explicit.complete(main)
        assert bodies[2] == bodies[3]
        assert schema == original


@pytest.mark.parametrize(
    "protocol",
    [ModelProtocol.CHAT_COMPLETIONS, ModelProtocol.ANTHROPIC_MESSAGES, ModelProtocol.GEMINI],
)
def test_default_contract_revision_matches_previous_wire_shape(protocol):
    item = profile(protocol)
    route = ModelRoute(task=ModelTask.CHAT_AGENT, profile_id="main")
    serialized = item.model_dump(
        mode="json",
        exclude={
            "max_input_tokens",
            "context_window_tokens",
            "max_output_tokens_limit",
            "wire_options",
            "headers",
            "search_mode",
        },
    )
    serialized["capabilities"] = sorted(value.value for value in item.capabilities)
    serialized["wire_options"] = wire_options(item.provider).model_dump(
        mode="json", exclude={"gemini_schema_format"}
    )
    serialized_route = route.model_dump(mode="json")
    serialized_route["required_capabilities"] = []
    old = hashlib.sha256(
        json.dumps(
            {"route": serialized_route, "profile": serialized},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    assert executor(item).profile_revision(ModelTask.CHAT_AGENT) == old
    if protocol is ModelProtocol.GEMINI:
        explicit = profile(protocol, ChatWireOptions(gemini_schema_format="response_schema"))
        assert executor(explicit).profile_revision(ModelTask.CHAT_AGENT) != old
