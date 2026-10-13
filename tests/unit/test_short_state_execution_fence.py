"""Short-state writes share the physical effect's live execution authority."""

import json
from dataclasses import replace

import pytest
from sqlalchemy import update
from tests.conftest import build_harness, make_settings
from tests.support.fixed_contract_fixture import bind_main_contract
from tests.support.work_session import invoke_tool
from tests.unit.test_tool_effect_audit import active_work

from qq_ai_bot.capabilities.invocation import direct_invocations
from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.domain.messages import ToolCall, ToolFunction
from qq_ai_bot.runtime.work_activation import current_work_control
from qq_ai_bot.runtime.work_repository import WorkConflict
from qq_ai_bot.services.agent_runner import AgentRuntime
from qq_ai_bot.services.main_agent_backend import MainAgentBackend
from qq_ai_bot.services.turn_coordinator import TurnSupersededError


@pytest.mark.parametrize(
    "scenario", ["valid", "expired_after_preflight", "generation_changed", "tools_closed"]
)
async def test_short_state_cannot_write_under_obsolete_or_closed_execution(
    database, tmp_path, monkeypatch, scenario
):
    env, owner, tools = await active_work(database, tmp_path)
    harness = build_harness(database, make_settings(database.url))
    bind_main_contract(harness, tmp_path)
    chat = harness.processor._chat
    contract = chat.runtime.runner.main_contract
    tools = replace(
        tools,
        runtime_config=await chat._runtime_config.snapshot(),
        tools_closed=scenario == "tools_closed",
        scope_type=ScopeType.GROUP,
        space_id=env.space,
        person_id=env.person,
    )
    backend = MainAgentBackend(chat, tools)
    await backend.prepare()
    actor = tools.actor_context
    runtime = AgentRuntime(
        origin=actor.origin,
        actor_user_id=actor.user_id,
        actor_is_superuser=False,
        delegated_authority=None,
        conversation_key=tools.conversation_key,
        current_group_id=actor.group_id,
        bot_user_id=actor.bot_user_id,
        gateway=None,
        runtime_config=tools.runtime_config,
        current_time=chat._time.current_default(),
        allowed_capabilities=frozenset(),
        max_tool_calls=None,
        max_model_requests=None,
        work_control=owner.control,
    )
    control_type = type(owner.control)
    pending = control_type.has_pending_business_inputs
    checks = 0

    async def pending_then_expire(control):
        nonlocal checks
        result = await pending(control)
        checks += 1
        # Kernel admission checks first, then the backend preflight. Expiry at
        # that await must still be caught before the separate workspace write.
        if scenario == "expired_after_preflight" and checks == 2:
            await owner.control.repository.release(owner.control.lease)
        if scenario == "generation_changed" and checks == 2:
            async with database.immediate_session() as writer:
                await writer.execute(
                    update(CanonicalConversationModel)
                    .where(CanonicalConversationModel.id == owner.control.lease.conversation_id)
                    .values(generation=CanonicalConversationModel.generation + 1)
                )
        return result

    monkeypatch.setattr(control_type, "has_pending_business_inputs", pending_then_expire)
    call = ToolCall(
        "short-state",
        ToolFunction("update_short_state", json.dumps({"slot": 1, "text": "actual write"})),
    )
    invocation = direct_invocations((call,), runtime, manifest_revision=owner.contract)[0]
    token = current_work_control.set(owner.control)
    try:
        version, _events = await chat._ledger.read_scope_context(
            ConversationScope.group(actor.bot_user_id, actor.group_id), limit=20
        )
        owner.control.validate = chat._context_validator(version)
        try:
            result = await invoke_tool(
                owner, call, lambda: backend.execute_call(invocation), invocation=invocation
            )
        except (WorkConflict, TurnSupersededError):
            assert scenario in {"expired_after_preflight", "generation_changed"}
        else:
            assert json.loads(result)["ok"] is (scenario == "valid")
        assert [row["text"] for row in contract.state.snapshot()] == (
            ["actual write"] if scenario == "valid" else []
        )
        if scenario in {"expired_after_preflight", "generation_changed"}:
            assert checks >= 2
        if scenario == "expired_after_preflight":
            assert not await owner.control.repository.valid(owner.control.lease)
        if scenario == "generation_changed":
            assert not await chat._ledger.read_version_matches(version)
    finally:
        current_work_control.reset(token)
        await owner.control.repository.release(owner.control.lease)
