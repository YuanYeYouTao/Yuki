"""Discovery changes model exposure, never the executable schema or permission."""

import json

import pytest
from scripts.export_pi_codemode_inventory import export_inventory

from qq_ai_bot.codemode.api_projection import NEVER_PROJECTED, project
from qq_ai_bot.codemode.tool_visibility import DIRECT_TOOL_NAMES, lookup_tools, model_definitions
from qq_ai_bot.domain.messages import ChatTool
from qq_ai_bot.runtime.subagent_tools import WORKER_NAMES


async def test_compact_view_keeps_web_and_full_execution_schemas():
    inventory = await export_inventory()
    full = tuple(ChatTool(**row) for row in inventory["frozen_definitions"])
    visible = tuple(ChatTool(**row) for row in inventory["model_definitions"])
    assert visible == model_definitions(full)
    names = {tool.name for tool in visible}
    assert {"web_search", "read_webpage", "execute_code", "lookup_tools", "workspace_read"} <= names
    # Terminal lifecycle controls are always direct.
    assert {"terminal_exec", "terminal_write", "terminal_control"} <= names
    assert not names & {"run_python", "admin_set_config", "automation_create"}
    # The requested three direct tools replace the old half-menu size heuristic.
    assert names == DIRECT_TOOL_NAMES & {tool.name for tool in full}
    api = project(full, inventory["manifest_revision"])
    before = api.digest()
    for name in ("terminal_exec", "admin_set_config", "automation_create", "web_search"):
        detail = json.loads(lookup_tools(api, json.dumps({"name": name})))
        original = next(tool for tool in full if tool.name == name)
        assert detail["data"]["parameters"] == original.parameters
        assert detail["data"]["description"] == original.description
        assert detail["data"]["direct"] is (name in DIRECT_TOOL_NAMES)
        assert api.tool_for(detail["data"]["script_name"]) == name
    assert api.digest() == before
    assert model_definitions(full) == visible
    assert set(api.wrappers.values()) == {tool.name for tool in full} - NEVER_PROJECTED


def test_lookup_pages_compact_metadata_and_worker_scope():
    full = (
        *(
            ChatTool(f"tool_{i:02d}", "special search description", {"type": "object"})
            for i in range(12)
        ),
        ChatTool("terminal_exec", "command", {"type": "object"}),
    )
    api = project(full, "catalog")
    first = json.loads(lookup_tools(api, '{"query":"special", "limit":10}'))
    assert first["data"]["total"] == 12 and first["data"]["next_offset"] == 10
    assert all("parameters" not in row for row in first["data"]["tools"])
    last = json.loads(lookup_tools(api, '{"query":"special", "offset":10}'))
    assert len(last["data"]["tools"]) == 2 and last["data"]["next_offset"] is None
    worker = project(tuple(tool for tool in full if tool.name in WORKER_NAMES), "worker")
    assert json.loads(lookup_tools(worker, '{"name":"tool_00"}'))["error"] == "unknown_capability"
    assert json.loads(lookup_tools(worker, '{"name":"terminal_exec"}'))["ok"]


@pytest.mark.parametrize(
    "raw",
    [
        "[]",
        "null",
        "bad",
        '{"name":null}',
        '{"query":3}',
        '{"limit":true}',
        '{"offset":-1}',
        '{"limit":11}',
        '{"extra":1}',
        '{"name":"x","query":"x"}',
    ],
)
def test_lookup_rejects_arguments_without_running_any_tool(raw):
    api = project((), "empty")
    assert json.loads(lookup_tools(api, raw)) == {
        "ok": False,
        "executed": False,
        "error": "invalid_lookup_arguments",
    }
