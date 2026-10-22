"""SDK children retain the live plugin grant and their original source after restart."""

import json
from dataclasses import replace

import pytest
from sqlalchemy import select, update
from tests.integration.test_self_common_capabilities import runtime, self_actor
from tests.support.runtime_execution import make_child_executor
from tests.unit.test_history_dispatch_ownership import _scene, _tool

from yuki.llm.fake import FakeLLMProvider
from yuki.plugin_host.facades import HostPluginContext, PluginFacadeServices
from yuki.runtime.work_budget_schema import budgets
from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_schema_v1 import work
from yuki.services.agent_tools import ToolRuntime
from yuki.services.execution_sources import SelfTaskSource, recover_execution_source
from yuki_plugin_sdk.permissions import PluginPermission


@pytest.mark.parametrize("legacy_names", [False, True])
async def test_sdk_child_recovers_original_self_source_and_current_plugin_grant(
    database, tmp_path, legacy_names
):
    provider = FakeLLMProvider()
    env, harness, chat, _state, _message = await _scene(database, tmp_path, provider)
    database.subagents_enabled = True
    services = PluginFacadeServices(
        approval_revision="original-approval",
        ledger=harness.ledger,
        agent_runner=chat.runtime.runner,
        runtime_config=chat._runtime_config,
    )
    host = HostPluginContext(
        plugin_id="test.sdk-children",
        approved_permissions=(PluginPermission.AGENT_RUN,),
        services=services,
    )
    actor = await self_actor(database, env)
    source_runtime = replace(
        runtime(actor, env), runtime_config=await chat._runtime_config.snapshot()
    )

    def respond(_request):
        if len(provider.requests) == 1:
            return _tool(
                "subagent_start", {"goal": "check the original SDK source"}, "spawn-sdk-child"
            )
        return _tool("task_control", {"action": "complete", "result": "checked"}, "complete")

    provider._responder = respond
    with host.invocation_scope(host.plugin_id, source_runtime):
        result = await host.agent.run("check the actual source and delegate")
    repository = WorkRepository(database)
    root = await repository.get(result.data["work_id"])
    async with database.sessions() as reader:
        child = dict(
            (await reader.execute(select(work).where(work.c.parent_work_id == root["id"])))
            .mappings()
            .one()
        )
    source = json.loads(child["source_json"])
    assert source["owner"] == "plugin_invocation"
    assert source["actor_source"]["initiative_run_id"] == actor.initiative_run_id
    assert "allowed_tools" not in source
    if legacy_names:
        # Exact pre-454 source format: these names are historical admission
        # metadata, not the live domain approval for this original child.
        source["allowed_tools"] = ["subagent_start", "workspace_read", "automation_list"]
        child["source_json"] = json.dumps(source, ensure_ascii=False, separators=(",", ":"))
        async with database.sessions() as writer, writer.begin():
            await writer.execute(
                update(work)
                .where(work.c.id == child["id"])
                .values(source_json=child["source_json"])
            )
    original_source = child["source_json"]
    await host.close_host_resources()
    await database.close()
    restored = HostPluginContext(
        plugin_id=host.plugin_id,
        approved_permissions=(PluginPermission.AGENT_RUN,),
        services=services,
    )
    hosts = {restored.plugin_id: restored}
    recovered = await recover_execution_source(
        database,
        child["conversation_id"],
        source,
        request_id=child["id"],
        lookup_plugin_host=hosts.get,
    )
    assert isinstance(recovered, SelfTaskSource)
    assert recovered.run_id == actor.initiative_run_id and recovered.actor_user_id == ""
    executor = make_child_executor(
        repository,
        chat=chat,
        config=chat._runtime_config,
        runner=chat.runtime.runner,
        load_tools=chat.runtime.runner.main_contract.definitions,
        router=env.router,
        lookup_plugin_host=hosts.get,
    )
    try:
        assert await executor.run(child["id"]) is None
        assert (await repository.get(child["id"]))["state"] == "completed"
        assert (await repository.get(child["id"]))["source_json"] == original_source
        assert len(provider.requests) == 3
        assert (await repository.get(root["id"]))["model_requests"] == 2
        async with database.sessions() as reader:
            assert (
                await reader.scalar(select(budgets.c.models).where(budgets.c.root_id == root["id"]))
                == 3
            )
        services.approval_revision = "changed-approval"
        with pytest.raises(ValueError, match="plugin_work_owner_changed"):
            await recover_execution_source(
                database,
                child["conversation_id"],
                source,
                request_id=child["id"],
                lookup_plugin_host=hosts.get,
            )
    finally:
        await restored.close_host_resources()


async def test_code_child_uses_main_script_api_and_fixed_direct_tools(database, tmp_path):
    provider = FakeLLMProvider()
    env, _harness, chat, _state, _message = await _scene(
        database, tmp_path, provider, code_enabled=True
    )
    executor = make_child_executor(
        WorkRepository(database),
        chat=chat,
        config=chat._runtime_config,
        runner=chat.runtime.runner,
        load_tools=chat.runtime.runner.main_contract.definitions,
        router=env.router,
    )
    await executor.prepare()
    contract = chat.runtime.runner.main_contract
    assert executor.script_api is contract.script_api
    assert executor.definitions == await contract.definitions()
    assert {"send", "subagent_start", "subagent_control", "update_short_state"} <= set(
        executor.script_api.schemas
    )


async def test_sdk_message_child_keeps_original_plugin_origin_and_delegation(database, tmp_path):
    from tests.support.canonical_identity import ensure_space

    from yuki.persistence.models import ChatEventModel
    from yuki.services.execution_sources import recover_source

    provider = FakeLLMProvider()
    env, harness, chat, _state, _message = await _scene(database, tmp_path, provider)
    chat._tools.social_service = env.service
    async with database.sessions() as session, session.begin():
        other_space = await ensure_space(session, "30001")
    database.subagents_enabled = True
    async with database.sessions() as reader:
        event_id = await reader.scalar(select(ChatEventModel.id))
    event = await harness.ledger.get_event(event_id)
    recovered = await recover_source(
        database,
        env.context.conversation_id,
        {
            "origin": "user_message",
            "generation": 1,
            "trigger_event_id": event.id,
            "actor_person_id": env.person,
            "presence_id": env.presence,
            "bot_user_id": "80001",
        },
        request_id="original-message-sdk",
    )
    source_runtime = ToolRuntime(
        inbound=recovered.inbound(event, legacy_conversation_key="bot:80001:group:20001"),
        runtime_config=await chat._runtime_config.snapshot(),
    )
    host = HostPluginContext(
        plugin_id="test.message-sdk",
        approved_permissions=(PluginPermission.AGENT_RUN, PluginPermission.MESSAGE_HISTORY_READ),
        services=PluginFacadeServices(
            approval_revision="original-approval",
            ledger=harness.ledger,
            agent_runner=chat.runtime.runner,
            runtime_config=chat._runtime_config,
        ),
    )
    results = []

    def respond(request):
        count = len(provider.requests)
        if count == 1:
            return _tool("subagent_start", {"goal": "keep original plugin authority"}, "spawn")
        if count == 3:
            return _tool("send", {"text": "this source did not grant sending"}, "ungranted")
        if count == 4:
            results.append(json.loads(request.messages[-1].content))
            return _tool(
                "query_chat_history",
                {"view": "recent", "event_kind": "all", "target": {"space_id": other_space}},
                "other",
            )
        if count == 5:
            results.append(json.loads(request.messages[-1].content))
        return _tool("task_control", {"action": "complete", "result": "checked"}, "complete")

    provider._responder = respond
    try:
        with host.invocation_scope(host.plugin_id, source_runtime):
            result = await host.agent.run("delegate with the original SDK grant")
        repository = WorkRepository(database)
        async with database.sessions() as reader:
            child = dict(
                (
                    await reader.execute(
                        select(work).where(work.c.parent_work_id == result.data["work_id"])
                    )
                )
                .mappings()
                .one()
            )
        executor = make_child_executor(
            repository,
            chat=chat,
            config=chat._runtime_config,
            runner=chat.runtime.runner,
            load_tools=chat.runtime.runner.main_contract.definitions,
            router=env.router,
            lookup_plugin_host=lambda _: host,
        )
        actual_runtime = []
        factory = executor.services.backend_factory

        def backend(tool_runtime, allowed):
            actual_runtime.append(tool_runtime)
            return factory(tool_runtime, allowed)

        executor.services = replace(executor.services, backend_factory=backend)
        assert await executor.run(child["id"]) is None
        assert actual_runtime[0].origin.value == "plugin_session"
        assert actual_runtime[0].effective_trigger_event_id == event.id
        assert actual_runtime[0].memory_allowed_scopes == ()
        assert actual_runtime[0].read_target_id == env.space
        assert actual_runtime[0].read_scope.conversation_id == event.canonical_conversation_id
        assert not results[0]["ok"]
        assert results[0]["error_code"] == "permission_denied", results[0]
        assert not results[1]["ok"] and results[1]["error_code"] == "history_scope_denied"
        assert not any(
            action in {"send_group_msg", "send_private_msg"} for action, _ in env.bot.calls
        )
        assert len(provider.requests) == 5
    finally:
        await host.close_host_resources()
