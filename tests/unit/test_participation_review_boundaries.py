"""Source fences outside the bounded hydration page still govern continuation."""

import asyncio
import time
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update
from tests.unit.test_participation_feedback import set_work
from tests.unit.test_participation_ordinary_feedback import ordinary_backend, ordinary_expression
from tests.unit.test_semantic_participation_host import (
    _event_and_route,
    _host,
    _item,
    _message,
    _observation,
    _proposal,
    _score,
)
from yuki_participation.self_report import SelfDelta, SelfReport

from qq_ai_bot.conversation.scope import ConversationTurnSnapshot
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.domain.messages import ChatMessage, ChatResponse
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.runtime.work_schema_v1 import work
from qq_ai_bot.services.main_agent_backend import MainAgentBackend
from qq_ai_bot.services.participation_feedback import (
    admission_unit_binding,
    reconcile_page,
    sync_scope_effects,
)
from qq_ai_bot.social.db_models import SocialOperationModel


async def test_out_of_hydration_page_changed_basis_cannot_admit_continuation(database, tmp_path):
    host, item, original, _, _ = await ordinary_expression(database, tmp_path)
    try:
        await sync_scope_effects(host, item)
        await host._hydrate(item)
        await sync_scope_effects(host, item)
        assert any(
            u.expressed for u in item.controller.participating_units(datetime.now(UTC).timestamp())
        )
        # Preserve the known participating unit while its old basis exits the
        # actual 64-row hydration page. No fake observer/current check is used.
        for index in range(65):
            await host.app.ledger.append(
                bot_user_id="8000",
                platform_message_id=str(uuid4()),
                scope_type=ScopeType.GROUP,
                sender_user_id="1002",
                direction="inbound",
                content=f"旁支消息{index}",
                group_id=original.group_id,
                occurred_at=datetime.now(UTC),
            )
        async with database.immediate_session() as session:
            await session.execute(
                update(ChatEventModel)
                .where(ChatEventModel.id == original.id)
                .values(content="已修订的原问题")
            )
        following, _ = await host.app.ledger.append(
            bot_user_id="8000",
            platform_message_id=str(uuid4()),
            scope_type=ScopeType.GROUP,
            sender_user_id="1001",
            direction="inbound",
            content="接着原来的问题",
            group_id=original.group_id,
            occurred_at=datetime.now(UTC),
        )
        assert await host.continuation_for_event(following.id) is None
        assert await host.ordinary.get(following.id) is None
    finally:
        await host.close()


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("continuous", [False, True])
@pytest.mark.parametrize("invalid", [None, "quiet", "source"])
async def test_confirmed_expression_survives_raw_retirement_without_footer(
    database, tmp_path, monkeypatch, continuous, invalid, restart
):
    host, item, original, _, _ = await ordinary_expression(database, tmp_path)
    try:
        await sync_scope_effects(host, item)
        await host._hydrate(item)
        await sync_scope_effects(host, item)
        assert any(u.expressed for u in item.controller.participating_units(time.time()))
        admission = await host.ordinary.current(original.id)
        assert admission is not None
        if invalid == "quiet":
            assert item.controller.observe_unit_hint(
                admission_unit_binding(admission),
                SelfReport(
                    run_ref=admission.activation_id,
                    sequence=1,
                    response_id="actual-quiet",
                    at=original.occurred_at.timestamp(),
                    delta=SelfDelta(engage="quiet"),
                ),
            )
        # Match canonical datetime precision, rather than rounding a future
        # input a fraction of a microsecond beyond the virtual current time.
        clock = [datetime.now(UTC).timestamp()]
        for module in (
            "qq_ai_bot.services.semantic_participation",
            "qq_ai_bot.services.participation_ordinary",
        ):
            monkeypatch.setattr(module + ".time", SimpleNamespace(time=lambda: clock[0]))
        steps = [120] * 7 if continuous else [840]
        for index, seconds in enumerate(steps):
            clock[0] += seconds
            item.controller.advance(
                clock[0], controller_epoch=item.controller.state.epoch, host_available=False
            )
            # The negative cases check quiet/source fences after repeated
            # retirement or one long pause, without adding a new Main hint.
            if invalid is not None and index < len(steps) - 1:
                continue
            if restart and index == len(steps) - 1:
                await host._save(item)
                host._sessions.clear()
                item = await host._session(item.scene)
            if invalid == "source":
                async with database.immediate_session() as session:
                    await session.execute(
                        update(ChatEventModel)
                        .where(ChatEventModel.id == original.id)
                        .values(content="原建立来源已修改")
                    )
            following, _ = await host.app.ledger.append(
                bot_user_id="8000",
                platform_message_id=str(uuid4()),
                scope_type=ScopeType.GROUP,
                sender_user_id="1001",
                direction="inbound",
                content="接着这个讨论",
                group_id=original.group_id,
                occurred_at=datetime.fromtimestamp(clock[0], UTC),
            )
            binding = await host.continuation_for_event(following.id)
            if invalid is not None:
                assert binding is None
                assert await host.ordinary.get(following.id) is None
            else:
                assert binding is not None and binding.unit_key == f"event:{original.id}"
                prepared = host.ordinary.prepare(following, binding.generation, 1, binding)
                assert await host.ordinary.commit(prepared)
                await host.on_ordinary_admitted(binding, prepared.admission)
        assert f"event:{original.id}" not in item.controller.state.events
        assert not host._observer.calls
        if invalid is None:
            assert any(
                u.expressed and u.engage is None
                for u in item.controller.participating_units(clock[0])
            )
    finally:
        await host.close()


@pytest.mark.parametrize(
    ("interpreted_before_main", "quiet_hint"), [(False, True), (True, True), (False, False)]
)
async def test_actual_main_quiet_closing_observer_and_later_continuation(
    database, tmp_path, interpreted_before_main, quiet_hint
):
    host, item, original, _, _ = await ordinary_expression(database, tmp_path, admission=False)
    try:
        # Freeze an actual earlier SELF proposal, then retire it through the
        # existing known-unaccepted path before ordinary admission. Its delayed
        # copy must not acquire a run after the later stop is interpreted.
        original_source = item.controller.state.events[f"event:{original.id}"]
        _score(item, original_source, act="open_group", unit="new")
        autonomy_binding = await host._binding(item)
        old_proposal = _proposal(item, autonomy_binding, original_source)
        assert (
            await host.repository.query_proposal(
                conversation_id=item.scene.conversation_id,
                generation=item.scene.generation,
                owner=autonomy_binding.effective_owner,
                controller_epoch=old_proposal.controller_epoch,
                proposal_id=old_proposal.proposal_id,
            )
            is None
        )
        assert item.controller.discard_unaccepted_proposal(old_proposal.proposal_id)
        original_binding = await host.binding_for_event(original.id)
        assert original_binding is not None
        prepared_original = host.ordinary.prepare(
            original, item.scene.generation, 1, original_binding
        )
        assert await host.ordinary.commit(prepared_original)
        await host.on_ordinary_admitted(original_binding, prepared_original.admission)
        await sync_scope_effects(host, item)
        await host._hydrate(item)
        await sync_scope_effects(host, item)
        assert item.controller.state.pending is None
        stop, _ = await host.app.ledger.append(
            bot_user_id="8000",
            platform_message_id=str(uuid4()),
            scope_type=ScopeType.GROUP,
            sender_user_id="1001",
            direction="inbound",
            content="这件事先不聊了",
            group_id=original.group_id,
            occurred_at=datetime.now(UTC),
        )
        frozen = await host.continuation_for_event(stop.id)
        assert frozen is not None
        prepared = host.ordinary.prepare(stop, frozen.generation, 1, frozen)
        assert await host.ordinary.commit(prepared)
        await host.on_ordinary_admitted(frozen, prepared.admission)
        # Ordinary admission consumed the input but supplied no semantic boundary.
        item.observation.queue.pending.clear()
        stopped = item.controller.state.events[f"event:{stop.id}"]

        async def closing(snapshot):
            host._observer.calls.append(snapshot)
            option = next(o for o in snapshot.focus.unit_options if o.thread == frozen.unit_key)
            return _observation(snapshot, act="ask_yuki_stop", unit=option.key)

        host._observer.evaluate = closing
        if interpreted_before_main:
            item.observation.request_observation(stopped.ref)
            await item.observation.evaluate_due(time.time() + 3, active=True)
            assert len(host._observer.calls) == 1
            assert not item.observation.queue.pending
        chat, provider, tool_runtime, runtime, _ = await ordinary_backend(
            database,
            'NO_REPLY\n<yuki-state>{"engage":"quiet"}</yuki-state>' if quiet_hint else "NO_REPLY",
        )
        host.app.chat = chat
        chat.observe_main_response = host.observe_main_response
        scope = ConversationScope.group(stop.bot_user_id, stop.group_id)
        state = await chat._conversation_scopes.get(scope)
        token = await chat._turn_coordinator.notify_message(scope.key)
        tool_runtime = replace(
            tool_runtime,
            actor_context=None,
            inbound=_message(stop),
            turn_snapshot=ConversationTurnSnapshot(
                state.id, scope.key, state.generation, stop.id, token.version
            ),
        )
        backend = MainAgentBackend(chat, tool_runtime)
        result = await chat.runtime.runner.run(
            (ChatMessage("system", "固定说明"), ChatMessage("user", stop.content)),
            replace(runtime, conversation_key=scope.key),
            backend,
        )
        assert result.text == "" and len(provider.requests) == 1
        assert not result.tool_calls_used and not backend.messages_sent
        if not quiet_hint:
            assert not item.observation.queue.pending and not host._observer.calls
            assert not any(u.closed for u in item.controller.participating_units(time.time()))
            later, _ = await host.app.ledger.append(
                bot_user_id="8000",
                platform_message_id=str(uuid4()),
                scope_type=ScopeType.GROUP,
                sender_user_id="1001",
                direction="inbound",
                content="继续这个讨论",
                group_id=original.group_id,
                occurred_at=datetime.now(UTC),
            )
            # A plain silent completion supplies no stop interpretation. The
            # report must expose that missing evidence instead of claiming closed.
            assert await host.continuation_for_event(later.id) is not None
            return
        if interpreted_before_main:
            assert not item.observation.queue.pending
        else:
            assert tuple(item.observation.queue.pending) == (stopped.ref.event_id,)
            assert not host._observer.calls
            await item.observation.evaluate_due(time.time() + 3, active=True)
        assert len(host._observer.calls) == 1
        unit = next(
            u
            for u in item.controller.participating_units(time.time())
            if u.unit.thread == frozen.unit_key
        )
        assert unit.closed
        assert not item.observation.queue.pending
        # The same interpreted source cannot provide either fast Main admission
        # or a SELF opportunity after closing. No pending proposal is required.
        assert not item.controller.source_allowed(stopped)
        assert not item.controller.source_allowed(
            item.controller.state.events[f"event:{original.id}"]
        )
        later, _ = await host.app.ledger.append(
            bot_user_id="8000",
            platform_message_id=str(uuid4()),
            scope_type=ScopeType.GROUP,
            sender_user_id="1001",
            direction="inbound",
            content="嗯",
            group_id=original.group_id,
            occurred_at=datetime.now(UTC),
        )
        assert await host.continuation_for_event(later.id) is None
        assert await host.ordinary.get(later.id) is None
        await host._admit(item, autonomy_binding, old_proposal)
        assert not await host.repository.list_active()
        async with database.sessions() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(work)
                    .where(work.c.conversation_id == item.scene.conversation_id)
                )
                == 0
            )
        assert not item.controller.state.proposals
    finally:
        await host.close()


async def test_fifty_confirmed_continuations_do_not_accumulate_every_prior_input_basis(
    database, tmp_path
):
    host, item, original, _, _ = await ordinary_expression(database, tmp_path)
    try:
        await sync_scope_effects(host, item)
        await host._hydrate(item)
        await sync_scope_effects(host, item)
        sizes = []
        for index in range(50):
            following, _ = await host.app.ledger.append(
                bot_user_id="8000",
                platform_message_id=str(uuid4()),
                scope_type=ScopeType.GROUP,
                sender_user_id="1001",
                direction="inbound",
                content=f"接着这次交流{index}",
                group_id=original.group_id,
                occurred_at=datetime.now(UTC),
            )
            frozen = await host.continuation_for_event(following.id)
            assert frozen is not None
            sizes.append(len(frozen.basis))
            prepared = host.ordinary.prepare(following, frozen.generation, 1, frozen)
            assert await host.ordinary.commit(prepared)
            await host.on_ordinary_admitted(frozen, prepared.admission)
            assert frozen.basis[-1][0] == f"event:{following.id}"
            assert (f"event:{original.id}", 1) in frozen.basis
        # The first interpretation's sources remain checked; intervening admitted
        # inputs are not an ever-growing history dependency for later admission.
        assert sizes == [sizes[0]] * 50, sizes
        assert not host._observer.calls
    finally:
        await host.close()


async def test_third_old_self_reply_anchor_reaches_real_observation_queue(database, tmp_path):
    host, _ = await _host(database, tmp_path)
    anchors = []
    try:
        for index in range(3):
            human = await _event_and_route(database, host.app.ledger, content=f"新讨论{index}")
            item = await _item(host, human)
            binding = await host._binding(item)
            source = item.controller.state.events[f"event:{human.id}"]
            _score(item, source, act="open_group", unit="new")
            source = item.controller.state.events[source.ref.event_id]
            proposal = _proposal(item, binding, source)
            await host._admit(item, binding, proposal)
            runs = await host.repository.list_active()
            assert len(runs) == 1
            run = runs[0]
            await host._dispatch(run)
            task = await host.work.by_source(f"initiative:{run.run_id}")
            assert task is not None
            own, _ = await host.app.ledger.append(
                bot_user_id="8000",
                platform_message_id=str(uuid4()),
                scope_type=ScopeType.GROUP,
                sender_user_id="8000",
                direction="outbound",
                content=f"原SELF回复{index}",
                group_id=human.group_id,
                occurred_at=datetime.now(UTC),
                origin="social_tool",
            )
            async with database.immediate_session() as session:
                session.add(
                    SocialOperationModel(
                        id=str(uuid4()),
                        source_turn_id=f"{item.scene.conversation_id}:initiative:{run.run_id}",
                        tool_call_id=f"self-send-{index}",
                        source_conversation_id=item.scene.conversation_id,
                        action="send_message",
                        payload_hash="0" * 64,
                        target_kind="space",
                        target_id=item.scene.space_id,
                        presence_id=human.ingress_presence_id,
                        status="succeeded",
                        event_id=own.id,
                        created_at=datetime.now(UTC),
                        updated_at=datetime.now(UTC),
                    )
                )
            await set_work(database, task, state="completed")
            await reconcile_page(host, (run,))
            await sync_scope_effects(host, item)
            await host._hydrate(item)
            await sync_scope_effects(host, item)
            anchors.append(item.controller.state.events[f"event:{own.id}"].ref)
        # Six more recent real messages put the oldest SELF outside the normal
        # newest-six context. No platform reply quote hints its identity.
        for index in range(6):
            await host.app.ledger.append(
                bot_user_id="8000",
                platform_message_id=str(uuid4()),
                scope_type=ScopeType.GROUP,
                sender_user_id="1002",
                direction="inbound",
                content=f"旁支信息{index}",
                group_id=human.group_id,
                occurred_at=datetime.now(UTC),
            )
        following, _ = await host.app.ledger.append(
            bot_user_id="8000",
            platform_message_id=str(uuid4()),
            scope_type=ScopeType.GROUP,
            sender_user_id="1003",
            direction="inbound",
            content="想继续最早那个讨论",
            group_id=human.group_id,
            occurred_at=datetime.now(UTC),
        )
        assert following.reply_to_event_id is None
        assert await host.continuation_for_event(following.id) is None
        event = item.controller.state.events[f"event:{following.id}"]
        assert event.reply_to is None
        assert anchors[0] in {option.self_anchor for option in event.unit_options}
        await item.observation.evaluate_due(time.time() + 3, active=True)
        snapshot = host._observer.calls[-1]
        assert snapshot.focus.ref == event.ref
        assert anchors[0] in {e.ref for e in snapshot.context}
        assert set(anchors) <= {e.ref for e in snapshot.context}
        assert not await host.ordinary.get(following.id)
        assert not await host.repository.list_active()
    finally:
        await host.close()


@pytest.mark.parametrize("new_coordinator_version", [False, True])
async def test_late_original_main_quiet_cannot_override_new_admitted_stay(
    database, tmp_path, new_coordinator_version
):
    host, item, original, _, _ = await ordinary_expression(database, tmp_path, send=False)
    try:
        chat, provider, tool_runtime, runtime, _ = await ordinary_backend(
            database, 'NO_REPLY\n<yuki-state>{"engage":"stay"}</yuki-state>'
        )
        host.app.chat = chat
        chat.observe_main_response = host.observe_main_response
        scope = ConversationScope.group(original.bot_user_id, original.group_id)
        state = await chat._conversation_scopes.get(scope)
        token = await chat._turn_coordinator.notify_message(scope.key)
        original_runtime = replace(
            tool_runtime,
            actor_context=None,
            inbound=_message(original),
            turn_snapshot=ConversationTurnSnapshot(
                state.id, scope.key, state.generation, original.id, token.version
            ),
        )
        original_backend = MainAgentBackend(chat, original_runtime)
        main_runtime = replace(runtime, conversation_key=scope.key)
        result = await chat.runtime.runner.run(
            (ChatMessage("system", "固定说明"), ChatMessage("user", original.content)),
            main_runtime,
            original_backend,
        )
        assert result.text == "" and len(provider.requests) == 1
        following, _ = await host.app.ledger.append(
            bot_user_id="8000",
            platform_message_id=str(uuid4()),
            scope_type=ScopeType.GROUP,
            sender_user_id="1001",
            direction="inbound",
            content="接着讨论下一点",
            group_id=original.group_id,
            occurred_at=datetime.now(UTC),
        )
        frozen = await host.continuation_for_event(following.id)
        assert frozen is not None
        if new_coordinator_version:
            token = await chat._turn_coordinator.notify_message(scope.key)
        prepared = host.ordinary.prepare(following, state.generation, token.version, frozen)
        assert await host.ordinary.commit(prepared)
        await host.on_ordinary_admitted(frozen, prepared.admission)
        following_runtime = replace(
            original_runtime,
            actor_context=None,
            inbound=_message(following),
            turn_snapshot=ConversationTurnSnapshot(
                state.id, scope.key, state.generation, following.id, token.version
            ),
        )
        provider._responder = lambda _: ChatResponse(
            'NO_REPLY\n<yuki-state>{"engage":"stay"}</yuki-state>',
            0,
            provider_request_id="following-stay-response",
        )
        following_backend = MainAgentBackend(chat, following_runtime)
        following_result = await chat.runtime.runner.run(
            (ChatMessage("system", "固定说明"), ChatMessage("user", following.content)),
            main_runtime,
            following_backend,
        )
        assert following_result.text == "" and len(provider.requests) == 2
        before = item.controller.state.host_checkpoint["participation_v1"]
        assert await chat.validate_turn_snapshot(original_runtime.turn_snapshot) is (
            not new_coordinator_version
        )
        before_queue = item.observation.queue.checkpoint()
        # A physical response that started in the original activation arrives
        # late. The real Backend callback keeps its original runtime and sequence.
        await original_backend.observe_response(
            ChatResponse(
                'NO_REPLY\n<yuki-state>{"engage":"quiet"}</yuki-state>',
                0,
                provider_request_id="late-original-response",
            ),
            main_runtime,
        )
        assert item.controller.state.host_checkpoint["participation_v1"] == before
        unit = next(iter(item.controller.participating_units(time.time())))
        assert unit.engage == "stay"
        assert (
            next(iter(before["units"].values()))["hint"]["response_id"] == "following-stay-response"
        )
        assert item.observation.queue.checkpoint() == before_queue
        assert not host._observer.calls
        assert not item.controller.state.effects and not item.controller.state.proposals
        assert original_backend.messages_sent == following_backend.messages_sent == 0
    finally:
        await host.close()


@pytest.mark.parametrize("edit_during_flight", [False, True])
async def test_inflight_old_stop_keeps_new_main_intent_and_revalidates_source(
    database, tmp_path, monkeypatch, edit_during_flight
):
    host, item, original, _, _ = await ordinary_expression(database, tmp_path)
    started, release = asyncio.Event(), asyncio.Event()
    task = None
    # This case exercises observation/admission ordering, not SELF arrival draws.
    monkeypatch.setattr(item.controller, "_sample", lambda _sequence, _stream: 1.0)
    try:
        await sync_scope_effects(host, item)
        await host._hydrate(item)
        await sync_scope_effects(host, item)
        stop, _ = await host.app.ledger.append(
            bot_user_id="8000",
            platform_message_id=str(uuid4()),
            scope_type=ScopeType.GROUP,
            sender_user_id="1001",
            direction="inbound",
            content="这件事先不聊了",
            group_id=original.group_id,
            occurred_at=datetime.now(UTC),
        )
        frozen_a = await host.continuation_for_event(stop.id)
        assert frozen_a is not None
        prepared = host.ordinary.prepare(stop, frozen_a.generation, 1, frozen_a)
        assert await host.ordinary.commit(prepared)
        await host.on_ordinary_admitted(frozen_a, prepared.admission)
        item.observation.queue.pending.clear()
        focus = item.controller.state.events[f"event:{stop.id}"]
        assert item.observation.request_observation(focus.ref)

        async def delayed(snapshot):
            host._observer.calls.append(snapshot)
            started.set()
            await release.wait()
            option = next(o for o in snapshot.focus.unit_options if o.thread == frozen_a.unit_key)
            return _observation(snapshot, act="ask_yuki_stop", unit=option.key)

        host._observer.evaluate = delayed
        item.observation.queue.pending[focus.ref.event_id].first_dirty -= 3
        item.observation.queue.pending[focus.ref.event_id].last_dirty -= 3
        task = asyncio.create_task(host._advance_scene(item))
        await asyncio.wait_for(started.wait(), timeout=5)
        chat, provider, runtime, agent, _ = await ordinary_backend(
            database, 'NO_REPLY\n<yuki-state>{"engage":"stay"}</yuki-state>'
        )
        host.app.chat = chat
        chat.observe_main_response = host.observe_main_response
        scope = ConversationScope.group(stop.bot_user_id, stop.group_id)
        state = await chat._conversation_scopes.get(scope)
        token_a = await chat._turn_coordinator.notify_message(scope.key)
        runtime_a = replace(
            runtime,
            actor_context=None,
            inbound=_message(stop),
            turn_snapshot=ConversationTurnSnapshot(
                state.id, scope.key, state.generation, stop.id, token_a.version
            ),
        )
        later, _ = await host.app.ledger.append(
            bot_user_id="8000",
            platform_message_id=str(uuid4()),
            scope_type=ScopeType.GROUP,
            sender_user_id="1001",
            direction="inbound",
            content="不过我想继续另一个角度",
            group_id=original.group_id,
            occurred_at=datetime.now(UTC),
        )
        frozen_b = await host.continuation_for_event(later.id)
        assert frozen_b is not None
        token_b = await chat._turn_coordinator.notify_message(scope.key)
        prepared_b = host.ordinary.prepare(later, frozen_b.generation, token_b.version, frozen_b)
        assert await host.ordinary.commit(prepared_b)
        await host.on_ordinary_admitted(frozen_b, prepared_b.admission)
        runtime_b = replace(
            runtime,
            actor_context=None,
            inbound=_message(later),
            turn_snapshot=ConversationTurnSnapshot(
                state.id, scope.key, state.generation, later.id, token_b.version
            ),
        )
        provider._responder = lambda _: ChatResponse(
            'NO_REPLY\n<yuki-state>{"engage":"stay"}</yuki-state>',
            0,
            provider_request_id="new-main-stay",
        )
        backend = MainAgentBackend(chat, runtime_b)
        result = await chat.runtime.runner.run(
            (ChatMessage("system", "fixed"), ChatMessage("user", later.content)),
            replace(agent, conversation_key=scope.key),
            backend,
        )
        assert result.text == "" and len(provider.requests) == 1
        before_hint = next(
            iter(item.controller.state.host_checkpoint["participation_v1"]["units"].values())
        )["hint"]
        if edit_during_flight:
            # The source changes after B's normal Host hydration while the real
            # observer request is still blocked. No test refresh masks this race.
            async with database.immediate_session() as session:
                await session.execute(
                    update(ChatEventModel)
                    .where(ChatEventModel.id == stop.id)
                    .values(content="旧焦点已修订")
                )
        release.set()
        await asyncio.wait_for(task, timeout=5)
        task = None
        assert len(host._observer.calls) == 1
        await MainAgentBackend(chat, runtime_a).observe_response(
            ChatResponse(
                '<yuki-state>{"engage":"quiet"}</yuki-state>',
                0,
                provider_request_id="late-old-main",
            ),
            replace(agent, conversation_key=scope.key),
        )
        after_hint = next(
            iter(item.controller.state.host_checkpoint["participation_v1"]["units"].values())
        )["hint"]
        assert after_hint == before_hint
        boundaries = list(item.controller.state.boundaries.values())
        if edit_during_flight:
            assert not boundaries, "changed DB source must not publish late stop boundary"
            assert focus.ref.event_id not in item.controller.state.observations
            assert not await host._source_current(item, focus.ref)
        else:
            assert len(boundaries) == 1 and boundaries[0].source == focus.ref
            assert boundaries[0].thread == frozen_a.unit_key
            # Own willingness alone never releases a real user stop.
            assert any(
                u.closed and u.engage == "stay"
                for u in item.controller.participating_units(time.time())
            )
            b_ref = item.controller.state.events[f"event:{later.id}"].ref

            async def extend(snapshot):
                host._observer.calls.append(snapshot)
                option = next(
                    o for o in snapshot.focus.unit_options if o.thread == frozen_a.unit_key
                )
                return _observation(snapshot, act="extend_yuki", unit=option.key)

            host._observer.evaluate = extend
            assert item.observation.request_observation(b_ref)
            item.observation.queue.last_call -= 9
            item.observation.queue.pending[b_ref.event_id].first_dirty -= 3
            item.observation.queue.pending[b_ref.event_id].last_dirty -= 3
            await host._advance_scene(item)
            assert len(host._observer.calls) == 2
            boundary = next(iter(item.controller.state.boundaries.values()))
            # Existing explicit-stop policy requires a strong invite_yuki;
            # an extend_yuki interpretation and own stay do not release it.
            assert boundary.released_by is None

            async def invite(snapshot):
                host._observer.calls.append(snapshot)
                option = next(
                    o for o in snapshot.focus.unit_options if o.thread == frozen_a.unit_key
                )
                return _observation(snapshot, act="invite_yuki", unit=option.key)

            host._observer.evaluate = invite
            assert item.observation.request_observation(b_ref)
            item.observation.queue.last_call -= 9
            item.observation.queue.pending[b_ref.event_id].first_dirty -= 3
            item.observation.queue.pending[b_ref.event_id].last_dirty -= 3
            await host._advance_scene(item)
            assert len(host._observer.calls) == 3
            boundary = next(iter(item.controller.state.boundaries.values()))
            assert boundary.released_by == b_ref
            assert any(
                not u.closed and u.engage == "stay"
                for u in item.controller.participating_units(time.time())
            )
            next_message, _ = await host.app.ledger.append(
                bot_user_id="8000",
                platform_message_id=str(uuid4()),
                scope_type=ScopeType.GROUP,
                sender_user_id="1001",
                direction="inbound",
                content="那继续这个问题",
                group_id=original.group_id,
                occurred_at=datetime.now(UTC),
            )
            resumed = await host.continuation_for_event(next_message.id)
            assert resumed is not None and resumed.unit_key == frozen_a.unit_key
    finally:
        release.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await host.close()
