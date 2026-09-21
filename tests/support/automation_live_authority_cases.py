"""Recheck live authority after a transient attempt without replaying its handler."""

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
    from qq_ai_bot.services.agent_tools import ToolRuntime
    from qq_ai_bot.services.main_agent_backend import MainAgentBackend

    async def revoked(capability=None):
        raise AutomationExecutionError("automation_inactive")

    guarded = replace(context, revalidate_authority=revoked)
    before = len(provider.requests)
    with pytest.raises(AutomationExecutionError) as caught:
        await handlers.generate({"instruction": "work", "context_profile": "none"}, guarded)
    assert caught.value.category == "automation_inactive"
    assert caught.value.llm_calls == 0
    assert len(provider.requests) == before
    backend = MainAgentBackend(
        handlers._agent_runner.main_contract.chat,
        ToolRuntime(
            inbound=None, gateway=None, allow_generic_onebot=False, before_model_request=revoked
        ),
    )
    for name in ("update_short_state",):
        with pytest.raises(AutomationExecutionError, match="automation_inactive"):
            await backend.execute(name, "{}", None)
