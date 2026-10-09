"""Bounded Host scope ownership across real async tick/legacy interleaving."""

import asyncio
import json
import time
from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update
from tests.conftest import make_settings
from tests.support.semantic_participation_host_helpers import _event_and_route, _host, _message
from yuki_participation.models import CandidateKind, Proposal

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.conversation.autonomy_binding import AutonomyOwner
from qq_ai_bot.conversation.autonomy_db_models import InitiativeRunModel
from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.identity.canonical_repository import ensure_space
from qq_ai_bot.identity.db_models import CanonicalSpaceModel
from qq_ai_bot.identity.errors import CanonicalIdentityError
from qq_ai_bot.runtime.work_control import WorkControl


async def test_tick_pins_waiters_and_retries_capacity_dirty_without_duplicate_controller(
    database, tmp_path
):
    host, _ = await _host(database, tmp_path, observer=False)
    started, release = asyncio.Event(), asyncio.Event()
    entered = []

    original_advance = host._advance_scene

    async def delayed(item, *, direct=None):
        entered.append(item)
        started.set()
        await release.wait()
        await original_advance(item, direct=direct)

    host._advance_scene = delayed
    task = None
    try:
        async with database.immediate_session() as session:
            for index in range(32):
                await ensure_space(
                    session, str(2000 + index), name=f"scope-{index}", require_mention=True
                )
        for index in range(32):
            event = await _event_and_route(database, host.app.ledger, group=str(2000 + index))
            await host._session(await host._scene(event.canonical_conversation_id))
        originals = dict(host._sessions)
        task = asyncio.create_task(host.tick())
        await started.wait()
        assert len(entered) == 2
        assert all(item.pins == 1 for item in originals.values())
        fresh = await _event_and_route(database, host.app.ledger, group="2999")
        message = _message(fresh)
        assert not await host.legacy_allowed(message)
        assert not await host.accept_legacy(message)
        assert host._dirty[message.conversation_id] == {fresh.id: False}
        assert all(host._sessions[key] is item for key, item in originals.items())
        assert len(host._sessions) == 32
        release.set()
        await task
        assert host._failures == 0
        assert len(entered) == 32
        assert all(item.pins == 0 for item in originals.values())
        await host.tick()
        key = (fresh.canonical_conversation_id, 1)
        current = host._sessions[key]
        assert f"event:{fresh.id}" in current.controller.state.events
        assert fresh.canonical_conversation_id not in host._dirty
        assert len(host._sessions) == 32
        assert host._failures == 0
        # Reset retires the old generation only after its last async holder exits.
        current.pins += 1
        async with database.immediate_session() as session:
            await session.execute(
                update(CanonicalConversationModel)
                .where(CanonicalConversationModel.id == fresh.canonical_conversation_id)
                .values(generation=2)
            )
        await host._retire_stale_sessions()
        assert host._sessions[key] is current
        current.pins -= 1
        await host._retire_stale_sessions()
        assert key not in host._sessions
        # Observation overload preserves existing scopes and records bounded loss.
        for index in range(128):
            host.observe_context(
                replace(message, conversation_id=str(uuid4()), source_event_id=index + 1),
                direct=False,
            )
        retained = set(host._dirty)
        host.observe_context(message, direct=False)
        assert set(host._dirty) == retained
        assert len(host._dirty) == 128
        assert host._dirty_overflows == 1
    finally:
        release.set()
        if task is not None:
            await task
        await host.close()


async def test_tick_pin_batch_waits_for_in_progress_cache_eviction(database, tmp_path):
    host, _ = await _host(database, tmp_path, observer=False)
    saving, release_save = asyncio.Event(), asyncio.Event()
    advancing, release_advance = asyncio.Event(), asyncio.Event()
    original_save = host._save
    eviction_task = tick_task = None
    try:
        async with database.immediate_session() as session:
            for index in range(33):
                await ensure_space(session, str(3000 + index), name=f"scope-{index}")
        for index in range(32):
            event = await _event_and_route(database, host.app.ledger, group=str(3000 + index))
            await host._session(await host._scene(event.canonical_conversation_id))
        oldest_key = next(iter(host._sessions))
        oldest = host._sessions[oldest_key]

        async def blocked_save(item):
            if item is oldest:
                saving.set()
                await release_save.wait()
            await original_save(item)

        async def advance(item, *, direct=None):
            advancing.set()
            await release_advance.wait()

        host._save = blocked_save
        host._advance_scene = advance
        # Isolate the pin batch from the earlier retirement/discovery lock acquisitions.
        host._retire_stale_sessions = AsyncMock()
        host._last_discovery_at = time.time()
        fresh = await _event_and_route(database, host.app.ledger, group="3032")
        scene = await host._scene(fresh.canonical_conversation_id)
        eviction_task = asyncio.create_task(host._session(scene))
        await saving.wait()
        tick_task = asyncio.create_task(host.tick())
        await asyncio.sleep(0)
        assert oldest.pins == 0
        assert not advancing.is_set()
        release_save.set()
        replacement = await eviction_task
        await advancing.wait()
        assert oldest_key not in host._sessions
        assert oldest.pins == 0
        assert replacement.pins == 1
        assert len(host._sessions) == 32
        release_advance.set()
        await tick_task
        assert host._failures == 0
    finally:
        release_save.set()
        release_advance.set()
        for task in (eviction_task, tick_task):
            if task is not None:
                await task
        await host.close()


@pytest.mark.parametrize("restart", [False, True])
async def test_disabled_scope_uses_real_global_policy_to_retire_selector_and_original_outbox(
    database, tmp_path, restart
):
    host, _ = await _host(database, tmp_path, observer=True)
    settings = make_settings(database.url, conversation_semantic_participation_enabled=True)
    host.app.settings = settings
    runtime_config = RuntimeConfigService(settings=settings, database=database)
    host.app.runtime_config = runtime_config
    requested_groups = []
    original_snapshot = runtime_config.snapshot

    async def snapshot(**kwargs):
        requested_groups.append(kwargs.get("group_id"))
        return await original_snapshot(**kwargs)

    runtime_config.snapshot = snapshot
    try:
        event = await _event_and_route(database, host.app.ledger)
        item = await host._session(await host._scene(event.canonical_conversation_id))
        await host._hydrate(item)
        binding = await host._binding(item)
        assert binding.effective_owner is AutonomyOwner.SEMANTIC
        assert requested_groups and all(group == "2001" for group in requested_groups)
        now = time.time()
        accepted = Proposal(
            proposal_id="original-accepted",
            scope=item.scene.scope,
            controller_epoch=binding.controller_epoch,
            kind=CandidateKind.INTRINSIC,
            thread="original-intrinsic",
            target_hint="group",
            sources=(),
            support=None,
            created_at=now,
            expires_at=now + 60,
        )
        item.controller.state.proposals[accepted.proposal_id] = accepted
        admitted = await host._admit_proposal(item, binding, accepted)
        assert admitted["outcome"] == "accepted"
        run_id = admitted["run_id"]
        pending = accepted.model_copy(update={"proposal_id": "old-unanswered"})
        item.controller.state.proposals[pending.proposal_id] = pending
        item.controller._set(pending=pending.proposal_id)
        await host._save(item)
        async with database.immediate_session() as session:
            await session.execute(
                update(CanonicalSpaceModel)
                .where(CanonicalSpaceModel.id == item.scene.space_id)
                .values(enabled=False)
            )
        # Live-scoped runtime reads remain forbidden; only the disabled Host's
        # control metadata read uses global policy to apply its existing OFF path.
        with pytest.raises(CanonicalIdentityError) as error:
            await original_snapshot(group_id="2001")
        assert error.value.category == "canonical_owner_disabled"
        if restart:
            # Simulate cold cache restore using the real persisted controller state.
            host._sessions.clear()
            host._last_discovery_at = 0
        requested_groups.clear()
        await host.tick()
        assert host._failures == 0
        current = await host.repository.get_binding(item.scene.conversation_id, 1)
        assert current.effective_owner is AutonomyOwner.OFF
        assert not current.master_enabled and current.external_enabled
        assert current.controller_epoch == binding.controller_epoch + 1
        assert requested_groups and all(group is None for group in requested_groups)
        current_item = host._sessions[(item.scene.conversation_id, 1)]
        assert current_item.controller.state.pending is None
        assert (
            current_item.controller.state.feedback["rejected:old-unanswered"].outcome == "rejected"
        )
        assert not host._observer.calls
        assert not current_item.observation.queue.in_flight
        assert (await host.repository.get_run(run_id)).state == "interrupted"
        assert await host.work.by_source(f"initiative:{run_id}") is None
        # A delayed legacy envelope from before disable follows the same actual
        # metadata read and cannot reopen a proposer or invoke an Agent.
        message = _message(event)
        assert not await host.legacy_allowed(message)
        assert not await host.accept_legacy(message)
        await host.tick()
        assert host._failures == 0 and not host._observer.calls
        assert all(group is None for group in requested_groups)
        async with database.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(InitiativeRunModel)) == 1
    finally:
        await host.close()


async def test_disabled_scope_preserves_completed_work_and_original_feedback(database, tmp_path):
    host, _ = await _host(database, tmp_path, observer=True)
    settings = make_settings(database.url, conversation_semantic_participation_enabled=True)
    host.app.settings = settings
    host.app.runtime_config = RuntimeConfigService(settings=settings, database=database)
    try:
        event = await _event_and_route(database, host.app.ledger)
        item = await host._session(await host._scene(event.canonical_conversation_id))
        await host._hydrate(item)
        binding = await host._binding(item)
        now = time.time()
        proposal = Proposal(
            proposal_id="original-completed-work",
            scope=item.scene.scope,
            controller_epoch=binding.controller_epoch,
            kind=CandidateKind.INTRINSIC,
            thread="original-completed",
            target_hint="group",
            sources=(),
            support=None,
            created_at=now,
            expires_at=now + 60,
        )
        item.controller.state.proposals[proposal.proposal_id] = proposal
        admitted = await host._admit_proposal(item, binding, proposal)
        assert admitted["outcome"] == "accepted"
        run = await host.repository.get_run(admitted["run_id"])
        await host._dispatch(run)
        row = await host.work.by_source(f"initiative:{run.run_id}")
        lease = await host.work.acquire(run.conversation_id, run.generation)
        await host.work.checkpoint(lease, row["id"], {"original": "retained"}, models=2)

        async def validate():
            assert await host.work.valid(lease)

        control = WorkControl(
            host.work,
            lease,
            row["source_key"],
            json.loads(row["source_json"]),
            validate,
            current=await host.work.get(row["id"]),
        )
        await control.execute(
            "task_control", {"action": "complete", "result": "original result"}, "original-complete"
        )
        await control.settle(pending_inputs=False)
        await host.work.release(lease)
        before = await host.work.get(row["id"])
        assert before["state"] == "completed"
        async with database.immediate_session() as session:
            await session.execute(
                update(CanonicalSpaceModel)
                .where(CanonicalSpaceModel.id == run.space_id)
                .values(enabled=False)
            )
        await host.tick()
        assert host._failures == 0 and not host._observer.calls
        assert (await host.repository.get_run(run.run_id)).state == "no_reply"
        assert (
            await host.repository.get_binding(run.conversation_id, run.generation)
        ).effective_owner is AutonomyOwner.OFF
        assert await host.work.get(row["id"]) == before
        assert item.controller.state.feedback[run.run_id].outcome == "no_reply"
        async with database.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(InitiativeRunModel)) == 1
    finally:
        await host.close()
