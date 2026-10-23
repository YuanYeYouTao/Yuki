"""Real wire serializers and frozen Code discovery retain operation-specific schemas.

HTTP responses are controlled boundaries; this verifies local paths, not remote API acceptance.
"""

import json
from dataclasses import replace

import httpx
import pytest
from tests.support.automation_unified_delivery_helpers import setup_run
from tests.support.correctness_wire import KINDS, body

from yuki.capabilities.validation import JsonSchemaCapabilityValidator
from yuki.codemode.api_projection import project
from yuki.codemode.tool_visibility import lookup_tools
from yuki.domain.messages import ChatMessage, ChatRequest, ChatResponse, ToolCall, ToolFunction
from yuki.services.agent_tools import ToolRuntime


@pytest.mark.parametrize("kind", ["chat", "responses", "gemini", "anthropic"])
async def test_real_provider_schema_call_and_authorized_get(database, tmp_path, kind):
    case = await setup_run(database, tmp_path, mode="silent", goal="PRIVATE_WIRE_GOAL")
    tools = case.chat._automation_tools
    definitions = tools.definitions()
    request = ChatRequest(
        messages=(ChatMessage("user", "读取我的完整安排"),),
        model="wire-model",
        tools=definitions,
        max_output_tokens=1024,
    )
    expected = {tool.name: tool.parameters for tool in definitions}
    arguments = {"view": "get", "automation_id": case.row.id}
    reply = ChatResponse(
        "",
        0,
        tool_calls=(
            ToolCall("read-original", ToolFunction("automation_query", json.dumps(arguments))),
        ),
    )
    captured = []

    def transport(http_request):
        captured.append(json.loads(http_request.content))
        return httpx.Response(200, json=body(kind, reply))

    client = httpx.AsyncClient(
        base_url="https://wire.invalid", transport=httpx.MockTransport(transport)
    )
    provider_type = KINDS[kind][0]
    provider = provider_type(
        base_url="https://wire.invalid",
        api_key="local-test",
        timeout_seconds=2,
        max_retries=0,
        client=client,
    )
    try:
        parsed = await provider.complete(request)
    finally:
        await client.aclose()
    payload = captured[0]
    if kind == "chat":
        actual = {
            item["function"]["name"]: item["function"]["parameters"] for item in payload["tools"]
        }
    elif kind == "responses":
        actual = {item["name"]: item["parameters"] for item in payload["tools"]}
    elif kind == "gemini":
        actual = {
            item["name"]: item["parametersJsonSchema"]
            for item in payload["tools"][0]["functionDeclarations"]
        }
    else:
        actual = {item["name"]: item["input_schema"] for item in payload["tools"]}
    assert actual == expected
    validator = JsonSchemaCapabilityValidator()
    assert validator.admit_tools(definitions) == ()
    call = parsed.tool_calls[0]
    assert validator.freeze().validate(call.function.name, call.function.arguments).ok
    execution = f"wire-{kind}"
    runtime = ToolRuntime(
        inbound=None,
        actor_context=replace(case.actor, execution_id=execution),
        execution_id=execution,
        conversation_id=case.actor.conversation_id,
        conversation_key=case.actor.conversation_id,
    )
    result = await tools.execute(call.function.name, call.function.arguments, runtime)
    assert result.ok and result.data["task"]["goal"] == "PRIVATE_WIRE_GOAL"
    # Code discovery carries exactly the same frozen schema and operation identity.
    api = project(definitions, "automation-contract")
    found = json.loads(lookup_tools(api, '{"name":"automation_query"}'))
    assert found["ok"] and found["data"]["parameters"] == actual["automation_query"]
    assert api.tool_for(found["data"]["script_name"]) == call.function.name
    assert not validator.validate(
        "automation_query", '{"view":"get","automation_id":1,"limit":7}'
    ).ok
