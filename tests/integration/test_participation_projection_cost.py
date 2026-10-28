"""Host projection keeps real source choices without evaluating all participation beliefs."""

import json
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import update
from tests.support.canonical_identity import ensure_person
from tests.support.event_seed import seed_event
from tests.support.semantic_participation_host_helpers import (
    _event_and_route,
    _host,
    _item,
    _observation,
)
from tests.unit.test_participation_ordinary_feedback import ordinary_expression
from tests.unit.test_participation_pipeline_retirement import _relationship
from yuki_participation.controller import Controller
from yuki_participation.models import Snapshot
from yuki_participation.observer import JevObserver
from yuki_participation.participation import ParticipationUnit, UnitBinding
from yuki_participation.self_report import SelfDelta, SelfReport

from yuki.config import BotIdentity
from yuki.domain.conversations import ScopeType
from yuki.event_prompt import ChatEventPromptRenderer
from yuki.persistence.models import ChatEventModel
from yuki.services.participation_feedback import admission_unit_binding, sync_scope_effects


async def test_jev_and_main_keep_the_same_named_mentions_in_original_positions(database, tmp_path):
    host, _ = await _host(database, tmp_path, observer=False)
    try:
        body = "@油桃 你先说这件事，@远野 等下接着说；@油桃 再确认。"
        row = await _event_and_route(database, host.ledger, content=body)
        item = await _item(host, row)
        event = host._event(row, item)
        snapshot = Snapshot(
            scope=item.scene.scope,
            focus=event,
            context=(),
            sequence=1,
            issued_at=time.time(),
        )

        requests = []

        def http(request):
            body = json.loads(request.content)
            requests.append(body)
            return httpx.Response(
                200,
                json={
                    "model": body["model"],
                    "answers": {
                        name: {
                            "type": "choice",
                            "choice": "unknown",
                            "probabilities": {
                                choice: float(choice == "unknown")
                                for choice in question["criteria"]
                            },
                        }
                        for name, question in body["questions"].items()
                    },
                    "usage": {},
                },
            )

        host.settings.character_identity = BotIdentity("阿澪", ("阿澪", "小澪"))
        async with httpx.AsyncClient(transport=httpx.MockTransport(http)) as transport:
            observer = JevObserver(
                "synthetic-key",
                identity=host._observer_identity,
                endpoint="https://semantic.example/systemone",
                request_model="fixture-model",
                client=transport,
            )
            await observer.evaluate(snapshot)
            host.settings.character_identity = BotIdentity("林舟", ("林舟", "阿舟"))
            await observer.evaluate(snapshot.model_copy(update={"sequence": 2}))
        assert len(requests) == 2
        payload = requests[0]["state"]
        assert payload["agent_identity"] == {"display_name": "阿澪", "aliases": ["阿澪", "小澪"]}
        assert requests[1]["state"]["agent_identity"] == {
            "display_name": "林舟",
            "aliases": ["林舟", "阿舟"],
        }
        assert requests[1]["state"]["focus"] == payload["focus"]
        assert requests[0]["questions"].keys() == requests[1]["questions"].keys()
        for dimension in requests[0]["questions"]:
            assert (
                requests[0]["questions"][dimension]["criteria"].keys()
                == requests[1]["questions"][dimension]["criteria"].keys()
            )
        for request, current_name, old_name in (
            (requests[0], "阿澪", "林舟"),
            (requests[1], "林舟", "阿澪"),
        ):
            for dimension in (
                "interaction_mark",
                "information_state",
                "floor_state",
                "boundary_scope",
            ):
                instructions = request["questions"][dimension]["instructions"]
                assert current_name in instructions and old_name not in instructions
            assert "{{name}}" not in json.dumps(request["questions"], ensure_ascii=False)
        main = ChatEventPromptRenderer((row,)).reference_message(
            row, current_event_id=row.id, current_content=body
        )

        assert payload["focus"]["text"] == body
        assert main.content.endswith(body)
        assert event.author == event.target == row.author_person_id
        assert "Yuki" not in payload["focus"]["text"]
        assert not host._observer
    finally:
        await host.close()


async def _append(host, *, author="1001", reply_to=None, cause=None, age=20):
    if author != "8000":
        async with host.database.immediate_session() as writer:
            await ensure_person(writer, author, display_name=f"synthetic-{author}")
    row, created = await seed_event(
        host.ledger,
        bot_user_id="8000",
        origin_key=str(uuid4()),
        scope_type=ScopeType.GROUP,
        sender_user_id=author,
        direction="outbound" if author == "8000" else "inbound",
        content=f"synthetic projection source {author}",
        group_id="2001",
        reply_to_event_id=reply_to,
        caused_by_event_id=cause,
        origin="system_task" if cause is not None else "user_message",
        occurred_at=datetime.now(UTC) - timedelta(seconds=age),
    )
    assert created
    return row


def _resolve_new(item, row, sequence):
    event = item.controller.state.events[f"event:{row.id}"]
    snapshot = Snapshot(
        scope=item.scene.scope,
        focus=event,
        context=(),
        sequence=sequence,
        issued_at=time.time(),
    )
    assert item.controller.apply_semantic_observation(_observation(snapshot, unit="new"))
    resolved = item.controller.resolved_unit(event)
    assert resolved is not None and resolved.author == row.author_person_id
    assert item.controller.observe_unit_input(
        UnitBinding(
            scope=item.scene.scope,
            unit=ParticipationUnit(thread=resolved.thread, target=resolved.target),
            actor=resolved.author,
            basis=(resolved.ref,),
        ),
        resolved.ref,
    )
    return resolved


async def test_projection_keeps_every_human_and_self_anchor_without_full_belief_query(
    database, tmp_path, monkeypatch
):
    host, _ = await _host(database, tmp_path, observer=False)
    try:
        first = await _event_and_route(database, host.ledger, content="synthetic first thread")
        item = await _item(host, first)
        first_unit = _resolve_new(item, first, 1)
        own_first = await _append(host, author="8000", cause=first.id, age=30)
        await host._hydrate(item)
        second = await _append(host, author="1002", age=25)
        await host._hydrate(item)
        second_unit = _resolve_new(item, second, 2)
        own_second = await _append(host, author="8000", cause=second.id, age=20)
        await host._hydrate(item)
        third = await _append(host, author="1003", age=15)
        await host._hydrate(item)
        third_unit = _resolve_new(item, third, 3)
        focus = await _append(host, author="1004", age=10)
        assert len(item.controller.participating_units(time.time())) == 3

        # The real controller and real ledger establish the choices. Only count
        # the global derived view: it has no role in projecting a new source.
        calls = []
        participating_units = Controller.participating_units

        def count_units(controller, now):
            calls.append(controller.state.scope)
            return participating_units(controller, now)

        monkeypatch.setattr(Controller, "participating_units", count_units)
        projected = host._event(focus, item)
        assert projected.author == projected.target == focus.author_person_id
        assert projected.ref.event_id == f"event:{focus.id}"
        assert projected.unit_ambiguous
        options = {
            (option.thread, option.target, option.self_anchor)
            for option in projected.unit_options
            if option.key != "new"
        }
        first_anchor = item.controller.state.events[f"event:{own_first.id}"].ref
        second_anchor = item.controller.state.events[f"event:{own_second.id}"].ref
        assert options == {
            (first_unit.thread, focus.author_person_id, None),
            (first_unit.thread, focus.author_person_id, first_anchor),
            (second_unit.thread, focus.author_person_id, None),
            (second_unit.thread, focus.author_person_id, second_anchor),
            (third_unit.thread, focus.author_person_id, None),
        }
        assert [option.self_anchor for option in projected.unit_options if option.self_anchor] == [
            second_anchor,
            first_anchor,
        ]

        quoted = await _append(host, author="1005", reply_to=own_first.id, age=5)
        answer = host._event(quoted, item)
        assert answer.reply_to == first_anchor
        assert answer.thread == first_unit.thread
        assert answer.author == answer.target == quoted.author_person_id
        assert not answer.unit_ambiguous and answer.unit_options == ()
        assert calls == [], "source projection must not evaluate every participation unit"
    finally:
        await host.close()


async def test_cold_hydration_and_warm_increment_project_sources_without_full_belief_query(
    database, tmp_path, monkeypatch
):
    host, _ = await _host(database, tmp_path, observer=False)
    try:
        first = await _event_and_route(database, host.ledger, content="synthetic cold source")
        scene = await host._scene(first.canonical_conversation_id)
        item = await host._session(scene)
        rows = [first]
        for index in range(12):
            rows.append(await _append(host, author=str(1100 + index), age=30 - index))

        calls = []
        participating_units = Controller.participating_units

        def count_units(controller, now):
            calls.append(controller.state.scope)
            return participating_units(controller, now)

        monkeypatch.setattr(Controller, "participating_units", count_units)
        await host._hydrate(item)
        original = dict(item.controller.state.events)
        assert set(original) == {f"event:{row.id}" for row in rows}
        for row in rows:
            event = original[f"event:{row.id}"]
            assert event.author == row.author_person_id and event.ref.revision == 1
        await host._hydrate(item)
        assert item.controller.state.events == original

        await host._save(item)
        host._sessions.clear()
        restored = await host._session(scene)
        await host._hydrate(restored)
        assert restored.controller.state.events == original
        added = await _append(host, author="1200", age=1)
        await host._hydrate(restored)
        assert {
            key: value
            for key, value in restored.controller.state.events.items()
            if key != f"event:{added.id}"
        } == original
        assert (
            restored.controller.state.events[f"event:{added.id}"].author == added.author_person_id
        )
        assert calls == [], "hydrating source pages must not evaluate global participation beliefs"
    finally:
        await host.close()


async def test_active_old_and_inactive_new_anchors_preserve_frozen_projection_after_feedback(
    database, tmp_path
):
    host, item, original, old_self, _receipt = await ordinary_expression(database, tmp_path)
    try:
        await host._hydrate(item)
        await sync_scope_effects(host, item)
        old_anchor = item.controller.state.events[f"event:{old_self.id}"].ref
        assert any(
            old_anchor in unit.anchors for unit in item.controller.participating_units(time.time())
        )
        new_self = await _append(host, author="8000", age=0)
        await host._hydrate(item)
        new_anchor = item.controller.state.events[f"event:{new_self.id}"].ref
        assert all(
            new_anchor not in unit.anchors
            for unit in item.controller.participating_units(time.time())
        )
        focus = await _append(host, age=0)
        projected = host._event(focus, item)
        assert [option.self_anchor for option in projected.unit_options if option.self_anchor] == [
            new_anchor,
            old_anchor,
        ]
        assert item.controller.observe_committed_event(projected)
        frozen = item.controller.freeze_observation(
            Snapshot(
                scope=item.scene.scope,
                focus=projected,
                context=tuple(
                    item.controller.state.events[f"event:{row.id}"]
                    for row in (original, old_self, new_self)
                ),
                sequence=1,
                issued_at=time.time(),
            )
        )
        frozen_json = frozen.model_dump_json()
        projected_json = projected.model_dump_json()
        await host._save(item)
        admission = await host.ordinary.get(original.id)
        unit = admission_unit_binding(admission)
        assert item.controller.observe_unit_hint(
            unit,
            SelfReport(
                run_ref=admission.activation_id,
                sequence=1,
                response_id="synthetic-later-quiet",
                at=item.controller.state.events[f"event:{original.id}"].at,
                source=unit.basis[0],
                delta=SelfDelta(engage="quiet"),
            ),
        )
        assert any(
            current.engage == "quiet"
            for current in item.controller.participating_units(time.time())
        )
        assert host._event(focus, item) is projected
        assert projected.model_dump_json() == projected_json
        assert frozen.model_dump_json() == frozen_json
    finally:
        await host.close()


@pytest.mark.parametrize("source_change", ["revision", "withdrawn"])
async def test_projection_change_preserves_restore_and_source_revocation_of_continuation(
    database, tmp_path, source_change
):
    host, item, original, _admission, unit = await _relationship(database, tmp_path)
    try:
        later = await _event_and_route(
            database, host.ledger, content="synthetic continuation candidate"
        )
        binding, reason = await host.continuation_for_event(later.id)
        assert reason == "continuation" and binding.unit_key == unit.unit.thread
        for _ in range(2):
            await host._hydrate(item)
            assert (await host.continuation_for_event(later.id))[0] == binding
        await host._save(item)
        host._sessions.clear()
        restored = await host._session(item.scene)
        await host._hydrate(restored)
        assert (await host.continuation_for_event(later.id))[0] == binding
        source = restored.controller.state.events[f"event:{original.id}"].ref

        async with database.immediate_session() as writer:
            await writer.execute(
                update(ChatEventModel)
                .where(ChatEventModel.id == original.id)
                .values(
                    **(
                        {"content": "synthetic revised original source"}
                        if source_change == "revision"
                        else {"suppression_status": "duplicate", "utterance_fingerprint": "f" * 64}
                    )
                )
            )
        await host._hydrate(restored)
        assert await host.continuation_for_event(later.id) is None
        assert restored.controller.state.invalidated[source.event_id] >= source.revision
        assert not host._observer.calls
    finally:
        await host.close()
