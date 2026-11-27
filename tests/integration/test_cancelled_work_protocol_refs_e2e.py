"""E2E: closed Works never acquire new ``runtime_protocol_refs`` after closing (#728).

Replays the 2026-11-23 production finding (#651/#728): a running root Work was
``operator_cancelled``, its in-flight activation still persisted protocol objects,
and because ``protocol_refs_closed_work`` only fires on state transition, those
late ``runtime_protocol_refs`` stayed attached forever and protocol GC could never
reclaim the files.

Each scenario uses a real SQLite database, real ``ProtocolStore`` files on disk,
real ``WorkRepository`` / ``WorkJournal`` / ``manage_work`` transactions, and real
``ProtocolStore.cleanup`` / ``WorkScheduler`` maintenance ticks. Structured facts
are written to ``test-results/issue-728-cancelled-protocol-refs-e2e.json``.

Failure and regression cases written before the change:

- F1 (cancelled root in-flight journal save): a running root Work is cancelled
  via ``manage_work(..., "cancel")`` (``operator_cancelled``), releasing prior
  refs; its still-running activation then saves a journal checkpoint containing
  new protocol objects. Must leave 0 ``runtime_protocol_refs``, keep
  ``runtime_protocol_usage`` equal to ``SUM(runtime_protocol_objects.byte_size)``,
  and let real ``WorkScheduler`` GC reclaim every newly written file and object row.
- F2 (cancelled root late ``record_effect`` with media under tight capacity): a
  tool effect prepared before ``operator_cancelled`` finishes after cancellation
  with image pixels (``WorkJournal.record_effect`` -> ``WorkRepository.record_effect``
  with ``prepared_protocol``), even when ``total_max_bytes`` is already tight.
  The late receipt itself MUST commit cleanly (``state="accepted"``, business
  outcome preserved), leave 0 ``runtime_protocol_refs``, and let GC reclaim the
  newly written media protocol objects and disk files with exact usage accounting.
- F3 (completed root late ``record_effect``): a completed root Work (not in
  ``runtime_subagents``) receives a late ``record_effect`` with media protocol
  objects. Receipt commits cleanly, leaves 0 ``runtime_protocol_refs``, and GC
  reclaims the objects and files.
- F4 (cancelled worker late ``record_effect``): a cancelled subagent worker
  (present in ``runtime_subagents``, ``state="cancelled"``) receives a late
  ``record_effect`` with media protocol objects. Receipt commits cleanly, leaves
  0 ``runtime_protocol_refs``, and GC reclaims the objects and files.
- F5 (completed worker unarchived vs archived): an unarchived ``completed``
  subagent worker stays resumable and MUST keep its ``runtime_protocol_refs``
  through late ``record_effect`` / ``publish_refs`` and GC; once archived
  (``archived_at IS NOT NULL``), subsequent ``publish_refs`` MUST leave 0 refs
  and let GC reclaim the objects.
- F6 (failed and active Works stay resumable): ``failed``, ``running``, and
  ``waiting_external`` Works MUST retain their ``runtime_protocol_refs`` on
  ``record_effect`` / ``publish_refs`` and survive GC untouched.
- F7 (shared object & concurrent GC ``deleting=True`` protection):
  (a) when a cancelled Work publishes an object also owned by a live Work, the
      cancelled Work gets 0 refs while the live Work keeps its ref and file;
  (b) when a cancelled Work publishes an already-unowned object marked
      ``deleting=True`` by GC, it MUST NOT flip ``deleting`` back to ``False``
      or add a ref, and GC finishes reclaiming it.
"""

from __future__ import annotations

import json
import time
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, func, select, update
from tests.support.work_effect_results_helpers import owned_session
from tests.unit.test_work_protocol_continuity import _control

from yuki.admin.models import WorkStorageRuntimeConfig
from yuki.capabilities.media import MediaResultText
from yuki.domain.messages import ChatImage, ChatMessage, ToolCall, ToolFunction
from yuki.runtime.protocol_schema import objects, refs, usage
from yuki.runtime.protocol_store import ProtocolStore
from yuki.runtime.subagent_schema import children
from yuki.runtime.work_journal import WorkJournal
from yuki.runtime.work_management import manage_work
from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_scheduler import WorkScheduler
from yuki.runtime.work_schema_v1 import effects, work

ARTIFACT = (
    Path(__file__).resolve().parents[2]
    / "test-results"
    / "issue-728-cancelled-protocol-refs-e2e.json"
)
RESULTS: dict[str, Any] = {}
DAY = 86400.0


@pytest.fixture(scope="module", autouse=True)
def _write_artifact():
    yield
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(
        json.dumps(
            {
                "suite": "tests/integration/test_cancelled_work_protocol_refs_e2e.py",
                "issue": 728,
                "scenarios": RESULTS,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _image(control, tag: str) -> ChatImage:
    payload = f"issue-728-pixels-{tag}"
    return ChatImage(
        f"data:image/png;base64,{payload}",
        source="history",
        conversation_id=control.lease.conversation_id,
        generation=control.lease.generation,
        source_event_id=728,
        attachment_index=0,
        content_hash=sha256(payload.encode()).hexdigest(),
    )


async def _work_refs(database, work_id: str) -> set[str]:
    async with database.read_session() as reader:
        return set(await reader.scalars(select(refs.c.sha256).where(refs.c.work_id == work_id)))


async def _all_objects(database) -> set[str]:
    async with database.read_session() as reader:
        return set(await reader.scalars(select(objects.c.sha256)))


async def _usage(database) -> tuple[int, int]:
    async with database.read_session() as reader:
        counted = await reader.scalar(select(usage.c.byte_size).where(usage.c.id == 1))
        actual = await reader.scalar(select(func.coalesce(func.sum(objects.c.byte_size), 0)))
    return int(counted or 0), int(actual or 0)


async def _age_objects(database, digests: set[str], seconds_ago: float = 2 * DAY) -> None:
    if not digests:
        return
    old_ts = time.time() - seconds_ago
    async with database.immediate_session() as writer:
        await writer.execute(
            update(objects).where(objects.c.sha256.in_(digests)).values(prepared_at=old_ts)
        )


async def _cancel_via_manage_work(database, work_id: str) -> tuple[int, str]:
    repo = WorkRepository(database)
    row = await repo.get(work_id)
    assert row is not None
    async with database.immediate_session() as writer:
        return await manage_work(writer, work_id, row["revision"], "cancel")


@pytest.mark.asyncio
async def test_f1_cancelled_root_inflight_journal_save_leaves_no_refs_and_gc_reclaims(
    database, tmp_path
):
    control, session, _artifacts = await owned_session(database, tmp_path)
    store = ProtocolStore(database)
    work_id = control.current["id"]

    # Initial checkpoint while running produces protocol refs.
    call1 = ToolCall("c1", ToolFunction("read_file", '{"path":"a"}'))
    session.transcript.append(ChatMessage("user", "initial running turn"))
    session.transcript.append(ChatMessage("assistant", tool_calls=(call1,)))
    await session.save("response", (call1,))
    before_cancel_refs = await _work_refs(database, work_id)
    assert before_cancel_refs

    # Operator cancels the running root Work (same path as production a32d808c).
    _, new_state = await _cancel_via_manage_work(database, work_id)
    assert new_state == "cancelled"
    refs_right_after_cancel = await _work_refs(database, work_id)
    assert refs_right_after_cancel == set()

    # The still-running activation saves another checkpoint with new items.
    session.transcript.append_result(call1.id, '{"ok":true,"data":"late-inflight-output"}')
    await session.save("paired")

    refs_after_late_save = await _work_refs(database, work_id)
    objects_after_late_save = await _all_objects(database)
    counted_before_gc, actual_before_gc = await _usage(database)

    # Age objects past 24h grace and run a real WorkScheduler maintenance tick.
    await _age_objects(database, objects_after_late_save)
    paths = {digest: store._path(digest) for digest in objects_after_late_save}

    async def _noop_resume(_item):
        return None

    scheduler = WorkScheduler(WorkRepository(database), _noop_resume, chat_admission_enabled=True)
    scheduler._last_reclaim = float("-inf")
    ticks = 0
    while await _all_objects(database) and ticks < 10:
        ticks += 1
        await scheduler.dispatch_once()

    remaining_objects = await _all_objects(database)
    remaining_files = sum(path.exists() for path in paths.values())
    counted_after_gc, actual_after_gc = await _usage(database)

    RESULTS["F1_cancelled_root_inflight_journal_save"] = {
        "before_cancel_ref_count": len(before_cancel_refs),
        "refs_after_cancel": len(refs_right_after_cancel),
        "refs_after_late_save": len(refs_after_late_save),
        "objects_registered_after_late_save": len(objects_after_late_save),
        "usage_before_gc": counted_before_gc,
        "actual_before_gc": actual_before_gc,
        "scheduler_ticks": ticks,
        "remaining_objects_after_gc": len(remaining_objects),
        "remaining_files_after_gc": remaining_files,
        "usage_after_gc": counted_after_gc,
        "actual_after_gc": actual_after_gc,
    }

    assert refs_after_late_save == set()
    assert counted_before_gc == actual_before_gc
    assert remaining_objects == set()
    assert remaining_files == 0
    assert counted_after_gc == actual_after_gc == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("tight_capacity", [False, True])
async def test_f2_cancelled_root_late_record_effect_persists_receipt_without_refs(
    database, tmp_path, tight_capacity
):
    control = await _control(database, tmp_path)
    work_id = control.current["id"]
    key = f"effect:f2-cancelled-late-media-{tight_capacity}"
    await control.repository.prepare_effect(control.lease, work_id, key, "tool")

    # Cancel the root Work before the already-dispatched tool finishes.
    _, new_state = await _cancel_via_manage_work(database, work_id)
    assert new_state == "cancelled"

    if tight_capacity:
        # Simulate storage capacity pressure (as on 2026-11-20): late receipt
        # must still commit cleanly instead of rolling back on storage quota.
        tight_policy = WorkStorageRuntimeConfig(total_max_bytes=1)
        database.protocol_storage_policy = lambda: _return_async(tight_policy)
    journal = WorkJournal(control.repository)
    late_image = _image(control, f"f2-late-cancelled-{tight_capacity}")
    await journal.record_effect(
        key,
        "accepted",
        {
            "result": MediaResultText('{"ok":true,"late":"preserved"}', (late_image,)),
            "outcome": {"ok": True, "tool": "inspect_image", "executed": True},
        },
    )

    async with database.read_session() as reader:
        effect_row = (
            (await reader.execute(select(effects).where(effects.c.effect_key == key)))
            .mappings()
            .one()
        )
    saved_receipt = json.loads(effect_row["receipt_json"])
    refs_after = await _work_refs(database, work_id)
    objects_before_gc = await _all_objects(database)
    counted_before, actual_before = await _usage(database)

    store = ProtocolStore(database)
    paths = {digest: store._path(digest) for digest in objects_before_gc}
    reclaimed = await store.cleanup(grace_seconds=0)
    objects_after_gc = await _all_objects(database)
    counted_after, actual_after = await _usage(database)

    scenario_key = (
        "F2_cancelled_root_late_record_effect_tight_cap"
        if tight_capacity
        else "F2_cancelled_root_late_record_effect_normal_cap"
    )
    RESULTS[scenario_key] = {
        "effect_state": effect_row["state"],
        "receipt_has_media_ref": bool(saved_receipt.get("media_result_ref")),
        "receipt_outcome_ok": saved_receipt.get("outcome", {}).get("ok"),
        "refs_after_late_effect": len(refs_after),
        "objects_before_gc": len(objects_before_gc),
        "reclaimed": reclaimed,
        "objects_after_gc": len(objects_after_gc),
        "files_left": sum(path.exists() for path in paths.values()),
        "usage_before_gc": counted_before,
        "actual_before_gc": actual_before,
        "usage_after_gc": counted_after,
        "actual_after_gc": actual_after,
    }

    assert effect_row["state"] == "accepted"
    assert saved_receipt["outcome"]["ok"] is True
    assert saved_receipt["media_result_ref"]
    assert refs_after == set()
    assert counted_before == actual_before
    assert reclaimed == len(objects_before_gc) and reclaimed >= 2
    assert objects_after_gc == set()
    assert not any(path.exists() for path in paths.values())
    assert counted_after == actual_after == 0


@pytest.mark.asyncio
async def test_f3_completed_root_late_record_effect_leaves_no_refs_and_gc_reclaims(
    database, tmp_path
):
    control = await _control(database, tmp_path)
    work_id = control.current["id"]
    key = "effect:f3-completed-root-late-media"
    await control.repository.prepare_effect(control.lease, work_id, key, "tool")

    # Complete the root Work (not a subagent).
    await control.repository.transition(
        control.lease, work_id, control.current["revision"], "completed"
    )
    assert await _work_refs(database, work_id) == set()

    journal = WorkJournal(control.repository)
    await journal.record_effect(
        key,
        "accepted",
        {
            "result": MediaResultText('{"ok":true}', (_image(control, "f3-completed-root"),)),
            "outcome": {"ok": True, "tool": "inspect_image", "executed": True},
        },
    )

    async with database.read_session() as reader:
        effect_state = await reader.scalar(
            select(effects.c.state).where(effects.c.effect_key == key)
        )
    refs_after = await _work_refs(database, work_id)
    objects_before = await _all_objects(database)
    store = ProtocolStore(database)
    paths = {digest: store._path(digest) for digest in objects_before}
    reclaimed = await store.cleanup(grace_seconds=0)
    counted_after, actual_after = await _usage(database)

    RESULTS["F3_completed_root_late_record_effect"] = {
        "effect_state": effect_state,
        "refs_after_late_effect": len(refs_after),
        "objects_before_gc": len(objects_before),
        "reclaimed": reclaimed,
        "files_left": sum(path.exists() for path in paths.values()),
        "usage_after_gc": counted_after,
        "actual_after_gc": actual_after,
    }

    assert effect_state == "accepted"
    assert refs_after == set()
    assert reclaimed == len(objects_before) and reclaimed >= 2
    assert not any(path.exists() for path in paths.values())
    assert counted_after == actual_after == 0


@pytest.mark.asyncio
async def test_f4_cancelled_worker_late_record_effect_leaves_no_refs_and_gc_reclaims(
    database, tmp_path
):
    control = await _control(database, tmp_path, worker=True)
    worker_id = control.current["id"]
    key = "effect:f4-cancelled-worker-late-media"
    await control.repository.prepare_effect(control.lease, worker_id, key, "tool")

    _, new_state = await _cancel_via_manage_work(database, worker_id)
    assert new_state == "cancelled"

    journal = WorkJournal(control.repository)
    await journal.record_effect(
        key,
        "accepted",
        {
            "result": MediaResultText('{"ok":true}', (_image(control, "f4-cancelled-worker"),)),
            "outcome": {"ok": True, "tool": "inspect_image", "executed": True},
        },
    )

    refs_after = await _work_refs(database, worker_id)
    objects_before = await _all_objects(database)
    store = ProtocolStore(database)
    reclaimed = await store.cleanup(grace_seconds=0)
    counted_after, actual_after = await _usage(database)

    RESULTS["F4_cancelled_worker_late_record_effect"] = {
        "refs_after_late_effect": len(refs_after),
        "objects_before_gc": len(objects_before),
        "reclaimed": reclaimed,
        "usage_after_gc": counted_after,
        "actual_after_gc": actual_after,
    }

    assert refs_after == set()
    assert reclaimed == len(objects_before) and reclaimed >= 2
    assert counted_after == actual_after == 0


@pytest.mark.asyncio
async def test_f5_completed_worker_keeps_refs_until_archived_then_rejects_new_refs(
    database, tmp_path
):
    control = await _control(database, tmp_path, worker=True)
    worker_id = control.current["id"]
    key = "effect:f5-completed-worker"
    await control.repository.prepare_effect(control.lease, worker_id, key, "tool")

    # Completed but NOT archived worker stays resumable -> MUST keep late refs.
    async with database.immediate_session() as writer:
        await writer.execute(update(work).where(work.c.id == worker_id).values(state="completed"))

    journal = WorkJournal(control.repository)
    await journal.record_effect(
        key,
        "accepted",
        {
            "result": MediaResultText('{"ok":true}', (_image(control, "f5-unarchived-worker"),)),
            "outcome": {"ok": True, "tool": "inspect_image", "executed": True},
        },
    )
    unarchived_refs = await _work_refs(database, worker_id)
    store = ProtocolStore(database)
    unarchived_reclaimed = await store.cleanup(grace_seconds=0)

    # Once archived (archived_at set and prior refs deleted), new publish_refs must NOT add refs.
    async with database.immediate_session() as writer:
        await writer.execute(
            update(children).where(children.c.work_id == worker_id).values(archived_at=time.time())
        )
        await writer.execute(delete(refs).where(refs.c.work_id == worker_id))

    archived_store = ProtocolStore(database)
    late_digest = await archived_store.put({"late": "after-worker-archive"})
    async with archived_store.publication(worker_id) as prepared:
        async with database.immediate_session() as writer:
            await archived_store.publish_refs(writer, worker_id, prepared)

    archived_refs = await _work_refs(database, worker_id)
    archived_reclaimed = await archived_store.cleanup(grace_seconds=0)
    counted_after, actual_after = await _usage(database)

    RESULTS["F5_completed_worker_unarchived_vs_archived"] = {
        "unarchived_ref_count": len(unarchived_refs),
        "unarchived_reclaimed": unarchived_reclaimed,
        "archived_ref_count": len(archived_refs),
        "archived_late_digest_exists": archived_store._path(late_digest).exists(),
        "archived_reclaimed": archived_reclaimed,
        "usage_after_gc": counted_after,
        "actual_after_gc": actual_after,
    }

    assert len(unarchived_refs) >= 2
    assert unarchived_reclaimed == 0
    assert archived_refs == set()
    assert not archived_store._path(late_digest).exists()
    assert archived_reclaimed >= 3
    assert counted_after == actual_after == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["running", "waiting_external", "failed"])
async def test_f6_resumable_works_keep_refs_on_record_effect(database, tmp_path, state):
    control = await _control(database, tmp_path)
    work_id = control.current["id"]
    key = f"effect:f6-{state}"
    await control.repository.prepare_effect(control.lease, work_id, key, "tool")
    async with database.immediate_session() as writer:
        await writer.execute(update(work).where(work.c.id == work_id).values(state=state))

    journal = WorkJournal(control.repository)
    await journal.record_effect(
        key,
        "accepted",
        {
            "result": MediaResultText('{"ok":true}', (_image(control, f"f6-{state}"),)),
            "outcome": {"ok": True, "tool": "inspect_image", "executed": True},
        },
    )

    kept_refs = await _work_refs(database, work_id)
    store = ProtocolStore(database)
    reclaimed = await store.cleanup(grace_seconds=0)
    counted, actual = await _usage(database)

    RESULTS[f"F6_resumable_{state}"] = {
        "kept_ref_count": len(kept_refs),
        "reclaimed": reclaimed,
        "usage": counted,
        "actual": actual,
    }

    assert len(kept_refs) >= 2
    assert reclaimed == 0
    assert counted == actual and counted > 0


@pytest.mark.asyncio
async def test_f7_shared_live_object_and_gc_deleting_object_are_protected(database, tmp_path):
    # Create a parent (live) and worker (will be cancelled).
    worker_control = await _control(database, tmp_path, worker=True)
    worker_id = worker_control.current["id"]
    async with database.read_session() as reader:
        live_id = await reader.scalar(select(work.c.parent_work_id).where(work.c.id == worker_id))
    assert live_id is not None

    # (a) Shared object owned by live_id.
    live_store = ProtocolStore(database)
    shared_payload = {"shared": "owned-by-live-work"}
    shared_digest = await live_store.put(shared_payload)
    async with live_store.publication(live_id) as prepared:
        async with database.immediate_session() as writer:
            await live_store.publish_refs(writer, live_id, prepared)

    # (b) Unowned object already marked deleting=True by a concurrent GC pass.
    deleting_payload = {"deleting": "already-marked-by-gc"}
    deleting_digest = await live_store.put(deleting_payload)
    async with live_store.publication(live_id) as prepared:
        async with database.immediate_session() as writer:
            await live_store.publish_refs(writer, live_id, prepared)
    async with database.immediate_session() as writer:
        await writer.execute(
            delete(refs).where(refs.c.work_id == live_id, refs.c.sha256 == deleting_digest)
        )
        await writer.execute(
            update(objects).where(objects.c.sha256 == deleting_digest).values(deleting=True)
        )

    # Cancel worker_id, then let a late publish_refs touch both digests.
    _, new_state = await _cancel_via_manage_work(database, worker_id)
    assert new_state == "cancelled"

    late_store = ProtocolStore(database)
    assert await late_store.put(shared_payload) == shared_digest
    assert await late_store.put(deleting_payload) == deleting_digest
    async with late_store.publication(worker_id) as prepared:
        async with database.immediate_session() as writer:
            await late_store.publish_refs(writer, worker_id, prepared)

    worker_refs = await _work_refs(database, worker_id)
    live_refs = await _work_refs(database, live_id)
    async with database.read_session() as reader:
        deleting_flag_after_late_publish = await reader.scalar(
            select(objects.c.deleting).where(objects.c.sha256 == deleting_digest)
        )

    reclaimed = await live_store.cleanup(grace_seconds=0)
    counted, actual = await _usage(database)

    RESULTS["F7_shared_and_deleting_protection"] = {
        "cancelled_worker_refs": len(worker_refs),
        "live_work_refs": len(live_refs),
        "deleting_flag_preserved": bool(deleting_flag_after_late_publish),
        "reclaimed": reclaimed,
        "shared_still_readable": (await live_store.get(shared_digest)) == shared_payload,
        "deleting_file_exists": live_store._path(deleting_digest).exists(),
        "usage": counted,
        "actual": actual,
    }

    assert worker_refs == set()
    assert live_refs == {shared_digest}
    assert deleting_flag_after_late_publish is True
    assert reclaimed == 1
    assert not live_store._path(deleting_digest).exists()
    assert await live_store.get(shared_digest) == shared_payload
    assert counted == actual and counted > 0


async def _return_async[T](value: T) -> T:
    return value
