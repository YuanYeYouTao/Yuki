"""A composition child reaches each tool through the same backend authorization path.

For every inventory tool, a child Invocation and a direct Invocation of the same
call get byte-identical refusals from the real MainAgentBackend: the child has
no private execution entry and no extra authority (T01, "拒绝项理由与直接调用一致").
"""

import json
from pathlib import Path

import pytest
from tests.conftest import build_harness, make_settings

from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.capabilities.invocation import (
    Invocation,
    InvocationIdentity,
    TrustedInvocationContext,
    child_operation_id,
    direct_invocations,
)
from qq_ai_bot.domain.messages import ToolCall, ToolFunction
from qq_ai_bot.runtime.work_control import WORK_CONTROL_NAMES
from qq_ai_bot.services.agent_runner import AgentRuntime
from qq_ai_bot.services.agent_tools import ToolRuntime
from qq_ai_bot.services.main_agent_backend import MainAgentBackend

INVENTORY = json.loads(
    (
        Path(__file__).parents[2] / "docs/architecture/pi-codemode-capability-inventory.json"
    ).read_text(encoding="utf-8")
)
# Lifecycle controls go through the Host control gate (tested separately) and
# execution and lookup entrypoints are Host-only; other tools are backend bindings.
BACKEND_TOOLS = [
    row["model_name"]
    for row in INVENTORY["tools"]
    if row["model_name"] not in WORK_CONTROL_NAMES
    and row["model_name"] not in {"execute_code", "lookup_tools"}
]


def child_of(direct: Invocation, ordinal: int) -> Invocation:
    parent = direct.identity.operation_id
    call = ToolCall(f"c{ordinal}", direct.call.function)
    return Invocation(
        InvocationIdentity(
            operation_id=child_operation_id(parent, ordinal),
            owner_execution_id=direct.identity.owner_execution_id,
            chain_id=direct.identity.chain_id,
            request_sequence=direct.identity.request_sequence,
            provider_call_id=call.id,
            parent_operation_id=parent,
            child_ordinal=ordinal,
            engine_call_id=str(ordinal),
            feed_index=0,
        ),
        call,
        TrustedInvocationContext(direct.context.runtime, direct.context.manifest_revision),
    )


@pytest.fixture
async def backends(database):
    chat = build_harness(database, make_settings(database.url)).processor._chat
    config = await chat._runtime_config.snapshot()
    runtime = AgentRuntime(
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id="10001",
        actor_is_superuser=False,
        delegated_authority=None,
        conversation_key="parity",
        current_group_id=None,
        bot_user_id="80001",
        gateway=None,
        runtime_config=config,
        current_time=chat._time.current_default(),
        allowed_capabilities=frozenset(),
        max_tool_calls=8,
        max_model_requests=8,
        execution_id="parity",
    )
    tool_runtime = ToolRuntime(
        inbound=None, gateway=None, allow_generic_onebot=False, runtime_config=config
    )

    def make(allowed):
        return MainAgentBackend(chat, tool_runtime, allowed_tools=allowed)

    return make, runtime


@pytest.mark.parametrize("name", BACKEND_TOOLS)
async def test_child_refusal_is_identical_to_direct(backends, name):
    make, runtime = backends
    call = ToolCall("direct", ToolFunction(name, "{}"))
    direct = direct_invocations((call,), runtime, chain_id="chain", request_sequence=1)[0]
    # Same principal without this capability: both refused by the same check.
    backend = make(frozenset())
    refused_direct = await backend.execute_call(direct)
    refused_child = await backend.execute_call(child_of(direct, 0))
    assert refused_direct == refused_child
    assert json.loads(refused_child)["error_code"] == "capability_not_allowed"


@pytest.mark.parametrize("name", BACKEND_TOOLS)
async def test_closed_tools_close_children_too(backends, name):
    make, runtime = backends
    backend = make(None)
    backend._tools_closed = True  # A committed mutation closed this turn's tools.
    call = ToolCall("direct", ToolFunction(name, "{}"))
    direct = direct_invocations((call,), runtime, chain_id="chain", request_sequence=1)[0]
    # Direct delivery remains available after a committed mutation. This checks
    # identical backend policy, while the composition Host closes after memory.
    assert await backend.execute_call(direct) == await backend.execute_call(child_of(direct, 3))
