"""Child execution uses Main tools and the original authority and receipts."""

import json

import pytest
from sqlalchemy import select, update
from tests.support.automation_unified_delivery_helpers import setup_run
from tests.support.runtime_execution import make_child_executor
from tests.support.subagents_helpers import stack
from tests.unit.test_plugin_native_media import environment

from yuki.domain.messages import ChatResponse, ToolCall, ToolFunction
from yuki.persistence.models import ChatEventModel
from yuki.runtime.subagent_repository import SubagentRepository
from yuki.runtime.subagent_tools import execute_subagent
from yuki.runtime.work_control import WorkControl
from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_schema_v1 import effects, inputs, work
from yuki.tool_results.artifacts import ToolArtifactRepository
from yuki_plugin_sdk.results import ToolResult


async def _message_child(database, tmp_path, *, privileged=False):
    database.subagents_enabled = True
    case = await setup_run(database, tmp_path, mode="silent", privileged=privileged)
    async with database.read_session() as reader:
        event = await reader.scalar(select(ChatEventModel))
    source = {
        "origin": "user_message",
        "trigger_event_id": event.id,
        "actor_person_id": case.env.person,
        "actor_user_id": "10001",
        "presence_id": case.env.presence,
        "bot_user_id": "80001",
        "conversation_id": case.env.context.conversation_id,
        "generation": 1,
    }
    repository = WorkRepository(database)
    lease = await repository.acquire(source["conversation_id"], 1)
    parent = await repository.accept(
        lease, source_key="original-message", source=source, goal="check"
    )
    child = await SubagentRepository(repository).start(
        lease, parent["id"], "original-child", {"goal": "execute the original authorized work"}
    )
    await repository.release(lease)
    return case, repository, parent, child


@pytest.mark.parametrize("privileged", [False, True])
async def test_child_executes_main_business_tools_with_own_receipts(database, tmp_path, privileged):
    case, repository, parent, child = await _message_child(
        database, tmp_path, privileged=privileged
    )
    from tests.unit.test_memory_mutation import _service

    mutations, _facts, _ledger, _processor = _service(database)
    case.chat._tools._memory_mutations = mutations
    plugin = await environment(
        database, tmp_path, handler=lambda _context, _handle: _plugin_result()
    )
    case.chat.set_plugin_tools(plugin.adapter)
    case.chat._tool_artifacts = ToolArtifactRepository(
        database, tmp_path / "results", retention_seconds=60, max_artifact_bytes=100000
    )
    calls = [
        ("send", {"text": "local child delivery"}),
        (
            "memory_change",
            {
                "operation": "create",
                "target": {"subject_ref": "current_speaker", "scope_type": "person"},
                "new_content": "hello",
                "memory_key": "episode:child-source",
                "category": "episode",
                "reason": "保存原用户消息",
            },
        ),
        ("update_short_state", {"slot": 1, "text": "child updated shared state"}),
        (
            "automation_manage",
            {
                "action": "create",
                "task": {
                    "name": "child reminder",
                    "goal": "check later",
                    "trigger": {"type": "after", "seconds": 3600},
                },
            },
        ),
        (plugin.name, {}),
        ("call_onebot_api", {"action": "get_status", "params": {}}),
        ("task_control", {"action": "complete", "result": "checked"}),
    ]
    observed = {}

    def respond(request):
        index = len(case.provider.requests) - 1
        if index:
            observed[calls[index - 1][0]] = json.loads(request.messages[-1].content)
        name, arguments = calls[index]
        return ChatResponse(
            "",
            0,
            tool_calls=(ToolCall(f"child:{index}", ToolFunction(name, json.dumps(arguments))),),
        )

    case.provider._responder = respond
    executor = make_child_executor(
        repository,
        chat=case.chat,
        config=case.chat._runtime_config,
        runner=case.chat.runtime.runner,
        load_tools=case.chat.runtime.runner.main_contract.definitions,
        router=case.env.router,
    )
    try:
        assert await executor.run(child) is None
        assert (await repository.get(child))["state"] == "completed"
        for name, _ in calls[:-2]:
            assert observed[name]["ok"], (name, observed[name])
        assert not observed["call_onebot_api"]["ok"]
        assert observed["call_onebot_api"]["error"] == "tool_not_declared"
        assert not any(action == "get_status" for action, _ in case.env.bot.calls)
        assert sum(action == "send_group_msg" for action, _ in case.env.bot.calls) == 1
        assert plugin.calls == [True]
        assert (
            case.chat.runtime.runner.main_contract.state.snapshot()[0]["text"]
            == calls[2][1]["text"]
        )
        assert len(await case.repository.list_current_for_creator(case.env.person)) == 2
        async with database.read_session() as reader:
            owned_effects = list((await reader.execute(select(effects))).mappings())
        assert owned_effects and {row["work_id"] for row in owned_effects} == {child}
        assert (await repository.get(parent["id"]))["model_requests"] == 0
        assert (
            case.provider.requests[0].tools
            == await case.chat.runtime.runner.main_contract.model_definitions()
        )
        assert "call_onebot_api" not in {tool.name for tool in case.provider.requests[0].tools}
        assert (
            case.provider.requests[0].messages[0] == case.chat._prompt_composer.static_messages()[0]
        )
    finally:
        await plugin.context.close_host_resources()


async def _plugin_result():
    return ToolResult(data={"checked": True})


@pytest.mark.parametrize("close_parent", [False, True])
async def test_real_worker_completion_keeps_original_tail_and_late_parent_mail(
    database, tmp_path, close_parent
):
    case, repository, parent, child = await _message_child(database, tmp_path)
    database.subagents_enabled = True
    if close_parent:
        lease = await repository.acquire(parent["conversation_id"], parent["generation"])
        await repository.transition(lease, parent["id"], parent["revision"], "completed")
        await repository.release(lease)
    parent_before = await repository.get(parent["id"])
    calls = (
        ("task_control", {"action": "complete", "result": "verified child result"}),
        ("send", {"text": "actual tail delivery"}),
        ("subagent_control", {"action": "message", "text": "original detailed report"}),
        ("subagent_start", {"goal": "independent nested step"}),
    )
    response = ChatResponse(
        "",
        0,
        tool_calls=tuple(
            ToolCall(f"original-tail:{index}", ToolFunction(name, json.dumps(arguments)))
            for index, (name, arguments) in enumerate(calls)
        ),
    )
    responses = iter([response])
    case.provider._responder = lambda _request: next(responses)
    executor = make_child_executor(
        repository,
        chat=case.chat,
        config=case.chat._runtime_config,
        runner=case.chat.runtime.runner,
        load_tools=case.chat.runtime.runner.main_contract.definitions,
        router=case.env.router,
    )
    assert await executor.run(child) is None
    settled = await repository.get(child)
    assert settled["state"] == "completed"
    assert json.loads(settled["checkpoint_json"])["sync_result"] == "verified child result"
    assert sum(action == "send_group_msg" for action, _ in case.env.bot.calls) == 1
    assert await repository.get(parent["id"]) == parent_before
    async with database.read_session() as reader:
        nested = list(await reader.scalars(select(work.c.id).where(work.c.parent_work_id == child)))
        mail = list(
            (
                await reader.execute(select(inputs).where(inputs.c.work_id == parent["id"]))
            ).mappings()
        )
        owned_effects = list(
            (await reader.execute(select(effects).where(effects.c.work_id == child))).mappings()
        )
    assert len(nested) == 1 and (await repository.get(nested[0]))["state"] == "queued"
    assert len(mail) == 2
    assert all(row["state"] == "accepted" for row in owned_effects)
    workers = SubagentRepository(repository)
    result = await workers.related(parent["id"], child, include_checkpoint=True)
    assert json.loads(result["result_json"])["text"] == "verified child result"
    # The actual executor has no paid corrective request after an accepted finish.
    assert len(case.provider.requests) == 1
    assert (
        case.provider.requests[0].tools
        == await case.chat.runtime.runner.main_contract.model_definitions()
    )


async def test_nested_subagent_parent_message_control_and_cancel_follow_real_tree(
    database, tmp_path
):
    repository, workers, root_lease, root, child = await stack(database, tmp_path)
    database.subagents_enabled = True
    child_lease = await workers.acquire(child)
    child_row = await repository.get(child)

    async def validate_child():
        assert await repository.valid(child_lease)

    control = WorkControl(
        repository,
        child_lease,
        child_row["source_key"],
        json.loads(child_row["source_json"]),
        validate_child,
        current=child_row,
    )
    spawned = await execute_subagent(
        control, "subagent_start", {"goal": "grandchild"}, "spawn-grandchild"
    )
    grandchild = spawned["child_id"]
    assert (await repository.get(grandchild))["parent_work_id"] == child
    assert (await execute_subagent(control, "subagent_control", {"action": "list"}, "list"))[
        "children"
    ][0]["child_id"] == grandchild
    await execute_subagent(
        control,
        "subagent_control",
        {"action": "message", "child_id": grandchild, "text": "check fact"},
        "to-grandchild",
    )
    grand_lease = await workers.acquire(grandchild)
    grand_row = await repository.get(grandchild)

    async def validate_grandchild():
        assert await repository.valid(grand_lease)

    grand_control = WorkControl(
        repository,
        grand_lease,
        grand_row["source_key"],
        json.loads(grand_row["source_json"]),
        validate_grandchild,
        current=grand_row,
    )
    result = await execute_subagent(
        grand_control,
        "subagent_control",
        {"action": "message", "text": "which fact?", "ask": True},
        "to-child",
    )
    assert result["waiting_parent"] and grand_control.ending == "waiting_user"
    async with database.read_session() as reader:
        messages = {
            row["source_key"]: row["work_id"]
            for row in (await reader.execute(select(inputs))).mappings()
        }
    assert messages["to-grandchild"] == grandchild and messages["to-child"] == child
    sibling = await workers.start(root_lease, root["id"], "sibling", {"goal": "other target"})
    with pytest.raises(ValueError, match="subagent_not_owned"):
        await execute_subagent(
            control, "subagent_control", {"action": "cancel", "child_id": sibling}, "unowned-cancel"
        )
    await execute_subagent(
        control,
        "subagent_control",
        {"action": "cancel", "child_id": grandchild},
        "cancel-grandchild",
    )
    assert (await repository.get(grandchild))["state"] == "cancelled"
    assert not await repository.valid(grand_lease)
    assert (await repository.get(sibling))["state"] == "queued"
    assert (await repository.get(root["id"]))["state"] == "running"


async def test_plugin_background_child_reuses_original_job_authority_after_restart(
    database, tmp_path
):
    from tests.conftest import build_harness, make_settings
    from tests.support.canonical_identity import ensure_presence
    from tests.unit.test_external_event_runtime_fences import _seed_job

    from yuki.llm.fake import FakeLLMProvider
    from yuki.plugin_host.db_models import (
        PluginBackgroundTurnJobModel,
        PluginInstallationModel,
    )
    from yuki.services.execution_sources import BackgroundTaskSource, recover_execution_source

    _notifications, event_id, conversation = await _seed_job(database)
    async with database.read_session() as reader:
        job = await reader.scalar(select(PluginBackgroundTurnJobModel))
    async with database.immediate_session() as writer:
        presence = await ensure_presence(writer, "8000")
    repository = WorkRepository(database)
    lease = await repository.acquire(conversation, 1)
    source = {
        "owner": "plugin_background",
        "origin": "plugin_background",
        "plugin_id": job.plugin_id,
        "trigger_event_id": event_id,
        "generation": 1,
        "conversation_id": conversation,
        "presence_id": presence,
        "bot_user_id": "8000",
    }
    root = await repository.accept(
        lease, source_key="original-plugin-event", source=source, goal="check"
    )
    async with database.immediate_session() as writer:
        await writer.execute(
            update(PluginBackgroundTurnJobModel)
            .where(PluginBackgroundTurnJobModel.id == job.id)
            .values(work_id=root["id"])
        )
    database.subagents_enabled = True
    workers = SubagentRepository(repository)
    child = await workers.start(
        lease, root["id"], "plugin-child", {"goal": "check original external event"}
    )
    await repository.release(lease)
    child_source = json.loads((await repository.get(child))["source_json"])
    await database.engine.dispose()
    recovered = await recover_execution_source(
        database, conversation, child_source, request_id=child
    )
    assert isinstance(recovered, BackgroundTaskSource)
    assert recovered.event_id == event_id and recovered.job_id == job.id
    assert recovered.actor_person_id is None and recovered.actor_user_id == ""
    provider = FakeLLMProvider(lambda _: "original plugin source checked")
    chat = build_harness(database, make_settings(database.url), provider).processor._chat
    from yuki.services.main_agent_contract import MainAgentContract
    from yuki.workspace.short_state import ShortState
    from yuki.workspace.store import WorkspaceStore

    chat.runtime.runner.main_contract = MainAgentContract(
        chat, ShortState(WorkspaceStore(tmp_path / "state"))
    )
    executor = make_child_executor(
        repository,
        chat=chat,
        config=chat._runtime_config,
        runner=chat.runtime.runner,
        load_tools=chat.runtime.runner.main_contract.definitions,
    )
    assert await executor.run(child) is None
    assert (await repository.get(child))["state"] == "completed" and len(provider.requests) == 1
    async with database.immediate_session() as writer:
        await writer.execute(
            update(PluginInstallationModel)
            .where(PluginInstallationModel.plugin_id == job.plugin_id)
            .values(enabled=False)
        )
    with pytest.raises(Exception, match="plugin_authority_revoked"):
        await recover_execution_source(database, conversation, child_source, request_id=child)
