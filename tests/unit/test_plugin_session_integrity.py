"""Isolated sessions keep selected visible text and use real request capacity."""

from dataclasses import replace

import pytest
from tests.conftest import make_settings
from tests.support.model_executor import InjectedModelExecutor
from tests.support.social_identity_cases import social_env

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.domain.messages import ChatResponse
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.model_runtime.capacity import ModelCapacity
from qq_ai_bot.plugin_host.repository import PluginInstallationRepository
from qq_ai_bot.plugin_host.session_facade import BoundAgentSessionFacade
from qq_ai_bot.plugin_host.session_repository import PluginAgentSessionRepository
from qq_ai_bot.runtime.work_repository import WorkCapacityError
from qq_ai_bot.services.concurrency import ConcurrencyManager
from qq_ai_bot.services.plugin_sessions import PluginAgentSessionService, PluginSessionAuthority
from yuki_plugin_sdk.sessions import CreateAgentSessionRequest, RunAgentSessionRequest


@pytest.mark.parametrize("input_tokens", [None, 45_000])
async def test_private_session_keeps_long_history_and_complete_sdk_result(
    database, tmp_path, input_tokens
):
    await social_env(database, tmp_path)
    plugin_id = "integrity.session"
    await PluginInstallationRepository(database).upsert_discovered(
        plugin_id=plugin_id,
        name="Integrity",
        version="1.0.0",
        plugin_api="3.5",
        yuki_requires=">=3.0.0,<4.0",
        manifest_hash="ab" * 32,
        entrypoint="plugin:Integrity",
        requested_permissions=("agent.session",),
    )
    repository = PluginAgentSessionRepository(database)
    visible_output = "  COMPLETE_OUTPUT_START" + "x" * 25_000 + "COMPLETE_OUTPUT_END  "
    provider = FakeLLMProvider(
        lambda request: ChatResponse(
            visible_output, 0, reasoning_content="private hidden reasoning"
        )
    )

    class Models(InjectedModelExecutor):
        def capacity(self, task):
            return ModelCapacity(input_tokens=input_tokens)

    service = PluginAgentSessionService(
        model_executor=Models(provider),
        concurrency=ConcurrencyManager(),
        runtime_config=RuntimeConfigService(
            database=database, settings=make_settings(database.url)
        ),
        repository=repository,
        max_history_messages=601,
    )
    facade = BoundAgentSessionFacade(
        service=service,
        plugin_id=plugin_id,
        actor_user_id="10001",
        current_group_id=None,
        approved_permissions=("agent.session",),
    )
    session = await facade.create(
        CreateAgentSessionRequest(name="long story", instructions="Continue this private story.")
    )
    old_input = "EARLY_HISTORY_START" + "a" * 60_000 + "EARLY_HISTORY_END"
    old_output = "PRIOR_OUTPUT_START" + "b" * 90_000 + "PRIOR_OUTPUT_END"
    for role, content in (("user", old_input), ("assistant", old_output)):
        await repository.append_message(
            plugin_id=plugin_id,
            session_id=str(session.session_id),
            role=role,
            sender_user_id="10001" if role == "user" else None,
            content=content,
        )
    middle = [f"middle chapter {index}" for index in range(598)]
    for content in middle:
        await repository.append_message(
            plugin_id=plugin_id,
            session_id=str(session.session_id),
            role="user",
            sender_user_id="10001",
            content=content,
        )
    request = RunAgentSessionRequest(session_id=session.session_id, user_input="next chapter")
    if input_tokens is not None:
        with pytest.raises(WorkCapacityError, match="model_request_capacity"):
            await facade.run(request)
        assert provider.requests == []
    else:
        result = await facade.run(request)
        assert result.text == visible_output
        assert "private hidden reasoning" not in result.model_dump_json()
        assert [message.content for message in provider.requests[0].messages[1:]] == [
            old_input,
            old_output,
            *middle,
            "next chapter",
        ]
        stored = await repository.list_messages(
            plugin_id=plugin_id, session_id=str(session.session_id), limit=700
        )
        assert [message.content for message in stored] == [
            old_input,
            old_output,
            *middle,
            "next chapter",
            visible_output,
        ]
    # Changing the real caller does not change private ownership or manufacture
    # access to the selected history.
    authority = PluginSessionAuthority(plugin_id, "10001", None, frozenset({"agent.session"}))
    from qq_ai_bot.services.plugin_sessions import PluginSessionNotFoundError

    with pytest.raises(PluginSessionNotFoundError):
        await service.run(
            replace(authority, actor_user_id="99999"),
            session_id=str(session.session_id),
            user_input="read another person's story",
            allowed_capabilities=None,
            max_tool_calls=None,
            max_model_requests=None,
        )
