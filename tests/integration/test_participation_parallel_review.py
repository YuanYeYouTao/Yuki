"""Actual Main and Host observer scheduling overlap without external model or QQ calls."""

import asyncio
import time
from dataclasses import replace

import pytest
from tests.support.semantic_participation_host_helpers import (
    _choice,
    _event_and_route,
    _message,
    _observation,
)
from tests.unit.test_participation_ordinary_feedback import ordinary_backend
from tests.unit.test_participation_pipeline_retirement import _relationship
from yuki_participation.participation import ParticipationCheckpoint

from qq_ai_bot.conversation.scope import ConversationTurnSnapshot
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.domain.messages import ChatMessage
from qq_ai_bot.services.main_agent_backend import MainAgentBackend


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
            async def evaluate(self, snapshot):
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
