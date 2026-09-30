"""Snapshot lock contention stays off the loop and cancellation preserves commit facts."""

import asyncio
import sqlite3
import threading

import pytest
from tests.unit.test_semantic_participation_host import _event_and_route, _host, _item
from yuki_participation.controller import Controller
from yuki_participation.models import Scope
from yuki_participation.store import SnapshotConflict, SnapshotStore

from qq_ai_bot.services.participation_snapshot import AsyncSnapshotStore


async def test_store_connection_lifetime_shares_worker_and_keeps_revision_cas(
    tmp_path, monkeypatch
):
    threads = []

    class CheckedStore(SnapshotStore):
        def __init__(self, path):
            threads.append(threading.get_ident())
            super().__init__(path)

        def load(self, scope):
            threads.append(threading.get_ident())
            return super().load(scope)

        def save(self, state, *, expected_revision):
            threads.append(threading.get_ident())
            return super().save(state, expected_revision=expected_revision)

        def close(self):
            threads.append(threading.get_ident())
            super().close()

    monkeypatch.setattr("qq_ai_bot.services.participation_snapshot.SnapshotStore", CheckedStore)
    store = await AsyncSnapshotStore.open(tmp_path / "snapshots.db")
    scope = Scope(conversation_id="scope", generation=1)
    payload = Controller(scope, 1).state.model_dump_json()
    try:
        assert await store.save(payload, expected_revision=0) == 1
        assert await store.load(scope) == (1, payload)
        with pytest.raises(SnapshotConflict):
            await store.save(payload, expected_revision=0)
    finally:
        await store.close()
        await store.close()
    assert len(set(threads)) == 1
    assert threads[0] != threading.get_ident()


async def test_snapshot_waiting_for_sqlite_writer_keeps_event_loop_responsive(tmp_path):
    path = tmp_path / "contended.db"
    store = await AsyncSnapshotStore.open(path)
    payload = Controller(Scope(conversation_id="scope", generation=1), 1).state.model_dump_json()
    blocker = sqlite3.connect(path)
    blocker.execute("BEGIN IMMEDIATE")
    task = asyncio.create_task(store.save(payload, expected_revision=0))
    try:
        # The save must still be waiting while this independent loop timer fires.
        await asyncio.sleep(0.05)
        assert not task.done()
        blocker.rollback()
        assert await asyncio.wait_for(task, 1) == 1
    finally:
        blocker.rollback()
        blocker.close()
        await asyncio.gather(task, return_exceptions=True)
        await store.close()


async def test_cancelled_host_save_freezes_payload_and_accounts_committed_revision(
    database, tmp_path, monkeypatch
):
    host, _ = await _host(database, tmp_path, observer=False)
    event = await _event_and_route(database, host.app.ledger)
    item = await _item(host, event)
    payload = item.controller.state.model_dump_json()
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    real_save = SnapshotStore.save
    first = True

    def blocked_save(store, state, *, expected_revision):
        nonlocal first
        if first:
            first = False
            loop.call_soon_threadsafe(started.set)
            assert release.wait(2), "test must release the worker"
        return real_save(store, state, expected_revision=expected_revision)

    monkeypatch.setattr(SnapshotStore, "save", blocked_save)
    task = asyncio.create_task(host._save(item))
    try:
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        item.controller.state = item.controller.state.model_copy(
            update={"now": item.controller.state.now + 1}
        )
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert item.revision == 1
        assert item.saved == payload
        assert await host._store.load(item.scene.scope) == (1, payload)
        await host._save(item)
        assert item.revision == 2
        assert item.saved == item.controller.state.model_dump_json()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await host.close()


async def test_concurrent_scope_loads_share_one_controller(database, tmp_path):
    host, _ = await _host(database, tmp_path, observer=False)
    event = await _event_and_route(database, host.app.ledger)
    scene = await host._scene(event.canonical_conversation_id)
    try:
        first, second = await asyncio.gather(host._session(scene), host._session(scene))
        assert first is second
        assert len(host._sessions) == 1
    finally:
        await host.close()


async def test_failed_commit_after_cancellation_still_stops_host_task(
    database, tmp_path, monkeypatch
):
    host, _ = await _host(database, tmp_path, observer=False)
    event = await _event_and_route(database, host.app.ledger)
    item = await _item(host, event)
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    real_save = SnapshotStore.save
    first = True

    def fail_first(store, state, *, expected_revision):
        nonlocal first
        if first:
            first = False
            loop.call_soon_threadsafe(started.set)
            assert release.wait(2), "test must release the worker"
            raise SnapshotConflict("snapshot_changed")
        return real_save(store, state, expected_revision=expected_revision)

    monkeypatch.setattr(SnapshotStore, "save", fail_first)
    task = asyncio.create_task(host._save(item))
    try:
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert item.revision == 0
        assert item.saved == ""
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await host.close()
