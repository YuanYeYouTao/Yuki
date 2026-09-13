"""Recheck live authority after a transient attempt without replaying its handler."""

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from qq_ai_bot.automation.executor import AutomationExecutionError, AutomationExecutor
from qq_ai_bot.automation.models import RetryPolicy
from qq_ai_bot.automation.registry import AutomationCapabilityRegistry
from qq_ai_bot.persistence.models import AutomationModel


async def authority_between_attempts(
    database, settings, registry, repository, row, run, time_service
):
    calls = 0

    async def first_attempt(arguments, context):
        nonlocal calls
        calls += 1
        assert context.revalidate_authority is not None
        assert context.automation_script_hash == row.script_hash
        assert context.source_step_id == row.script.steps[0].id
        # This fixture's target has not had a conversation yet: never invent an epoch.
        assert context.conversation_generation is None
        async with database.sessions() as session, session.begin():
            current = await session.get(AutomationModel, row.id)
            current.status = "paused"
        raise AutomationExecutionError("temporary_failure", transient=True)

    executing = AutomationCapabilityRegistry()
    for definition in registry.list():
        if definition.name == "onebot.send_private_message":
            definition = replace(
                definition, handler=first_attempt, retry_policy=RetryPolicy.TRANSIENT_ONCE
            )
        executing.register(definition)
    result = await AutomationExecutor(
        settings=settings,
        registry=executing,
        repository=repository,
        time_service=time_service,
        router=SimpleNamespace(resolve_send_for_person=AsyncMock()),
    ).execute(row, run)
    assert calls == 1, result
    # A SEND handler is never replayed on a transient hint alone.
    assert result.error_category == "temporary_failure"
    assert result.messages_sent == 0


async def guarded_agent_calls(handlers, context, provider):
    from qq_ai_bot.automation.handlers import _AutomationAgentBackend
    from qq_ai_bot.automation.registry import CapabilityResult, build_capability_registry

    async def revoked(capability):
        raise AutomationExecutionError("automation_inactive")

    guarded = replace(context, revalidate_authority=revoked)
    before = len(provider.requests)
    with pytest.raises(AutomationExecutionError) as caught:
        await handlers.generate({"instruction": "work", "context_profile": "none"}, guarded)
    assert caught.value.category == "automation_inactive"
    assert caught.value.llm_calls == 0
    assert len(provider.requests) == before
    capability = build_capability_registry().require("onebot.send_private_message")
    effect = AsyncMock(return_value=CapabilityResult(data={"sent": True}))
    registry = AutomationCapabilityRegistry()
    registry.register(replace(capability, handler=effect))
    backend = _AutomationAgentBackend(registry, guarded)
    backend._name_map = {"guarded_send": capability.name}
    result = json.loads(
        await backend.execute("guarded_send", '{"user_id":"10001","text":"hi"}', None)
    )
    assert result["error"] == "automation_inactive"
    effect.assert_not_called()
    backend.short_state = SimpleNamespace(execute=AsyncMock())
    result = json.loads(await backend.execute("update_short_state", "{}", None))
    assert result["error"] == "automation_inactive"
    backend.short_state.execute.assert_not_called()
