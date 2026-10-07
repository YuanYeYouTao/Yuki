"""Static deployment mode agrees across actual main and worker requests."""

import gc
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from tests.conftest import MemorySender, build_harness, make_settings
from tests.support.fixed_contract_fixture import bind_main_contract
from tests.support.runtime_execution import make_child_executor
from tests.support.social_identity_cases import social_env
from tests.unit.test_commands_and_chat import inbound

from qq_ai_bot.codemode.contract import CODE_MODE_POLICY
from qq_ai_bot.domain.messages import ChatResponse
from qq_ai_bot.identity.db_models import CanonicalSpaceModel
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.prompting.contracts import DIRECT_TOOL_POLICY, core_contract
from qq_ai_bot.runtime.subagent_repository import SubagentRepository
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.services.agent_runner import AgentRunner
from qq_ai_bot.tool_results.artifacts import ToolArtifactRepository


def no_native(*args, **kwargs):
    raise AssertionError("ordinary direct turns must not construct a native Code host")


@pytest.mark.parametrize("code_enabled", [False, True])
async def test_main_provider_prompt_matches_static_mode(
    database, tmp_path, monkeypatch, code_enabled
):
    provider = FakeLLMProvider(lambda _: ChatResponse("", 0))
    harness = build_harness(
        database, make_settings(database.url, code_mode_enabled=code_enabled), provider
    )
    bind_main_contract(harness, tmp_path)
    monkeypatch.setattr(AgentRunner, "_code_host", no_native)
    for index in range(8 if not code_enabled else 1):
        result = await harness.processor.handle(
            inbound("hello", message_id=f"mode-{index}"), MemorySender()
        )
        assert result.reason == "chat"
        assert provider.requests
        request = provider.requests[-1]
        system = "\n".join(m.content or "" for m in request.messages if m.role == "system")
        assert core_contract(code_enabled=code_enabled) in system
        assert (CODE_MODE_POLICY in system) is code_enabled
        assert (DIRECT_TOOL_POLICY in system) is not code_enabled
        assert ("execute_code" in {tool.name for tool in request.tools}) is code_enabled
        # The test provider deliberately retains requests; release this test-only owner.
        provider.requests.clear()
    gc.collect()
    assert not harness.concurrency._active
    assert not harness.concurrency._locks
    for state in harness.processor._turn_coordinator._states.values():
        assert not state.tasks and not state.registrations and not state.holders


@pytest.mark.parametrize("code_enabled", [False, True])
async def test_worker_provider_prompt_uses_frozen_main_contract(database, tmp_path, code_enabled):
    env = await social_env(database, tmp_path)
    async with database.sessions() as session, session.begin():
        event_id = await session.scalar(select(ChatEventModel.id))
        space = await session.get(CanonicalSpaceModel, env.space)
        space.enabled = True
    source = dict(
        origin="user_message",
        conversation_id=env.context.conversation_id,
        actor_user_id="10001",
        trigger_id="inbound",
        trigger_event_id=event_id,
        bot_user_id="80001",
        presence_id=env.presence,
        generation=1,
    )
    repo = WorkRepository(database)
    lease = await repo.acquire(env.context.conversation_id, 1)
    parent = await repo.accept(
        lease, source_key="mode-parent", source=source, goal="research", output_kind="answer"
    )
    workers = SubagentRepository(repo)
    child = await workers.start(
        lease, parent["id"], "mode-child", {"goal": "research", "output_kind": "answer"}
    )
    provider = FakeLLMProvider(lambda _: "Verified result")
    harness = build_harness(
        database,
        make_settings(
            database.url,
            runtime_work_enabled=True,
            code_mode_enabled=code_enabled,
            enabled_groups_csv="20001",
        ),
        provider,
    )
    bind_main_contract(harness, tmp_path)
    chat = harness.processor._chat
    chat._tools.sandbox_client = SimpleNamespace()
    chat._tool_artifacts = ToolArtifactRepository(
        database, tmp_path / "tool-results", retention_seconds=86400
    )
    executor = make_child_executor(
        repo,
        chat=chat,
        config=chat._runtime_config,
        runner=chat.runtime.runner,
        load_tools=chat.runtime.runner.main_contract.definitions,
    )
    await executor.prepare(admission_enabled=True)
    await executor.run(child)
    assert provider.requests
    system = "\n".join(m.content or "" for m in provider.requests[0].messages if m.role == "system")
    assert (CODE_MODE_POLICY in system) is code_enabled
    assert (DIRECT_TOOL_POLICY in system) is not code_enabled
    assert ("execute_code" in {tool.name for tool in provider.requests[0].tools}) is code_enabled
    assert (executor.script_api is not None) is code_enabled
