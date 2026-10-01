"""Original Work refs, shared hashes and deletion fences own protocol files."""

import pytest
from sqlalchemy import select
from tests.unit.test_work_protocol_continuity import _control

from qq_ai_bot.domain.messages import ChatMessage
from qq_ai_bot.runtime.protocol_schema import objects, refs
from qq_ai_bot.runtime.protocol_store import ProtocolStore
from qq_ai_bot.runtime.work_session import WorkSession
from qq_ai_bot.services.turn_transcript import TurnTranscript


@pytest.mark.asyncio
async def test_active_shared_protocol_refs_survive_gc_and_privacy_releases_last_owner(
    database, tmp_path
):
    control = await _control(database, tmp_path)
    first = WorkSession(control, "same")
    await first.restore(TurnTranscript((ChatMessage("user", "private same bytes"),)))
    await first.save("paired")
    owner = control.current["id"]
    other = await control.repository.accept(
        control.lease, source_key="another", source={}, goal="same"
    )
    control.current = other
    second = WorkSession(control, "same")
    await second.restore(TurnTranscript((ChatMessage("user", "private same bytes"),)))
    await second.save("paired")
    async with database.sessions() as reader:
        first_refs = set(await reader.scalars(select(refs.c.sha256).where(refs.c.work_id == owner)))
        other_refs = set(
            await reader.scalars(select(refs.c.sha256).where(refs.c.work_id == other["id"]))
        )
    assert first_refs & other_refs
    assert await first.journal.objects.cleanup(grace_seconds=0) == 0
    await first.journal.invalidate(control.lease, owner)
    assert await first.journal.objects.cleanup(grace_seconds=0) == 0
    for digest in first_refs & other_refs:
        assert await second.journal.objects.get_bytes(digest)
    async with database.immediate_session() as writer:
        await control.repository.purge_scope(writer, control.lease.conversation_id)
    assert await second.journal.objects.cleanup(grace_seconds=0) > 0
    async with database.sessions() as reader:
        assert not list(await reader.scalars(select(refs.c.sha256)))
        assert not list(await reader.scalars(select(objects.c.sha256)))


@pytest.mark.asyncio
async def test_batch_admission_refuses_quota_without_losing_existing_owned_data(database, tmp_path):
    control = await _control(database, tmp_path)
    store = ProtocolStore(database, max_total_bytes=8)
    digest = await store.put("a")

    async def publish():
        async with store.publication(control.current["id"]) as prepared:
            async with database.immediate_session() as writer:
                await store.publish_refs(writer, control.current["id"], prepared)

    await publish()
    await store.put("too large")
    with pytest.raises(ValueError, match="work_protocol_storage_capacity"):
        await publish()
    assert await store.get(digest) == "a"
    assert await store.cleanup(grace_seconds=0) == 1
    assert await store.get(digest) == "a"


@pytest.mark.asyncio
async def test_deleting_fence_blocks_new_reference_and_gc_allows_safe_retry(database, tmp_path):
    control = await _control(database, tmp_path)
    store = ProtocolStore(database)
    digest = await store.put("a")
    from sqlalchemy import insert

    async with database.immediate_session() as writer:
        await writer.execute(
            insert(objects).values(sha256=digest, byte_size=3, prepared_at=0, deleting=True)
        )
    with pytest.raises(ValueError, match="work_protocol_reference_deleting"):
        async with store.publication(control.current["id"]) as prepared:
            async with database.immediate_session() as writer:
                await store.publish_refs(writer, control.current["id"], prepared)
    assert await store.cleanup() == 1
    # A rejected publication is retried from the same original Work snapshot.
    store.prepared_refs.clear()
    store.prepared_sizes.clear()
    assert await store.put("a") == digest
    async with store.publication(control.current["id"]) as prepared:
        async with database.immediate_session() as writer:
            await store.publish_refs(writer, control.current["id"], prepared)
    assert await store.get(digest) == "a"


@pytest.mark.asyncio
async def test_old_manifest_media_and_observations_are_owned_until_archive(database, tmp_path):
    control = await _control(database, tmp_path)
    session = WorkSession(control, "fixed")
    await session.restore(
        TurnTranscript((ChatMessage("system", "fixed"), ChatMessage("user", "goal"))),
        compaction_brief=ChatMessage("user", "goal"),
    )
    store = session.journal.objects
    historical = await store.put({"old protocol": "opaque"})
    session.progress["previous_protocol_ref"] = historical
    session.progress["model_observations"] = [{"usage": 1}]
    await session.save("paired")
    async with database.sessions() as reader:
        owned = set(
            await reader.scalars(
                select(refs.c.sha256).where(refs.c.work_id == control.current["id"])
            )
        )
    assert historical in owned
    assert len(owned) >= 5
    assert await store.cleanup(grace_seconds=0) == 0


@pytest.mark.asyncio
async def test_put_is_sql_free_and_repeated_checkpoint_does_not_rewrite_objects(database, tmp_path):
    from sqlalchemy import event

    control = await _control(database, tmp_path)
    session = WorkSession(control, "fixed")
    await session.restore(TurnTranscript((ChatMessage("user", "original goal"),)))
    statements = []

    def sql(connection, cursor, statement, parameters, context, many):
        statements.append(statement.lower())

    event.listen(database.engine.sync_engine, "before_cursor_execute", sql)
    try:
        await session.journal.objects.put({"private immutable evidence": 1})
        assert statements == []
        await session.save("paired")
        statements.clear()
        await session.save("paired")
        assert not any(
            statement.startswith(("insert", "update", "delete"))
            and "runtime_protocol_" in statement
            for statement in statements
        )
        assert not any("sum(" in statement for statement in statements)
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", sql)


@pytest.mark.asyncio
async def test_subagent_archive_releases_tree_refs_but_retains_shared_active_owner(
    database, tmp_path
):
    import time

    from sqlalchemy import update

    from qq_ai_bot.runtime.subagent_repository import SubagentRepository
    from qq_ai_bot.runtime.subagent_schema import children
    from qq_ai_bot.runtime.work_schema_v1 import work

    control = await _control(database, tmp_path)
    root = control.current["id"]
    workers = SubagentRepository(control.repository)
    child = await workers.start(
        control.lease, root, "spawn-for-archive", {"goal": "check", "output_kind": "answer"}
    )
    active = await control.repository.accept(
        control.lease, source_key="other-active-owner", source={}, goal="continue"
    )
    store = ProtocolStore(database)

    async def publish(owner, value):
        digest = await store.put(value)
        async with store.publication(owner) as prepared:
            async with database.immediate_session() as writer:
                await store.publish_refs(writer, owner, prepared)
        return digest

    root_digest = await publish(root, {"parent": "private protocol"})
    child_digest = await publish(child, {"child": "private protocol"})
    shared_digest = await publish(child, {"shared": "private protocol"})
    assert await publish(active["id"], {"shared": "private protocol"}) == shared_digest
    assert await store.cleanup(grace_seconds=0) == 0
    async with database.immediate_session() as writer:
        await writer.execute(
            update(work)
            .where(work.c.id.in_((root, child)))
            .values(state="completed", updated=time.time() - 8 * 86400)
        )
        await writer.execute(
            update(children).where(children.c.work_id == child).values(notified_revision=100)
        )
    await workers.maintain()
    async with database.sessions() as reader:
        assert not list(
            await reader.scalars(select(refs.c.sha256).where(refs.c.work_id.in_((root, child))))
        )
        assert await reader.scalar(
            select(children.c.archived_at).where(children.c.work_id == child)
        )
    assert await store.cleanup(grace_seconds=0) == 2
    assert not store._path(root_digest).exists()
    assert not store._path(child_digest).exists()
    assert await store.get(shared_digest) == {"shared": "private protocol"}
