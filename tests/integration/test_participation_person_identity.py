"""Actual pinned library, Person merge, SQLite admissions and current Jev HTTP projection."""

import json
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
from sqlalchemy import select
from tests.conftest import make_settings
from tests.integration.test_telegram_runtime import telegram_env as telegram_env
from tests.integration.test_telegram_runtime import update
from tests.support.event_seed import seed_event
from tests.support.semantic_participation_host_helpers import _host, _item
from tests.unit.test_participation_pipeline_retirement import _relationship
from yuki_participation.models import Effect, Snapshot, SourceRef
from yuki_participation.observer import JevObserver
from yuki_participation.self_report import SelfDelta, SelfReport

from yuki.admin.config_service import RuntimeConfigService
from yuki.conversation.canonical_db_models import (
    CanonicalConversationModel,
    SpaceActiveRouteModel,
)
from yuki.conversation.initiative_sources import source_revision
from yuki.domain.conversations import ScopeType
from yuki.identity.db_models import CanonicalSpaceModel, SpaceBindingModel
from yuki.identity.person_identity import merge_persons
from yuki.persistence.models import ChatEventModel
from yuki.services.participation_feedback import admission_unit_binding
from yuki.services.participation_identity import actor_identity
from yuki.services.participation_trace import TracedSemanticObserver


def actual_config(host):
    host.app.runtime_config = RuntimeConfigService(
        settings=make_settings(host.database.url, conversation_semantic_participation_enabled=True),
        database=host.database,
    )


async def append(host, original, sender, *, reply=None):
    row, _ = await seed_event(
        host.app.ledger,
        bot_user_id=original.bot_user_id,
        platform_message_id=str(uuid4()),
        scope_type=ScopeType.GROUP,
        group_id=original.group_id,
        sender_user_id=sender,
        direction="inbound",
        content="synthetic same discussion",
        reply_to_event_id=reply,
        occurred_at=datetime.now(UTC) - timedelta(seconds=20),
    )
    return row


async def merge(database, source, target):
    async with database.sessions() as session, session.begin():
        assert await merge_persons(session, source, target) == target


def report(admission, event, engage, sequence=1):
    return SelfReport(
        run_ref=admission.activation_id,
        sequence=sequence,
        response_id=f"{admission.activation_id}:{sequence}",
        at=event.at,
        source=event.ref,
        delta=SelfDelta(engage=engage),
    )


async def test_current_merge_quiet_cannot_be_overridden_by_original_stay_or_late_send(
    database, tmp_path
):
    host, item, a, accepted_a, ua = await _relationship(database, tmp_path)
    actual_config(host)
    try:
        b = await append(host, a, "1002", reply=a.id)
        binding_b = await host.binding_for_event(b.id)
        accepted_b = host.ordinary.prepare(b, item.scene.generation, 2, binding_b, reason="mention")
        assert await host.ordinary.commit(accepted_b)
        await host.on_ordinary_admitted(binding_b, accepted_b.admission)
        ub = admission_unit_binding(accepted_b.admission)
        eb = item.controller.state.events[f"event:{b.id}"]
        ea = item.controller.state.events[f"event:{a.id}"]
        assert ua.unit.thread == ub.unit.thread
        assert item.controller.observe_unit_hint(ub, report(accepted_b.admission, eb, "quiet"))
        original = item.controller.state.model_dump_json()
        await merge(database, a.author_person_id, b.author_person_id)
        assert item.controller.state.model_dump_json() == original
        assert item.controller.observe_unit_expression(
            ua,
            accepted_a.admission.activation_id,
            Effect(
                effect_id="actual-late-send",
                kind="message",
                at=time.time(),
                actual_targets=(a.author_person_id,),
            ),
        )
        assert item.controller.observe_unit_hint(ua, report(accepted_a.admission, ea, "stay", 2))
        c = await append(host, a, "1002")
        assert await host.continuation_for_event(c.id) is None
        identity = await actor_identity(host, item)
        current = item.controller.participating_units(time.time(), actor_identity=identity)
        assert len(current) == 1 and current[0].engage == "quiet"
        assert (
            str(source_revision(await host.app.ledger.get_event(a.id)))
            == accepted_a.admission.source_revision
        )
        assert item.controller.state.events[ea.ref.event_id] == ea
        assert (await host.ordinary.get(a.id)).actor_person_id == a.author_person_id
    finally:
        await host.close()


async def test_merged_new_actor_has_one_original_admission_and_cold_progress_after_flatten(
    database, tmp_path
):
    host, item, a, accepted_a, ua = await _relationship(database, tmp_path)
    actual_config(host)
    try:
        b = await append(host, a, "1002")
        await merge(database, a.author_person_id, b.author_person_id)
        binding_b, reason = await host.continuation_for_event(b.id)
        assert reason == "continuation" and binding_b.target_hint == b.author_person_id
        async with database.sessions() as writer:
            assert await host.continuation_current(binding_b, session=writer)
        accepted_b = host.ordinary.prepare(b, item.scene.generation, 2, binding_b, reason=reason)
        assert await host.ordinary.commit(accepted_b)
        await host.on_ordinary_admitted(binding_b, accepted_b.admission)
        assert not await host.ordinary.commit(accepted_b)
        await host.on_ordinary_admitted(binding_b, accepted_b.admission)
        before = item.controller.state.host_checkpoint["participation_v1"]
        assert (
            before["units"][item.controller._unit_key(ua.unit.thread, a.author_person_id)][
                "binding"
            ]["actor"]
            == a.author_person_id
        )
        assert sum(len(unit["fast_inputs"]) for unit in before["units"].values()) == 1
        c = await append(host, a, "1003")
        await merge(database, b.author_person_id, c.author_person_id)
        await host._save(item)
        host._sessions.clear()
        restored = await host._session(item.scene)
        for _ in range(3):
            await host._hydrate(restored)
        assert restored.controller.state.host_checkpoint["participation_v1"] == before
        identity = await actor_identity(host, restored)
        current = restored.controller.participating_units(time.time(), actor_identity=identity)
        assert len(current) == 1 and current[0].unconfirmed_inputs == 1
        assert (await host.ordinary.get(a.id)).activation_id == accepted_a.admission.activation_id
        assert (await host.ordinary.get(b.id)).activation_id == accepted_b.admission.activation_id
        assert not host._observer.calls
    finally:
        await host.close()


async def test_new_native_jev_http_uses_current_person_but_returns_original_snapshot(
    database, tmp_path
):
    host, item, a, _accepted, _unit = await _relationship(database, tmp_path)
    try:
        b = await append(host, a, "1002")
        await host.binding_for_event(b.id)
        await merge(database, a.author_person_id, b.author_person_id)
        ea, eb = (item.controller.state.events[f"event:{row.id}"] for row in (a, b))
        snapshot = Snapshot(
            scope=item.scene.scope, focus=eb, context=(ea,), sequence=1, issued_at=time.time()
        )
        original = snapshot.model_dump_json()
        requests = []

        def http(request):
            body = json.loads(request.content)
            requests.append(body)
            preferred = {
                "interaction_mark": "extend_yuki",
                "information_state": "refine",
                "floor_state": "yuki",
                "boundary_scope": "target_thread",
                "unit_selection": "new",
            }
            answers = {}
            for name, question in body["questions"].items():
                choice = preferred.get(name, "unknown")
                answers[name] = {
                    "type": "choice",
                    "choice": choice,
                    "probabilities": {key: float(key == choice) for key in question["criteria"]},
                }
            return httpx.Response(
                200, json={"model": body["model"], "answers": answers, "usage": {}}
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(http)) as transport:
            observer = JevObserver(
                "synthetic-key",
                identity=lambda: {"display_name": "测试角色", "aliases": ()},
                client=transport,
            )
            result = await TracedSemanticObserver(observer, None, database=database).evaluate(
                snapshot
            )
        assert len(requests) == 1
        state = requests[0]["state"]
        assert state["focus"]["author"] == state["context"][0]["author"] == b.author_person_id
        assert result.snapshot.model_dump_json() == snapshot.model_dump_json() == original
        assert ea.author == a.author_person_id and eb.author == b.author_person_id
    finally:
        await host.close()


async def test_current_representative_keeps_new_input_actors_original_hint_basis(
    database, tmp_path
):
    host, item, a, accepted_a, ua = await _relationship(database, tmp_path)
    actual_config(host)
    try:
        b = await append(host, a, "1002", reply=a.id)
        first_binding = await host.binding_for_event(b.id)
        first = host.ordinary.prepare(b, item.scene.generation, 2, first_binding, reason="mention")
        assert await host.ordinary.commit(first)
        await host.on_ordinary_admitted(first_binding, first.admission)
        ub = admission_unit_binding(first.admission)
        eb = item.controller.state.events[f"event:{b.id}"]
        assert item.controller.observe_unit_hint(ub, report(first.admission, eb, "stay"))
        await merge(database, a.author_person_id, b.author_person_id)
        assert item.controller.observe_unit_expression(
            ua,
            accepted_a.admission.activation_id,
            Effect(
                effect_id="representative-late-A",
                kind="message",
                at=time.time(),
                actual_targets=(a.author_person_id,),
            ),
        )
        c = await append(host, a, "1002")
        current_binding, reason = await host.continuation_for_event(c.id)
        assert set(ub.basis).issubset(
            {SourceRef(event_id=key, revision=revision) for key, revision in current_binding.basis}
        )
        current = host.ordinary.prepare(c, item.scene.generation, 3, current_binding, reason=reason)
        assert await host.ordinary.commit(current)
        await host.on_ordinary_admitted(current_binding, current.admission)
        ec = item.controller.state.events[f"event:{c.id}"]
        assert item.controller.observe_unit_hint(
            admission_unit_binding(current.admission), report(current.admission, ec, "quiet")
        )
        d = await append(host, a, "1002")
        assert await host.continuation_for_event(d.id) is None
        original_b = item.controller._read_participation_checkpoint().units[
            item.controller._unit_key(ub.unit.thread, ub.unit.target)
        ]
        assert original_b.binding == ub
    finally:
        await host.close()


async def test_native_telegram_participation_reads_real_internal_space_configuration(
    telegram_env, database, tmp_path
):
    env = telegram_env
    incoming = update(chat=-10042)
    incoming["message"]["date"] = int(time.time()) - 40
    await env.gateway.receive(incoming)
    async with database.sessions() as session:
        event_id = await session.scalar(
            select(ChatEventModel.id).where(ChatEventModel.direction == "inbound")
        )
    event = await env.harness.ledger.get_event(event_id)
    async with database.immediate_session() as session:
        conversation = await session.get(
            CanonicalConversationModel, event.canonical_conversation_id
        )
        space = await session.get(CanonicalSpaceModel, conversation.space_id)
        space.enabled = space.autonomous_enabled = True
        binding = await session.scalar(
            select(SpaceBindingModel).where(
                SpaceBindingModel.space_id == space.id, SpaceBindingModel.status == "active"
            )
        )
        if await session.get(SpaceActiveRouteModel, space.id) is None:
            session.add(
                SpaceActiveRouteModel(
                    space_id=space.id,
                    space_binding_id=binding.id,
                    presence_id=event.ingress_presence_id,
                    route_generation=1,
                    paused=False,
                    revision=1,
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
    host, _policy = await _host(database, tmp_path)
    actual_config(host)
    try:
        item = await _item(host, event)
        assert item.scene.platform == "telegram" and item.scene.group_id != item.scene.space_id
        binding = await host.binding_for_event(event.id)
        assert binding is not None and binding.event_id == event.id
        assert not host._observer.calls
    finally:
        await host.close()
