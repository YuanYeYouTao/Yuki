"""Bounded protocol maintenance, exact cached bytes and real file/SQLite races."""

import asyncio
import hashlib
import json
import threading
import time
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import delete, event, select, update
from tests.support.work_session import WorkSession
from tests.unit.test_work_protocol_continuity import _control

from qq_ai_bot.domain.messages import (
    ChatMessage,
    ProviderContinuation,
    ToolCall,
    ToolFunction,
)
from qq_ai_bot.runtime import protocol_store
from qq_ai_bot.runtime.protocol_schema import objects, refs
from qq_ai_bot.runtime.protocol_store import ProtocolStore, _finish_thread
from qq_ai_bot.runtime.work_journal import encode_transcript
from qq_ai_bot.services.turn_transcript import TurnTranscript


async def _publish(store, owner, value):
    digest = await store.put(value)
    async with store.publication(owner) as prepared:
        async with store.database.immediate_session() as writer:
            await store.publish_refs(writer, owner, prepared)
    return digest


@pytest.mark.asyncio
async def test_owned_metadata_pages_advance_with_exact_indexed_sql(database, tmp_path):
    control = await _control(database, tmp_path)
    store = ProtocolStore(database)
    # Identical timestamps force the digest tie-breaker; owned pages must progress.
    for index in range(130):
        await _publish(store, control.current["id"], {"owned": index})
    target = await store.put({"unowned": "later metadata page"})
    async with store.publication(control.current["id"]) as prepared:
        async with database.immediate_session() as writer:
            await store.publish_refs(writer, control.current["id"], prepared)
    async with database.immediate_session() as writer:
        await writer.execute(update(objects).values(prepared_at=1))
        await writer.execute(delete(refs).where(refs.c.sha256 == target))
        # Sort this unowned row after the full first metadata page.
        await writer.execute(
            update(objects).where(objects.c.sha256 == target).values(prepared_at=2)
        )
    captured = []

    def capture(connection, cursor, statement, parameters, context, many):
        if (
            statement.startswith("SELECT runtime_protocol_objects.")
            and "LIMIT" in statement
            and "ORDER BY" in statement
        ):
            captured.append((statement, parameters))

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        assert await store.cleanup(grace_seconds=0) == 0
        assert await store.cleanup(grace_seconds=0) == 0
        assert await store.cleanup(grace_seconds=0) == 1
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    assert not store._path(target).exists()
    # Explain the actual ORM-generated ceiling/cursor queries, with original binds.
    assert any(" > " in sql for sql, _ in captured)
    assert any(" <= " in sql for sql, _ in captured)
    assert all("NOT (EXISTS" not in sql for sql, _ in captured)
    async with database.engine.connect() as reader:
        for sql, parameters in captured:
            plan = await reader.exec_driver_sql("EXPLAIN QUERY PLAN " + sql, parameters)
            details = " ".join(str(row[3]) for row in plan)
            assert "ix_protocol_objects_gc_cursor" in details
            assert "SCAN runtime_protocol_objects" not in details
            assert "TEMP B-TREE" not in details


@pytest.mark.asyncio
async def test_gc_waiting_for_writer_does_not_hold_file_lock(database, tmp_path, monkeypatch):
    control = await _control(database, tmp_path)
    store = ProtocolStore(database)
    digest = await _publish(store, control.current["id"], {"orphan": 1})
    async with database.immediate_session() as writer:
        await writer.execute(delete(refs).where(refs.c.sha256 == digest))
    discovered = asyncio.Event()
    original_page = store._gc_page

    async def page(*args):
        result = await original_page(*args)
        if result:
            discovered.set()
        return result

    monkeypatch.setattr(store, "_gc_page", page)
    async with database.immediate_session() as competing_writer:
        await competing_writer.execute(
            update(objects).where(objects.c.sha256 == digest).values(byte_size=objects.c.byte_size)
        )
        cleanup = asyncio.create_task(store.cleanup(grace_seconds=0))
        await asyncio.wait_for(discovered.wait(), 2)
        # A put is SQL-free and progresses while GC awaits the real second writer.
        assert await asyncio.wait_for(store.put({"foreground": "progress"}), 2)
    assert await cleanup == 1


@pytest.mark.asyncio
async def test_publication_wins_after_gc_candidate_read(database, tmp_path, monkeypatch):
    control = await _control(database, tmp_path)
    store = ProtocolStore(database)
    value = {"shared": "original bytes"}
    digest = await _publish(store, control.current["id"], value)
    async with database.immediate_session() as writer:
        await writer.execute(delete(refs).where(refs.c.sha256 == digest))
    original_page = store._gc_page
    republished = False

    async def page(*args):
        nonlocal republished
        result = await original_page(*args)
        if result and not republished:
            republished = True
            # Another store publishes before GC's writer; CAS must preserve its ref.
            await _publish(ProtocolStore(database), control.current["id"], value)
        return result

    monkeypatch.setattr(store, "_gc_page", page)
    assert await store.cleanup(grace_seconds=0) == 0
    assert await store.get(digest) == value
    async with database.sessions() as reader:
        assert await reader.scalar(select(refs.c.sha256).where(refs.c.sha256 == digest)) == digest


@pytest.mark.asyncio
async def test_gc_mark_wins_and_blocks_new_refs(database, tmp_path, monkeypatch):
    control = await _control(database, tmp_path)
    store = ProtocolStore(database)
    value = {"original": "object"}
    digest = await _publish(store, control.current["id"], value)
    async with database.immediate_session() as writer:
        await writer.execute(delete(refs).where(refs.c.sha256 == digest))
    entered = threading.Event()
    release = threading.Event()
    original_unlink = store._unlink_objects

    def unlink(rows):
        entered.set()
        assert release.wait(5)
        return original_unlink(rows)

    monkeypatch.setattr(store, "_unlink_objects", unlink)
    cleanup = asyncio.create_task(store.cleanup(grace_seconds=0))
    assert await asyncio.to_thread(entered.wait, 2)
    # Test the durable fence directly: preparation does not grant ref ownership.
    async with database.immediate_session() as writer:
        with pytest.raises(ValueError, match="work_protocol_reference_deleting"):
            await store.publish_refs(
                writer,
                control.current["id"],
                (
                    {
                        "sha256": digest,
                        "byte_size": store._path(digest).stat().st_size,
                        "prepared_at": time.time(),
                        "deleting": False,
                    },
                ),
            )
    release.set()
    assert await cleanup == 1
    assert await _publish(store, control.current["id"], value) == digest


@pytest.mark.asyncio
async def test_gc_cancellation_joins_old_unlink_before_second_gc(database, tmp_path, monkeypatch):
    control = await _control(database, tmp_path)
    store = ProtocolStore(database)
    value = {"cancel": "keep original deletion identity"}
    digest = await _publish(store, control.current["id"], value)
    async with database.immediate_session() as writer:
        await writer.execute(delete(refs).where(refs.c.sha256 == digest))
    entered = threading.Event()
    release = threading.Event()
    original_unlink = store._unlink_objects

    def unlink(rows):
        entered.set()
        assert release.wait(5)
        return original_unlink(rows)

    monkeypatch.setattr(store, "_unlink_objects", unlink)
    cleanup = asyncio.create_task(store.cleanup(grace_seconds=0))
    assert await asyncio.to_thread(entered.wait, 2)
    cleanup.cancel()
    await asyncio.sleep(0)  # Deliver cancellation, not a timing correctness threshold.
    cleanup.cancel()
    second = asyncio.create_task(ProtocolStore(database).cleanup(grace_seconds=0))
    await asyncio.sleep(0)
    assert store._lock.locked()
    assert not cleanup.done() and not second.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await cleanup
    # File gone, metadata unacknowledged: second GC resumes its existing barrier.
    assert await second == 1
    assert await _publish(store, control.current["id"], value) == digest
    assert await store.get(digest) == value


@pytest.mark.asyncio
async def test_thread_failure_during_cancel_preserves_cancellation():
    entered = threading.Event()
    release = threading.Event()

    def failing():
        entered.set()
        assert release.wait(5)
        raise OSError("controlled failure")

    pending = asyncio.create_task(_finish_thread(failing))
    assert await asyncio.to_thread(entered.wait, 2)
    pending.cancel()
    await asyncio.sleep(0)
    pending.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError) as cancelled:
        await pending
    assert "protocol_thread_cleanup_failed:OSError" in cancelled.value.__notes__


@pytest.mark.asyncio
async def test_cancelled_publish_finishes_file_before_releasing_shared_lock(database, monkeypatch):
    store = ProtocolStore(database)
    content = b"original immutable publication"
    digest = hashlib.sha256(content).hexdigest()
    entered = threading.Event()
    release = threading.Event()
    original_publish = store._publish

    def publish(*args):
        entered.set()
        assert release.wait(5)
        original_publish(*args)

    monkeypatch.setattr(store, "_publish", publish)
    pending = asyncio.create_task(store.put_bytes(content))
    assert await asyncio.to_thread(entered.wait, 2)
    pending.cancel()
    await asyncio.sleep(0)
    following = asyncio.create_task(ProtocolStore(database).put_bytes(content))
    await asyncio.sleep(0)
    assert store._lock.locked() and not following.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert await following == digest
    assert await store.get_bytes(digest) == content


@pytest.mark.asyncio
@pytest.mark.parametrize("lost_at", [1, 2])
async def test_gc_commit_confirmation_lost_recovers_existing_barrier(
    database, tmp_path, monkeypatch, lost_at
):
    control = await _control(database, tmp_path)
    store = ProtocolStore(database)
    value = {"unknown commit": "same immutable object"}
    digest = await _publish(store, control.current["id"], value)
    async with database.immediate_session() as writer:
        await writer.execute(delete(refs).where(refs.c.sha256 == digest))
    original = database.immediate_session
    calls = 0

    @asynccontextmanager
    async def uncertain_commit():
        nonlocal calls
        calls += 1
        async with original() as writer:
            yield writer
        if calls == lost_at:
            raise RuntimeError("commit_confirmation_lost")

    monkeypatch.setattr(database, "immediate_session", uncertain_commit)
    with pytest.raises(RuntimeError, match="commit_confirmation_lost"):
        await store.cleanup(grace_seconds=0)
    monkeypatch.setattr(database, "immediate_session", original)
    assert await ProtocolStore(database).cleanup(grace_seconds=0) == (1 if lost_at == 1 else 0)
    assert not store._path(digest).exists()
    assert await _publish(store, control.current["id"], value) == digest
    assert await store.get(digest) == value


@pytest.mark.asyncio
async def test_record_cache_has_byte_and_count_budgets_and_chain_lifetime(database, monkeypatch):
    monkeypatch.setattr(protocol_store, "_CACHE_RECORD_BYTES", 600)
    monkeypatch.setattr(protocol_store, "_CACHE_ENTRIES", 2)
    store = ProtocolStore(database)
    store.begin_record_chain("original_work", "original_chain")
    for index in range(5):
        await store.put_record(ChatMessage("user", str(index) + "x" * 50))
        assert len(store._records) <= 2
        assert store._record_bytes <= 600
    assert store._records
    store.begin_record_chain("original_work", "replacement_chain")
    assert not store._records and store._record_bytes == 0


@pytest.mark.asyncio
async def test_cached_checkpoint_repairs_missing_file_and_rejects_changed_bytes(
    database, tmp_path, monkeypatch
):
    control = await _control(database, tmp_path)
    session = WorkSession(control, "fixed")
    original = ChatMessage("user", "unchanged record")
    transcript = await session.restore(TurnTranscript((original,)))
    store = session.journal.objects
    encodings = []
    original_encode = store._encoded_record

    def encode(record):
        encodings.append(record)
        return original_encode(record)

    monkeypatch.setattr(store, "_encoded_record", encode)
    await session.save("paired")
    digest = store._records[id(original)][1]
    encodings.clear()
    await session.save("paired")
    assert encodings == []
    added = ChatMessage("assistant", "new actual record")
    transcript.append(added)
    await session.save("paired")
    assert encodings == [added]
    store._path(digest).unlink()
    encodings.clear()
    await session.save("paired")
    assert encodings == [original]
    assert hashlib.sha256(await store.get_bytes(digest)).hexdigest() == digest
    store._path(digest).write_bytes(b"changed object")
    with pytest.raises(ValueError, match="work_protocol_object_corrupt"):
        await session.save("paired")
    assert not store._prepared_sources


@pytest.mark.asyncio
async def test_failed_journal_keeps_extra_protocol_ownership_without_encoded_bytes(
    database, tmp_path, monkeypatch
):
    control = await _control(database, tmp_path)
    session = WorkSession(control, "fixed")
    await session.restore(TurnTranscript((ChatMessage("user", "original objective"),)))
    store = session.journal.objects
    extra = await store.put({"previous protocol": "opaque pre-compaction chain"})
    original = store.publish_refs

    async def fail_after_refs(*args):
        await original(*args)
        raise RuntimeError("original publication rolled back")

    monkeypatch.setattr(store, "publish_refs", fail_after_refs)
    with pytest.raises(RuntimeError, match="rolled back"):
        await session.save("paired")
    assert extra in store.prepared_refs and not store._prepared_sources
    monkeypatch.setattr(store, "publish_refs", original)
    await session.save("paired")
    async with database.sessions() as reader:
        assert (
            await reader.scalar(
                select(refs.c.sha256).where(
                    refs.c.work_id == control.current["id"],
                    refs.c.sha256 == extra,
                )
            )
            == extra
        )
    assert not store.prepared_refs and not store._prepared_sources


@pytest.mark.asyncio
async def test_failed_extra_object_missing_file_rejects_without_recreating_refs(database, tmp_path):
    control = await _control(database, tmp_path)
    store = ProtocolStore(database)
    extra = await store.put({"previous protocol": "original source"})
    with pytest.raises(RuntimeError, match="publication failed"):
        async with store.publication(control.current["id"]):
            raise RuntimeError("publication failed")
    store._path(extra).unlink()
    with pytest.raises(ValueError, match="work_protocol_object_missing_source"):
        async with store.publication(control.current["id"]):
            pytest.fail("missing original file must not pass publication")
    assert extra in store.prepared_refs and not store._prepared_sources
    async with database.sessions() as reader:
        assert await reader.scalar(select(refs.c.sha256).where(refs.c.sha256 == extra)) is None
        assert (
            await reader.scalar(select(objects.c.sha256).where(objects.c.sha256 == extra)) is None
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["role", "tool_call_id", "function_arguments", "result"])
async def test_media_strings_outside_content_keep_refs_and_exact_protocol(
    database, tmp_path, field
):
    control = await _control(database, tmp_path)
    media = "data:image/png;base64,aW1hZ2U="
    transcript = TurnTranscript((ChatMessage("user", "ordinary initial record"),))
    if field == "result":
        transcript.accept(ProviderContinuation("test", "responses", {"opaque": [1, 2]}))
        transcript.append_result("original_call", media)
    else:
        kwargs = (
            {field: media}
            if field != "function_arguments"
            else {"tool_calls": (ToolCall("call", ToolFunction("original_function", media)),)}
        )
        transcript.append(ChatMessage(**{"role": "assistant", "content": "ordinary", **kwargs}))
    session = WorkSession(control, "fixed")
    await session.restore(transcript)
    expected = json.loads(json.dumps(encode_transcript(transcript)))
    await session.save("paired")
    await session.save("paired")
    digest = hashlib.sha256(media.encode()).hexdigest()
    async with database.sessions() as reader:
        owned = set(
            await reader.scalars(
                select(refs.c.sha256).where(refs.c.work_id == control.current["id"])
            )
        )
    assert digest in owned
    loaded = await session.journal.load(control.lease, control.current["id"], "fixed")
    assert json.loads(loaded.record["payload_json"])["transcript"] == expected
