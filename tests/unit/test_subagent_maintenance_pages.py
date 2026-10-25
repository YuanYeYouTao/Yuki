"""Missed result repair keeps exact revisions and the parent's real wait state."""

import asyncio
import json
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar

import pytest
from sqlalchemy import insert, select, update
from tests.support.subagents_helpers import stack

from yuki.runtime import subagent_repository
from yuki.runtime.subagent_schema import children
from yuki.runtime.work_repository import WorkConflict
from yuki.runtime.work_schema_v1 import inputs, work
from yuki.runtime.work_wait_schema import waits


async def settled_children(database, tmp_path, count=35):
    repo, workers, parent_lease, parent, identity = await stack(database, tmp_path)
    async with database.read_session() as reader:
        original = dict(
            (await reader.execute(select(work).where(work.c.id == identity))).mappings().one()
        )
        child = dict(
            (await reader.execute(select(children).where(children.c.work_id == identity)))
            .mappings()
            .one()
        )
    rows = [
        {
            **original,
            "id": f"child-{index:04}",
            "source_key": f"source-{index}",
            "revision": 7,
            "state": "completed",
            "checkpoint_json": json.dumps({"sync_result": f"result-{index}", "note": "x" * 10000}),
        }
        for index in range(count)
    ]
    async with database.immediate_session() as writer:
        await writer.execute(
            update(children)
            .where(children.c.work_id == identity)
            .values(notified_revision=original["revision"])
        )
        await writer.execute(insert(work), rows)
        await writer.execute(
            insert(children),
            [{**child, "work_id": row["id"], "source_key": row["source_key"]} for row in rows],
        )
    return repo, workers, parent_lease, parent, rows


async def result_with_execution_owner(database, tmp_path, state="completed"):
    repo, workers, parent_lease, parent, identity = await stack(database, tmp_path)
    lease = await workers.acquire(identity)
    assert lease is not None
    await repo.checkpoint(lease, identity, {"sync_result": "result-0"})
    current = await repo.get(identity)
    original = await repo.transition(lease, identity, current["revision"], state)
    return repo, workers, parent_lease, parent, [original], lease


async def test_multiple_pages_repair_once_and_keep_committed_work_unchanged(
    database, tmp_path, monkeypatch
):
    repo, workers, parent_lease, _parent, originals = await settled_children(
        database, tmp_path, 140
    )
    immediate = database.immediate_session
    writer_active = ContextVar("maintenance_writer", default=False)
    commits = 0
    loads = json.loads

    def decode(value):
        assert not writer_active.get()
        return loads(value)

    @asynccontextmanager
    async def observed_writer():
        nonlocal commits
        async with immediate() as writer:
            token = writer_active.set(True)
            try:
                yield writer
            finally:
                writer_active.reset(token)
        commits += 1

    monkeypatch.setattr(
        subagent_repository, "json", type("Json", (), {"loads": staticmethod(decode)})
    )
    monkeypatch.setattr(database, "immediate_session", observed_writer)
    await workers.maintain()
    first_commits = commits
    await workers.maintain()
    async with database.read_session() as reader:
        notifications = list(
            await reader.scalars(
                select(inputs.c.source_key)
                .where(inputs.c.kind == "subagent")
                .order_by(inputs.c.created, inputs.c.id)
            )
        )
        for original in originals:
            current = dict(
                (await reader.execute(select(work).where(work.c.id == original["id"])))
                .mappings()
                .one()
            )
            assert current == original
        records = (
            (
                await reader.execute(
                    select(children).where(children.c.work_id.in_([row["id"] for row in originals]))
                )
            )
            .mappings()
            .all()
        )
    assert set(notifications) == {f"worker-result:{row['id']}:7" for row in originals}
    assert notifications == [f"worker-result:{row['id']}:7" for row in originals]
    assert len(notifications) == 140
    assert all(
        row["owner"] is None and row["lease_until"] == 0 and row["notified_revision"] == 7
        for row in records
    )
    assert first_commits < 140 and commits == first_commits
    await repo.release(parent_lease)


async def test_changed_revision_is_reprepared_and_terminal_parent_is_not_revived(
    database, tmp_path, monkeypatch
):
    repo, workers, parent_lease, parent, originals = await settled_children(database, tmp_path, 1)
    identity = originals[0]["id"]
    immediate = database.immediate_session
    changed = False

    @asynccontextmanager
    async def before_writer():
        nonlocal changed
        if not changed:
            changed = True
            async with immediate() as writer:
                await writer.execute(
                    update(work)
                    .where(work.c.id == identity)
                    .values(revision=8, checkpoint_json='{"sync_result":"new result"}')
                )
                await writer.execute(
                    update(work).where(work.c.id == parent["id"]).values(state="completed")
                )
        async with immediate() as writer:
            yield writer

    monkeypatch.setattr(database, "immediate_session", before_writer)
    await workers.maintain()
    assert (await repo.get(parent["id"]))["state"] == "completed"
    async with database.read_session() as reader:
        keys = list(
            await reader.scalars(select(inputs.c.source_key).where(inputs.c.kind == "subagent"))
        )
        result = json.loads(
            await reader.scalar(
                select(children.c.result_json).where(children.c.work_id == identity)
            )
        )
    assert keys == [f"worker-result:{identity}:8"]
    assert result["text"] == "new result"
    await repo.release(parent_lease)


@pytest.mark.parametrize("change", ["opaque", "true_to_int", "missing_to_null"])
async def test_result_leaf_is_the_actual_cas_dependency(database, tmp_path, monkeypatch, change):
    repo, workers, parent_lease, _parent, originals = await settled_children(database, tmp_path, 1)
    identity = originals[0]["id"]
    async with database.immediate_session() as writer:
        await writer.execute(
            update(work)
            .where(work.c.id == identity)
            .values(
                state="waiting_user",
                checkpoint_json="{}"
                if change == "missing_to_null"
                else '{"reason":true,"opaque":"original"}',
            )
        )
    immediate = database.immediate_session
    prepare = subagent_repository._prepare_result
    prepared = []
    changed = False

    def prepare_result(row):
        prepared.append(row["result_leaf"])
        return prepare(row)

    @asynccontextmanager
    async def before_writer():
        nonlocal changed
        if not changed:
            changed = True
            await repo.checkpoint(
                parent_lease,
                identity,
                {
                    "reason": True
                    if change == "opaque"
                    else None
                    if change == "missing_to_null"
                    else 1,
                    "opaque": "changed" * 10000,
                },
            )
        async with immediate() as writer:
            yield writer

    monkeypatch.setattr(subagent_repository, "_prepare_result", prepare_result)
    monkeypatch.setattr(database, "immediate_session", before_writer)
    await workers.maintain()
    async with database.read_session() as reader:
        result = json.loads(
            await reader.scalar(
                select(children.c.result_json).where(children.c.work_id == identity)
            )
        )
    assert result["text"] == {"opaque": "True", "true_to_int": "1", "missing_to_null": ""}[change]
    assert (
        prepared
        == {"opaque": ["true"], "true_to_int": ["true", "1"], "missing_to_null": [None, "null"]}[
            change
        ]
    )
    assert (await repo.get(identity))["revision"] == 7
    await repo.release(parent_lease)


async def test_json_leaf_types_keep_original_result_rendering(database, tmp_path):
    values = [
        None,
        True,
        1,
        False,
        0,
        -0.0,
        1.2e-7,
        10**100,
        {"b": True, "a": [None, "中😀"]},
        '"\\\n',
    ]
    cases = [
        (state, value) for state in ("completed", "waiting_user", "failed") for value in values
    ]
    repo, workers, parent_lease, _parent, originals = await settled_children(
        database, tmp_path, len(cases)
    )
    async with database.immediate_session() as writer:
        for row, (state, value) in zip(originals, cases, strict=True):
            await writer.execute(
                update(work)
                .where(work.c.id == row["id"])
                .values(
                    state=state,
                    checkpoint_json=json.dumps(
                        {"sync_result": value, "reason": value}, ensure_ascii=False
                    ),
                    reason="row fallback",
                )
            )
    await workers.maintain()
    async with database.read_session() as reader:
        for row, (state, value) in zip(originals, cases, strict=True):
            result = json.loads(
                await reader.scalar(
                    select(children.c.result_json).where(children.c.work_id == row["id"])
                )
            )
            assert result["text"] == str(
                (value if state != "failed" else value or "row fallback") or ""
            )
    await repo.release(parent_lease)


@pytest.mark.parametrize("active_wait", [False, True])
async def test_parent_wait_keeps_its_original_subscription(database, tmp_path, active_wait):
    repo, workers, parent_lease, parent, _originals = await settled_children(database, tmp_path, 1)
    async with database.immediate_session() as writer:
        await writer.execute(
            update(work).where(work.c.id == parent["id"]).values(state="waiting_external")
        )
        if active_wait:
            await writer.execute(
                insert(waits).values(
                    id="original-wait",
                    work_id=parent["id"],
                    conversation_id=parent["conversation_id"],
                    generation=parent["generation"],
                    principal_kind="self",
                    principal_id="self",
                    call_key="original-wait-key",
                    request_json="{}",
                    mode="any",
                    conditions_json="[]",
                    status="active",
                    created=time.time(),
                    updated=time.time(),
                )
            )
    await workers.maintain()
    assert (await repo.get(parent["id"]))["state"] == (
        "waiting_external" if active_wait else "queued"
    )
    async with database.read_session() as reader:
        assert (
            len(list(await reader.scalars(select(inputs.c.id).where(inputs.c.kind == "subagent"))))
            == 1
        )
        if active_wait:
            assert (
                await reader.scalar(select(waits.c.status).where(waits.c.id == "original-wait"))
                == "active"
            )
    await repo.release(parent_lease)


async def test_busy_child_does_not_take_a_maintenance_writer(database, tmp_path, monkeypatch):
    repo, workers, parent_lease, _parent, originals = await settled_children(database, tmp_path, 1)
    identity = originals[0]["id"]
    async with database.immediate_session() as writer:
        await writer.execute(
            update(children)
            .where(children.c.work_id == identity)
            .values(owner="real-other-owner", lease_until=time.time() + 60)
        )
    immediate = database.immediate_session

    @asynccontextmanager
    async def unexpected_writer():
        raise AssertionError("an already leased child is not an available notification")
        yield

    monkeypatch.setattr(database, "immediate_session", unexpected_writer)
    await workers.maintain()
    monkeypatch.setattr(database, "immediate_session", immediate)
    async with database.read_session() as reader:
        row = (
            (await reader.execute(select(children).where(children.c.work_id == identity)))
            .mappings()
            .one()
        )
    assert row["owner"] == "real-other-owner" and row["notified_revision"] == 0
    await repo.release(parent_lease)


@pytest.mark.parametrize("state", ["completed", "failed", "cancelled"])
async def test_real_terminal_checkpoint_rejects_both_child_and_scope_owners(
    database, tmp_path, state
):
    (
        repo,
        workers,
        parent_lease,
        _parent,
        originals,
        child_lease,
    ) = await result_with_execution_owner(database, tmp_path, state)
    identity = originals[0]["id"]
    for lease in (child_lease, parent_lease):
        with pytest.raises(WorkConflict, match=r"work_(activation|checkpoint)_obsolete"):
            await repo.checkpoint(lease, identity, {"sync_result": "not an accepted write"})
    if state == "completed":
        await workers.finish(child_lease)
    else:
        with pytest.raises(WorkConflict, match="work_activation_obsolete"):
            await workers.finish(child_lease)
        await workers.maintain()
    await repo.release(child_lease)
    await repo.release(parent_lease)


@pytest.mark.parametrize("public_finish", [False, True])
async def test_terminal_resume_between_read_and_publish_never_posts_the_old_result(
    database, tmp_path, monkeypatch, public_finish
):
    if public_finish:
        repo, workers, parent_lease, parent, originals, lease = await result_with_execution_owner(
            database, tmp_path
        )
    else:
        repo, workers, parent_lease, parent, originals = await settled_children(
            database, tmp_path, 1
        )
        lease = None
    identity = originals[0]["id"]
    immediate = database.immediate_session
    resumed = False

    @asynccontextmanager
    async def resume_before_writer():
        nonlocal resumed
        if not resumed:
            resumed = True
            await workers.resume(parent_lease, parent["id"], identity, key="real-resume")
        async with immediate() as writer:
            yield writer

    monkeypatch.setattr(database, "immediate_session", resume_before_writer)
    if public_finish:
        await workers.finish(lease)
    else:
        await workers.maintain()
    current = await repo.get(identity)
    assert current["state"] == "queued" and current["revision"] == originals[0]["revision"] + 1
    async with database.read_session() as reader:
        assert not list(
            await reader.scalars(select(inputs.c.id).where(inputs.c.kind == "subagent"))
        )
    if lease is not None:
        await repo.release(lease)
    await repo.release(parent_lease)


@pytest.mark.parametrize("public_finish", [False, True])
async def test_real_archive_respects_the_child_lease_and_does_not_republish_cleared_body(
    database, tmp_path, monkeypatch, public_finish
):
    if public_finish:
        repo, workers, parent_lease, _parent, originals, lease = await result_with_execution_owner(
            database, tmp_path
        )
    else:
        repo, workers, parent_lease, _parent, originals = await settled_children(
            database, tmp_path, 1
        )
        lease = None
    identity = originals[0]["id"]
    async with database.immediate_session() as writer:
        await writer.execute(
            update(work).values(state="completed", updated=time.time() - 8 * 86400)
        )
    immediate = database.immediate_session
    archived = False

    @asynccontextmanager
    async def archive_before_writer():
        nonlocal archived
        if not archived:
            archived = True
            await workers.maintain()
        async with immediate() as writer:
            yield writer

    monkeypatch.setattr(database, "immediate_session", archive_before_writer)
    if public_finish:
        await workers.finish(lease)
    else:
        await workers.maintain()
    async with database.read_session() as reader:
        receipt = (
            (await reader.execute(select(children).where(children.c.work_id == identity)))
            .mappings()
            .one()
        )
    assert (receipt["archived_at"] is None) == public_finish
    assert json.loads(receipt["result_json"])["text"] == "result-0"
    assert (await repo.get(identity))["checkpoint_json"] == (
        originals[0]["checkpoint_json"] if public_finish else "{}"
    )
    if lease is not None:
        await repo.release(lease)
    await repo.release(parent_lease)


async def test_expired_execution_is_not_reauthorized_by_result_maintenance(database, tmp_path):
    repo, workers, parent_lease, _parent, originals, lease = await result_with_execution_owner(
        database, tmp_path, "waiting_user"
    )
    identity = originals[0]["id"]
    async with database.immediate_session() as writer:
        await writer.execute(
            update(children)
            .where(children.c.work_id == identity)
            .values(lease_until=time.time() - 1)
        )
    await workers.maintain()
    async with database.read_session() as reader:
        current = (
            (await reader.execute(select(children).where(children.c.work_id == identity)))
            .mappings()
            .one()
        )
    assert current["owner"] is None and current["lease_until"] == 0
    assert current["fence"] == lease.fence and current["cancel_epoch"] == lease.cancel_epoch
    assert current["notified_revision"] == originals[0]["revision"]
    assert not await repo.valid(lease) and not await repo.renew(lease)
    with pytest.raises(WorkConflict, match="work_activation_obsolete"):
        await repo.checkpoint(lease, identity, {"reason": "expired owner cannot change the result"})
    with pytest.raises(WorkConflict, match="work_activation_obsolete"):
        await workers.finish(lease)
    await repo.release(parent_lease)


async def test_new_execution_between_read_and_write_remains_busy_and_is_not_polled(
    database, tmp_path, monkeypatch
):
    repo, workers, parent_lease, parent, originals = await settled_children(database, tmp_path, 1)
    identity = originals[0]["id"]
    immediate = database.immediate_session
    active = None

    @asynccontextmanager
    async def execute_before_writer():
        nonlocal active
        if active is None:
            # Mark the hook before invoking the real writer-owning APIs.
            active = False
            await workers.resume(parent_lease, parent["id"], identity, key="new-execution")
            active = await workers.acquire(identity)
            await repo.checkpoint(active, identity, {"sync_result": "still running"})
        async with immediate() as writer:
            yield writer

    monkeypatch.setattr(database, "immediate_session", execute_before_writer)
    await asyncio.wait_for(workers.maintain(), 5)
    assert await repo.valid(active)
    current = await repo.get(identity)
    assert current["state"] == "running" and current["revision"] == 8
    async with database.read_session() as reader:
        assert not list(
            await reader.scalars(select(inputs.c.id).where(inputs.c.kind == "subagent"))
        )
        record = (
            (await reader.execute(select(children).where(children.c.work_id == identity)))
            .mappings()
            .one()
        )
    assert record["owner"] == active.owner and record["fence"] == active.fence
    assert record["notified_revision"] == 0
    await repo.release(active)
    await repo.release(parent_lease)


async def test_generation_change_between_read_and_write_does_not_post_or_loop(
    database, tmp_path, monkeypatch
):
    from yuki.conversation.hydrate import bump_canonical_generation
    from yuki.persistence.models import ChatEventModel

    repo, workers, parent_lease, parent, originals = await settled_children(database, tmp_path, 1)
    immediate = database.immediate_session
    async with database.read_session() as reader:
        event_id = await reader.scalar(select(ChatEventModel.id))
    reset = False

    @asynccontextmanager
    async def reset_before_writer():
        nonlocal reset
        if not reset:
            reset = True
            async with immediate() as writer:
                await bump_canonical_generation(
                    writer, parent["conversation_id"], event_id=event_id
                )
        async with immediate() as writer:
            yield writer

    monkeypatch.setattr(database, "immediate_session", reset_before_writer)
    await asyncio.wait_for(workers.maintain(), 5)
    async with database.read_session() as reader:
        assert not list(
            await reader.scalars(select(inputs.c.id).where(inputs.c.kind == "subagent"))
        )
        assert (
            await reader.scalar(
                select(children.c.notified_revision).where(children.c.work_id == originals[0]["id"])
            )
            == 0
        )
    assert not await repo.valid(parent_lease)
