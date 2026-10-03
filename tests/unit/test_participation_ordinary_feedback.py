"""Ordinary Main sparse feedback and silence use the original tool loop."""

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, update
from tests.conftest import build_harness, make_settings
from tests.unit.test_semantic_participation_host import _event_and_route, _host, _item, _message
from yuki_participation.controller import Controller
from yuki_participation.participation import ParticipationCheckpoint

from qq_ai_bot.conversation.autonomy_db_models import InitiativeFeedbackModel, InitiativeRunModel
from qq_ai_bot.conversation.initiative_sources import source_revision
from qq_ai_bot.conversation.ordinary_admission import (
    OrdinaryAdmissionRepository,
    OrdinaryParticipationBinding,
)
from qq_ai_bot.conversation.scope import ConversationTurnSnapshot
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.domain.messages import ChatMessage, ChatResponse, InboundMessage, SenderIdentity
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.services.agent_runner import AgentRuntime
from qq_ai_bot.services.agent_tools import ToolRuntime
from qq_ai_bot.services.main_agent_backend import MainAgentBackend
from qq_ai_bot.services.participation_feedback import (
    _logical_social_effects,
    admission_unit_binding,
    sync_scope_effects,
)
from qq_ai_bot.social.db_models import SocialOperationModel


async def ordinary_backend(database, content, *, callback=None):
    provider = FakeLLMProvider(lambda _: ChatResponse(content, 0, provider_request_id="response-1"))
    chat = build_harness(database, make_settings(database.url), provider).processor._chat
    chat.observe_main_response = callback
    config = await chat._runtime_config.snapshot()
    inbound = InboundMessage(
        message_id="transport-only",
        event_type="message",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity("10001"),
        text="继续这段交流",
        group_id="20001",
        source_event_id=1,
    )
    tool_runtime = ToolRuntime(
        inbound, None, False, runtime_config=config, current_group_id="20001"
    )
    runtime = AgentRuntime(
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id="10001",
        actor_is_superuser=False,
        delegated_authority=None,
        conversation_key="ordinary-sparse-feedback",
        current_group_id="20001",
        bot_user_id="80001",
        gateway=None,
        runtime_config=config,
        current_time=chat._time.current_default(),
        allowed_capabilities=frozenset(),
        max_tool_calls=8,
        max_model_requests=8,
        dynamic_context_prepared=True,
    )
    return chat, provider, tool_runtime, runtime, MainAgentBackend(chat, tool_runtime)


@pytest.mark.parametrize(
    ("content", "engage"),
    [
        ("", None),
        ("NO_REPLY", None),
        ('<yuki-state>{"engage":"quiet"}</yuki-state>', "quiet"),
        ('NO_REPLY\n<yuki-state>{"engage":"stay"}</yuki-state>', "stay"),
        ("<yuki-state>{broken}</yuki-state>", None),
    ],
)
async def test_ordinary_silence_is_one_main_request_without_send_or_work(database, content, engage):
    callback = AsyncMock()
    chat, provider, tool_runtime, runtime, backend = await ordinary_backend(
        database, content, callback=callback
    )
    result = await chat.runtime.runner.run(
        (ChatMessage("system", "固定说明"), ChatMessage("user", "继续这段交流")),
        runtime,
        backend,
    )
    assert result.text == ""
    assert result.model_requests == len(provider.requests) == 1
    assert result.tool_calls_used == backend.messages_sent == 0
    assert runtime.work_control is None
    if engage is None:
        callback.assert_not_awaited()
    else:
        callback.assert_awaited_once()
        seen_runtime, sequence, response, delta = callback.await_args.args
        assert seen_runtime is tool_runtime and sequence == 1
        assert response.provider_request_id == "response-1" and delta.engage == engage


async def test_failed_derived_hint_save_does_not_retry_main(database):
    callback = AsyncMock(side_effect=RuntimeError("snapshot rejected"))
    chat, provider, _, runtime, backend = await ordinary_backend(
        database, '<yuki-state>{"engage":"quiet"}</yuki-state>', callback=callback
    )
    result = await chat.runtime.runner.run(
        (ChatMessage("system", "固定说明"), ChatMessage("user", "告别")), runtime, backend
    )
    assert result.text == "" and len(provider.requests) == 1
    callback.assert_awaited_once()


async def test_nonordinary_main_does_not_apply_ordinary_hint(database):
    callback = AsyncMock()
    _, _, _, runtime, backend = await ordinary_backend(database, "", callback=callback)
    response = ChatResponse('<yuki-state>{"engage":"quiet"}</yuki-state>', 0)
    await backend.observe_response(response, replace(runtime, origin=TurnOrigin.PLUGIN_BACKGROUND))
    callback.assert_not_awaited()


async def ordinary_expression(database, tmp_path, *, admission=True, status="succeeded", send=True):
    host, _ = await _host(database, tmp_path)
    event = await _event_and_route(database, host.app.ledger)
    item = await _item(host, event)
    source = item.controller.state.events[f"event:{event.id}"]
    repository = OrdinaryAdmissionRepository(database)
    prepared = repository.prepare(
        event,
        item.scene.generation,
        1,
        OrdinaryParticipationBinding(
            item.scene.conversation_id,
            item.scene.generation,
            event.id,
            str(source_revision(event)),
            source.thread,
            source.target,
            ((source.ref.event_id, source.ref.revision),),
        ),
    )
    if admission:
        assert await repository.commit(prepared)
        binding = admission_unit_binding(prepared.admission)
        assert item.controller.observe_unit_input(binding, source.ref)
    if not send:
        return host, item, event, None, None
    own, _ = await host.app.ledger.append(
        bot_user_id="8000",
        platform_message_id=str(uuid4()),
        scope_type=ScopeType.GROUP,
        sender_user_id="8000",
        direction="outbound",
        content="接着原讨论回答。",
        group_id=event.group_id,
        occurred_at=datetime.now(UTC),
        origin="social_tool",
    )
    receipt = SocialOperationModel(
        id=str(uuid4()),
        source_turn_id=f"{item.scene.conversation_id}:event:{event.id}",
        tool_call_id="original-send",
        source_conversation_id=item.scene.conversation_id,
        action="send_message",
        payload_hash="0" * 64,
        target_kind="space",
        target_id=item.scene.space_id,
        presence_id=event.ingress_presence_id,
        status=status,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        event_id=own.id,
    )
    async with database.immediate_session() as session:
        session.add(receipt)
    return host, item, event, own, receipt


@pytest.mark.parametrize("previous_host_feedback", [False, True])
async def test_confirmed_ordinary_group_send_binds_original_unit_and_replays_once(
    database, tmp_path, previous_host_feedback
):
    host, item, event, own, receipt = await ordinary_expression(database, tmp_path)
    try:
        if previous_host_feedback:
            # A pre-upgrade snapshot already knows the real send. New association
            # must retain that effect/run identity rather than invent another.
            for run_ref, effect in _logical_social_effects([receipt]).values():
                item.controller.observe_committed_effect(run_ref, effect)
        await sync_scope_effects(host, item)  # Receipt precedes public hydration.
        first = ParticipationCheckpoint.model_validate(
            item.controller.state.host_checkpoint["participation_v1"]
        )
        assert len(first.expressions) == len(item.controller.state.effects) == 1
        assert next(iter(first.expressions.values())).anchor is None
        await host._hydrate(item)
        await sync_scope_effects(host, item)
        state = item.controller.state
        checkpoint = ParticipationCheckpoint.model_validate(
            state.host_checkpoint["participation_v1"]
        )
        unit = next(iter(checkpoint.units.values()))
        assert unit.binding.actor == event.author_person_id
        assert unit.binding.unit.thread == f"event:{event.id}"
        assert unit.binding.unit.target == event.author_person_id
        assert unit.anchors == (state.events[f"event:{own.id}"].ref,)
        assert state.events[f"event:{own.id}"].thread == unit.binding.unit.thread
        assert next(iter(state.effects.values())).effect.actual_targets == ("group",)
        assert not state.proposals and not state.feedback
        snapshot = item.controller.state.model_copy(deep=True)
        item.controller = Controller.restore(snapshot, state.now)
        await sync_scope_effects(host, item)
        assert item.controller.state.effects == state.effects
        assert item.controller.state.host_checkpoint["participation_v1"] == checkpoint.model_dump(
            mode="json"
        )
        async with database.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(InitiativeRunModel)) == 0
            assert (
                await session.scalar(select(func.count()).select_from(InitiativeFeedbackModel)) == 0
            )
    finally:
        await host._store.close()


@pytest.mark.parametrize("engage", ["quiet", "stay"])
async def test_actual_host_observes_no_send_hint_and_only_quiet_rechecks_original_focus(
    database, tmp_path, engage
):
    host, item, event, _, _ = await ordinary_expression(database, tmp_path, send=False)
    try:
        content = f'NO_REPLY\n<yuki-state>{{"engage":"{engage}"}}</yuki-state>'
        chat, provider, tool_runtime, runtime, _ = await ordinary_backend(database, content)
        host.app.chat = chat
        chat.observe_main_response = host.observe_main_response
        scope = ConversationScope.group(event.bot_user_id, event.group_id)
        state = await chat._conversation_scopes.get(scope)
        token = await chat._turn_coordinator.notify_message(scope.key)
        tool_runtime = replace(
            tool_runtime,
            inbound=_message(event),
            turn_snapshot=ConversationTurnSnapshot(
                state.id, scope.key, state.generation, event.id, token.version
            ),
        )
        backend = MainAgentBackend(chat, tool_runtime)
        assert item.observation is not None
        item.observation.queue.pending.clear()
        result = await chat.runtime.runner.run(
            (ChatMessage("system", "固定说明"), ChatMessage("user", event.content)),
            replace(runtime, conversation_key=scope.key),
            backend,
        )
        assert result.text == "" and len(provider.requests) == 1
        assert backend.messages_sent == result.tool_calls_used == 0
        checkpoint = ParticipationCheckpoint.model_validate(
            item.controller.state.host_checkpoint["participation_v1"]
        )
        unit = next(iter(checkpoint.units.values()))
        assert unit.hint.delta.engage == engage
        assert unit.hint_basis == (item.controller.state.events[f"event:{event.id}"].ref,)
        assert not checkpoint.expressions and not item.controller.state.effects
        assert not item.controller.state.proposals and not item.controller.state.feedback
        assert tuple(item.observation.queue.pending) == (
            (f"event:{event.id}",) if engage == "quiet" else ()
        )
        if engage == "quiet":
            # The real _save persists the existing observer queue separately
            # from consumed admission. Reloading cannot discard this correction.
            host._sessions.clear()
            item = await host._session(await host._scene(event.canonical_conversation_id))
            assert tuple(item.observation.queue.pending) == (f"event:{event.id}",)
            assert item.controller.state.consumed[f"event:{event.id}"] == unit.input_ref.revision
        item.observation.queue.pending.clear()
        # A repeated physical response is not a fresh hint or correction demand.
        from yuki_participation.self_report import SelfDelta

        await host.observe_main_response(
            tool_runtime,
            1,
            ChatResponse(content, 0, provider_request_id="response-1"),
            SelfDelta(engage=engage),
        )
        assert not item.observation.queue.pending
        assert not host._observer.calls
    finally:
        await host._store.close()


@pytest.mark.parametrize("invalid", ["missing", "failed", "uncertain", "source", "output"])
async def test_invalid_or_unbound_ordinary_send_cannot_invent_unit_mapping(
    database, tmp_path, invalid
):
    host, item, event, own, _ = await ordinary_expression(
        database,
        tmp_path,
        admission=invalid != "missing",
        status=invalid if invalid in {"failed", "uncertain"} else "succeeded",
    )
    try:
        if invalid in {"source", "output"}:
            async with database.immediate_session() as session:
                await session.execute(
                    update(ChatEventModel)
                    .where(ChatEventModel.id == event.id)
                    .values(content="原来源已修改")
                    if invalid == "source"
                    else delete(ChatEventModel).where(ChatEventModel.id == own.id)
                )
        await host._hydrate(item)
        await sync_scope_effects(host, item)
        checkpoint = ParticipationCheckpoint.model_validate(
            item.controller.state.host_checkpoint.get("participation_v1", {})
        )
        assert not checkpoint.expressions
        assert not item.controller.state.host_checkpoint.get("outbound_threads")
        assert not item.controller.state.proposals and not item.controller.state.feedback
    finally:
        await host._store.close()
