"""The 42-name policy is tested on complete services, separately from narrow fixtures."""

import json

from tests.support.full_contract_fixture import full_contract

from qq_ai_bot.codemode.tool_visibility import DIRECT_TOOL_NAMES, lookup_tools


async def test_complete_service_declaration_keeps_all_direct_entrypoints(tmp_path):
    app = await full_contract(tmp_path)
    try:
        contract = app.main_agent_contract
        complete = await contract.definitions()
        visible = await contract.model_definitions()
        # #262: common terminal entrypoints join the frozen direct declaration.
        assert len(DIRECT_TOOL_NAMES) == 42
        assert {tool.name for tool in visible} == DIRECT_TOOL_NAMES
        assert {
            "send_message",
            "search_memory",
            "web_search",
            "read_webpage",
            "task_control",
            "lookup_tools",
            "execute_code",
        } <= DIRECT_TOOL_NAMES
        assert {"terminal_exec", "terminal_read", "environment_status"} <= DIRECT_TOOL_NAMES
        assert {"terminal_write", "terminal_control", "environment_packages"}.isdisjoint(
            DIRECT_TOOL_NAMES
        )
        assert len(complete) > len(visible)
        revision = contract.revision
        for tool in complete:
            if tool.name in contract.script_api.schemas:
                assert json.loads(
                    lookup_tools(contract.script_api, json.dumps({"name": tool.name}))
                )["ok"]
        assert await contract.model_definitions() == visible
        assert contract.revision == revision
    finally:
        await app.close()
