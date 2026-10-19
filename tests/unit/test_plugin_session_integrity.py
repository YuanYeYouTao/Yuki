"""Isolated sessions keep selected visible text and use real request capacity."""

from dataclasses import replace

import pytest
from tests.conftest import make_settings
from tests.support.model_executor import InjectedModelExecutor
from tests.support.social_identity_cases import social_env

from yuki.admin.config_service import RuntimeConfigService
from yuki.domain.messages import ChatResponse
from yuki.llm.fake import FakeLLMProvider
from yuki.model_runtime.capacity import ModelCapacity
from yuki.plugin_host.repository import PluginInstallationRepository
from yuki.plugin_host.session_facade import BoundAgentSessionFacade
from yuki.plugin_host.session_repository import PluginAgentSessionRepository
from yuki.runtime.work_repository import WorkCapacityError
from yuki.services.concurrency import ConcurrencyManager
from yuki.services.plugin_sessions import PluginAgentSessionService, PluginSessionAuthority
from yuki_plugin_sdk.sessions import CreateAgentSessionRequest, RunAgentSessionRequest


@pytest.mark.parametrize("input_tokens", [None, 45_000])
async def test_private_session_keeps_long_history_and_complete_sdk_result(
    database, tmp_path, input_tokens
):
    env = await social_env(database, tmp_path)
    plugin_id = "integrity.session"
    await PluginInstallationRepository(database).upsert_discovered(
        plugin_id=plugin_id,
        name="Integrity",
        version="1.0.0",
        plugin_api="6.0",
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
        actor_user_id=env.person,
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
            sender_user_id=env.person if role == "user" else None,
            content=content,
        )
    middle = [f"middle chapter {index}" for index in range(598)]
    for content in middle:
        await repository.append_message(
            plugin_id=plugin_id,
            session_id=str(session.session_id),
            role="user",
            sender_user_id=env.person,
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
    authority = PluginSessionAuthority(plugin_id, env.person, None, frozenset({"agent.session"}))
    from yuki.services.plugin_sessions import PluginSessionNotFoundError

    with pytest.raises(PluginSessionNotFoundError):
        await service.run(
            replace(authority, actor_user_id="99999"),
            session_id=str(session.session_id),
            user_input="read another person's story",
            allowed_capabilities=None,
            max_tool_calls=None,
            max_model_requests=None,
        )


@pytest.mark.parametrize("group", [False, True])
async def test_actorless_session_keeps_real_group_or_plugin_scope(database, tmp_path, group):
    from yuki.services.plugin_sessions import PluginSessionPermissionError

    env = await social_env(database, tmp_path)
    plugin_id = "integrity.actorless"
    await PluginInstallationRepository(database).upsert_discovered(
        plugin_id=plugin_id,
        name="Actorless",
        version="1.0.0",
        plugin_api="6.0",
        yuki_requires=">=3.0.0",
        manifest_hash="a" * 64,
        entrypoint="fixture:Plugin",
        requested_permissions=("agent.session", "group.current.read", "person.current.read"),
    )
    repository = PluginAgentSessionRepository(database)
    provider = FakeLLMProvider(lambda _: "only the real original scope")
    service = PluginAgentSessionService(
        model_executor=InjectedModelExecutor(provider),
        concurrency=ConcurrencyManager(),
        runtime_config=RuntimeConfigService(
            database=database, settings=make_settings(database.url)
        ),
        repository=repository,
    )
    facade = BoundAgentSessionFacade(
        service=service,
        plugin_id=plugin_id,
        actor_user_id=None,
        current_group_id=env.space if group else None,
        approved_permissions=("agent.session", "group.current.read", "person.current.read"),
        conversation_id=env.context.conversation_id if group else None,
    )
    with pytest.raises(PluginSessionPermissionError, match="real Person"):
        await facade.create(
            CreateAgentSessionRequest(
                name="no fabricated human", instructions="context", context_profile="current_user"
            )
        )
    session = await facade.create(
        CreateAgentSessionRequest(
            name="real self computation",
            instructions="use only its actual context",
            context_profile="current_group" if group else "none",
        )
    )
    result = await facade.run(
        RunAgentSessionRequest(session_id=session.session_id, user_input="continue")
    )
    assert result.text == "only the real original scope" and len(provider.requests) == 1
    record = await repository.get(plugin_id=plugin_id, session_id=str(session.session_id))
    assert record.owner_user_id is None and record.canonical_owner_person_id is None
    assert record.scope_type == ("group" if group else "plugin")
    assert record.canonical_space_id == (env.space if group else None)
    # A fresh service has no invocation or in-memory session ownership to borrow.
    # Its durable history and scope come from the same original internal session.
    restored_provider = FakeLLMProvider(lambda _: "continued after service restart")
    restored = BoundAgentSessionFacade(
        service=PluginAgentSessionService(
            model_executor=InjectedModelExecutor(restored_provider),
            concurrency=ConcurrencyManager(),
            runtime_config=RuntimeConfigService(
                database=database, settings=make_settings(database.url)
            ),
            repository=PluginAgentSessionRepository(database),
        ),
        plugin_id=plugin_id,
        actor_user_id=None,
        current_group_id=env.space if group else None,
        approved_permissions=("agent.session", "group.current.read", "person.current.read"),
        conversation_id=env.context.conversation_id if group else None,
    )
    result = await restored.run(
        RunAgentSessionRequest(session_id=session.session_id, user_input="next original step")
    )
    assert result.session.session_id == session.session_id
    assert result.text == "continued after service restart"
    assert [
        message.content
        for message in restored_provider.requests[0].messages
        if message.role in {"user", "assistant"}
    ] == [
        "continue",
        "only the real original scope",
        "next original step",
    ]
    stored = await repository.get(plugin_id=plugin_id, session_id=str(session.session_id))
    assert stored.canonical_owner_person_id is None and stored.scope_type == record.scope_type
    assert stored.canonical_space_id == record.canonical_space_id
