"""Two schedules in one SDK invocation have independent retained identities."""

import pytest
from tests.integration.test_self_common_capabilities import runtime
from tests.support.automation_unified_delivery_helpers import setup_run

from yuki.capabilities.invocation import ToolInvocationContext, current_invocation
from yuki.plugin_host.facades import HostPluginContext, PluginFacadeServices
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
    )
    second_task = {**first_task, "name": "second", "goal": "check second"}
    token = (
        current_invocation.set(ToolInvocationContext(source, invocation_key="original-plugin-call"))
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


async def test_plugin_max_runs_omission_null_and_name_match_host_contract(database, tmp_path):
    case = await setup_run(database, tmp_path, principal="self")
    host = HostPluginContext(
        plugin_id="schedule.counts",
        approved_permissions=(
            PluginPermission.AUTOMATION_MANAGE_SELF,
            PluginPermission.AUTOMATION_READ,
        ),
        services=PluginFacadeServices(ledger=case.chat._ledger, automation=case.service),
    )
    source = runtime(case.actor, case.env)
    task = {"goal": "PRIVATE_SDK_GOAL", "trigger": {"type": "after", "seconds": 60}}
    with host.invocation_scope(host.plugin_id, source):
        created = await host.automation.create_task(task, request_id="finite", max_runs=7)
        item = created.data["automation"]
        task_id = item["task_id"]
        assert task_id.isdigit() and item["max_runs"] == 7
        assert item["name"] == f"自动化 #{task_id}"
        assert (await host.automation.update_task(task_id, {**task, "name": "public"})).data[
            "automation"
        ]["max_runs"] == 7
        cleared = await host.automation.update_task(task_id, {**task, "name": None}, max_runs=None)
        assert cleared.data["automation"]["name"] == f"自动化 #{task_id}"
        assert cleared.data["automation"]["max_runs"] is None
        replay = await host.automation.create_task(task, request_id="finite", max_runs=7)
        assert replay.data["automation"]["task_id"] == task_id
        assert replay.data["automation"]["max_runs"] is None
        with pytest.raises(ValueError, match="creation_key_conflict"):
            await host.automation.create_task(task, request_id="finite", max_runs=None)
        await host.automation.cancel(task_id)
        assert task_id not in {row["task_id"] for row in await host.automation.list_current_owner()}
        with pytest.raises(ValueError, match="已经结束"):
            await host.automation.update_task(task_id, task, max_runs=None)


async def test_old_sdk_original_key_replays_omitted_defaults_after_lean_publication(
    database, tmp_path
):
    import hashlib
    import json

    from sqlalchemy import func, select

    from yuki.persistence.models import AutomationRunModel, AutomationVersionModel

    case = await setup_run(database, tmp_path, principal="self")
    host = HostPluginContext(
        plugin_id="schedule.upgrade",
        approved_permissions=(PluginPermission.AUTOMATION_MANAGE_SELF,),
        services=PluginFacadeServices(ledger=case.chat._ledger, automation=case.service),
    )
    source = runtime(case.actor, case.env)
    submitted = {
        "name": "original name",
        "goal": "ORIGINAL_SDK_REQUEST",
        "trigger": {"type": "interval", "seconds": 60},
    }
    with host.invocation_scope(host.plugin_id, source):
        created = await host.automation.create_task(
            submitted, request_id="before-upgrade", max_runs=7
        )
    task_id = int(created.data["automation"]["task_id"])
    original = await case.repository.get(task_id)
    # Seed the exact immutable pre-0151 publication: old Host filled these defaults.
    # The original submitted mapping omitted all four fields; the saved source is real.
    old = {
        "version": 1,
        **submitted,
        "timezone": "Asia/Shanghai",
        "constraints": [],
        "delivery": {"target": "current_group"},
    }
    old_hash = hashlib.sha256(
        json.dumps(old, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    async with database.immediate_session() as writer:
        version = await writer.get(AutomationVersionModel, original.version_id)
        version.script_json = json.dumps(old, ensure_ascii=False)
        version.script_hash = old_hash
    accepted = await case.repository.create_run(
        task_id,
        scheduled_for=original.next_run_at,
        actual_started_at=case.clock.now(),
        version_id=original.version_id,
    )
    # A later, lean publication and current count change cannot replace original identity.
    await case.service.update_task(
        task_id,
        {**submitted, "goal": "CURRENT_FUTURE_REQUEST"},
        actor=case.actor,
        conversation_key=case.actor.conversation_id,
        max_runs=None,
    )
    async with database.read_session() as reader:
        before_versions = await reader.scalar(
            select(func.count()).select_from(AutomationVersionModel)
        )
        before_runs = await reader.scalar(select(func.count()).select_from(AutomationRunModel))
    case.clock.advance(3600)
    with host.invocation_scope(host.plugin_id, source):
        replay = await host.automation.create_task(
            submitted, request_id="before-upgrade", max_runs=7
        )
        assert replay.data["automation"]["task_id"] == str(task_id)
        assert replay.data["automation"]["max_runs"] is None
        with pytest.raises(ValueError, match="creation_key_conflict"):
            await host.automation.create_task(
                {**submitted, "goal": "different original"}, request_id="before-upgrade", max_runs=7
            )
        with pytest.raises(ValueError, match="creation_key_conflict"):
            await host.automation.create_task(submitted, request_id="before-upgrade", max_runs=None)
        # Historical decoding does not authorize new calls using the retired schema.
        with pytest.raises(ValueError, match="定时任务格式错误"):
            await host.automation.create_task(
                {**submitted, "constraints": []}, request_id="new-call"
            )
    async with database.read_session() as reader:
        assert (
            await reader.scalar(select(func.count()).select_from(AutomationVersionModel))
            == before_versions
        )
        assert (
            await reader.scalar(select(func.count()).select_from(AutomationRunModel)) == before_runs
        )
        version = await reader.get(AutomationVersionModel, original.version_id)
        run = await reader.get(AutomationRunModel, accepted.id)
        assert json.loads(version.script_json) == old and version.script_hash == old_hash
        assert run.version_id == original.version_id and run.status == "running"


@pytest.mark.parametrize(
    "trigger",
    [
        {"type": "daily", "hour": 9, "minute": 3},
        {"type": "weekly", "weekdays": [1, 5], "hour": 9, "minute": 3},
    ],
)
async def test_sdk_original_timezone_default_survives_new_time_service(database, tmp_path, trigger):
    from sqlalchemy import func, select

    from yuki.persistence.models import AutomationVersionModel
    from yuki.time.service import TimeContextService

    case = await setup_run(database, tmp_path, principal="self")
    host = HostPluginContext(
        plugin_id="schedule.defaults",
        approved_permissions=(PluginPermission.AUTOMATION_MANAGE_SELF,),
        services=PluginFacadeServices(ledger=case.chat._ledger, automation=case.service),
    )
    source = runtime(case.actor, case.env)
    task = {"goal": "ORIGINAL_WALL_CLOCK_REQUEST", "trigger": trigger}
    with host.invocation_scope(host.plugin_id, source):
        created = await host.automation.create_task(task, request_id="original", max_runs=7)
        task_id = int(created.data["automation"]["task_id"])
        await host.automation.update_task(str(task_id), task, max_runs=None)
    before = await case.repository.get(task_id)
    assert before.script["trigger"]["timezone"] == "Asia/Shanghai"
    # Simulate reassembly after a default configuration change; original input has no timezone.
    case.service._time = TimeContextService(default_timezone="America/New_York", clock=case.clock)
    with host.invocation_scope(host.plugin_id, source):
        replay = await host.automation.create_task(task, request_id="original", max_runs=7)
        assert replay.data["automation"]["task_id"] == str(task_id)
        assert replay.data["automation"]["max_runs"] is None
        with pytest.raises(ValueError, match="creation_key_conflict"):
            await host.automation.create_task(task, request_id="original", max_runs=None)
        fresh = await host.automation.create_task(task, request_id="new-after-restart")
    after = await case.repository.get(task_id)
    assert after.version_id == before.version_id and after.script == before.script
    assert after.next_run_at == before.next_run_at
    new = await case.repository.get(int(fresh.data["automation"]["task_id"]))
    assert new.script["trigger"]["timezone"] == "America/New_York"
    async with database.read_session() as reader:
        assert (
            await reader.scalar(
                select(func.count())
                .select_from(AutomationVersionModel)
                .where(AutomationVersionModel.automation_id == task_id)
            )
            == 2
        )
