"""P02 windows not covered elsewhere: object publication, ref commit, writer queue."""

import asyncio
import json
from dataclasses import replace

import pytest
from sqlalchemy import select, update
from tests.integration.test_code_boundary_publication import (
    PARENT,
    child,
    composed,
    publish,
    rows,
)
from tests.integration.test_invocation_crash_windows import facts, prepared

from qq_ai_bot.runtime.protocol_schema import objects
from qq_ai_bot.runtime.work_repository import WorkConflict
from qq_ai_bot.runtime.work_schema_v1 import scope


async def test_snapshot_file_publication_failure_registers_nothing(database, tmp_path):
    owner, binding = await composed(database, tmp_path)
    store = owner.journal.objects
    store.policy = replace(store.policy, object_max_bytes=8)
    with pytest.raises(ValueError, match="work_protocol_object_capacity"):
        await publish(owner, binding, child(binding.work_id), dump=b"x" * 64)
    found, owned, tools = await rows(database, binding.work_id)
    # T0 failed before the writer: no child intent, no checkpoint advance, no ref.
    assert set(found) == {PARENT} and not owned and tools == 0
    parent = json.loads(found[PARENT]["receipt_json"])["composition"]
    assert parent["snapshot_revision"] == 0


async def test_interrupted_ref_commit_rolls_back_child_and_checkpoint(database, tmp_path):
    owner, binding = await composed(database, tmp_path)
    store = owner.journal.objects
    original = store.publish_refs

    async def interrupted(session, work_id, prepared_objects):
        await original(session, work_id, prepared_objects)
        raise ValueError("work_protocol_reference_deleting")  # e.g. concurrent GC

    store.publish_refs = interrupted
    with pytest.raises(ValueError, match="work_protocol_reference_deleting"):
        await publish(owner, binding, child(binding.work_id))
    found, owned, _ = await rows(database, binding.work_id)
    assert set(found) == {PARENT} and not owned
    assert json.loads(found[PARENT]["receipt_json"])["composition"]["snapshot_revision"] == 0
    # The already-written file is kept as a safe orphan for maintenance, never deleted inline.
    async with database.sessions() as reader:
        assert await reader.scalar(select(objects.c.sha256)) is None
    store.publish_refs = original
    # A retry from the same revision succeeds once; nothing was half-published.
    snapshot_ref = await publish(owner, binding, child(binding.work_id))
    found, owned, _ = await rows(database, binding.work_id)
    assert snapshot_ref in owned and f"{PARENT}/child/0" in found


async def test_lease_expiring_while_queued_for_writer_admits_nothing(database, tmp_path):
    owner, invocation = await prepared(database, tmp_path)
    repository = owner.control.repository
    lease, identity = owner.control.lease, owner.control.current["id"]
    key = invocation.identity.operation_id
    holder_ready, release = asyncio.Event(), asyncio.Event()

    async def hold_writer():
        async with database.immediate_session() as writer:
            holder_ready.set()
            await release.wait()
            # The lease lapses while T2 waits behind this writer.
            await writer.execute(
                update(scope)
                .where(scope.c.conversation_id == lease.conversation_id)
                .values(lease_until=0)
            )

    holder = asyncio.create_task(hold_writer())
    await holder_ready.wait()
    admission = asyncio.create_task(repository.admit_dispatch(lease, identity, key))
    await asyncio.sleep(0.2)
    assert not admission.done()  # queued behind the writer reservation
    release.set()
    await holder
    with pytest.raises(WorkConflict, match="work_activation_obsolete"):
        await admission
    receipt, total, root = await facts(database, key)
    assert receipt["invocation"]["dispatch_started"] is False
    assert total == 0 and root is None
