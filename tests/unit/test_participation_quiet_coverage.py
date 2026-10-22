"""Quiet covers actual same-Main inputs without borrowing other admissions."""

import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from tests.support.event_seed import seed_event
from tests.support.semantic_participation_host_helpers import _event_and_route
from tests.unit.test_participation_foreground_preparation import _runtime
from tests.unit.test_participation_pipeline_retirement import _relationship
from yuki_participation.participation import ParticipationCheckpoint
from yuki_participation.self_report import SelfDelta

from yuki.domain.conversations import ScopeType
from yuki.domain.messages import ChatResponse


async def _admit(host, item, original, event, *, same_activation=True, reason="continuation"):
    if reason == "continuation":
        binding, resolved = await host.continuation_for_event(event.id)
        assert resolved == reason
    else:
        binding = await host.binding_for_event(event.id)
    prepared = host.ordinary.prepare(event, item.scene.generation, 2, binding, reason=reason)
    if same_activation:
        async with host.database.immediate_session() as writer:
            assert await host.ordinary.commit_to_activation(
                prepared, original.admission, session=writer
            )
        admission = await host.ordinary.get(event.id)
    else:
        assert await host.ordinary.commit(prepared)
        admission = prepared.admission
    await host.on_ordinary_admitted(binding, admission)
    return admission


def _original_unit(item, binding):
    checkpoint = ParticipationCheckpoint.model_validate(
        item.controller.state.host_checkpoint["participation_v1"]
    )
    return checkpoint.units[item.controller._unit_key(binding.unit.thread, binding.unit.target)]


@pytest.mark.parametrize("other_input", ["none", "unobserved", "other_activation"])
async def test_quiet_only_covers_observed_same_actor_inputs_in_its_original_activation(
    database, tmp_path, other_input
):
    host, item, a, original, unit = await _relationship(database, tmp_path)
    try:
        b = await _event_and_route(database, host.app.ledger, content="selected observed B")
        await _admit(host, item, original, b)
        c = None
        if other_input != "none":
            c = await _event_and_route(database, host.app.ledger, content="not covered C")
            await _admit(host, item, original, c, same_activation=other_input == "unobserved")
        d, _ = await seed_event(
            host.app.ledger,
            bot_user_id=a.bot_user_id,
            platform_message_id=str(uuid4()),
            scope_type=ScopeType.GROUP,
            sender_user_id="1002",
            direction="inbound",
            content="another actor in the same activation",
            group_id=a.group_id,
            occurred_at=datetime.now(UTC) - timedelta(seconds=40),
        )
        await _admit(host, item, original, d, reason="mention")
        for key in list(item.observation.queue.pending):
            item.observation.queue.discard(key)
        for event in (b, c, d):
            if event is not None:
                source = item.controller.state.events[f"event:{event.id}"]
                assert item.observation.request_observation(source.ref)
        before = tuple(item.observation.queue.pending)
        runtime = _runtime(host, item, a)
        runtime.visible_event_ids = frozenset(
            (a.id, b.id, d.id, *((c.id,) if other_input == "other_activation" else ()))
        )
        response = ChatResponse("", 0, provider_request_id="quiet-after-observed-B")
        await host.observe_main_response(runtime, 2, response, SelfDelta(engage="quiet"))
        current = _original_unit(item, unit)
        a_ref = item.controller.state.events[f"event:{a.id}"].ref
        b_ref = item.controller.state.events[f"event:{b.id}"].ref
        assert current.hint.source == a_ref
        assert current.hint.at == item.controller.state.events[a_ref.event_id].at
        assert current.hint.run_ref == original.admission.activation_id
        assert set(current.hint_basis) == {a_ref, b_ref}
        assert tuple(item.observation.queue.pending) == before
        assert not host._observer.calls

        # A repeated response and later hydration cannot expand what this paid
        # request actually observed, even if more sources are visible afterward.
        runtime.visible_event_ids = frozenset((a.id, b.id, d.id, *((c.id,) if c else ())))
        await host.observe_main_response(runtime, 2, response, SelfDelta(engage="quiet"))
        await host._hydrate(item)
        assert _original_unit(item, unit).hint_basis == current.hint_basis
        assert _original_unit(item, unit).hint.source == a_ref

        await item.observation.evaluate_due(time.time())
        remaining_or_observed = set(item.observation.queue.pending) | {
            snapshot.focus.ref.event_id for snapshot in host._observer.calls
        }
        assert remaining_or_observed == {
            f"event:{event.id}" for event in (c, d) if event is not None
        }
        assert f"event:{b.id}" not in remaining_or_observed
    finally:
        await host.close()
