"""Independent plugin computation shares Pi without acquiring business tools."""

import asyncio
from dataclasses import replace

import pytest
from sqlalchemy import func, select
from tests.conftest import build_harness, make_settings
from tests.unit.test_tool_effect_audit import active_work

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.domain.messages import ChatResponse, ToolCall, ToolFunction
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.plugin_host.repository import PluginInstallationRepository
from qq_ai_bot.plugin_host.session_repository import PluginAgentSessionRepository
from qq_ai_bot.runtime.work_activation import current_work_control
from qq_ai_bot.services.concurrency import RequestCancelledError
from qq_ai_bot.services.plugin_sessions import (
    PluginAgentSessionService,
    PluginSessionAuthority,
    PluginSessionPermissionError,
)


@pytest.mark.parametrize("scenario", ["normal", "tool_attempt", "revoked", "cancelled"])
async def test_isolated_session_has_no_tools_or_new_conversation_and_charges_parent(
    database, tmp_path, scenario
):
    env, owner, _ = await active_work(database, tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    class Provider(FakeLLMProvider):
        async def complete(self, request):
            # ChatRequest represents the service's tools=None backend as an
            # empty wire declaration. Neither business nor native tools exist.
            assert request.tools == ()
            assert request.native_tools == ()
            entered.set()
            if scenario == "cancelled":
                await release.wait()
            return await super().complete(request)

    def respond(_request):
        if scenario == "tool_attempt":
            return ChatResponse(
                "computed",
                0,
                tool_calls=(ToolCall("spoof", ToolFunction("execute_code", '{"code":"1"}')),),
            )
        return "computed"

    provider = Provider(respond)
    chat = build_harness(database, make_settings(database.url), provider).processor._chat
    repository = PluginAgentSessionRepository(database)
    service = PluginAgentSessionService(
        provider=provider,
        concurrency=chat._concurrency,
        runtime_config=chat._runtime_config,
        repository=repository,
    )
    authority = PluginSessionAuthority(
        "test.compute", "10001", "20001", frozenset({"agent.session"})
    )
    await PluginInstallationRepository(database).upsert_discovered(
        plugin_id=authority.plugin_id,
        name="Independent computation",
        version="1.0",
        plugin_api="1",
        yuki_requires=">=3",
        entrypoint="plugin:Plugin",
        requested_permissions=("agent.session",),
        manifest_hash="a" * 64,
    )
    session = await service.create(
        authority,
        name="independent",
        instructions="Calculate independently",
        persistence="durable",
        context_profile="none",
        allowed_capabilities=("execute_code", "send_message"),
    )
    async with database.sessions() as reader:
        before = await reader.scalar(select(func.count()).select_from(CanonicalConversationModel))
    token = current_work_control.set(owner.control)
    try:
        if scenario == "revoked":
            with pytest.raises(PluginSessionPermissionError):
                await service.run(
                    replace(authority, approved_permissions=frozenset()),
                    session_id=session.session_id,
                    user_input="compute",
                    allowed_capabilities=("execute_code",),
                    max_tool_calls=100,
                    max_model_requests=2,
                )
        else:
            task = asyncio.create_task(
                service.run(
                    authority,
                    session_id=session.session_id,
                    user_input="compute",
                    allowed_capabilities=("execute_code", "send_message"),
                    max_tool_calls=100,
                    max_model_requests=2,
                )
            )
            if scenario == "cancelled":
                await asyncio.wait_for(entered.wait(), 5)
                task.cancel()
                with pytest.raises(RequestCancelledError):
                    await task
            else:
                result = await task
                assert result.tool_calls_used == 0
                assert result.model_requests == (2 if scenario == "tool_attempt" else 1)
    finally:
        current_work_control.reset(token)
        release.set()
        await owner.control.repository.release(owner.control.lease)
    updated = await owner.control.repository.get(owner.control.current["id"])
    assert (
        updated["model_requests"]
        == {
            "normal": 1,
            "tool_attempt": 2,
            "revoked": 0,
            "cancelled": 1,
        }[scenario]
    )
    assert updated["tool_calls"] == 0
    history = await repository.list_messages(
        plugin_id=authority.plugin_id, session_id=session.session_id, limit=100
    )
    assert sum(message.role == "assistant" for message in history) == int(
        scenario in {"normal", "tool_attempt"}
    )
    async with database.sessions() as reader:
        after = await reader.scalar(select(func.count()).select_from(CanonicalConversationModel))
    assert after == before
    assert not any(action.startswith("send_") for action, _ in env.bot.calls)
