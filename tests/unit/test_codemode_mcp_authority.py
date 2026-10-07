"""Retired MCP grants no authority; retained result ownership still works.

main PR259 removes MCPManager and its HTTP-specific dispatch policy. The old
experiment's manager tests cannot execute that removed contract. These tests
check retirement and retain the original Work no-replay/read-vs-write invariant;
approved-plugin lifecycle and scope revocation run in dedicated plugin regressions.
"""

import importlib.util
import json

import pytest
from tests.unit.test_tool_effect_audit import active_work

from qq_ai_bot.capabilities.results import ToolExecutionResult
from qq_ai_bot.domain.messages import ToolCall, ToolFunction


def test_retired_manager_and_binding_cannot_supply_code_authority():
    for name in ("qq_ai_bot.mcp.manager", "qq_ai_bot.mcp.binding", "qq_ai_bot.mcp.fake"):
        try:
            spec = importlib.util.find_spec(name)
        except ModuleNotFoundError:
            spec = None
        assert spec is None


@pytest.mark.parametrize("read_only", [False, True])
async def test_original_unknown_effect_is_never_replayed_and_reads_do_not_own_mutations(
    database, tmp_path, read_only
):
    _, owner, _ = await active_work(database, tmp_path)
    external = tmp_path / "downstream.jsonl"
    call = ToolCall("original-external", ToolFunction("external_fixture", "{}"))

    async def invoke():
        with external.open("a") as output:
            output.write("committed-or-observed\n")
        result = ToolExecutionResult(
            ok=False,
            uncertain=not read_only,
            mutation_committed=False if read_only else None,
            provider_id="plugin",
            tool_name="external_fixture",
            error_code="response_lost",
        )
        return json.dumps(result.model_payload())

    await owner.execute(call, invoke, side_effecting=not read_only)
    await owner.execute(call, invoke, side_effecting=not read_only)
    assert len(external.read_text().splitlines()) == 1
    if not read_only:
        rejected = await owner.execute(
            ToolCall("new-write", call.function), invoke, side_effecting=True
        )
        assert json.loads(rejected)["error"] == "unresolved_prior_effect"
        assert len(external.read_text().splitlines()) == 1
