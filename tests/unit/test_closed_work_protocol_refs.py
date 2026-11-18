"""Protocol objects of a Work that can no longer resume are released to the existing GC.

Failure cases written before the change:

- a completed or cancelled root Work keeps its refs forever, so GC never reclaims files;
- a failed Work is resumable (``manage_work`` resume) and must keep its journal objects;
- a completed persistent worker can still be resumed by its parent until archived;
- a cancelled worker cannot be resumed or messaged, so it releases like a root;
- a running/queued Work keeps every ref;
- an object also referenced by another live Work survives GC;
- runtime_work, effects, inputs, waits and the journal row itself are never deleted;
- an upgraded database releases the refs of Works that were already closed.
"""

import asyncio
import sqlite3

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, update
from tests.unit.test_work_protocol_continuity import _control

from yuki.runtime.protocol_schema import objects, refs
from yuki.runtime.protocol_store import ProtocolStore
from yuki.runtime.work_schema_v1 import journal, work


async def _owned(database, work_id, value):
    store = ProtocolStore(database)
    digest = await store.put(value)
    async with store.publication(work_id) as prepared:
        async with database.immediate_session() as writer:
            await store.publish_refs(writer, work_id, prepared)
    return store, digest


async def _refs(database, work_id):
    async with database.read_session() as reader:
        return set(await reader.scalars(select(refs.c.sha256).where(refs.c.work_id == work_id)))


async def _object(database, digest):
    async with database.read_session() as reader:
        return await reader.scalar(select(objects.c.sha256).where(objects.c.sha256 == digest))


async def _set_state(database, work_id, state):
    async with database.immediate_session() as writer:
        await writer.execute(update(work).where(work.c.id == work_id).values(state=state))


@pytest.mark.parametrize("state", ["completed", "cancelled"])
async def test_closed_root_work_releases_refs_and_gc_reclaims_files(database, tmp_path, state):
    control = await _control(database, tmp_path)
    identity = control.current["id"]
    store, digest = await _owned(database, identity, {"paid": "closed root protocol"})
    path = store._path(digest)
    assert path.exists()
    await control.repository.transition(control.lease, identity, control.current["revision"], state)
    assert await _refs(database, identity) == set()
    assert await store.cleanup(grace_seconds=0) == 1
    assert not path.exists()
    assert await _object(database, digest) is None
    async with database.read_session() as reader:
        assert await reader.scalar(select(work.c.state).where(work.c.id == identity)) == state


@pytest.mark.parametrize("state", ["running", "queued", "waiting_user", "suspended", "failed"])
async def test_live_or_resumable_work_keeps_refs(database, tmp_path, state):
    control = await _control(database, tmp_path)
    identity = control.current["id"]
    store, digest = await _owned(database, identity, {"paid": f"{state} protocol"})
    await _set_state(database, identity, state)
    assert await _refs(database, identity) == {digest}
    assert await store.cleanup(grace_seconds=0) == 0
    assert await store.get(digest) == {"paid": f"{state} protocol"}


async def test_completed_worker_stays_resumable_but_cancelled_worker_releases(database, tmp_path):
    control = await _control(database, tmp_path, worker=True)
    identity = control.current["id"]
    store, digest = await _owned(database, identity, {"paid": "worker protocol"})
    await _set_state(database, identity, "completed")
    assert await _refs(database, identity) == {digest}
    assert await store.cleanup(grace_seconds=0) == 0
    await _set_state(database, identity, "cancelled")
    assert await _refs(database, identity) == set()
    assert await store.cleanup(grace_seconds=0) == 1


async def test_shared_object_survives_while_another_work_owns_it(database, tmp_path):
    control = await _control(database, tmp_path, worker=True)
    worker = control.current["id"]
    async with database.read_session() as reader:
        parent = await reader.scalar(select(work.c.parent_work_id).where(work.c.id == worker))
    store, digest = await _owned(database, parent, {"paid": "same content"})
    await _owned(database, worker, {"paid": "same content"})
    await _set_state(database, parent, "completed")
    assert await _refs(database, parent) == set()
    assert await _refs(database, worker) == {digest}
    assert await store.cleanup(grace_seconds=0) == 0
    assert await store.get(digest) == {"paid": "same content"}


async def test_closing_keeps_work_and_journal_rows(database, tmp_path):
    control = await _control(database, tmp_path)
    identity = control.current["id"]
    await _owned(database, identity, {"paid": "journal object"})
    async with database.immediate_session() as writer:
        await writer.execute(
            journal.insert().values(
                work_id=identity,
                chain_id="original-chain",
                contract="original-contract",
                source_revision=0,
                phase="paired",
                payload_json="{}",
                updated=0,
            )
        )
    await _set_state(database, identity, "completed")
    async with database.read_session() as reader:
        assert await reader.scalar(select(work.c.id).where(work.c.id == identity)) == identity
        assert (
            await reader.scalar(select(journal.c.work_id).where(journal.c.work_id == identity))
            == identity
        )


async def test_upgrade_releases_refs_of_already_closed_works(tmp_path, monkeypatch):
    path = tmp_path / "closed-refs.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path.as_posix()}")
    config = Config("alembic.ini")
    await asyncio.to_thread(command.upgrade, config, "0157")
    states = {
        "root-completed": ("completed", None, False),
        "root-cancelled": ("cancelled", None, False),
        "root-failed": ("failed", None, False),
        "root-running": ("running", None, False),
        "worker-completed": ("completed", "root-running", True),
        "worker-cancelled": ("cancelled", "root-running", True),
    }
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        for identity, (state, parent, worker) in states.items():
            connection.execute(
                "INSERT INTO runtime_work (id, conversation_id, generation, source_key,"
                " source_json, goal, revision, state, model_requests, tool_calls,"
                " active_seconds, sent_messages, checkpoint_json, created, updated,"
                " parent_work_id) VALUES (?, 'c', 1, ?, '{}', 'g', 1, ?, 0, 0, 0, 0, '{}',"
                " 0, 0, ?)",
                (identity, identity, state, parent),
            )
            if worker:
                connection.execute(
                    "INSERT INTO runtime_subagents (work_id, source_key, brief_json)"
                    " VALUES (?, ?, '{}')",
                    (identity, identity),
                )
            digest = identity.encode().hex().ljust(64, "0")
            connection.execute(
                "INSERT INTO runtime_protocol_objects (sha256, byte_size, prepared_at, deleting)"
                " VALUES (?, 1, 0, 0)",
                (digest,),
            )
            connection.execute(
                "INSERT INTO runtime_protocol_refs (work_id, sha256) VALUES (?, ?)",
                (identity, digest),
            )
    await asyncio.to_thread(command.upgrade, config, "head")
    with sqlite3.connect(path) as connection:
        remaining = {
            row[0] for row in connection.execute("SELECT work_id FROM runtime_protocol_refs")
        }
        works = {row[0] for row in connection.execute("SELECT id FROM runtime_work")}
    assert remaining == {"root-failed", "root-running", "worker-completed"}
    assert works == set(states)
