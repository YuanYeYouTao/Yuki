"""Bounded Host scope ownership across real async tick/legacy interleaving."""

import asyncio
from dataclasses import replace
from uuid import uuid4

from sqlalchemy import update
from tests.unit.test_semantic_participation_host import _event_and_route, _host, _message

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.identity.canonical_repository import ensure_space


async def test_tick_pins_waiters_and_retries_capacity_dirty_without_duplicate_controller(
    database, tmp_path
):
    host, _ = await _host(database, tmp_path, observer=False)
    started, release = asyncio.Event(), asyncio.Event()
    entered = []

    async def delayed(item):
        entered.append(item)
        started.set()
        await release.wait()
        host._save(item)

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
            host._session(await host._scene(event.canonical_conversation_id))
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
