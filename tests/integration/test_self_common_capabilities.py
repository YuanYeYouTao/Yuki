"""Real local SELF sources and shared tools; no live model or QQ requests."""

import json
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from tests.support.semantic_participation_host_helpers import _event_and_route, _host, _item
from tests.support.social_identity_cases import social_env
from tests.unit.test_plugin_native_media import environment
from yuki_participation.models import CandidateKind, Proposal

from qq_ai_bot.capabilities.invocation import ToolInvocationContext, current_invocation
from qq_ai_bot.conversation.autonomy_repository import AutonomyRepository
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.tool_actor import ToolActor
from qq_ai_bot.plugin_host.facades import HostPluginContext, PluginFacadeServices
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.services.agent_tools import ToolRuntime
from qq_ai_bot.social.service import SocialContext
from yuki_plugin_sdk.models import TurnOrigin as SdkTurnOrigin
from yuki_plugin_sdk.permissions import PluginPermission


async def self_actor(database, env, *, proposal_id="local-self-opportunity"):
    repository = AutonomyRepository(database)
    binding = await repository.ensure_binding(env.context.conversation_id, 1)
    binding = await repository.transition(binding, master_enabled=True, external_enabled=True)
    accepted = await repository.accept_host_proposal(
        proposal_id=proposal_id,
        binding=binding,
        owner=binding.effective_owner,
        space_id=env.space,
        presence_id=env.presence,
        sources=(),
        trigger_kind="intrinsic",
    )
    return ToolActor(
        user_id="",
        bot_user_id="80001",
        group_id="20001",
        origin=TurnOrigin.SELF_INITIATIVE,
        instruction="使用共同工具完成当前工作",
        execution_id="original-self-execution",
        conversation_id=env.context.conversation_id,
        presence_id=env.presence,
        principal_kind="self",
        initiative_run_id=accepted.run.run_id,
    )


def runtime(actor, env):
    async def call_api(action, params):
        return await env.bot.call_api(action, **params)

    return ToolRuntime(
        inbound=None,
        gateway=SimpleNamespace(call_api=call_api),
        allow_generic_onebot=False,
        actor_context=actor,
        origin=actor.origin,
        initiative_run_id=actor.initiative_run_id,
        execution_id=actor.execution_id,
        conversation_id=actor.conversation_id,
        space_id=env.space,
        target_presence_id=actor.presence_id,
        scope_type=ScopeType.GROUP,
        external_target_id=actor.group_id,
        conversation_key=f"bot:{actor.bot_user_id}:group:{actor.group_id}",
    )


@pytest.mark.asyncio
async def test_self_mentions_poke_recall_and_receipt_history_use_original_social_backend(
    database, tmp_path
):
    env = await social_env(database, tmp_path)
    actor = await self_actor(database, env)
    context = SocialContext(
        actor.source_key,
        "send-with-mention",
        actor.conversation_id,
        person_refs={"known_person": env.person},
        account_refs={"known_person": "10001"},
        space_id=env.space,
        origin=actor.origin.value,
        initiative_run_id=actor.initiative_run_id,
        presence_id=env.presence,
        actor=actor,
    )
    arguments = {"text": "hello", "mentions": [{"subject_ref": "known_person"}]}
    sent = await env.service.execute("send_message", arguments, context)
    assert sent["status"] == "succeeded"
    assert await env.service.execute("send_message", arguments, context) == sent
    sends = [params for action, params in env.bot.calls if action == "send_group_msg"]
    assert len(sends) == 1 and sends[0]["message"][0]["type"] == "at"
    await env.service.execute(
        "poke_person", {"subject_ref": "known_person"}, replace(context, call_id="poke")
    )
    assert env.bot.calls[-1] == ("send_poke", {"user_id": 10001, "group_id": 20001})
    operation = sent["operation_id"]
    receipt = await env.service.receipts.get(operation)
    assert receipt.event_id is not None
    await env.service.execute(
        "read_conversation_history",
        {"operation_id": operation},
        replace(context, call_id="history"),
    )
    await env.service.execute(
        "find_contacts", {"kind": "person"}, replace(context, call_id="contacts")
    )
    recalled = await env.service.execute(
        "recall_own_message", {"event_id": receipt.event_id}, replace(context, call_id="recall")
    )
    assert recalled["status"] == "succeeded"
    assert await env.router.cas_takeover_person(env.person) == "taken"
    private = await env.service.execute(
        "send_message",
        {"target": {"kind": "person", "target_id": env.person}, "text": "explicit target"},
        replace(context, call_id="explicit-private"),
    )
    assert private["status"] == "succeeded" and private["target"]["kind"] == "person"


@pytest.mark.asyncio
async def test_self_sdk_media_music_and_schedule_use_true_actor_without_inbound(database, tmp_path):
    env = await social_env(database, tmp_path)
    actor = await self_actor(database, env)
    source = runtime(actor, env)
    media = await environment(database, tmp_path)
    selected = await media.adapter.execute(media.name, "{}", source)
    assert selected.ok and selected.images
    automation = SimpleNamespace(
        create_task=AsyncMock(
            return_value=SimpleNamespace(
                id=7,
                name="plan",
                status=SimpleNamespace(value="active"),
                next_run_at=None,
                last_run_at=None,
                run_count=0,
            )
        )
    )
    host = HostPluginContext(
        plugin_id="example.plugin",
        approved_permissions=(
            PluginPermission.ONEBOT_SEND,
            PluginPermission.AUTOMATION_MANAGE_SELF,
        ),
        services=PluginFacadeServices(
            ledger=SimpleNamespace(_database=database, _writer=env.service.writer),
            automation=automation,
        ),
    )
    token = current_invocation.set(
        ToolInvocationContext(runtime=source, call_id="original-plugin-call")
    )
    try:
        with host.invocation_scope("example.plugin", source):
            invocation = host._invocation()
            assert invocation.inbound is None and invocation.actor is actor
            assert invocation.origin.value == SdkTurnOrigin.SELF_INITIATIVE.value
            assert invocation.actor_user_id == ""
            assert not host._is_real_superuser(invocation)
            music = await host.onebot.send_music_card(provider="netease", resource_id="123")
            assert music.ok
            await host.automation.create_task(
                {"name": "plan", "goal": "保留完整目标"}, request_id="original-plan"
            )
        assert automation.create_task.await_args.kwargs["actor"] is actor
        receipt = await env.service.receipts.get(music.data["operation_id"])
        assert receipt.event_id is not None and receipt.status.value == "succeeded"
        from qq_ai_bot.social.db_models import SocialOperationModel

        async with database.sessions() as session:
            saved = await session.get(SocialOperationModel, receipt.operation_id)
            assert saved.source_conversation_id == actor.conversation_id
            assert saved.tool_call_id == "send:1"
    finally:
        current_invocation.reset(token)


@pytest.mark.asyncio
async def test_self_new_proposals_ignore_expiry_scoring_reuse_and_existing_work(database, tmp_path):
    host, _ = await _host(database, tmp_path)
    try:
        event = await _event_and_route(database, host.app.ledger)
        item = await _item(host, event)
        binding = await host._binding(item)
        focus = item.controller.state.events[f"event:{event.id}"]
        now = time.time()
        proposal = Proposal(
            proposal_id="long-real-proposal" * 20,
            scope=item.scene.scope,
            controller_epoch=binding.controller_epoch,
            kind=CandidateKind.CONVERSATION,
            thread=focus.thread,
            target_hint=focus.target,
            sources=(focus.ref,),
            support=None,
            created_at=now - 300,
            expires_at=now - 200,
        )
        item.controller.state.proposals[proposal.proposal_id] = proposal
        first = await host._admit_proposal(item, binding, proposal)
        assert first["outcome"] == "accepted"
        await host._dispatch(await host.repository.get_run(first["run_id"]))
        original = await host.work.by_source(f"initiative:{first['run_id']}")
        assert original["state"] == "queued"
        next_proposal = proposal.model_copy(update={"proposal_id": "another-real-opportunity"})
        item.controller.state.proposals[next_proposal.proposal_id] = next_proposal
        second = await host._admit_proposal(item, binding, next_proposal)
        assert second["outcome"] == "accepted" and second["run_id"] != first["run_id"]
        duplicate = await host._admit_proposal(item, binding, proposal)
        assert duplicate["outcome"] == "duplicate" and duplicate["run_id"] == first["run_id"]
        assert await host.work.by_source(f"initiative:{first['run_id']}") == original
        assert "Yuki也来说说吧" not in json.loads(original["source_json"])["instruction"]
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_self_sdk_main_reads_original_result_and_keeps_real_sources_distinct(
    database, tmp_path, monkeypatch
):
    from tests.unit.test_history_dispatch_ownership import _scene, _tool

    from qq_ai_bot.llm.fake import FakeLLMProvider
    from qq_ai_bot.runtime.work_repository import WorkRepository

    provider = FakeLLMProvider()
    env, harness, chat, _, _ = await _scene(database, tmp_path, provider)
    provider._responder = lambda _: _tool(
        "task_control",
        {"action": "complete", "result": f"SDK result {len(provider.requests)}"},
        "finish",
    )
    host = HostPluginContext(
        plugin_id="test.self-main",
        approved_permissions=(PluginPermission.AGENT_RUN,),
        services=PluginFacadeServices(
            approval_revision="approved-real-fixture",
            ledger=harness.ledger,
            agent_runner=chat.runtime.runner,
            runtime_config=chat._runtime_config,
        ),
    )
    actor = await self_actor(database, env)
    source = replace(runtime(actor, env), runtime_config=await chat._runtime_config.snapshot())
    from qq_ai_bot.plugin_host import main_turn

    original_backend = main_turn.MainAgentBackend
    actors = []

    def backend(service, tool_runtime, **kwargs):
        actors.append(tool_runtime.require_actor())
        assert tool_runtime.inbound is None and not tool_runtime.actor_is_superuser
        return original_backend(service, tool_runtime, **kwargs)

    monkeypatch.setattr(main_turn, "MainAgentBackend", backend)
    instruction = "使用相同 Main 完成插件工作" + "x" * 12_001
    with host.invocation_scope(host.plugin_id, source):
        first = await host.agent.run(instruction)
        original = await WorkRepository(database).get(first.data["work_id"])
        repeated = await host.agent.run(instruction)
    assert first.ok and first.data["state"] == "completed"
    assert repeated.data["work_id"] == first.data["work_id"]
    assert repeated.data["text"] == first.data["text"] == "SDK result 1"
    assert len(provider.requests) == 1
    assert await WorkRepository(database).get(first.data["work_id"]) == original
    saved = json.loads(original["source_json"])
    assert saved["instruction"] == original["goal"] == instruction
    assert saved["actor_source"]["initiative_run_id"] == actor.initiative_run_id
    assert saved["principal_kind"] == "self" and saved["actor_user_id"] == ""
    assert saved["trigger_event_id"] is None and not saved["actor_is_superuser"]
    another = await self_actor(database, env, proposal_id="another-sdk-source")
    source = replace(source, actor_context=another, initiative_run_id=another.initiative_run_id)
    with host.invocation_scope(host.plugin_id, source):
        second = await host.agent.run(instruction)
    assert second.ok and second.data["work_id"] != first.data["work_id"]
    assert second.data["text"] == "SDK result 2" and len(provider.requests) == 2
    assert [item.initiative_run_id for item in actors] == [
        actor.initiative_run_id,
        another.initiative_run_id,
    ]


@pytest.mark.asyncio
async def test_self_sdk_main_isolates_existing_work_and_reports_actual_scope_lease_conflict(
    database, tmp_path, monkeypatch
):
    from tests.unit.test_history_dispatch_ownership import _scene, _tool

    from qq_ai_bot.domain.conversations import ConversationScope
    from qq_ai_bot.llm.fake import FakeLLMProvider
    from qq_ai_bot.runtime.work_activation import activate_work, current_work_control
    from qq_ai_bot.runtime.work_repository import WorkConflict, WorkRepository

    provider = FakeLLMProvider(
        responder=lambda _: _tool("task_control", {"action": "complete", "result": "done"}, "done")
    )
    env, harness, chat, _, _ = await _scene(database, tmp_path, provider)
    host = HostPluginContext(
        plugin_id="test.self-main",
        approved_permissions=(PluginPermission.AGENT_RUN,),
        services=PluginFacadeServices(
            ledger=harness.ledger,
            agent_runner=chat.runtime.runner,
            runtime_config=chat._runtime_config,
        ),
    )
    actor = await self_actor(database, env)
    source = replace(runtime(actor, env), runtime_config=await chat._runtime_config.snapshot())
    private = await env.service.writer.append(
        scope=ConversationScope.private(actor.bot_user_id, "10001"),
        platform_message_id="independent-real-source",
        sender_user_id="10001",
        direction="inbound",
        content="separate caller",
    )
    repository = WorkRepository(database)
    original_assemble = chat._context_assembler.assemble_plugin
    preparations = []

    async def assemble(**kwargs):
        control = current_work_control.get()
        assert control is not None and control.current is not None
        assert await repository.valid(control.lease)
        preparations.append(control.current["id"])
        return await original_assemble(**kwargs)

    monkeypatch.setattr(chat._context_assembler, "assemble_plugin", assemble)

    async def validate():
        pass

    async with activate_work(
        repository, private.event.canonical_conversation_id, 1, "independent-caller", {}, validate
    ) as outer:
        with host.invocation_scope(host.plugin_id, source):
            result = await host.agent.run("合法独立来源")
        assert result.ok and result.data["state"] == "completed"
        assert current_work_control.get() is outer and outer.current is None
        assert preparations == [result.data["work_id"]]
    async with activate_work(
        repository, actor.conversation_id, 1, actor.source_key, {}, validate
    ) as occupied:
        with host.invocation_scope(host.plugin_id, source):
            with pytest.raises(WorkConflict, match="conversation_activation_busy"):
                await host.agent.run("已有租约的会话")
        assert current_work_control.get() is occupied and occupied.current is None
    assert len(provider.requests) == 1
    assert preparations == [result.data["work_id"]]


@pytest.mark.asyncio
async def test_self_sdk_main_context_rollup_wait_returns_original_durable_work(
    database, tmp_path, monkeypatch
):
    from tests.unit.test_history_dispatch_ownership import _scene

    from qq_ai_bot.llm.fake import FakeLLMProvider
    from qq_ai_bot.runtime.context_preparation import ContextRollupRequired
    from qq_ai_bot.runtime.work_activation import current_work_control
    from qq_ai_bot.runtime.work_repository import WorkRepository

    provider = FakeLLMProvider()
    env, harness, chat, _, _ = await _scene(database, tmp_path, provider)
    host = HostPluginContext(
        plugin_id="test.self-main",
        approved_permissions=(PluginPermission.AGENT_RUN, PluginPermission.MESSAGE_HISTORY_READ),
        services=PluginFacadeServices(
            ledger=harness.ledger,
            agent_runner=chat.runtime.runner,
            runtime_config=chat._runtime_config,
        ),
    )
    actor = await self_actor(database, env)
    source = replace(runtime(actor, env), runtime_config=await chat._runtime_config.snapshot())

    async def prepare(**kwargs):
        control = current_work_control.get()
        assert control is not None and control.current is not None
        assert await control.repository.valid(control.lease)
        version, events = await harness.ledger.read_scope_context(kwargs["identity"], limit=50)
        raise ContextRollupRequired(version, max((event.id for event in events), default=0), 60)

    monkeypatch.setattr(chat._context_assembler, "assemble_plugin", prepare)
    with host.invocation_scope(host.plugin_id, source):
        first = await host.agent.run("沿原工作等待上下文准备")
        original = await WorkRepository(database).get(first.data["work_id"])
        repeated = await host.agent.run("沿原工作等待上下文准备")
    assert first.ok and first.data["state"] == "waiting_external"
    assert repeated.data["work_id"] == first.data["work_id"]
    assert await WorkRepository(database).get(first.data["work_id"]) == original
    assert original["model_requests"] == 0 and provider.requests == []
    assert "context_rollup" in json.loads(original["checkpoint_json"])


@pytest.mark.asyncio
async def test_self_sdk_main_resume_keeps_original_actor_source_and_work(database, tmp_path):
    from tests.unit.test_history_dispatch_ownership import _scene, _tool

    from qq_ai_bot.llm.fake import FakeLLMProvider
    from qq_ai_bot.plugin_host.main_turn import resume_plugin_work
    from qq_ai_bot.runtime.work_repository import WorkRepository
    from qq_ai_bot.runtime.work_wait import WorkWaitRepository

    provider = FakeLLMProvider(
        responder=lambda _: _tool(
            "task_control",
            {"action": "wait", "conditions": [{"kind": "time_due", "after_seconds": 1}]},
            "wait",
        )
    )
    env, harness, chat, _, _ = await _scene(database, tmp_path, provider)
    host = HostPluginContext(
        plugin_id="test.self-main",
        approved_permissions=(PluginPermission.AGENT_RUN,),
        services=PluginFacadeServices(
            approval_revision="same-approved-code",
            ledger=harness.ledger,
            agent_runner=chat.runtime.runner,
            runtime_config=chat._runtime_config,
        ),
    )
    actor = await self_actor(database, env)
    source = replace(runtime(actor, env), runtime_config=await chat._runtime_config.snapshot())
    with host.invocation_scope(host.plugin_id, source):
        first = await host.agent.run("按原真实来源继续")
    assert first.ok and first.data["state"] == "waiting_external"
    repository = WorkRepository(database)
    row = await repository.get(first.data["work_id"])
    saved = json.loads(row["source_json"])
    waits = WorkWaitRepository(repository)
    wait = await waits.describe(row["id"])
    assert await waits.deliver_due(now=wait["registered_at"] + 2) == 1
    provider._responder = lambda _: _tool(
        "task_control", {"action": "complete", "result": "same original work"}, "complete"
    )
    await resume_plugin_work(lambda _: host, harness.ledger, await repository.get(row["id"]), saved)
    final = await repository.get(row["id"])
    assert final["state"] == "completed" and final["source_json"] == row["source_json"]
    assert len(provider.requests) == 2
    result = await host.agent.result(row["id"])
    assert result.ok and result.data["text"] == "same original work"
