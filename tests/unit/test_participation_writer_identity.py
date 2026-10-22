"""Current participation survives FIFO waiting without a writer-side actor history scan."""

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from tests.support.semantic_participation_host_helpers import _event_and_route
from tests.unit.test_participation_pipeline_retirement import _relationship
from yuki_participation.self_report import SelfDelta, SelfReport

from yuki.conversation.ordinary_admission import OrdinaryAdmissionConflict
from yuki.identity.db_models import CanonicalPersonModel
from yuki.persistence.database import write_failure_committed
from yuki.services import participation_ordinary


async def test_queued_quiet_uses_current_predicate_without_collecting_actor_history(
    database, tmp_path, monkeypatch
):
    host, item, _a, original, unit = await _relationship(database, tmp_path)
    in_writer = ContextVar("participation_test_writer", default=False)
    original_writer = database.immediate_session
    collect = participation_ordinary.actor_ids
    collected = []

    @asynccontextmanager
    async def tracked_writer():
        async with original_writer() as writer:
            token = in_writer.set(True)
            try:
                yield writer
            finally:
                in_writer.reset(token)

    def outside_writer(*args, **kwargs):
        assert not in_writer.get()
        collected.append(True)
        return collect(*args, **kwargs)

    task = None
    try:
        event = await _event_and_route(database, host.app.ledger, content="queued qualification")
        binding, reason = await host.continuation_for_event(event.id)
        admission = host.ordinary.prepare(event, item.scene.generation, 2, binding, reason=reason)
        monkeypatch.setattr(database, "immediate_session", tracked_writer)
        monkeypatch.setattr(participation_ordinary, "actor_ids", outside_writer)
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
        assert not await task
        assert collected == [True]
        assert await host.ordinary.get(event.id) is None
        assert (
            await host.ordinary.get(original.event.id)
        ).activation_id == original.admission.activation_id
    finally:
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await host.close()


async def test_current_person_roots_are_read_after_writer_admission(
    database, tmp_path, monkeypatch
):
    host, _item, _a, _original, _unit = await _relationship(database, tmp_path)
    try:
        event = await _event_and_route(database, host.app.ledger, content="original current root")
        binding, _reason = await host.continuation_for_event(event.id)
        continuation = await host.prepare_continuation(binding)
        current_root = str(uuid4())
        now = datetime.now(UTC)
        async with database.immediate_session() as writer:
            writer.add(CanonicalPersonModel(id=current_root, created_at=now, updated_at=now))
            await writer.flush()
            person = await writer.get(CanonicalPersonModel, event.author_person_id)
            person.merged_into_person_id = current_root
        read_roots = participation_ordinary.current_person_ids
        observed = []

        async def current_roots(writer, actors):
            roots = await read_roots(writer, actors)
            observed.append(roots[event.author_person_id])
            return roots

        monkeypatch.setattr(participation_ordinary, "current_person_ids", current_roots)
        async with database.immediate_session() as writer:
            assert await host.continuation_matches(binding, continuation, session=writer)
        assert observed == [current_root]
    finally:
        await host.close()


async def test_new_self_target_reprepares_once_and_preserves_absent_person_identity(
    database, tmp_path
):
    host, item, _a, _original, _unit = await _relationship(database, tmp_path)
    try:
        event = await _event_and_route(database, host.app.ledger, content="new self target")
        binding, reason = await host.continuation_for_event(event.id)
        admission = host.ordinary.prepare(event, item.scene.generation, 2, binding, reason=reason)
        old = await host.prepare_continuation(binding)
        checkpoint = item.controller._participation_checkpoint()
        source = next(iter(checkpoint.units.values()))
        target = str(uuid4())
        self_binding = source.binding.model_copy(
            update={
                "actor": "SELF",
                "unit": source.binding.unit.model_copy(update={"target": target}),
            }
        )
        checkpoint.units[f"retained-self:{target}"] = source.model_copy(
            update={"binding": self_binding}
        )
        item.controller._save_participation(checkpoint)

        async def stale_allowed(writer):
            return await host.continuation_matches(binding, old, session=writer)

        with pytest.raises(
            OrdinaryAdmissionConflict, match="ordinary_participation_preparation_changed"
        ) as caught:
            await host.ordinary.commit(admission, admission_allowed=stale_allowed)
        assert write_failure_committed(caught.value) is False
        assert await host.ordinary.get(event.id) is None

        current = await host.prepare_continuation(binding)
        assert target in current.actors

        async def current_allowed(writer):
            return await host.continuation_matches(binding, current, session=writer)

        assert await host.ordinary.commit(admission, admission_allowed=current_allowed)
        assert (
            await host.ordinary.get(event.id)
        ).activation_id == admission.admission.activation_id
    finally:
        await host.close()
