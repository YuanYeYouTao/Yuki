"""Auxiliary Gemini schemas do not change the configured Main tool strategy."""

import json
from dataclasses import replace

import httpx
import pytest
from tests.support.work_compaction import summary_json

from qq_ai_bot.domain.messages import ChatMessage, ChatRequest, ChatTool
from qq_ai_bot.llm.gemini import GeminiProvider
from qq_ai_bot.model_runtime.executor import TaskModelExecutor
from qq_ai_bot.model_runtime.models import (
    ModelCapability,
    ModelProfile,
    ModelProtocol,
    ModelRoute,
    ModelTask,
    StructuredOutputMode,
)
from qq_ai_bot.model_runtime.pool import ModelClientPool
from qq_ai_bot.model_runtime.profiles import ModelProfileCatalog
from qq_ai_bot.model_runtime.routes import ModelRouter
from qq_ai_bot.model_runtime.structured import (
    tool_free_json_format,
    tool_free_structured_output_mode,
)
from qq_ai_bot.runtime.work_compaction import CompactionSummary, validate_summary
from qq_ai_bot.runtime.work_repository import WorkCapacityError
from qq_ai_bot.services.ordinary_compaction import OrdinarySummary, summarize_records


def models(protocol, mode, *, provider=None, structured=True):
    capabilities = {ModelCapability.REASONING, ModelCapability.TOOLS}
    if structured:
        capabilities.add(ModelCapability.STRUCTURED_OUTPUT)
    profile = ModelProfile(
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
        structured_output_mode=mode,
        capabilities=frozenset(capabilities),
    )
    catalog = ModelProfileCatalog(
        profiles={"main": profile},
        routes={task: ModelRoute(task=task, profile_id="main") for task in ModelTask},
    )
    return TaskModelExecutor(
        router=ModelRouter(catalog),
        pool=ModelClientPool(injected_profiles={"main": provider} if provider else {}),
    )


@pytest.mark.parametrize("protocol", list(ModelProtocol))
@pytest.mark.parametrize("mode", list(StructuredOutputMode))
def test_tool_free_mode_retains_explicit_configuration_and_other_protocols(protocol, mode):
    executor = models(protocol, mode)
    before = executor.profile_revision(ModelTask.CHAT_AGENT)
    expected = (
        StructuredOutputMode.JSON_SCHEMA
        if protocol is ModelProtocol.GEMINI and mode is StructuredOutputMode.FUNCTION_TOOL
        else mode
    )
    assert tool_free_structured_output_mode(executor, ModelTask.CHAT_AGENT) is expected
    assert executor.structured_output_mode(ModelTask.CHAT_AGENT) is mode
    assert executor.profile_revision(ModelTask.CHAT_AGENT) == before


def test_native_mode_requires_declared_structured_capability():
    executor = models(ModelProtocol.GEMINI, StructuredOutputMode.FUNCTION_TOOL, structured=False)
    assert (
        tool_free_structured_output_mode(executor, ModelTask.CHAT_AGENT)
        is StructuredOutputMode.FUNCTION_TOOL
    )


@pytest.mark.parametrize("kind", ["ordinary", "work"])
async def test_full_auxiliary_schema_on_real_gemini_wire_preserves_main_tools(kind):
    captured = []
    source = {
        "source_refs": ["goal", "event:1", "record:0"],
        "original_request_ref": "event:1",
        "task_material": {"directives": [], "corrections": []},
        "task_inputs": [],
        "recent_task_inputs": [],
    }
    schema = (
        OrdinarySummary.model_json_schema()
        if kind == "ordinary"
        else CompactionSummary.model_json_schema()
    )

    def transport(request):
        body = json.loads(request.content)
        captured.append(body)
        config = body["generationConfig"]
        if len(captured) == 2:
            assert "responseJsonSchema" not in config
            assert body["tools"][0]["functionDeclarations"][0]["name"] == "read_fixture"
            content = "done"
        else:
            assert not body.get("tools") and not body.get("toolConfig")
            assert config["responseMimeType"] == "application/json"
            assert config["responseJsonSchema"] == schema
            assert schema["additionalProperties"] is False
            assert schema["$defs"]["SourcedFact"]["properties"]["refs"]["minItems"] == 1
            if kind == "work":
                content = summary_json(source)
            else:
                content = json.dumps(
                    {
                        "facts": [{"text": "saved", "refs": ["record:0"]}],
                        "pending": [],
                        "next_steps": [],
                    }
                )
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {"role": "model", "parts": [{"text": content}]},
                    }
                ]
            },
        )

    async with httpx.AsyncClient(
        base_url="https://schema.invalid", transport=httpx.MockTransport(transport)
    ) as client:
        adapter = GeminiProvider(
            base_url="https://schema.invalid",
            api_key="unused",
            client=client,
            timeout_seconds=2,
            max_retries=0,
        )
        executor = models(
            ModelProtocol.GEMINI, StructuredOutputMode.FUNCTION_TOOL, provider=adapter
        )
        main = ChatRequest(
            model="gemini-3.8-flash",
            messages=(ChatMessage("user", "continue"),),
            tools=(ChatTool("read_fixture", "Read", {"type": "object", "properties": {}}),),
            tool_choice="auto",
        )
        revision = executor.profile_revision(ModelTask.CHAT_AGENT)
        mode = tool_free_structured_output_mode(executor, ModelTask.CHAT_AGENT)
        if kind == "ordinary":
            result = await summarize_records(
                [("record:0", "real material")],
                main_request=main,
                structured_mode=mode,
                summary_budget=20000,
                output_tokens=1024,
                prepare=lambda request: executor.capacity_request(ModelTask.CHAT_AGENT, request),
                execute=lambda request: executor.execute(ModelTask.CHAT_AGENT, request),
            )
            assert result["facts"][0]["refs"] == ["record:0"]
        else:
            response = await executor.execute(
                ModelTask.CHAT_AGENT,
                replace(
                    main,
                    tools=(),
                    tool_choice=None,
                    structured_output=True,
                    response_format=tool_free_json_format(mode, name="work", schema=schema),
                ),
            )
            validate_summary(response.content, source)
            invalid = json.loads(response.content)
            invalid["pending"][0]["refs"] = ["record:999"]
            with pytest.raises(WorkCapacityError, match="invalid_reference"):
                validate_summary(json.dumps(invalid), source)
        await executor.execute(ModelTask.CHAT_AGENT, main)
        assert executor.profile_revision(ModelTask.CHAT_AGENT) == revision
        assert (
            executor.structured_output_mode(ModelTask.CHAT_AGENT)
            is StructuredOutputMode.FUNCTION_TOOL
        )
        assert len(captured) == 2
