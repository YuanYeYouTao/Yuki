"""E2E: abandoning a failed Work releases its protocol storage; the watchdog names holders.

Replays #666 (2026-11-20 production: 1141 MB of 1.43 GB held by 55 failed Works that
no path could release). Real SQLite files, real ``ProtocolStore`` files and GC, real
Main/Social/proactive turn (the watchdog E2E Scene), real personal and admin HTTP
(``app_env``), the real admin chat command (``stack``); a fake provider only. Failed
Works come from real turns whose model returns ``LLMInvalidRequestError``. Facts go to
``test-results/failed-work-abandon-storage-e2e.json`` with stable labels instead of IDs,
digests or times, so two runs write the same bytes.

Failure cases written before the implementation (task book §4):

Cancelling
F1  cancelling a failed Work returns ``precondition_failed`` at any entry;
F2  entries disagree (state, revision step, receipt or replay);
F3  ``management_view`` says ``cancel: false`` for failed, or true for completed/cancelled;
F4  failed -> cancelled keeps its refs, drops another Work's refs, or GC deletes an
    object a live Work still references;
F5  GC deletes inside the grace period or not after it; usage does not drop by exactly
    the reclaimed bytes;
F6  a cancelled Work can be resumed;
F7  concurrent resume and cancel on one revision both succeed, or the loser does not
    get ``version_conflict``;
F8  a stale revision still writes or touches refs; the device receipt lacks the problem;
F9  ``fail`` on failed, or anything on completed/cancelled, is accepted;
F10 live descendants are not cancelled; terminal descendants (failed/completed workers)
    change state or lose refs (decision Q5: no cascade);
F11 a cancelled failed worker's parent gets no ``worker-result``, or a suspended parent
    is revived;
F12 an archived failed root (journal and refs already gone) cannot be cancelled;
F13 a pending input or an active wait wakes the Work again after cancel;
F14 ``failure_json``, journal rows or effects are deleted or rewritten;
F15 a failed delegation record answers ``cancelled`` but stays failed, or entries
    disagree on it (Q6);
F16 a device cancels another person's failed Work instead of ``not_found``;
F17 after Yuki read a failed Work with ``get_tasks``, another entry resumed it, and the
    following ``manage_task cancel`` cancels the resumed Work (Q8);
F18 the revision check over-reaches: Yuki's own successful write makes its next call a
    ``version_conflict``; a cancel without a prior read is refused; a read leaks into
    the next activation; a Work read while not failed (whose own execution moves its
    revision) is bound, so cancelling it later fails;
F19 cancelling a failed record sends a native cancel/release or registers an operation.

F20 a read is bypassed: a delegation record ignores it, a subtask drops it before
    the call so a retry acts at the newest revision, a subtask or neutral resume never
    compares it, or a subtask conflict ends the whole turn;
F21 a failed delegation record opens more than cancel: releasing a currently failed
    record reaches the native service (or ends the record) instead of being refused
    first with the cancel-first hint; releasing a cancelled record or stopping an
    observation no longer behaves as on main; a cancel after another entry already
    ended the read record answers "cancelled" instead of ``version_conflict``;
F22 a neutral resume passes the read check, another entry resumes the Work before
    the enqueue writer, and the Work is queued again with Yuki's input.

Watchdog
W1  near the cap the facts lack failed holders; below the threshold they are computed;
W2  holders not ordered by ``held_bytes``; wrong bytes (a shared object counted as
    exclusive); inexact totals; more than five; no estimate wording (Q3, Q4);
W3  a goal or free text reaches the facts, ``facts_json`` or the model request (Q1);
W4  a tick changes any Work state/revision or ref;
W5  observing takes the writer;
W6  a failing holder query clears the storage issue or stops the check;
W7  the abandon runs with the notice turn's authority instead of the owner's turn.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select, update
from tests.conftest import MemorySender
from tests.integration.test_watchdog_capacity_incident_e2e import (
    CHECK,
    Scene,
    _call,
    _runtime_context,
    _secret_free,
    _tool_results,
)
from tests.support.social_identity_cases import social_env
from tests.unit.test_admin_api import TOKEN, command
from tests.unit.test_app_messages import admin as admin_fixture
from tests.unit.test_app_messages import app_env as app_env_fixture
from tests.unit.test_client_api import bearer
from tests.unit.test_client_works import owner, seeded
from tests.unit.test_group_route_recovery import message, stack

import yuki.application.watchdog as watchdog_module
from yuki.client_api.db_models import ClientDeviceModel
from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.domain.profiles import UserProfileSnapshot
from yuki.external_agents.db_models import AgentInstanceModel, AgentOperationModel
from yuki.llm.base import LLMInvalidRequestError
from yuki.runtime import protocol_store as protocol_store_module
from yuki.runtime.proactive_schema import watchdog_issues
from yuki.runtime.protocol_schema import objects, refs, usage
from yuki.runtime.protocol_store import ProtocolStore
from yuki.runtime.subagent_schema import children
from yuki.runtime.work_management import WorkManagementError, manage_work, management_view
from yuki.runtime.work_recovery_schema import recovery
from yuki.runtime.work_schema_v1 import inputs, journal, work
from yuki.runtime.work_wait_schema import waits
from yuki.services.policies import CommandName

ARTIFACT = (
    Path(__file__).resolve().parents[2] / "test-results" / "failed-work-abandon-storage-e2e.json"
)
RESULTS: dict[str, Any] = {}
# Fixtures of the personal and admin HTTP scene (S3).
admin = admin_fixture
app_env = app_env_fixture
DAY = 86400.0
KB = 1024


@pytest.fixture(scope="module", autouse=True)
def _write_artifact():
    yield
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    scenarios = json.dumps(RESULTS, ensure_ascii=False, indent=2, sort_keys=True)
    ARTIFACT.write_text(
        json.dumps(
            {
                "suite": "tests/integration/test_failed_work_abandon_storage_e2e.py",
                "suite_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "scenarios": RESULTS,
                "results_sha256": hashlib.sha256(scenarios.encode()).hexdigest(),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- helpers


async def _attach(database, work_id: str, label: str, size: int) -> str:
    """One protocol object owned by ``work_id``, published as a checkpoint does."""
    store = ProtocolStore(database)
    digest = await store.put({"label": label, "pad": "x" * size})
    async with store.publication(work_id) as prepared:
        async with database.immediate_session() as writer:
            await store.publish_refs(writer, work_id, prepared)
    return digest


async def _refs(database, work_id: str) -> set[str]:
    async with database.read_session() as reader:
        return set(await reader.scalars(select(refs.c.sha256).where(refs.c.work_id == work_id)))


async def _usage(database) -> int:
    async with database.read_session() as reader:
        return int(await reader.scalar(select(usage.c.byte_size).where(usage.c.id == 1)) or 0)


async def _row(database, work_id: str) -> dict[str, Any]:
    async with database.read_session() as reader:
        return dict(
            (await reader.execute(select(work).where(work.c.id == work_id))).mappings().one()
        )


async def _state(database) -> dict[str, Any]:
    """Every Work's (state, revision) and every ref: what a read-only tick must not move."""
    async with database.read_session() as reader:
        rows = (await reader.execute(select(work.c.id, work.c.state, work.c.revision))).all()
        owned = (await reader.execute(select(refs.c.work_id, refs.c.sha256))).all()
    return {
        "works": sorted((row.id, row.state, row.revision) for row in rows),
        "refs": sorted((row.work_id, row.sha256) for row in owned),
    }


async def _expected_holders(database) -> dict[str, Any]:
    """Independent arithmetic over refs/objects/states, compared with the facts."""
    async with database.read_session() as reader:
        owned = (await reader.execute(select(refs.c.work_id, refs.c.sha256))).all()
        sizes = dict((await reader.execute(select(objects.c.sha256, objects.c.byte_size))).all())
        states = dict((await reader.execute(select(work.c.id, work.c.state))).all())
    owners: dict[str, set[str]] = {}
    for work_id, digest in owned:
        owners.setdefault(digest, set()).add(work_id)
    failed = {work_id for work_id, state in states.items() if state == "failed"}
    held: dict[str, int] = {}
    exclusive: dict[str, int] = {}
    for digest, works in owners.items():
        for work_id in works & failed:
            held[work_id] = held.get(work_id, 0) + sizes[digest]
            if works == {work_id}:
                exclusive[work_id] = exclusive.get(work_id, 0) + sizes[digest]
    order = sorted(held, key=lambda work_id: (-held[work_id], -exclusive.get(work_id, 0), work_id))
    return {
        "order": order,
        "held": held,
        "exclusive": {work_id: exclusive.get(work_id, 0) for work_id in held},
        "total": len(held),
        "failed_only": sum(
            sizes[digest] for digest, works in owners.items() if works and works <= failed
        ),
    }


async def _fail_by_turn(scene: Scene, key: str, goal: str) -> str:
    """A real private turn: the task is registered, then the model rejects the request."""
    seen: list[Any] = []

    def ordinary(request):
        seen.append(request)
        if len(seen) == 1:
            return _call("manage_task", {"action": "start", "goal": goal}, f"{key}-start")
        raise LLMInvalidRequestError("SECRET-FAILURE provider rejected the request")

    scene.ordinary = ordinary
    before = {row["id"] for row in await scene.rows(work)}
    result = await scene.harness.processor.handle(scene.message(goal, key), MemorySender())
    scene.ordinary = lambda request: "好"
    assert result.reason == "chat", result
    failed = [
        row["id"]
        for row in await scene.rows(work, work.c.state == "failed")
        if row["id"] not in before
    ]
    assert len(failed) == 1, failed
    return str(failed[0])


async def _owner_turn(scene: Scene, key: str, calls: list[tuple[str, dict[str, Any]]], hook=None):
    """One ordinary owner turn whose model makes ``calls`` in order, one per request.

    ``hook(index)`` runs (async) before the model answers request ``index``: another
    entry acting while this turn is between two tool calls.
    """
    answered: list[Any] = []

    def ordinary(request):
        answered.append(request)
        index = len(answered) - 1
        if index < len(calls):
            name, arguments = calls[index]
            return _call(name, arguments, f"{key}-{index}")
        return "好"

    original = scene.provider._complete

    async def hooked(request):
        if hook is not None and "watchdog" not in (_runtime_context(request) or {}):
            await hook(len(answered))
        return await original(request)

    scene.ordinary = ordinary
    scene.provider._complete = hooked
    try:
        result = await scene.harness.processor.handle(
            scene.message(f"{key} 处理一下", key), MemorySender()
        )
    finally:
        scene.provider._complete = original
        scene.ordinary = lambda request: "好"
    assert result.reason == "chat", result
    results: dict[str, Any] = {}
    for request in answered:
        results.update(_tool_results(request))
    return [results.get(f"{key}-{index}") for index in range(len(calls))]


def _error_text(result: Any) -> str:
    return json.dumps(result, ensure_ascii=False)


class Recorder:
    """Stands for the native executor service: any use is recorded and refused."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str):
        async def call(*args, **kwargs):
            self.calls.append(name)
            raise AssertionError(f"native service used: {name}")

        return call


async def _failed_record(
    database, conversation: str, person: str, relationship: str = "delegate"
) -> dict[str, str]:
    """A delegation record whose native run already ended with a final failed receipt."""
    now = time.time()
    created = datetime.fromtimestamp(now, UTC)
    record, device, instance = (str(uuid.uuid4()) for _ in range(3))
    operation = f"prompt:{uuid.uuid4()}"
    async with database.immediate_session() as writer:
        writer.add(
            ClientDeviceModel(
                id=device,
                person_id=person,
                token_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                label="mac",
                created_at=created,
            )
        )
        await writer.flush()
        writer.add(
            AgentInstanceModel(id=instance, device_id=device, local_id="claude", catalog_json="{}")
        )
        await writer.flush()
        await writer.execute(
            work.insert().values(
                id=record,
                conversation_id=conversation,
                generation=1,
                source_key=f"executor:{record}",
                source_json=json.dumps(
                    {"origin": "user_message", "worker": True, "actor_person_id": person}
                ),
                goal="SECRET-GOAL 修复构建",
                state="failed",
                reason="native_failed",
                created=now,
                updated=now,
            )
        )
        await writer.execute(
            children.insert().values(
                work_id=record,
                source_key=f"delegate:{record}",
                brief_json=json.dumps({"goal": "SECRET-BRIEF"}),
                executor_json=json.dumps(
                    {"relationship": relationship, "device_id": device, "instance_id": instance}
                ),
            )
        )
        writer.add(
            AgentOperationModel(
                id=operation,
                instance_id=instance,
                session_id=None,
                actor_person_id=person,
                work_id=record,
                source_json="{}",
                action="prompt",
                payload_hash="0" * 64,
                command_json=json.dumps({"prompt": "SECRET-CMD"}),
                result_json=json.dumps({"status": "failed", "pending": False}),
                created_at=created,
            )
        )
    return {"record": record, "operation": operation}


async def _operations(database) -> int:
    async with database.read_session() as reader:
        return int(await reader.scalar(select(func.count()).select_from(AgentOperationModel)))


# --------------------------------------------------------------------------- S1


async def test_failed_holders_are_told_and_the_owner_abandons_one(database, tmp_path, monkeypatch):
    scene = await Scene(database, tmp_path).build()
    a = await _fail_by_turn(scene, "fa", "SECRET-GOAL-A 整理全年账单")
    b = await _fail_by_turn(scene, "fb", "SECRET-GOAL-B 迁移照片")
    c = await _fail_by_turn(scene, "fc", "SECRET-GOAL-C 写周报")
    label = {a: "A", b: "B", c: "C"}
    # A live Work in the group conversation shares one of A's objects.
    live = str(uuid.uuid4())
    async with database.immediate_session() as writer:
        await writer.execute(
            work.insert().values(
                id=live,
                conversation_id=scene.env.context.conversation_id,
                generation=1,
                source_key=f"live:{live}",
                source_json=json.dumps({"origin": "user_message"}),
                goal="SECRET-GOAL-LIVE",
                state="running",
                created=time.time(),
                updated=time.time(),
            )
        )
    pad_a = await _attach(database, a, "pad-A", 300 * KB)
    pad_b = await _attach(database, b, "pad-B", 200 * KB)
    pad_c = await _attach(database, c, "pad-C", 100 * KB)
    shared_live = await _attach(database, a, "shared-live", 50 * KB)
    assert await _attach(database, live, "shared-live", 50 * KB) == shared_live
    shared_failed = await _attach(database, b, "shared-failed", 40 * KB)
    assert await _attach(database, c, "shared-failed", 40 * KB) == shared_failed

    calls: list[int] = []
    original = getattr(protocol_store_module, "failed_holders", None)

    async def spy(reader, *args, **kwargs):
        calls.append(1)
        assert original is not None
        return await original(reader, *args, **kwargs)

    monkeypatch.setattr(watchdog_module, "failed_holders", spy, raising=False)

    # W1/Q2: below the warn line nothing about storage is opened or computed.
    assert await scene.tick() == []
    keys = {row["issue_key"] for row in await scene.rows(watchdog_issues)}
    assert not {key for key in keys if key.startswith("protocol_storage_")}
    below_calls = len(calls)
    assert below_calls == 0

    # Near the cap: usage at 90% of it.
    used = await _usage(database)
    scene.cap[0] = int(used / 0.9)
    snapshot = await scene.config.snapshot()
    writers: list[int] = []
    real_writer = database.immediate_session

    def counting_writer(*args, **kwargs):
        writers.append(1)
        return real_writer(*args, **kwargs)

    monkeypatch.setattr(database, "immediate_session", counting_writer)
    observed = await scene.watchdog.observe(scene.clock(), snapshot)
    monkeypatch.setattr(database, "immediate_session", real_writer)
    assert writers == []  # W5
    assert "protocol_storage_near_cap" in observed.present

    expected = await _expected_holders(database)
    before_tick = await _state(database)
    outcomes = await scene.tick()
    assert outcomes == ["finished"]
    assert await _state(database) == before_tick  # W4: the notice moved no Work or ref
    context = _runtime_context(scene.notice_requests[0])
    assert context is not None
    said = {issue["key"]: issue["facts"] for issue in context["watchdog"]["issues"]}
    facts = said["protocol_storage_near_cap"]
    holders = facts["failed_holders"]
    # W2/Q3/Q4: ordered by held bytes, at most five, exact arithmetic, estimate wording.
    assert [holder["work_id"] for holder in holders] == expected["order"][:5]
    assert [label[holder["work_id"]] for holder in holders] == ["A", "B", "C"]
    for holder in holders:
        identity = holder["work_id"]
        assert holder["held_bytes"] == expected["held"][identity]
        assert holder["exclusive_bytes"] == expected["exclusive"][identity]
        row = await _row(database, identity)
        assert holder["revision"] == row["revision"]
        assert holder["reason"] == "LLMInvalidRequestError"
        assert holder["origin"] == "user_message"
        assert holder["conversation_id"] == row["conversation_id"]
        assert holder["model_requests"] == row["model_requests"]
        assert set(holder) == {
            "work_id",
            "parent_work_id",
            "revision",
            "origin",
            "owner",
            "conversation_id",
            "reason",
            "failed_at",
            "model_requests",
            "held_bytes",
            "exclusive_bytes",
        }
    assert facts["failed_holders_total"] == expected["total"] == 3
    assert facts["failed_only_bytes"] == expected["failed_only"]
    assert "估算" in facts["reclaim_note"] and "不保证" in facts["reclaim_note"]
    # The shared-with-live object is held by A but never exclusive to it.
    assert expected["exclusive"][a] < expected["held"][a]
    # W3: no goal or failure text anywhere the facts go.
    _secret_free(context)
    async with database.read_session() as reader:
        stored = " ".join(await reader.scalars(select(watchdog_issues.c.facts_json)))
    _secret_free(stored)
    assert "SECRET-FAILURE" not in stored and "SECRET-FAILURE" not in json.dumps(context)
    assert len(calls) >= 1
    # W7: the notice turn only reported; nothing was managed by it.
    notice_calls = " ".join(
        str(call.function.name)
        for request in scene.notice_requests
        for message in request.messages
        for call in (getattr(message, "tool_calls", None) or ())
    )
    assert "manage_task" not in notice_calls
    sends = scene.sends()
    assert len(sends) == 1 and "protocol_storage_near_cap" in sends[0]

    # The owner agrees in an ordinary turn: Yuki reads A, then abandons it.
    scene.cap[0] = 2 * 1024**3
    a_before = await _row(database, a)
    exclusive_a = expected["exclusive"][a]
    a_refs = await _refs(database, a)
    async with database.read_session() as reader:
        others = set(
            await reader.scalars(
                select(refs.c.sha256).where(refs.c.sha256.in_(a_refs), refs.c.work_id != a)
            )
        )
    exclusive_digests = a_refs - others
    assert pad_a in exclusive_digests and shared_live not in exclusive_digests
    async with database.read_session() as reader:
        journal_rows = int(
            await reader.scalar(
                select(func.count()).select_from(journal).where(journal.c.work_id == a)
            )
        )
    read, cancelled = await _owner_turn(
        scene,
        "abandon",
        [
            ("get_tasks", {"id": a}),
            ("manage_task", {"action": "cancel", "id": a, "text": "主人同意放弃恢复"}),
        ],
    )
    assert read["status"] == "failed", read
    assert cancelled == {"id": a, "status": "cancelled"}, cancelled
    a_after = await _row(database, a)
    assert a_after["state"] == "cancelled" and a_after["revision"] == a_before["revision"] + 1
    assert a_after["reason"] == "主人同意放弃恢复"
    assert await _refs(database, a) == set()  # F4
    assert await _refs(database, live) == {shared_live}
    assert await _refs(database, b) and await _refs(database, c)
    async with database.read_session() as reader:
        failure = json.loads(
            await reader.scalar(select(recovery.c.failure_json).where(recovery.c.work_id == a))
        )
        kept_journal = int(
            await reader.scalar(
                select(func.count()).select_from(journal).where(journal.c.work_id == a)
            )
        )
    assert failure["code"] == "LLMInvalidRequestError"  # F14
    assert kept_journal == journal_rows

    # F5: nothing is reclaimed inside the grace period ...
    store = ProtocolStore(database)
    used_before_gc = await _usage(database)
    while await store.cleanup():
        pass
    assert await _usage(database) == used_before_gc
    assert store._path(pad_a).exists()
    # ... and after it exactly the unowned bytes go, A's exclusive ones among them.
    async with database.immediate_session() as writer:
        await writer.execute(update(objects).values(prepared_at=time.time() - 2 * DAY))
    async with database.read_session() as reader:
        unowned = (
            await reader.execute(
                select(objects.c.sha256, objects.c.byte_size).where(
                    ~select(refs.c.sha256).where(refs.c.sha256 == objects.c.sha256).exists()
                )
            )
        ).all()
    unowned_digests = {row.sha256 for row in unowned}
    assert exclusive_digests <= unowned_digests
    while await store.cleanup():
        pass
    dropped = used_before_gc - await _usage(database)
    assert dropped == sum(row.byte_size for row in unowned)
    assert dropped >= exclusive_a
    for digest in exclusive_digests:
        assert not store._path(digest).exists()
    for kept in (shared_live, pad_b, pad_c, shared_failed):
        assert store._path(kept).exists()

    # W6: a failing holder query keeps the open storage issue and the loop alive.
    scene.cap[0] = int(await _usage(database) / 0.9)
    await scene.tick(CHECK)

    async def broken(reader, *args, **kwargs):
        raise RuntimeError("holder query failed")

    monkeypatch.setattr(watchdog_module, "failed_holders", broken, raising=False)
    open_before = {row["issue_key"] for row in await scene.rows(watchdog_issues)}
    await scene.tick(CHECK * 4)
    open_after = {row["issue_key"] for row in await scene.rows(watchdog_issues)}
    assert "protocol_storage_near_cap" in open_before and open_after == open_before
    assert scene.watchdog.status()["check_errors"].get("works") == "RuntimeError"

    RESULTS["S1_holders_told_and_owner_abandons"] = {
        "holder_order": [label[holder["work_id"]] for holder in holders],
        "holder_fields": sorted(holders[0]),
        "holders_match_independent_arithmetic": True,
        "failed_holders_total": facts["failed_holders_total"],
        "shared_with_live_counted_as_exclusive": False,
        "holder_query_calls_below_threshold": below_calls,
        "writers_while_observing": len(writers),
        "works_and_refs_unchanged_by_tick": True,
        "goal_or_failure_text_in_facts": False,
        "notice_turn_managed_a_work": False,
        "owner_turn": {
            "get_tasks_status": read["status"],
            "manage_task_result": {"id": "A", "status": cancelled["status"]},
            "revision_step": a_after["revision"] - a_before["revision"],
            "refs_after_cancel": len(await _refs(database, a)),
            "failure_code_kept": failure["code"],
            "journal_rows_kept": kept_journal == journal_rows,
        },
        "gc": {
            "reclaimed_inside_grace": False,
            "usage_drop_equals_unowned_bytes": True,
            "drop_covers_exclusive_estimate": True,
            "a_exclusive_files_removed": True,
            "shared_and_other_failed_files_kept": True,
        },
        "failing_holder_query": {"issue_kept_open": True, "check_error": "RuntimeError"},
    }


# --------------------------------------------------------------------------- S2


async def test_owner_turn_checks_revision_and_failed_record_is_really_cancelled(database, tmp_path):
    scene = await Scene(database, tmp_path).build()
    c = await _fail_by_turn(scene, "fc", "SECRET-GOAL-C 写周报")
    d = await _fail_by_turn(scene, "fd", "SECRET-GOAL-D 订机票")
    b = await _fail_by_turn(scene, "fb", "SECRET-GOAL-B 迁移照片")

    # F18: Yuki's own successful cancel updates what it read; the second call meets
    # the real state (precondition_failed), not a stale-revision conflict.
    read, first, second = await _owner_turn(
        scene,
        "own",
        [
            ("get_tasks", {"id": c}),
            ("manage_task", {"action": "cancel", "id": c, "text": "放弃"}),
            ("manage_task", {"action": "cancel", "id": c, "text": "再次"}),
        ],
    )
    assert read["status"] == "failed"
    assert first == {"id": c, "status": "cancelled"}, first
    assert "precondition_failed" in _error_text(second), second
    assert "version_conflict" not in _error_text(second)

    # F18: a read never leaks into the next activation; a cancel without a read works.
    (read_d,) = await _owner_turn(scene, "look", [("get_tasks", {"id": d})])
    assert read_d["status"] == "failed"
    async with database.immediate_session() as writer:
        await writer.execute(
            update(work).where(work.c.id == d).values(revision=work.c.revision + 1)
        )
    (direct,) = await _owner_turn(
        scene, "direct", [("manage_task", {"action": "cancel", "id": d, "text": "放弃"})]
    )
    assert direct == {"id": d, "status": "cancelled"}, direct

    # Q6/F15/F19: a failed delegation record is really cancelled, and its ended
    # native run is not touched.
    conversation = await scene.conversation()
    delegated = await _failed_record(database, conversation, scene.env.person)
    recorder = Recorder()
    database.external_agents = recorder
    operations = await _operations(database)
    (record_result,) = await _owner_turn(
        scene,
        "record",
        [("manage_task", {"action": "cancel", "id": delegated["record"], "text": "不要了"})],
    )
    record_row = await _row(database, delegated["record"])
    assert record_result["status"] == "cancelled", record_result
    assert record_row["state"] == "cancelled", record_row["state"]
    assert recorder.calls == []
    assert await _operations(database) == operations

    # F17: Yuki read B as failed; another entry resumed it before Yuki's cancel.
    b_failed = await _row(database, b)
    b_refs = await _refs(database, b)
    resumed: list[tuple[int, str]] = []

    async def resume_elsewhere(index: int) -> None:
        if index == 1 and not resumed:
            async with database.immediate_session() as writer:
                resumed.append(await manage_work(writer, b, b_failed["revision"], "resume"))

    read_b, conflict = await _owner_turn(
        scene,
        "race",
        [
            ("get_tasks", {"id": b}),
            ("manage_task", {"action": "cancel", "id": b, "text": "放弃"}),
        ],
        hook=resume_elsewhere,
    )
    assert read_b["status"] == "failed"
    assert resumed == [(b_failed["revision"] + 1, "queued")]
    assert "version_conflict" in _error_text(conflict), conflict
    b_now = await _row(database, b)
    assert b_now["state"] != "cancelled", b_now["state"]
    assert b_now["reason"] != "放弃"
    assert await _refs(database, b) == b_refs

    # F18: a Work read while it is not failed is not bound; its own execution may
    # move the revision between the read and the owner's cancel.
    async def progressed(index: int) -> None:
        if index == 1:
            async with database.immediate_session() as writer:
                await writer.execute(
                    update(work).where(work.c.id == b).values(revision=work.c.revision + 1)
                )

    read_live, cancelled_live = await _owner_turn(
        scene,
        "live",
        [
            ("get_tasks", {"id": b}),
            ("manage_task", {"action": "cancel", "id": b, "text": "主人确认取消"}),
        ],
        hook=progressed,
    )
    assert read_live["status"] != "failed", read_live
    assert cancelled_live == {"id": b, "status": "cancelled"}, cancelled_live

    RESULTS["S2_revision_check_and_failed_record"] = {
        "non_failed_read_not_bound": cancelled_live["status"],
        "own_cancel_then_again": {
            "first": first["status"],
            "second_error": "precondition_failed",
        },
        "read_does_not_leak_to_next_turn": direct["status"],
        "failed_record": {
            "tool_status": record_result["status"],
            "stored_state": record_row["state"],
            "native_service_calls": len(recorder.calls),
            "new_native_operations": 0,
        },
        "resumed_between_read_and_cancel": {
            "other_entry": "resumed",
            "manage_task_error": "version_conflict",
            "work_cancelled": False,
            "refs_kept": True,
        },
    }


# --------------------------------------------------------------------------- S3


async def _failed_seeded(env, database, label: str, **kwargs) -> dict[str, Any]:
    original = await seeded(env, database, state="failed", **kwargs)
    digest = await _attach(database, original.row["id"], label, 64 * KB)
    return {"id": original.row["id"], "revision": original.row["revision"], "digest": digest}


async def test_device_and_admin_entries_abandon_alike(app_env, database):
    env = app_env
    device, person = await owner(env, database)
    admin_headers = {"Authorization": f"Bearer {TOKEN}"}
    by_device = await _failed_seeded(env, database, "device")
    by_admin = await _failed_seeded(env, database, "admin")

    # F3: the admin detail offers cancel for failed.
    detail = await env.client.get(f"/admin/v1/works/{by_admin['id']}", headers=admin_headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["data"]["actions"]["cancel"] is True

    # F1/F2: both entries abandon the same shape alike, and replay their receipts.
    request = {"request_id": str(uuid.uuid4()), "expected_revision": by_device["revision"]}
    device_first = await env.client.post(
        f"/client/v1/works/{by_device['id']}/cancel", json=request, headers=bearer(device["token"])
    )
    assert device_first.status_code == 200, device_first.text
    device_replay = await env.client.post(
        f"/client/v1/works/{by_device['id']}/cancel", json=request, headers=bearer(device["token"])
    )
    body = command(by_admin["revision"])
    admin_first = await env.client.post(
        f"/admin/v1/works/{by_admin['id']}/cancel", json=body, headers=admin_headers
    )
    assert admin_first.status_code == 200, admin_first.text
    admin_replay = await env.client.post(
        f"/admin/v1/works/{by_admin['id']}/cancel", json=body, headers=admin_headers
    )
    assert admin_replay.json() == admin_first.json()
    shapes = {}
    for name, item in (("device", by_device), ("admin", by_admin)):
        row = await _row(database, item["id"])
        shapes[name] = {
            "state": row["state"],
            "revision_step": row["revision"] - item["revision"],
            "refs_after": len(await _refs(database, item["id"])),
        }
    assert (
        shapes["device"]
        == shapes["admin"]
        == {
            "state": "cancelled",
            "revision_step": 1,
            "refs_after": 0,
        }
    )
    replayed = (
        device_replay.json()["data"] if "data" in device_replay.json() else device_replay.json()
    )
    assert replayed["state"] == "cancelled" and replayed["replay"] is True
    after = await env.client.get(f"/admin/v1/works/{by_admin['id']}", headers=admin_headers)
    assert after.json()["data"]["actions"]["cancel"] is False  # F3 / F6
    assert after.json()["data"]["actions"]["resume"] is False

    # F9: completed and cancelled stay closed at both entries.
    done = await seeded(env, database, state="completed")
    refused = await env.client.post(
        f"/client/v1/works/{done.row['id']}/cancel",
        json={"request_id": str(uuid.uuid4()), "expected_revision": done.row["revision"]},
        headers=bearer(device["token"]),
    )
    assert refused.status_code == 409 and refused.json()["problem"]["code"] == "precondition_failed"
    again = await env.client.post(
        f"/admin/v1/works/{by_admin['id']}/cancel",
        json=command(by_admin["revision"] + 1),
        headers=admin_headers,
    )
    assert again.status_code == 409 and again.json()["problem"]["code"] == "precondition_failed"

    # F8: a stale revision writes nothing and the device receipt keeps the problem.
    stale = await _failed_seeded(env, database, "stale")
    stale_request = {"request_id": str(uuid.uuid4()), "expected_revision": stale["revision"] + 7}
    conflict = await env.client.post(
        f"/client/v1/works/{stale['id']}/cancel",
        json=stale_request,
        headers=bearer(device["token"]),
    )
    assert conflict.status_code == 409
    assert conflict.json()["problem"]["code"] == "version_conflict"
    repeated = await env.client.post(
        f"/client/v1/works/{stale['id']}/cancel",
        json=stale_request,
        headers=bearer(device["token"]),
    )
    assert repeated.json()["problem"] == conflict.json()["problem"]
    assert (await _row(database, stale["id"]))["state"] == "failed"
    assert await _refs(database, stale["id"]) == {stale["digest"]}

    # F16: another person's failed Work is not found for this device.
    foreign = await _failed_seeded(env, database, "foreign", account="1002")
    hidden = await env.client.post(
        f"/client/v1/works/{foreign['id']}/cancel",
        json={"request_id": str(uuid.uuid4()), "expected_revision": foreign["revision"]},
        headers=bearer(device["token"]),
    )
    assert hidden.status_code == 404 and hidden.json()["problem"]["code"] == "not_found"
    assert (await _row(database, foreign["id"]))["state"] == "failed"

    # F15: a failed delegation record is cancelled through both entries as well.
    conversation = (await _row(database, by_device["id"]))["conversation_id"]
    record_device = await _failed_record(database, conversation, person)
    record_admin = await _failed_record(database, conversation, person)
    by_device_record = await env.client.post(
        f"/client/v1/works/{record_device['record']}/cancel",
        json={"request_id": str(uuid.uuid4()), "expected_revision": 1},
        headers=bearer(device["token"]),
    )
    by_admin_record = await env.client.post(
        f"/admin/v1/works/{record_admin['record']}/cancel", json=command(1), headers=admin_headers
    )
    assert by_device_record.status_code == 200, by_device_record.text
    assert by_admin_record.status_code == 200, by_admin_record.text
    assert (await _row(database, record_device["record"]))["state"] == "cancelled"
    assert (await _row(database, record_admin["record"]))["state"] == "cancelled"

    RESULTS["S3_device_and_admin"] = {
        "admin_actions_cancel_for_failed": True,
        "entries": shapes,
        "device_replay": {"state": replayed["state"], "replay": replayed["replay"]},
        "admin_replay_identical": True,
        "after_cancel_actions": {"cancel": False, "resume": False},
        "completed_or_cancelled": "precondition_failed",
        "stale_revision": {
            "problem": "version_conflict",
            "replayed_problem_identical": True,
            "state": "failed",
            "refs_kept": True,
        },
        "foreign_person": "not_found",
        "failed_record": {"device": "cancelled", "admin": "cancelled"},
    }


# --------------------------------------------------------------------------- S4


async def test_admin_chat_command_abandons_a_failed_work(database):
    app, bot, _presence, space, _binding = await stack(database)
    async with database.read_session() as reader:
        conversation = await reader.scalar(
            select(CanonicalConversationModel.id).where(
                CanonicalConversationModel.space_id == space
            )
        )
    identity = str(uuid.uuid4())
    async with database.immediate_session() as writer:
        await writer.execute(
            work.insert().values(
                id=identity,
                conversation_id=conversation,
                generation=1,
                source_key=f"chat-command:{identity}",
                source_json=json.dumps({"origin": "user_message"}),
                goal="SECRET-GOAL 群任务",
                state="failed",
                reason="LLMInvalidRequestError",
                created=time.time(),
                updated=time.time(),
            )
        )
    await _attach(database, identity, "chat-command", 64 * KB)
    argument = f"work cancel {identity}"
    request = message("/ai admin " + argument, message_id="abandon-by-command")
    try:
        admitted = await app.canonical_ingress.pre_admit(bot, request)
        appended = await app.canonical_uow.append_inbound(admitted.message, admitted)
        request = replace(admitted.message, source_event_id=appended.event.id)
        profile = UserProfileSnapshot(
            request.sender.user_id, request.scope_type, group_id=request.group_id
        )
        answer = await app.command_service.execute(
            CommandName.ADMIN, request, request.scope(), profile, argument, time.perf_counter()
        )
    finally:
        await app.close()
    row = await _row(database, identity)
    assert row["state"] == "cancelled", (row["state"], answer.text)
    assert row["revision"] == 2
    assert await _refs(database, identity) == set()
    RESULTS["S4_admin_chat_command"] = {
        "state": row["state"],
        "revision_step": row["revision"] - 1,
        "refs_after": 0,
    }


# --------------------------------------------------------------------------- S5


async def _insert(
    database,
    conversation: str,
    *,
    state: str,
    parent: str | None = None,
    worker: bool = False,
    archived: bool = False,
    model_requests: int = 0,
) -> str:
    identity = str(uuid.uuid4())
    now = time.time()
    async with database.immediate_session() as writer:
        await writer.execute(
            work.insert().values(
                id=identity,
                parent_work_id=parent,
                conversation_id=conversation,
                generation=1,
                source_key=f"matrix:{identity}",
                source_json=json.dumps({"origin": "user_message", "worker": worker}),
                goal="SECRET-GOAL matrix",
                state=state,
                reason="LLMInvalidRequestError" if state == "failed" else None,
                model_requests=model_requests,
                created=now,
                updated=now,
            )
        )
        if worker:
            await writer.execute(
                children.insert().values(
                    work_id=identity,
                    source_key=f"worker:{identity}",
                    brief_json="{}",
                    archived_at=now if archived else None,
                )
            )
    return identity


async def _manage(database, identity: str, action: str, revision: int | None = None):
    if revision is None:
        revision = (await _row(database, identity))["revision"]
    try:
        async with database.immediate_session() as writer:
            return await manage_work(writer, identity, revision, action)
    except WorkManagementError as exc:
        return exc.code


async def test_failed_work_state_matrix(database, tmp_path):
    env = await social_env(database, tmp_path)
    conversation = env.context.conversation_id
    matrix: dict[str, Any] = {}

    # F6: once abandoned it cannot be resumed.
    failed = await _insert(database, conversation, state="failed")
    await _attach(database, failed, "f6", 8 * KB)
    assert await _manage(database, failed, "cancel") == (2, "cancelled")
    matrix["F6_resume_after_cancel"] = await _manage(database, failed, "resume")
    assert matrix["F6_resume_after_cancel"] == "precondition_failed"

    # F7: concurrent resume and cancel on one revision: exactly one wins.
    raced = await _insert(database, conversation, state="failed", model_requests=0)
    await _attach(database, raced, "f7", 8 * KB)
    outcomes = await asyncio.gather(
        _manage(database, raced, "resume", 1), _manage(database, raced, "cancel", 1)
    )
    winners = [outcome for outcome in outcomes if isinstance(outcome, tuple)]
    losers = [outcome for outcome in outcomes if not isinstance(outcome, tuple)]
    assert len(winners) == 1 and losers == ["version_conflict"], outcomes
    final = await _row(database, raced)
    assert final["state"] == winners[0][1] and final["revision"] == 2
    assert bool(await _refs(database, raced)) == (final["state"] != "cancelled")
    matrix["F7_concurrent"] = {"winners": 1, "loser": losers[0]}

    # F8: stale revision.
    stale = await _insert(database, conversation, state="failed")
    stale_digest = await _attach(database, stale, "f8", 8 * KB)
    matrix["F8_stale"] = await _manage(database, stale, "cancel", 5)
    assert matrix["F8_stale"] == "version_conflict"
    assert await _refs(database, stale) == {stale_digest}

    # F9: fail on failed, and anything on completed/cancelled, stays closed.
    closed = {}
    for state, action in (
        ("failed", "fail"),
        ("completed", "cancel"),
        ("cancelled", "cancel"),
        ("completed", "resume"),
    ):
        identity = await _insert(database, conversation, state=state)
        closed[f"{action}_{state}"] = await _manage(database, identity, action)
    assert set(closed.values()) == {"precondition_failed"}
    matrix["F9_closed"] = closed
    # F3: the read model agrees.
    views = {}
    for state in ("failed", "completed", "cancelled"):
        identity = await _insert(database, conversation, state=state)
        async with database.read_session() as reader:
            views[state] = (await management_view(reader, identity))["actions"]["cancel"]
    assert views == {"failed": True, "completed": False, "cancelled": False}
    matrix["F3_view_cancel"] = views

    # F10/Q5: a failed root with a live, a failed and a completed worker.
    root = await _insert(database, conversation, state="failed")
    live = await _insert(database, conversation, state="waiting_external", parent=root, worker=True)
    failed_worker = await _insert(database, conversation, state="failed", parent=root, worker=True)
    done_worker = await _insert(database, conversation, state="completed", parent=root, worker=True)
    kept = {
        failed_worker: await _attach(database, failed_worker, "f10-failed", 8 * KB),
        done_worker: await _attach(database, done_worker, "f10-done", 8 * KB),
    }
    await _attach(database, root, "f10-root", 8 * KB)
    assert await _manage(database, root, "cancel") == (2, "cancelled")
    tree = {
        "root": (await _row(database, root))["state"],
        "live_worker": (await _row(database, live))["state"],
        "failed_worker": (await _row(database, failed_worker))["state"],
        "completed_worker": (await _row(database, done_worker))["state"],
    }
    assert tree == {
        "root": "cancelled",
        "live_worker": "cancelled",
        "failed_worker": "failed",
        "completed_worker": "completed",
    }
    for identity, digest in kept.items():
        assert await _refs(database, identity) == {digest}
    assert await _refs(database, root) == set()
    matrix["F10_tree"] = tree

    # F11: a failed worker's parent gets the stop evidence; a suspended one stays.
    for parent_state, woken in (("waiting_external", "queued"), ("suspended", "suspended")):
        parent = await _insert(database, conversation, state=parent_state)
        child = await _insert(database, conversation, state="failed", parent=parent, worker=True)
        assert await _manage(database, child, "cancel") == (2, "cancelled")
        async with database.read_session() as reader:
            mailbox = list(
                await reader.scalars(
                    select(inputs.c.payload_json).where(inputs.c.work_id == parent)
                )
            )
        assert len(mailbox) == 1
        assert json.loads(json.loads(mailbox[0])["text"])["state"] == "cancelled"
        assert (await _row(database, parent))["state"] == woken
        matrix[f"F11_parent_{parent_state}"] = woken

    # F12: an archived failed root (journal and refs already gone) can be abandoned.
    archived = await _insert(database, conversation, state="failed", model_requests=3)
    async with database.read_session() as reader:
        view = await management_view(reader, archived)
    assert view["actions"] == {
        "resume": False,
        "resume_blocked_by": "state_mismatch",
        "cancel": True,
    }
    assert await _manage(database, archived, "cancel") == (2, "cancelled")
    matrix["F12_archived_root"] = "cancelled"

    # F13: a pending input and an active wait do not wake it again.
    waiting = await _insert(database, conversation, state="failed")
    now = time.time()
    async with database.immediate_session() as writer:
        await writer.execute(
            inputs.insert().values(
                conversation_id=conversation,
                generation=1,
                source_key=f"late:{waiting}",
                work_id=waiting,
                kind="message",
                payload_json=json.dumps({"text": "late"}),
                created=now,
            )
        )
        await writer.execute(
            waits.insert().values(
                id=str(uuid.uuid4()),
                work_id=waiting,
                conversation_id=conversation,
                generation=1,
                principal_kind="person",
                principal_id=env.person,
                call_key=f"wait:{waiting}",
                request_json="{}",
                mode="any",
                conditions_json="[]",
                status="active",
                created=now,
                updated=now,
            )
        )
    assert await _manage(database, waiting, "cancel") == (2, "cancelled")
    async with database.read_session() as reader:
        left = (
            list(await reader.scalars(select(inputs.c.state).where(inputs.c.work_id == waiting))),
            list(await reader.scalars(select(waits.c.status).where(waits.c.work_id == waiting))),
        )
    assert left == (["cancelled"], ["cancelled"])
    assert (await _row(database, waiting))["state"] == "cancelled"
    matrix["F13_inputs_and_waits"] = {"input": left[0][0], "wait": left[1][0]}

    RESULTS["S5_state_matrix"] = matrix


# --------------------------------------------------------------------------- S6


async def _failed_child(database, parent: str) -> str:
    """A failed worker of ``parent``, visible through the parent's original source."""
    row = await _row(database, parent)
    identity = str(uuid.uuid4())
    now = time.time()
    async with database.immediate_session() as writer:
        await writer.execute(
            work.insert().values(
                id=identity,
                parent_work_id=parent,
                conversation_id=row["conversation_id"],
                generation=row["generation"],
                source_key=f"child:{identity}",
                source_json=json.dumps({**json.loads(row["source_json"]), "worker": True}),
                goal="SECRET-GOAL 子任务",
                state="failed",
                reason="LLMInvalidRequestError",
                created=now,
                updated=now,
            )
        )
        await writer.execute(
            children.insert().values(
                work_id=identity, source_key=f"worker:{identity}", brief_json="{}"
            )
        )
    return identity


async def _resumed_elsewhere(database, identity: str, state: str = "queued") -> None:
    """Another entry moved the Work on (as its resume writes: new state, revision + 1)."""
    async with database.immediate_session() as writer:
        await writer.execute(
            update(work)
            .where(work.c.id == identity)
            .values(state=state, revision=work.c.revision + 1, updated=time.time())
        )


async def test_a_failed_read_guards_every_manage_path(database, tmp_path):
    """F17 on every path: generic, subtask, delegation record, neutral resume; retries.

    Each case: Yuki reads the target as failed with get_tasks(id); another entry
    moves it on before Yuki's call; the call and a retry without a new read must
    both be ``version_conflict`` and leave no write or native side effect.
    """
    scene = await Scene(database, tmp_path).build()
    recorder = Recorder()
    database.external_agents = recorder
    outcome: dict[str, Any] = {}

    def moved(identity: str, state: str = "queued"):
        async def hook(index: int) -> None:
            if index == 1:
                await _resumed_elsewhere(database, identity, state)

        return hook

    def codes(results) -> list[str]:
        return [
            "version_conflict" if "version_conflict" in _error_text(r) else _error_text(r)
            for r in results
        ]

    # Generic path: conflict, then a retry without a new read.
    generic = await _fail_by_turn(scene, "fg", "SECRET-GOAL-G 整理")
    read, first, retry = await _owner_turn(
        scene,
        "generic",
        [
            ("get_tasks", {"id": generic}),
            ("manage_task", {"action": "cancel", "id": generic, "text": "放弃"}),
            ("manage_task", {"action": "cancel", "id": generic, "text": "重试"}),
        ],
        hook=moved(generic),
    )
    assert read["status"] == "failed"
    assert codes([first, retry]) == ["version_conflict"] * 2, (first, retry)
    assert (await _row(database, generic))["state"] == "queued"
    outcome["generic_cancel_and_retry"] = codes([first, retry])

    # Subtask path: cancel and a retry; resume (with and without text) as well.
    parent = await _fail_by_turn(scene, "fp", "SECRET-GOAL-P 父任务")
    for action, extra in (("cancel", {}), ("resume", {}), ("resume", {"text": "继续"})):
        child = await _failed_child(database, parent)
        call = {"action": action, "id": child, **extra}
        read, first, retry = await _owner_turn(
            scene,
            f"child-{action}-{len(extra)}",
            [("get_tasks", {"id": child}), ("manage_task", call), ("manage_task", call)],
            hook=moved(child),
        )
        assert read["status"] == "failed", read
        assert codes([first, retry]) == ["version_conflict"] * 2, (action, first, retry)
        row = await _row(database, child)
        assert (row["state"], row["revision"]) == ("queued", 2), row
        async with database.read_session() as reader:
            mailbox = int(
                await reader.scalar(
                    select(func.count()).select_from(inputs).where(inputs.c.work_id == child)
                )
            )
        assert mailbox == 0
        outcome[f"subtask_{action}{'_with_text' if extra else ''}"] = codes([first, retry])

    # Delegation record: cancel and resume never act on the moved record, and nothing
    # native is sent (release of a failed record is refused outright, see S7).
    conversation = await scene.conversation()
    # The record either runs again or failed again since the read (same state, newer
    # revision): only the read revision tells the second case apart.
    for action, extra, state in (
        ("cancel", {}, "waiting_external"),
        ("cancel", {}, "failed"),
        ("resume", {"text": "继续"}, "waiting_external"),
        ("resume", {"text": "继续"}, "failed"),
    ):
        record = (await _failed_record(database, conversation, scene.env.person))["record"]
        call = {"action": action, "id": record, **extra}
        operations = await _operations(database)
        read, first, retry = await _owner_turn(
            scene,
            f"record-{action}-{state}",
            [("get_tasks", {"id": record}), ("manage_task", call), ("manage_task", call)],
            hook=moved(record, state),
        )
        assert read["status"] == "failed", read
        assert codes([first, retry]) == ["version_conflict"] * 2, (action, first, retry)
        row = await _row(database, record)
        assert (row["state"], row["revision"]) == (state, 2), row
        assert recorder.calls == [], recorder.calls
        assert await _operations(database) == operations
        outcome[f"record_{action}_after_{state}"] = codes([first, retry])

    # Neutral resume (no current Work): it is not queued again.
    neutral = await _fail_by_turn(scene, "fn", "SECRET-GOAL-N 订票")
    read, first = await _owner_turn(
        scene,
        "neutral",
        [("get_tasks", {"id": neutral}), ("manage_task", {"action": "resume", "id": neutral})],
        hook=moved(neutral, "failed"),
    )
    assert read["status"] == "failed"
    assert codes([first]) == ["version_conflict"], first
    row = await _row(database, neutral)
    assert (row["state"], row["revision"]) == ("failed", 3), row
    outcome["neutral_resume"] = codes([first])
    RESULTS["S6_every_path_guarded"] = outcome


# --------------------------------------------------------------------------- S7


class ReleaseRecorder:
    """The native service: release calls are recorded; anything else is refused."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    async def release(self, work_id, source, *, request_id, close_owner):
        self.calls.append((work_id, close_owner))
        return []

    def __getattr__(self, name: str):
        async def call(*args, **kwargs):
            raise AssertionError(f"native service used: {name}")

        return call


FAILED_RELEASE_HINT = "已失败的委派先用 cancel 放弃恢复；需要交还会话的话，之后再 release"


async def test_failed_record_opens_only_cancel_and_resume_window_is_closed(
    database, tmp_path, monkeypatch
):
    """F21/F22: a failed record only opens cancel; neutral resume re-checks in its writer."""
    from yuki.runtime.work_repository import WorkRepository

    scene = await Scene(database, tmp_path).build()
    conversation = await scene.conversation()
    service = ReleaseRecorder()
    database.external_agents = service
    outcome: dict[str, Any] = {}

    # F21: releasing a currently failed record is refused before any native call,
    # and so is a retry; then cancel gives up its recovery.
    record = (await _failed_record(database, conversation, scene.env.person))["record"]
    operations = await _operations(database)
    read, refused, again, cancelled = await _owner_turn(
        scene,
        "failed-release",
        [
            ("get_tasks", {"id": record}),
            ("manage_task", {"action": "release", "id": record}),
            ("manage_task", {"action": "release", "id": record}),
            ("manage_task", {"action": "cancel", "id": record, "text": "放弃恢复"}),
        ],
    )
    assert read["status"] == "failed", read
    for reply in (refused, again):
        assert reply["error"] == "failed_record_release", reply
        assert reply["message"] == FAILED_RELEASE_HINT, reply
    assert service.calls == []
    assert await _operations(database) == operations
    assert cancelled["status"] == "cancelled", cancelled
    row = await _row(database, record)
    assert (row["state"], row["revision"]) == ("cancelled", 2), row
    # Releasing the cancelled record afterwards keeps main's behaviour: Yuki's
    # operations are released (the session can be handed back), the record stays.
    (released,) = await _owner_turn(
        scene, "cancelled-release", [("manage_task", {"action": "release", "id": record})]
    )
    assert service.calls == [(record, True)]
    assert released["status"] == "cancelled", released
    assert (await _row(database, record))["revision"] == 2
    outcome["failed_record"] = {
        "release_refused": refused["error"],
        "retry_refused": again["error"],
        "native_calls_before_cancel": 0,
        "cancel": cancelled["status"],
        "release_after_cancel": {"native_release": True, "record_unchanged": True},
    }

    # F21: stopping an observation of a failed record is unchanged from main: the
    # observation's operations are released without closing anything, and the
    # record keeps its state.
    service.calls.clear()
    observed = (await _failed_record(database, conversation, scene.env.person, "observe"))["record"]
    await _owner_turn(
        scene, "stop-observing", [("manage_task", {"action": "cancel", "id": observed})]
    )
    assert service.calls == [(observed, False)]
    row = await _row(database, observed)
    assert (row["state"], row["revision"]) == ("failed", 1), row
    outcome["stop_observing_failed_record"] = {"native_release_close": False, "state": "failed"}

    # F21: another entry ended the record Yuki read as failed; Yuki's cancel is a
    # version_conflict, never a "cancelled" for a record it did not end.
    ended = (await _failed_record(database, conversation, scene.env.person))["record"]
    read, reply = await _owner_turn(
        scene,
        "ended-elsewhere",
        [
            ("get_tasks", {"id": ended}),
            ("manage_task", {"action": "cancel", "id": ended, "text": "放弃"}),
        ],
        hook=lambda index: (
            _resumed_elsewhere(database, ended, "cancelled") if index == 1 else asyncio.sleep(0)
        ),
    )
    assert read["status"] == "failed"
    assert "version_conflict" in _error_text(reply), reply
    row = await _row(database, ended)
    assert (row["state"], row["revision"], row["reason"]) == ("cancelled", 2, "native_failed")
    outcome["cancel_after_other_entry_ended_it"] = "version_conflict"

    # F22: neutral resume. Another entry resumes the Work after the read check and
    # before the enqueue writer: nothing of Yuki's call is written.
    neutral = await _fail_by_turn(scene, "fn", "SECRET-GOAL-N 订票")
    original_enqueue = WorkRepository.enqueue

    async def enqueue(self, *args, **kwargs):
        if kwargs.get("work_id") == neutral and kwargs.get("resume") is not None:
            await _resumed_elsewhere(database, neutral)
        return await original_enqueue(self, *args, **kwargs)

    monkeypatch.setattr(WorkRepository, "enqueue", enqueue)
    async with database.read_session() as reader:
        inputs_before = int(
            await reader.scalar(
                select(func.count()).select_from(inputs).where(inputs.c.work_id == neutral)
            )
        )
    read, reply = await _owner_turn(
        scene,
        "window-neutral",
        [("get_tasks", {"id": neutral}), ("manage_task", {"action": "resume", "id": neutral})],
    )
    monkeypatch.setattr(WorkRepository, "enqueue", original_enqueue)
    assert read["status"] == "failed"
    assert "version_conflict" in _error_text(reply), reply
    row = await _row(database, neutral)
    assert (row["state"], row["revision"]) == ("queued", 3), row
    async with database.read_session() as reader:
        inputs_after = int(
            await reader.scalar(
                select(func.count()).select_from(inputs).where(inputs.c.work_id == neutral)
            )
        )
    assert inputs_after == inputs_before
    outcome["neutral_resume_enqueue_window"] = {
        "error": "version_conflict",
        "revision_moved_only_by_other_entry": True,
        "input_written": False,
    }
    RESULTS["S7_failed_record_and_resume_window"] = outcome
