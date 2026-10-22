"""Runtime feedback reads the original admitted discussion and no observer work."""

import asyncio
import time
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import event as sqlalchemy_events
from tests.support.canonical_identity import ensure_person
from tests.support.event_seed import seed_event
from tests.support.semantic_participation_host_helpers import (
    _choice,
    _event_and_route,
    _item,
    _message,
    _observation,
)
from tests.unit.test_participation_pipeline_retirement import _relationship
from yuki_participation.models import Snapshot
from yuki_participation.participation import ParticipationCheckpoint
from yuki_participation.self_report import SelfDelta, SelfReport

from yuki.conversation.scope import ConversationTurnSnapshot
from yuki.domain.conversations import ScopeType
from yuki.domain.messages import ChatResponse
from yuki.identity.person_identity import merge_persons
from yuki.services.agent_tools import ToolRuntime
from yuki.services.main_agent_backend import MainAgentBackend
from yuki.services.participation_feedback import admission_unit_binding


def _runtime(event, *, recovered=False):
    inbound = _message(event)
    return ToolRuntime(
        inbound=None if recovered else inbound,
        conversation_id=event.canonical_conversation_id,
        trigger_event_id=event.id,
        turn_snapshot=ConversationTurnSnapshot(
            event.canonical_conversation_id,
            f"canonical:{event.canonical_conversation_id}",
            1,
            event.id,
            1,
        ),
    )


@pytest.mark.parametrize("recovered", [False, True])
async def test_real_backend_reads_quiet_and_semantic_facts_without_observer_lock_or_writer(
    database, tmp_path, monkeypatch, recovered
):
    host, item, event, original, unit = await _relationship(database, tmp_path, engage="quiet")
    try:
        source = item.controller.state.events[f"event:{event.id}"]
        snapshot = item.controller.freeze_observation(
            Snapshot(
                scope=item.scene.scope, focus=source, context=(), sequence=1, issued_at=time.time()
            )
        )
        observation = _observation(snapshot, act="other_exchange")
        observation = observation.model_copy(
            update={
                "answers": {**observation.answers, "floor_state": _choice("floor_state", "other")}
            }
        )
        assert item.controller.apply_semantic_observation(observation)
        runtime = _runtime(event, recovered=recovered)
        backend = MainAgentBackend(
            SimpleNamespace(participation_runtime_context=host.runtime_context), runtime
        )
        statements = []

        def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement.strip().upper())

        sqlalchemy_events.listen(database.engine.sync_engine, "before_cursor_execute", capture)
        try:
            with monkeypatch.context() as patches:
                for name in ("_save", "_hydrate", "_advance_scene", "_validate_boundaries"):
                    patches.setattr(host, name, AsyncMock(side_effect=AssertionError(name)))
                async with host._lock:
                    facts = await asyncio.wait_for(backend.participation_context(None), 5)
        finally:
            sqlalchemy_events.remove(database.engine.sync_engine, "before_cursor_execute", capture)

        assert facts["thread"] == original.admission.binding.unit_key
        assert facts["target_person_id"] == original.admission.binding.target_hint
        assert len(facts["reports"]) == 1
        report = facts["reports"][0]
        assert report["actor_person_id"] == unit.actor
        assert report["self_intent"]["engage"] == "quiet"
        review = report["semantic_review"]
        assert review["interaction"] == "other_exchange" and review["floor"] == "other"
        assert review["boundary_scope"] == "target_thread"
        assert review["source"] == source.ref.model_dump(mode="json")
        assert statements and not any(
            statement.startswith(("INSERT", "UPDATE", "DELETE", "REPLACE", "BEGIN IMMEDIATE"))
            for statement in statements
        )
        assert not host._observer.calls
    finally:
        await host.close()


@pytest.mark.parametrize("other_scope", ["person", "conversation"])
async def test_latest_other_discussion_does_not_replace_original_scope(
    database, tmp_path, other_scope
):
    host, _item_value, event, original, _unit = await _relationship(database, tmp_path)
    try:
        if other_scope == "person":
            other, _ = await seed_event(
                host.app.ledger,
                bot_user_id="8000",
                platform_message_id=str(uuid4()),
                scope_type=ScopeType.GROUP,
                sender_user_id="1002",
                group_id="2001",
                direction="inbound",
                content="synthetic unrelated person invitation",
                occurred_at=datetime.now(UTC),
            )
        else:
            other = await _event_and_route(database, host.app.ledger, group="2002")
        other_item = await _item(host, other)
        binding = await host.binding_for_event(other.id)
        accepted = host.ordinary.prepare(
            other, other_item.scene.generation, 1, binding, reason="mention"
        )
        assert await host.ordinary.commit(accepted)
        await host.on_ordinary_admitted(binding, accepted.admission)
        source = other_item.controller.state.events[f"event:{other.id}"]
        assert other_item.controller.observe_unit_hint(
            admission_unit_binding(accepted.admission),
            SelfReport(
                run_ref=accepted.admission.activation_id,
                sequence=1,
                response_id="unrelated-quiet",
                at=source.at,
                source=source.ref,
                delta=SelfDelta(engage="quiet"),
            ),
        )
        facts = await host.runtime_context(_runtime(event))
        assert facts["thread"] == original.admission.binding.unit_key
        assert facts["target_person_id"] == original.admission.binding.target_hint
        assert {report["actor_person_id"] for report in facts["reports"]} == {
            original.admission.actor_person_id
        }
        assert all(report["self_intent"]["engage"] == "stay" for report in facts["reports"])
        assert not host._observer.calls
    finally:
        await host.close()


@pytest.mark.parametrize("missing", ["binding", "loaded", "conversation", "generation", "source"])
async def test_unrelated_or_missing_admission_returns_none_without_hydration(
    database, tmp_path, monkeypatch, missing
):
    host, item, event, _, _ = await _relationship(database, tmp_path)
    try:
        runtime = _runtime(event)
        if missing == "binding":
            other = await _event_and_route(database, host.app.ledger)
            accepted = host.ordinary.prepare(
                other, item.scene.generation, 2, None, reason="mention"
            )
            assert await host.ordinary.commit(accepted)
            runtime = _runtime(other)
        elif missing == "loaded":
            host._sessions.clear()
        elif missing == "conversation":
            runtime = replace(
                runtime,
                inbound=replace(runtime.inbound, conversation_id=str(uuid4())),
                actor_context=None,
            )
        elif missing == "generation":
            runtime = replace(runtime, turn_snapshot=replace(runtime.turn_snapshot, generation=2))
        else:
            runtime = ToolRuntime(inbound=None, conversation_id=event.canonical_conversation_id)
        with monkeypatch.context() as patches:
            patches.setattr(
                host, "_hydrate", AsyncMock(side_effect=AssertionError("unexpected hydrate"))
            )
            assert await host.runtime_context(runtime) is None
        assert not host._observer.calls
    finally:
        await host.close()


async def test_person_merge_resolves_current_membership_but_preserves_original_fact_source(
    database, tmp_path
):
    host, _item_value, event, original, _unit = await _relationship(database, tmp_path)
    try:
        before = await host.runtime_context(_runtime(event))
        async with database.immediate_session() as writer:
            current = await ensure_person(writer, "another-account")
            await merge_persons(writer, original.admission.actor_person_id, current)
        after = await host.runtime_context(_runtime(event))
        assert after == before
        assert after["target_person_id"] == original.admission.actor_person_id
        assert after["reports"][0]["actor_person_id"] == original.admission.actor_person_id
        assert after["reports"][0]["actor_person_id"] != current
        assert not host._observer.calls
    finally:
        await host.close()


async def test_other_author_group_stop_is_visible_to_original_main_until_released(
    database, tmp_path
):
    host, item, event, original, unit = await _relationship(database, tmp_path)
    try:
        stop, _ = await seed_event(
            host.app.ledger,
            bot_user_id="8000",
            platform_message_id=str(uuid4()),
            scope_type=ScopeType.GROUP,
            sender_user_id="1002",
            group_id="2001",
            direction="inbound",
            content="synthetic stop for this whole discussion",
            occurred_at=datetime.now(UTC),
        )
        await _item(host, stop)
        focus = item.controller.state.events[f"event:{stop.id}"]
        snapshot = item.controller.freeze_observation(
            Snapshot(
                scope=item.scene.scope, focus=focus, context=(), sequence=2, issued_at=time.time()
            )
        )
        selected = next(
            option.key
            for option in snapshot.focus.unit_options
            if option.thread == unit.unit.thread
        )
        observation = _observation(snapshot, act="ask_yuki_stop", unit=selected)
        observation = observation.model_copy(
            update={
                "answers": {
                    **observation.answers,
                    "boundary_scope": _choice("boundary_scope", "group_thread"),
                }
            }
        )
        assert item.controller.apply_semantic_observation(observation)
        before = await host.runtime_context(_runtime(event))
        assert before["reports"][0]["self_intent"]["engage"] == "stay"
        assert len(before["boundaries"]) == 1
        boundary = before["boundaries"][0]
        assert boundary["source"] == focus.ref.model_dump(mode="json")
        assert boundary["actor_person_id"] == stop.author_person_id
        assert boundary["interaction"] == "ask_yuki_stop"
        assert boundary["scope"] == "group_thread"
        assert boundary["source_at"] == focus.at
        assert boundary["source_order"] == focus.source_order
        assert before["target_person_id"] == original.admission.actor_person_id

        invitation, _ = await seed_event(
            host.app.ledger,
            bot_user_id="8000",
            platform_message_id=str(uuid4()),
            scope_type=ScopeType.GROUP,
            sender_user_id="1002",
            group_id="2001",
            direction="inbound",
            content="synthetic new invitation",
            occurred_at=datetime.now(UTC),
        )
        await _item(host, invitation)
        invite = item.controller.state.events[f"event:{invitation.id}"]
        next_snapshot = item.controller.freeze_observation(
            Snapshot(
                scope=item.scene.scope, focus=invite, context=(), sequence=3, issued_at=time.time()
            )
        )
        selected = next(
            option.key
            for option in next_snapshot.focus.unit_options
            if option.thread == unit.unit.thread
        )
        reopening = _observation(next_snapshot, act="invite_yuki", unit=selected)
        reopening = reopening.model_copy(
            update={
                "answers": {
                    **reopening.answers,
                    "boundary_scope": _choice("boundary_scope", "group_thread"),
                }
            }
        )
        assert item.controller.apply_semantic_observation(reopening)
        after = await host.runtime_context(_runtime(event))
        assert not after.get("boundaries")
        assert not host._observer.calls
    finally:
        await host.close()


async def test_quiet_covers_observed_same_person_alias_without_rewriting_its_original_actor(
    database, tmp_path
):
    host, item, event, original, original_unit = await _relationship(database, tmp_path)
    try:
        async with database.immediate_session() as writer:
            other_original_person = await ensure_person(writer, "1002")
            await merge_persons(writer, other_original_person, original.admission.actor_person_id)

        async def incoming(account, text):
            row, _ = await seed_event(
                host.app.ledger,
                bot_user_id="8000",
                platform_message_id=str(uuid4()),
                scope_type=ScopeType.GROUP,
                sender_user_id=account,
                group_id="2001",
                direction="inbound",
                content=text,
                reply_to_event_id=event.id,
                occurred_at=datetime.now(UTC),
            )
            return row

        async def admit(row, reason):
            binding = (
                (await host.continuation_for_event(row.id))[0]
                if reason == "continuation"
                else await host.binding_for_event(row.id)
            )
            selected = host.ordinary.prepare(row, item.scene.generation, 1, binding, reason=reason)
            async with database.immediate_session() as writer:
                assert await host.ordinary.commit_to_activation(
                    selected, original.admission, session=writer
                )
            admission = await host.ordinary.get(row.id)
            await host.on_ordinary_admitted(binding, admission)
            return admission, admission_unit_binding(admission)

        alias = await incoming("1002", "same person's observed continuation")
        assert alias.author_person_id == other_original_person
        alias_admission, _ = await admit(alias, "continuation")
        outsider = await incoming("1003", "another person's same-thread direct input")
        outsider_admission, outsider_unit = await admit(outsider, "mention")
        source = item.controller.state.events[f"event:{outsider.id}"]
        assert item.controller.observe_unit_hint(
            outsider_unit,
            SelfReport(
                run_ref=original.admission.activation_id,
                sequence=1,
                response_id="outsider-original-stay",
                at=source.at,
                source=source.ref,
                delta=SelfDelta(engage="stay"),
            ),
        )
        outsider_next = await incoming("1003", "another person's observed continuation")
        outsider_next_admission, _ = await admit(outsider_next, "continuation")
        assert alias_admission.binding.unit_key == outsider_next_admission.binding.unit_key
        assert alias_admission.activation_id == outsider_next_admission.activation_id

        host.app.chat = SimpleNamespace(validate_turn_snapshot=AsyncMock(return_value=True))
        runtime = replace(
            _runtime(event),
            visible_event_ids=frozenset({event.id, alias.id, outsider.id, outsider_next.id}),
        )
        await host.observe_main_response(
            runtime,
            2,
            ChatResponse("", 0, provider_request_id="quiet-after-real-alias-input"),
            SelfDelta(engage="quiet"),
        )
        checkpoint = ParticipationCheckpoint.model_validate(
            item.controller.state.host_checkpoint["participation_v1"]
        )
        current = checkpoint.units[
            item.controller._unit_key(original_unit.unit.thread, original_unit.unit.target)
        ]
        original_ref = item.controller.state.events[f"event:{event.id}"].ref
        alias_ref = item.controller.state.events[f"event:{alias.id}"].ref
        outsider_ref = item.controller.state.events[f"event:{outsider_next.id}"].ref
        assert set(current.hint_basis) == {original_ref, alias_ref}
        assert outsider_ref not in current.hint_basis
        assert current.hint.source == original_ref
        assert current.binding.actor == original.admission.actor_person_id
        context = await host.runtime_context(runtime)
        intent = next(
            report["self_intent"]
            for report in context["reports"]
            if report["actor_person_id"] == original.admission.actor_person_id
        )
        assert intent["source"] == original_ref.model_dump(mode="json")
        assert {ref["event_id"] for ref in intent["basis"]} == {
            original_ref.event_id,
            alias_ref.event_id,
        }
        assert (await host.ordinary.get(alias.id)).actor_person_id == other_original_person
        assert (
            await host.ordinary.get(outsider_next.id)
        ).actor_person_id == outsider_admission.actor_person_id
        assert not host._observer.calls
    finally:
        await host.close()
