"""Actorless plugin wakeups keep real sends, and cannot archive anonymously."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from tests.conftest import build_harness, make_settings
from tests.support.background_authority import approve_background_plugin
from tests.support.social_identity_cases import social_env

from qq_ai_bot.capabilities.results import ToolExecutionResult, ToolResultBudgeter
from qq_ai_bot.conversation.scope import ConversationTurnSnapshot
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.domain.messages import ChatResponse, ToolCall, ToolFunction
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.persistence.models import ChatEventModel, ToolArtifactModel
from qq_ai_bot.runtime.effect_outcomes import ResultCapture, current_result_capture
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.runtime.trigger import ExternalEventTurnTrigger
from qq_ai_bot.services.agent_tools import ToolRuntime
from qq_ai_bot.social.db_models import SocialOperationModel
from qq_ai_bot.tool_results.access import ArtifactAccess
from qq_ai_bot.tool_results.artifacts import ToolArtifactRepository


@pytest.mark.parametrize(
    ("budget", "artifacts_enabled"), [(24000, True), (128, False), (128, True)]
)
async def test_plugin_wakeup_send_receipt_only_requires_actor_for_actual_archive(
    database, tmp_path, monkeypatch, budget, artifacts_enabled
):
    env = await social_env(database, tmp_path)
    # Background sends now recheck the current installation and canonical
    # target grant at the actual Social claim, including this direct fixture.
    await approve_background_plugin(
        database,
        plugin_id="test.result-access",
        bot_user_id="80001",
        group_id="20001",
        creator_user_id="10001",
    )
    scope = ConversationScope.group(env.bot.self_id, "20001")
    external = await env.service.writer.append_external(
        scope=scope,
        platform_message_id="plugin-result-source",
        source_plugin_id="test.result-access",
        external_source="fixture",
        external_event_key="one-original-event",
        external_event_type="fixture.created",
        external_payload={"id": "one-original-event"},
        external_target_id="20001",
        content="A plugin asks the main agent to acknowledge this update.",
        occurred_at=datetime.now(UTC),
    )
    responses = iter(
        (
            ChatResponse(
                "",
                0,
                tool_calls=(
                    ToolCall(
                        "original-send", ToolFunction("send_message", '{"text":"已收到更新。"}')
                    ),
                ),
            ),
            ChatResponse("", 0),
        )
    )
    provider = FakeLLMProvider(lambda _: next(responses))
    harness = build_harness(
        database,
        make_settings(
            database.url,
            runtime_work_enabled=False,
            agent_tool_result_max_characters=24000,
            tooling_result_token_budget=budget // 4,
            tooling_result_artifact_enabled=artifacts_enabled,
        ),
        provider,
    )
    chat = harness.processor._chat
    chat._tools.social_service = env.service
    store = ToolArtifactRepository(database, tmp_path / "tool-artifacts", retention_seconds=60)
    chat._tool_artifacts = store
    event = await harness.ledger.get_event(external.event.id)
    state = await harness.conversation_scopes.get(scope)
    config = await chat._runtime_config.snapshot(group_id="20001")
    chat.configure_runtime_controls(config)
    actor_checks = []
    original = ToolRuntime.require_actor

    def require_actor(runtime):
        if runtime.origin is TurnOrigin.PLUGIN_BACKGROUND:
            actor_checks.append(runtime)
            assert runtime.inbound is None and runtime.actor_context is None
            assert not runtime.actor_user_id and not runtime.actor_is_superuser
        return original(runtime)

    monkeypatch.setattr(ToolRuntime, "require_actor", require_actor)
    async with chat._turn_coordinator.background_turn(scope.key) as token:
        snapshot = ConversationTurnSnapshot(
            state.id,
            scope.key,
            state.generation,
            event.id,
            token.version,
            transport_scope_key=scope.key,
        )
        invocation = chat.generate_main_agent_wakeup(
            event=event,
            trigger=ExternalEventTurnTrigger(
                plugin_id="test.result-access",
                source_event_id=event.id,
                target_type="group",
                target_id="20001",
                agent_intent="Acknowledge once with the send tool.",
            ),
            identity=scope,
            runtime=config,
            turn_token=token,
            turn_snapshot=snapshot,
            gateway=env.bot,
            space_id=env.space,
            presence_id=env.presence,
            conversation_id=event.canonical_conversation_id,
        )
        if artifacts_enabled and budget == 128:
            with pytest.raises(PermissionError, match="tool_actor_unavailable"):
                await invocation
            assert len(provider.requests) == 1 and len(actor_checks) == 1
        else:
            result = await invocation
            assert result.model_requests == 2 and result.tool_calls_used == 1
            assert not actor_checks
            replies = [
                message for message in provider.requests[1].messages if message.role == "tool"
            ]
            assert len(replies) == 1 and replies[0].tool_call_id == "original-send"
            receipt = json.loads(replies[0].content)
            assert receipt["ok"] is True
            assert "artifact_handle" not in receipt
            if artifacts_enabled:
                assert receipt["data"]["status"] == "succeeded"
            else:
                assert receipt["truncated"] is True
    # Rendering cannot invalidate or repeat the already confirmed transport effect.
    assert [action for action, _ in env.bot.calls].count("send_group_msg") == 1
    async with database.sessions() as session:
        operations = (await session.scalars(select(SocialOperationModel))).all()
        assert len(operations) == 1
        operation = operations[0]
        assert operation.status == "succeeded"
        assert (
            operation.source_turn_id == f"{event.canonical_conversation_id}:plugin-event:{event.id}"
        )
        assert operation.event_id is not None and operation.platform_reference
        delivered = await session.get(ChatEventModel, operation.event_id)
        assert delivered.content == "已收到更新。" and delivered.caused_by_event_id == event.id
        assert not (await session.scalars(select(ToolArtifactModel))).all()
    assert not (tmp_path / "tool-artifacts").exists()


@pytest.mark.parametrize("name", ["web_search", "read_webpage"])
async def test_small_research_resolves_original_scope_and_archives_complete_result(name):
    access = ArtifactAccess("original-conversation", 3, "original-person")
    checks = []

    def resolve():
        checks.append(access)
        return access

    writer = SimpleNamespace(write_artifact=AsyncMock(return_value="immutable-original"))
    outcome = ToolExecutionResult(ok=True, data={"text": "complete research"}, tool_name=name)
    rendered = await ToolResultBudgeter(
        max_characters=24000, artifacts=writer, artifact_access_resolver=resolve
    ).render(outcome)
    assert checks == [access] and rendered.artifact_id == "immutable-original"
    writer.write_artifact.assert_awaited_once()
    written = writer.write_artifact.await_args.kwargs
    assert written["access"] is access
    assert json.loads(written["content"])["data"] == outcome.data


async def test_oversized_artifact_read_does_not_resolve_or_recursively_archive():
    def resolve():
        raise AssertionError("paged artifact reads must not acquire a new archive identity")

    writer = SimpleNamespace(write_artifact=AsyncMock())
    rendered = await ToolResultBudgeter(
        max_characters=512, artifacts=writer, artifact_access_resolver=resolve
    ).render(
        ToolExecutionResult(
            ok=True,
            data="original-page " * 1000,
            provider_id="artifacts",
            tool_name="read_tool_artifact",
        )
    )
    assert rendered.truncated and rendered.artifact_id is None
    writer.write_artifact.assert_not_awaited()


async def test_archive_identity_rejection_keeps_original_typed_outcome_without_anonymous_write():
    def resolve():
        raise PermissionError("tool_actor_unavailable")

    writer = SimpleNamespace(write_artifact=AsyncMock())
    outcome = ToolExecutionResult(
        ok=True, data="complete original receipt " * 1000, mutation_committed=True
    )
    capture = ResultCapture("original-work", "original-effect")
    token = current_result_capture.set(capture)
    try:
        with pytest.raises(PermissionError, match="tool_actor_unavailable"):
            await ToolResultBudgeter(
                max_characters=512, artifacts=writer, artifact_access_resolver=resolve
            ).render(outcome)
        assert capture.outcome is outcome and capture.outcome.mutation_committed is True
        assert capture.artifact_handle is None
        writer.write_artifact.assert_not_awaited()
    finally:
        current_result_capture.reset(token)
