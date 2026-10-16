"""Actual Main and Host observer scheduling overlap without external model or QQ calls."""

import asyncio
import time
from dataclasses import replace

import pytest
from sqlalchemy import update
from tests.conftest import MemorySender
from tests.support.semantic_participation_host_helpers import (
    _choice,
    _event_and_route,
    _host,
    _item,
    _message,
    _observation,
)
from tests.unit.test_auto_reply_merge import _wait_for_worker
from tests.unit.test_ordinary_input_handoff import (
    A,
    C,
    _PausedProvider,
    _plain,
    _scene,
    _tool,
    _work_count,
)
from tests.unit.test_participation_ordinary_feedback import ordinary_backend
from tests.unit.test_participation_pipeline_retirement import _relationship
from yuki_participation.models import Snapshot
from yuki_participation.participation import ParticipationCheckpoint
from yuki_participation.self_report import SelfDelta, SelfReport

from qq_ai_bot.conversation.ordinary_admission import OrdinaryAdmissionRepository
from qq_ai_bot.conversation.scope import ConversationTurnSnapshot
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.domain.messages import ChatMessage
from qq_ai_bot.identity.db_models import CanonicalSpaceModel
from qq_ai_bot.services.main_agent_backend import MainAgentBackend
from qq_ai_bot.services.participation_feedback import admission_unit_binding
from qq_ai_bot.services.turn_coordinator import HistorySourceChangedError
from qq_ai_bot.services.turn_execution import _PrimaryDispatch


@pytest.mark.parametrize("exit_first", ["semantic", "quiet"])
async def test_inflight_main_and_jev_each_close_fast_path_without_replaying_main(
    database, tmp_path, monkeypatch, exit_first
):
    host, item, _a, _original, unit = await _relationship(database, tmp_path)
    main_started, release_main = asyncio.Event(), asyncio.Event()
    jev_started, release_jev = asyncio.Event(), asyncio.Event()
    tasks = []
    snapshots = []
    try:
        # This fixture exercises ordinary response/review, without an intrinsic draw.
        config = await host.app.runtime_config.snapshot()
        config.conversation_policy().proactive_wakeup_enabled = False
        item.observation.queue.pending.clear()
        b = await _event_and_route(database, host.app.ledger, content="synthetic continuation B")
        binding, reason = await host.continuation_for_event(b.id)
        assert reason == "continuation"
        prepared = host.ordinary.prepare(b, item.scene.generation, 2, binding, reason=reason)
        assert await host.ordinary.commit(prepared)
        await host.on_ordinary_admitted(binding, prepared.admission)

        content = (
            'NO_REPLY\n<yuki-state>{"engage":"quiet"}</yuki-state>'
            if exit_first == "quiet"
            else 'NO_REPLY\n<yuki-state>{"engage":"stay"}</yuki-state>'
        )
        chat, provider, tool_runtime, runtime, _ = await ordinary_backend(database, content)
        host.app.chat = chat
        chat.observe_main_response = host.observe_main_response
        scope = ConversationScope.group(b.bot_user_id, b.group_id)
        state = await chat._conversation_scopes.get(scope)
        token = await chat._turn_coordinator.notify_message(scope.key)
        tool_runtime = replace(
            tool_runtime,
            inbound=_message(b),
            actor_context=None,
            turn_snapshot=ConversationTurnSnapshot(
                state.id, scope.key, state.generation, b.id, token.version
            ),
        )
        backend = MainAgentBackend(chat, tool_runtime)
        complete = provider.complete

        async def delayed_main(request):
            main_started.set()
            await release_main.wait()
            return await complete(request)

        class DelayedObserver:
            async def evaluate(self, snapshot, **_kwargs):
                snapshots.append(snapshot)
                jev_started.set()
                await release_jev.wait()
                selected = next(
                    (
                        option.key
                        for option in snapshot.focus.unit_options
                        if option.thread == unit.unit.thread and option.target == unit.unit.target
                    ),
                    None,
                )
                result = _observation(
                    snapshot,
                    act="other_exchange" if exit_first == "semantic" else "extend_yuki",
                    unit=selected,
                )
                if exit_first == "semantic":
                    result = result.model_copy(
                        update={
                            "answers": {
                                **result.answers,
                                "floor_state": _choice("floor_state", "other"),
                            }
                        }
                    )
                return result

        monkeypatch.setattr(provider, "complete", delayed_main)
        item.observation.observer = DelayedObserver()
        main = asyncio.create_task(
            chat.runtime.runner.run(
                (ChatMessage("system", "固定说明"), ChatMessage("user", b.content)),
                replace(runtime, conversation_key=scope.key, actor_user_id=b.sender_user_id),
                backend,
            )
        )
        tasks.append(main)
        async with asyncio.timeout(15):
            await main_started.wait()
            review = asyncio.create_task(host._advance_scene(item))
            tasks.append(review)
            await jev_started.wait()
            assert not main.done() and not review.done()
            frozen = snapshots[0].model_dump_json()
            assert snapshots[0].focus.ref.event_id == f"event:{b.id}"
            assert snapshots[0].projection_revision == "semantic-state-v2"
            assert any(hint.engage == "stay" for hint in snapshots[0].participation.units)

            if exit_first == "semantic":
                release_jev.set()
                await review
                assert not main.done()
            else:
                release_main.set()
                assert (await main).text == ""
                assert not review.done()

            c = await _event_and_route(
                database, host.app.ledger, content="synthetic continuation C"
            )
            assert await host.continuation_for_event(c.id) is None
            assert snapshots[0].model_dump_json() == frozen

            release_main.set()
            release_jev.set()
            await asyncio.gather(main, review)
            assert await host.continuation_for_event(c.id) is None
            current = next(
                iter(
                    ParticipationCheckpoint.model_validate(
                        item.controller.state.host_checkpoint["participation_v1"]
                    ).units.values()
                )
            )
            assert current.hint.delta.engage == ("quiet" if exit_first == "quiet" else "stay")
            assert not item.controller.participation_view(
                item.controller.state.events[f"event:{c.id}"], time.time()
            ).matched_unit
            assert len(provider.requests) == 1
            assert len(snapshots) == 1
            assert backend.messages_sent == 0
            assert (await host.ordinary.get(b.id)).activation_id == prepared.admission.activation_id
            assert not item.controller.state.proposals
            assert not await host.repository.list_active()
    finally:
        release_main.set()
        release_jev.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await host.close()


@pytest.mark.parametrize("late_exit", ["quiet", "semantic"])
async def test_selected_same_main_input_survives_late_participation_exit(
    database, tmp_path, monkeypatch, late_exit
):
    provider = _PausedProvider([_tool("get_recent_chat_history", {}, "original-read"), "NO_REPLY"])
    env, harness, worker, message = await _scene(
        database, tmp_path, provider, reason="continuation"
    )
    host, policy = await _host(database, tmp_path)
    policy.proactive_wakeup_enabled = False
    harness.processor.set_participation(host)
    host.app.chat = harness.processor._chat
    sender = MemorySender()
    classified = []
    checked, late_errors = asyncio.Event(), []
    selected_id = None
    flipped = False
    try:
        async with database.immediate_session() as writer:
            await writer.execute(
                update(CanonicalSpaceModel)
                .where(CanonicalSpaceModel.id == env.space)
                .values(autonomous_enabled=True)
            )
        prior = await env.service.writer.append(
            scope=message(0, "original exchange").scope(),
            platform_message_id="prior-participation-source",
            sender_user_id="10001",
            direction="inbound",
            content="synthetic original exchange",
        )
        item = await _item(host, prior.event)
        prior_binding = await host.binding_for_event(prior.event.id)
        original = host.ordinary.prepare(
            prior.event, item.scene.generation, 1, prior_binding, reason="mention"
        )
        assert await host.ordinary.commit(original)
        await host.on_ordinary_admitted(prior_binding, original.admission)
        unit = admission_unit_binding(original.admission)
        assert unit is not None
        prior_source = item.controller.state.events[f"event:{prior.event.id}"]
        assert item.controller.observe_unit_hint(
            unit,
            SelfReport(
                run_ref=original.admission.activation_id,
                sequence=1,
                response_id="original-stay",
                at=prior_source.at,
                source=prior_source.ref,
                delta=SelfDelta(engage="stay"),
            ),
        )
        await host._save(item)
        classify = host.continuation_for_event

        async def classify_real(event_id, *, check_budget=True):
            result = await classify(event_id, check_budget=check_budget)
            classified.append((event_id, check_budget, result))
            return result

        monkeypatch.setattr(host, "continuation_for_event", classify_real)
        assert (await harness.processor.handle(message(1, A), sender)).reason == "ordinary_deferred"
        await provider.wait(1)
        key = next(iter(worker._states))
        a_id = worker._states[key].message.source_event_id
        admitted_a = await host.ordinary.get(a_id)
        assert admitted_a is not None and admitted_a.route == "ordinary"
        source_a = item.controller.state.events[f"event:{a_id}"]
        snapshot = item.controller.freeze_observation(
            Snapshot(
                scope=item.scene.scope,
                focus=source_a,
                context=(),
                sequence=1,
                issued_at=time.time(),
            )
        )
        assert (await harness.processor.handle(message(2, C), sender)).reason == "ordinary_deferred"
        selected_id = worker._states[key].message.source_event_id
        assert selected_id != a_id and await host.ordinary.get(selected_id) is None
        publish = OrdinaryAdmissionRepository.commit_to_activation

        async def late_update(repository, prepared, activation, *, session, admission_allowed=None):
            nonlocal flipped
            if prepared.event.id == selected_id and not flipped:
                # This is the actual same-Main publication, after C's real reader
                # classification and after A paid its first request/tool response.
                assert activation.activation_id == admitted_a.activation_id
                assert len(provider.requests) == 1 and len(provider.entered) == 1
                assert prepared.admission.reason == "continuation"
                assert (
                    sum(
                        event_id == selected_id and not budget and found is not None
                        for event_id, budget, found in classified
                    )
                    >= 2
                )
                binding = prepared.admission.binding
                assert binding is not None and await host.continuation_current(
                    binding, check_budget=False
                )
                if late_exit == "quiet":
                    current_unit = admission_unit_binding(admitted_a)
                    assert item.controller.observe_unit_hint(
                        current_unit,
                        SelfReport(
                            run_ref=admitted_a.activation_id,
                            sequence=2,
                            response_id="late-original-quiet",
                            at=source_a.at,
                            source=source_a.ref,
                            delta=SelfDelta(engage="quiet"),
                        ),
                    )
                else:
                    option = next(
                        (
                            value.key
                            for value in snapshot.focus.unit_options
                            if value.thread == unit.unit.thread and value.target == unit.unit.target
                        ),
                        None,
                    )
                    observation = _observation(snapshot, act="other_exchange", unit=option)
                    observation = observation.model_copy(
                        update={
                            "answers": {
                                **observation.answers,
                                "floor_state": _choice("floor_state", "other"),
                            }
                        }
                    )
                    assert item.controller.apply_semantic_observation(observation)
                assert not await host.continuation_current(binding, check_budget=False)
                flipped = True
            return await publish(
                repository,
                prepared,
                activation,
                session=session,
                admission_allowed=admission_allowed,
            )

        admit = _PrimaryDispatch.admit

        async def check_dispatch(dispatch):
            try:
                await admit(dispatch)
            except HistorySourceChangedError as error:
                late_errors.append(error)
                raise
            finally:
                if flipped:
                    checked.set()

        monkeypatch.setattr(OrdinaryAdmissionRepository, "commit_to_activation", late_update)
        monkeypatch.setattr(_PrimaryDispatch, "admit", check_dispatch)
        provider.release[1].set()
        await asyncio.wait_for(checked.wait(), 10)
        if late_errors:
            # Show the real publication exception instead of a timeout waiting
            # for the second model, which the old late predicate never reaches.
            raise late_errors[0]
        await provider.wait(2)
        admitted_c = await host.ordinary.get(selected_id)
        assert admitted_c is not None and admitted_c.activation_id == admitted_a.activation_id
        assert C in _plain(provider.entered[1])
        assert provider.entered[1].messages[: len(provider.entered[0].messages)] == (
            provider.entered[0].messages
        )
        assert provider.entered[1].tools == provider.entered[0].tools
        assert provider.entered[1].request_chain_id == provider.entered[0].request_chain_id
        provider.release[2].set()
        await asyncio.wait_for(_wait_for_worker(worker, key), 10)
        assert len(provider.requests) == 2 and await _work_count(database) == sender.calls == 0
        later = await env.service.writer.append(
            scope=message(3, "later unselected input").scope(),
            platform_message_id="later-unselected-source",
            sender_user_id="10001",
            direction="inbound",
            content="synthetic later unselected input",
        )
        assert await host.continuation_for_event(later.event.id, check_budget=False) is None
        assert await host.ordinary.get(later.event.id) is None
        assert await host.ordinary.get(a_id) == admitted_a
    finally:
        for release in provider.release.values():
            release.set()
        await worker.close()
        await host.close()
