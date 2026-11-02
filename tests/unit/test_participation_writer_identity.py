"""Current participation survives FIFO waiting without a writer-side actor history scan."""

import asyncio
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import event as sql_event
from tests.support.canonical_identity import ensure_person
from tests.support.person_merge import merge_persons
from tests.support.semantic_participation_host_helpers import _event_and_route
from tests.unit.test_participation_pipeline_retirement import _relationship
from yuki_participation.self_report import SelfDelta, SelfReport

from yuki.conversation.ordinary_admission import OrdinaryAdmissionConflict
from yuki.identity.db_models import CanonicalPersonModel
from yuki.persistence.database import write_failure_committed
from yuki.services.participation_identity import actor_identity


async def test_queued_quiet_uses_current_predicate_without_collecting_actor_history(
    database, tmp_path, monkeypatch
):
    host, item, _a, original, unit = await _relationship(database, tmp_path)
    in_writer = ContextVar("participation_test_writer", default=False)
    original_writer = database.immediate_session
    statements = []

    @asynccontextmanager
    async def tracked_writer():
        async with original_writer() as writer:
            token = in_writer.set(True)
            try:
                yield writer
            finally:
                in_writer.reset(token)

    def capture(_connection, _cursor, sql, parameters, _context, _many):
        if in_writer.get() and sql.lstrip().upper().startswith("SELECT") and "persons" in sql:
            statements.append((sql, parameters))

    task = None
    listening = False
    try:
        event = await _event_and_route(database, host.ledger, content="queued qualification")
        binding, reason = await host.continuation_for_event(event.id)
        admission = host.ordinary.prepare(event, item.scene.generation, 2, binding, reason=reason)
        monkeypatch.setattr(database, "immediate_session", tracked_writer)
        sql_event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
        listening = True
        continuation = await host.prepare_continuation(binding)
        queued = asyncio.Event()

        async def allowed(writer):
            return await host.continuation_matches(binding, continuation, session=writer)

        async def commit():
            queued.set()
            return await host.ordinary.commit(admission, admission_allowed=allowed)

        async with original_writer():
            task = asyncio.create_task(commit())
            await queued.wait()
            await asyncio.sleep(0)
            assert not task.done()
            assert item.controller.observe_unit_hint(
                unit,
                SelfReport(
                    run_ref=original.admission.activation_id,
                    sequence=2,
                    response_id="quiet-while-writer-queued",
                    at=item.controller.state.events[f"event:{original.event.id}"].at,
                    delta=SelfDelta(engage="quiet"),
                ),
            )
        try:
            selected = await task
        except OrdinaryAdmissionConflict as error:
            assert str(error) == "ordinary_participation_preparation_changed"
            assert write_failure_committed(error) is False
            continuation = await host.prepare_continuation(binding)
            selected = await host.ordinary.commit(admission, admission_allowed=allowed)
        assert not selected
        assert statements
        plans = []
        async with database.read_session() as reader:
            connection = await reader.connection()
            for sql, parameters in statements:
                plans.extend(
                    row[3]
                    for row in (
                        await connection.exec_driver_sql("EXPLAIN QUERY PLAN " + sql, parameters)
                    ).all()
                )
        assert not any("SCAN persons" in plan and "USING" not in plan for plan in plans)
        assert await host.ordinary.get(event.id) is None
        assert (
            await host.ordinary.get(original.event.id)
        ).activation_id == original.admission.activation_id
    finally:
        if listening:
            sql_event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await host.close()


async def test_current_person_roots_are_read_after_writer_admission(database, tmp_path):
    host, _item, _a, _original, _unit = await _relationship(database, tmp_path)
    try:
        event = await _event_and_route(database, host.ledger, content="original current root")
        binding, _reason = await host.continuation_for_event(event.id)
        continuation = await host.prepare_continuation(binding)
        current_root = str(uuid4())
        now = datetime.now(UTC)
        async with database.immediate_session() as writer:
            writer.add(CanonicalPersonModel(id=current_root, created_at=now, updated_at=now))
            await writer.flush()
            person = await writer.get(CanonicalPersonModel, event.author_person_id)
            person.merged_into_person_id = current_root
        with pytest.raises(
            OrdinaryAdmissionConflict, match="ordinary_participation_preparation_changed"
        ) as caught:
            async with database.immediate_session() as writer:
                await host.continuation_matches(binding, continuation, session=writer)
        assert write_failure_committed(caught.value) is False
        current = await host.prepare_continuation(binding)
        async with database.immediate_session() as writer:
            person = await writer.get(CanonicalPersonModel, event.author_person_id)
            assert person.merged_into_person_id == current_root
            assert await host.continuation_matches(binding, current, session=writer)
    finally:
        await host.close()


async def test_self_target_enters_selection_only_after_actual_person_family_merge(
    database, tmp_path
):
    host, item, _a, _original, _unit = await _relationship(database, tmp_path)
    try:
        event = await _event_and_route(database, host.ledger, content="new self target")
        binding, _reason = await host.continuation_for_event(event.id)
        old = await host.prepare_continuation(binding)
        checkpoint = item.controller._participation_checkpoint()
        source = next(iter(checkpoint.units.values()))
        async with database.immediate_session() as writer:
            target = await ensure_person(writer, "synthetic-competitor")
        self_binding = source.binding.model_copy(
            update={
                "actor": "SELF",
                "unit": source.binding.unit.model_copy(update={"target": target}),
            }
        )
        item.controller._put_participation_unit(
            checkpoint,
            item.controller._unit_key(self_binding.unit.thread, target),
            source.model_copy(update={"binding": self_binding}),
        )
        item.controller._save_participation(checkpoint)

        async with database.immediate_session() as writer:
            assert await host.continuation_matches(binding, old, session=writer)
        async with database.immediate_session() as writer:
            assert await merge_persons(writer, event.author_person_id, target) == target
        with pytest.raises(
            OrdinaryAdmissionConflict, match="ordinary_participation_preparation_changed"
        ) as caught:
            async with database.immediate_session() as writer:
                await host.continuation_matches(binding, old, session=writer)
        assert write_failure_committed(caught.value) is False
        assert await host.ordinary.get(event.id) is None

        current = await host.prepare_continuation(binding)
        identity = await actor_identity(host, item, (event.author_person_id, target))
        focus = item.controller.state.events[f"event:{event.id}"]
        view = item.controller.participation_view(focus, time.time(), actor_identity=identity)
        assert any(unit.binding.actor == "SELF" for unit in view.candidates)
        expected = bool(
            view.matched_unit is not None
            and view.matched_unit.unit.thread == binding.unit_key
            and identity(view.matched_unit.unit.target) == identity(binding.target_hint)
        )
        async with database.immediate_session() as writer:
            assert await host.continuation_matches(binding, current, session=writer) is expected
        assert await host.ordinary.get(event.id) is None
    finally:
        await host.close()
