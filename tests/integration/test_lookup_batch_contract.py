"""Bounded pure discovery pairs in order without changing any submitted wire tools."""

import json
from dataclasses import replace

import pytest
from tests.support.correctness_wire import wire
from tests.unit.test_work_reporting_runner import case, response, tool

from qq_ai_bot.codemode.api_projection import project
from qq_ai_bot.codemode.tool_visibility import LOOKUP_TOOLS
from qq_ai_bot.domain.messages import ChatMessage, ChatResponse, FunctionCallOutput


@pytest.mark.parametrize("kind", ["chat", "responses", "anthropic", "gemini"])
@pytest.mark.parametrize("count", range(2, 11))
async def test_pure_lookup_batch_is_free_and_keeps_wire_contract(database, tmp_path, kind, count):
    calls = tuple(
        tool("lookup_tools", {"name": "write_fixture"}, f"discover-{i}") for i in range(count)
    )
    env = await case(
        database, tmp_path, [response(*calls), ChatResponse("done", 0)], reporting="quiet"
    )
    full = (*env.backend.definitions(None), LOOKUP_TOOLS)
    runtime = replace(
        env.runtime, fixed_tools=full, script_api=project(full, "batch-v14"), work_control=None
    )
    client, wires = wire(env, kind)
    try:
        await env.runner.run((ChatMessage("user", "discover only"),), runtime, env.backend)
    finally:
        await client.aclose()
    request = env.provider.requests[-1]
    paired = [(m.tool_call_id, m.content) for m in request.messages if m.role == "tool"]
    paired.extend(
        (m.call_id, m.output)
        for m in request.continuation_items
        if isinstance(m, FunctionCallOutput)
    )
    assert [identity for identity, _ in paired] == [c.id for c in calls]
    assert all(json.loads(content)["ok"] for _, content in paired)
    assert env.observed == [] and env.control.tools_started == 0
    assert (await env.repository.get(env.control.current["id"]))["tool_calls"] == 0
    assert len(wires) == 2
    assert json.dumps(wires[0]["tools"]) == json.dumps(wires[1]["tools"])


@pytest.mark.parametrize("mixed", [False, True])
async def test_lookup_rejects_oversized_or_business_mixed_batch(database, tmp_path, mixed):
    calls = (
        (
            tool("lookup_tools", {"name": "write_fixture"}, "discover"),
            tool("write_fixture", {}, "write"),
        )
        if mixed
        else tuple(tool("lookup_tools", {}, f"discover-{i}") for i in range(11))
    )
    env = await case(
        database, tmp_path, [response(*calls), ChatResponse("done", 0)], reporting="quiet"
    )
    full = (*env.backend.definitions(None), LOOKUP_TOOLS)
    runtime = replace(
        env.runtime, fixed_tools=full, script_api=project(full, "batch-v14"), work_control=None
    )
    await env.runner.run((ChatMessage("user", "discover only"),), runtime, env.backend)
    paired = [m for m in env.provider.requests[-1].messages if m.role == "tool"]
    assert [m.tool_call_id for m in paired] == [c.id for c in calls]
    assert all(json.loads(m.content)["error"] == "tool_lookup_requires_own_batch" for m in paired)
    assert env.observed == [] and env.control.tools_started == 0
