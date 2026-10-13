"""Two schedules in one SDK invocation have independent retained identities."""

import pytest
from tests.integration.test_self_common_capabilities import runtime
from tests.support.automation_unified_delivery_helpers import setup_run

from qq_ai_bot.capabilities.invocation import ToolInvocationContext, current_invocation
from qq_ai_bot.plugin_host.facades import HostPluginContext, PluginFacadeServices
from yuki_plugin_sdk.permissions import PluginPermission


@pytest.mark.parametrize("bound_tool_call", [False, True])
async def test_one_plugin_invocation_creates_two_tasks_and_replays_original_request(
    database, tmp_path, bound_tool_call
):
    case = await setup_run(database, tmp_path, principal="self")
    host = HostPluginContext(
        plugin_id="schedule.identity",
        approved_permissions=(PluginPermission.AUTOMATION_MANAGE_SELF,),
        services=PluginFacadeServices(ledger=case.chat._ledger, automation=case.service),
    )
    source = runtime(case.actor, case.env)
    first_task = dict(
        name="first",
        goal="check first",
        trigger={"type": "after", "seconds": 1},
        delivery={"target": "current_group"},
    )
    second_task = {**first_task, "name": "second", "goal": "check second"}
    token = (
        current_invocation.set(ToolInvocationContext(source, call_id="original-plugin-call"))
        if bound_tool_call
        else None
    )
    try:
        with host.invocation_scope(host.plugin_id, source):
            first = await host.automation.create_task(first_task, request_id="first")
            second = await host.automation.create_task(second_task, request_id="second")
            assert first.data["automation"]["task_id"] != second.data["automation"]["task_id"]
            assert await host.automation.create_task(first_task, request_id="first") == first
            with pytest.raises(ValueError, match="automation_creation_key_conflict"):
                await host.automation.create_task(second_task, request_id="first")
        # Restoring the same real source in another Host binding reuses the receipt.
        with host.invocation_scope(host.plugin_id, source):
            assert await host.automation.create_task(first_task, request_id="first") == first
        rows = await case.service.list_for_actor(case.actor)
        assert len(rows) == 3
    finally:
        if token is not None:
            current_invocation.reset(token)
