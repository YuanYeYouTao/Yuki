"""An actual automation commit keeps its unknown acknowledgement through Main."""

import json
from dataclasses import replace

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.support.automation_unified_delivery_helpers import setup_run

from yuki.automation.models import AutomationStatus
from yuki.capabilities.invocation import direct_invocations
from yuki.domain.conversations import ScopeType
from yuki.domain.messages import ToolCall, ToolFunction
from yuki.persistence.models import AutomationModel
from yuki.services.agent_runner import AgentRuntime
from yuki.services.agent_tools import ToolRuntime
from yuki.services.main_agent_backend import MainAgentBackend


@pytest.mark.parametrize("action", ["create", "pause"])
async def test_automation_commit_ack_loss_survives_the_real_main_binding(
    database, tmp_path, monkeypatch, action
):
    case = await setup_run(database, tmp_path)
    actor = replace(case.actor, event_id=None, execution_id="new-host-execution")
    config = await case.chat._runtime_config.snapshot()
    runtime = ToolRuntime(
        inbound=None,
        actor_context=actor,
        execution_id=actor.execution_id,
        conversation_id=actor.conversation_id,
        conversation_key=actor.conversation_id,
        person_id=actor.person_id,
        space_id=case.env.space,
        runtime_config=config,
        scope_type=ScopeType.GROUP,
    )
    agent = AgentRuntime(
        origin=actor.origin,
        actor_user_id=actor.user_id,
        actor_is_superuser=False,
        conversation_key=runtime.conversation_key,
        current_group_id=actor.group_id,
        bot_user_id=actor.bot_user_id,
        gateway=None,
        runtime_config=config,
        allowed_capabilities=frozenset(),
        max_tool_calls=config.agent.max_tool_calls,
        max_model_requests=config.agent.max_model_requests,
    )
    backend = MainAgentBackend(case.chat, runtime)
    await backend.prepare(agent)
    backend.refresh_catalog(agent)
    arguments = {"action": action}
    if action == "create":
        arguments["task"] = {**case.row.script, "name": "original unknown-ACK creation"}
    else:
        arguments["automation_id"] = case.row.id
    call = ToolCall("original-call", ToolFunction("automation_manage", json.dumps(arguments)))
    invocation = direct_invocations((call,), agent)[0]
    commit = AsyncSession.commit
    commits = 0

    async def lost_ack(session):
        nonlocal commits
        await commit(session)
        commits += 1
        raise ValueError("actual commit acknowledgement lost")

    with monkeypatch.context() as fault:
        fault.setattr(AsyncSession, "commit", lost_ack)
        payload = json.loads(await backend.execute_call(invocation))
    assert payload["ok"] is False
    assert payload["uncertain"] is True
    assert payload.get("mutation_committed") is None
    assert payload["retryable"] is False
    assert payload["metadata"]["database_write_phase"] == "commit"
    assert commits == 1  # No failed-creation audit or original operation replay.
    if action == "create":
        async with database.read_session() as reader:
            assert await reader.scalar(select(func.count()).select_from(AutomationModel)) == 2
    else:
        assert (await case.repository.get(case.row.id)).status is AutomationStatus.PAUSED
