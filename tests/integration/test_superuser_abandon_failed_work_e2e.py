"""E2E: a current superuser abandons a failed Work outside the turn's own scope (#673).

Replays #673 (2026-11-21 production: after #671 the owner asked Yuki to abandon the
largest failed holders; ``6cd62f2f``, a SELF Work of group ``5b234414`` at generation 32
while the group was at 44, could be cancelled by no entry but the operator CLI). Real
SQLite files, real ``ProtocolStore`` files and GC, the real watchdog, real Main private
and group turns and a real scheduled occurrence (the watchdog E2E Scene); a fake
provider only. The SELF Work is seeded with the source its admission writes
(``work_repository.accept_in_session``); the group then gets a new generation through
the real ``bump_canonical_generation``. Facts go to
``test-results/superuser-abandon-failed-work-e2e.json`` with stable labels instead of
IDs, digests or times, so two runs write the same bytes.

Authorization checked (task book §3.1): A1 the turn's original actor Person is a current
superuser, re-read in the writer; A2 the turn is that person's own direct input
(``principal_kind=person``, ``origin=user_message``, a trigger event); A3 the turn's
conversation is that person's private conversation; A4 a Main activation; A5 the target
is failed in the same writer and the action is cancel; A6 the activation lease still
holds; A7 the revision goes through the existing CAS (the one ``get_tasks`` read, else
the one read in the writer).

Failure cases written before the implementation (task book §4):

F1  a superuser in their private turn cannot abandon a failed SELF Work of an older
    group generation (today: ``work_not_found_or_not_authorized``);
F2  a superuser cannot abandon their own failed Work of an older private generation;
F3  a non-superuser abandons another scope's failed Work, or gets an error other than
    today's (``work_not_found_or_not_authorized`` / ``subtask_not_owned``);
F4  qualification revoked after the reader check and before the writer still
    abandons;
F5  a superuser's group turn or scheduled occurrence abandons another scope's Work;
F7  a target that is not failed (queued/running/suspended/completed/cancelled) is
    cancelled through this path; a resume landing between the reader and the writer
    is overwritten;
F8  this path opens resume, fail or release on another scope's Work;
F9  after ``get_tasks`` read failed at r, another entry moved it to r+1 (still failed)
    and the abandon succeeds; a retry without a new read succeeds;
F11 abandoning another person's failed delegation record calls the native service or
    registers an operation; abandoning a failed worker gives its parent no
    ``worker-result`` or revives a suspended parent;
F12 refs are not released, another Work's refs are dropped, a shared object is
    collected, or GC deletes inside the grace period;
F13 the target's source, owner, conversation, generation or ``failure_json`` change,
    or its reason carries the model's free text instead of ``superuser_abandoned``;
F14 the turn's own scope (local paths) changes behaviour (covered by the #671 E2E,
    ``tests/integration/test_failed_work_abandon_storage_e2e.py``, run alongside);
F16 any refused call writes: the target's state/revision/refs or the parent mailbox
    move;
F18 a call made inside a Work (the turn's own registered task, a worker or a later
    activation) abandons, or its refusal does not ask for a new direct message;
F19 the superuser's Desktop App or ACP session (both ``user_message`` private input) is
    refused (task book Q5);
F20 against the real resume entry (the superuser's chat device on
    ``POST /admin/v1/works/{id}/resume``) and the real scheduler: after ``get_tasks`` read
    failed at r, a resume first, or a resume and a real new failure (failed again at a
    newer revision), still lets the abandon or its retry through instead of
    ``version_conflict``; when both act on r at once and the abandon holds the writer,
    the resume is not refused with ``version_conflict``; a resume between the reader
    check and the writer is overwritten. Each conflict must leave every fact of the
    Work (row, refs, mailbox, waits, recovery, journal, effects) unchanged.

The artifact lists the scenarios whose assertions all passed (each records its facts
last) and ``all_scenarios_passed``; CI checks that flag, not only that a file exists.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select, update
from tests.conftest import MemorySender
from tests.integration.test_acp_runtime_integration import acp_runtime as acp_runtime
from tests.integration.test_acp_runtime_integration import new_session, selected
from tests.integration.test_failed_work_abandon_storage_e2e import (
    DAY,
    KB,
    Recorder,
    _attach,
    _error_text,
    _failed_record,
    _operations,
    _refs,
    _row,
    _usage,
)
from tests.integration.test_watchdog_capacity_incident_e2e import (
    Scene,
    _call,
    _runtime_context,
    _tool_results,
)
from tests.support.automation_runtime_helpers import FakeClock
from tests.support.canonical_identity import ensure_person
from tests.unit.test_admin_api import command as admin_command
from tests.unit.test_client_api import bearer

import yuki.runtime.work_control as work_control_module
from yuki.admin_api.http import attach_admin_api
from yuki.automation.repository import AutomationRepository
from yuki.automation.service import AutomationService
from yuki.automation.tools import AutomationToolService
from yuki.automation.worker import AutomationWorker
from yuki.client_api.db_models import ClientDeviceModel
from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.conversation.hydrate import bump_canonical_generation
from yuki.domain.conversations import ScopeType
from yuki.domain.messages import InboundMessage, SenderIdentity
from yuki.identity.db_models import CanonicalPersonModel
from yuki.llm.base import LLMInvalidRequestError
from yuki.persistence.models import AutomationRunModel, ChatEventModel
from yuki.runtime.proactive_schema import watchdog_issues
from yuki.runtime.protocol_schema import objects, refs
from yuki.runtime.protocol_store import ProtocolStore
from yuki.runtime.subagent_schema import children
from yuki.runtime.work_activation import current_work_control
from yuki.runtime.work_recovery_schema import recovery
from yuki.runtime.work_schema_v1 import effects, inputs, journal, work
from yuki.runtime.work_wait_schema import waits
from yuki.time.service import TimeContextService

ARTIFACT = (
    Path(__file__).resolve().parents[2] / "test-results" / "superuser-abandon-failed-work-e2e.json"
)
RESULTS: dict[str, Any] = {}
DENIED = "work_not_found_or_not_authorized"
PRIVATE_ONLY = "superuser_abandon_private_only"
FAILED_ONLY = "superuser_abandon_failed_only"
DIRECT_ONLY = "superuser_abandon_direct_only"
# Each scenario records its facts only after all its assertions passed.
EXPECTED = (
    "S1_old_group_self_failed_work",
    "S2_own_old_generation_worker_and_record",
    "S3_refusals_write_nothing",
    "S4_only_failed_targets",
    "S5_desktop_app_and_acp",
    "S6_real_resume_races",
)


@pytest.fixture(scope="module", autouse=True)
def _write_artifact():
    yield
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    scenarios = json.dumps(RESULTS, ensure_ascii=False, indent=2, sort_keys=True)
    ARTIFACT.write_text(
        json.dumps(
            {
                "suite": "tests/integration/test_superuser_abandon_failed_work_e2e.py",
                "suite_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "scenarios": RESULTS,
                "scenarios_passed": sorted(RESULTS),
                "all_scenarios_passed": sorted(RESULTS) == sorted(EXPECTED),
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


async def _generation(database, conversation: str) -> int:
    async with database.read_session() as reader:
        return int(
            await reader.scalar(
                select(CanonicalConversationModel.generation).where(
                    CanonicalConversationModel.id == conversation
                )
            )
        )


async def _new_generation(database, conversation: str) -> int:
    """The real ``/ai new`` transition of a conversation."""
    async with database.immediate_session() as writer:
        event = await writer.scalar(select(func.max(ChatEventModel.id)))
        return await bump_canonical_generation(writer, conversation, event_id=event, force=True)


async def _self_work(
    scene: Scene, *, state: str = "failed", parent: str | None = None, worker: bool = False
) -> str:
    """A SELF Work of the group's current generation, as its admission writes it."""
    database = scene.database
    conversation = scene.env.context.conversation_id
    generation = await _generation(database, conversation)
    identity = str(uuid.uuid4())
    now = time.time()
    source = {
        "origin": "self_initiative",
        "principal_kind": "self",
        "initiative_run_id": str(uuid.uuid4()),
        "conversation_id": conversation,
        "generation": generation,
        "presence_id": scene.env.presence,
        "space_id": scene.env.space,
        **({"worker": True} if worker else {}),
    }
    async with database.immediate_session() as writer:
        await writer.execute(
            work.insert().values(
                id=identity,
                parent_work_id=parent,
                conversation_id=conversation,
                generation=generation,
                source_key=f"initiative:{source['initiative_run_id']}",
                source_json=json.dumps(source),
                goal="SECRET-GOAL SELF 自主整理",
                state=state,
                reason="LLMInvalidRequestError" if state == "failed" else None,
                model_requests=434,
                created=now,
                updated=now,
            )
        )
        if state == "failed":
            await writer.execute(
                recovery.insert().values(
                    work_id=identity,
                    activation_id=str(uuid.uuid4()),
                    exit_reason="failed",
                    failure_json=json.dumps({"code": "LLMInvalidRequestError"}),
                    updated=now,
                )
            )
        if worker:
            await writer.execute(
                children.insert().values(
                    work_id=identity, source_key=f"worker:{identity}", brief_json="{}"
                )
            )
    return identity


def _group_message(scene: Scene, text: str, key: str) -> InboundMessage:
    return InboundMessage(
        ingress_key=key,
        native_message_id=key,
        event_type="message:test",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity("10001", nickname="本人"),
        text=text,
        bot_user_id="80001",
        group_id="20001",
        mentions_bot=True,
        conversation_id=scene.env.context.conversation_id,
        person_id=scene.env.person,
        presence_id=scene.env.presence,
        space_id=scene.env.space,
    )


async def _turn(
    scene: Scene,
    key: str,
    calls: list[tuple[str, dict[str, Any]]],
    *,
    group: bool = False,
    hook=None,
) -> list[Any]:
    """One ordinary turn of the owner whose model makes ``calls`` in order.

    ``hook(index)`` runs before the model answers request ``index``: another entry
    acting between two tool calls of this turn.
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
    message = (
        _group_message(scene, f"{key} 处理一下", key)
        if group
        else scene.message(f"{key} 处理一下", key)
    )
    try:
        await scene.harness.processor.handle(message, MemorySender())
    finally:
        scene.provider._complete = original
        scene.ordinary = lambda request: "好"
    results: dict[str, Any] = {}
    for request in answered:
        results.update(_tool_results(request))
    return [results.get(f"{key}-{index}") for index in range(len(calls))]


async def _facts(database, *identities: str) -> dict[str, Any]:
    """What a refused call must not move: each target's row, its refs, and all mail."""
    rows = {}
    for identity in identities:
        row = await _row(database, identity)
        rows[identity] = (
            row["state"],
            row["revision"],
            row["reason"],
            row["source_json"],
            row["conversation_id"],
            row["generation"],
            tuple(sorted(await _refs(database, identity))),
        )
    async with database.read_session() as reader:
        mail = int(await reader.scalar(select(func.count()).select_from(inputs)))
    return {"rows": rows, "inputs": mail}


async def _set_superuser(database, person: str, value: bool) -> None:
    async with database.immediate_session() as writer:
        await writer.execute(
            update(CanonicalPersonModel)
            .where(CanonicalPersonModel.id == person)
            .values(is_superuser=value)
        )


def _code(result: Any, code: str) -> bool:
    return code in _error_text(result)


def _failure_code(database_row_failure: str | None) -> str | None:
    return json.loads(database_row_failure)["code"] if database_row_failure else None


async def _failure(database, identity: str) -> str | None:
    async with database.read_session() as reader:
        return _failure_code(
            await reader.scalar(
                select(recovery.c.failure_json).where(recovery.c.work_id == identity)
            )
        )


# --------------------------------------------------------------------------- S1


async def test_owner_abandons_an_old_group_self_failed_work_from_private(database, tmp_path):
    """#673 production shape: watchdog names it, the owner abandons it, GC reclaims it."""
    scene = await Scene(database, tmp_path).build()
    group = scene.env.context.conversation_id
    held = await _self_work(scene)
    old_generation = await _generation(database, group)
    pad = await _attach(database, held, "pad-SELF", 300 * KB)
    shared = await _attach(database, held, "shared-live", 50 * KB)
    # The group moves on (production: generation 32 -> 44).
    current_generation = await _new_generation(database, group)
    assert current_generation == old_generation + 1
    # A live Work of the group's current generation shares one object with it.
    live = await _self_work(scene, state="running")
    assert await _attach(database, live, "shared-live", 50 * KB) == shared
    assert (await _row(database, held))["state"] == "failed"
    assert (await _row(database, held))["generation"] == old_generation

    # The watchdog names it among the failed holders near the cap.
    scene.cap[0] = int(await _usage(database) / 0.9)
    assert await scene.tick() == ["finished"]
    context = _runtime_context(scene.notice_requests[0])
    assert context is not None
    facts = {issue["key"]: issue["facts"] for issue in context["watchdog"]["issues"]}
    holders = facts["protocol_storage_near_cap"]["failed_holders"]
    named = [holder for holder in holders if holder["work_id"] == held]
    assert named and named[0]["origin"] == "self_initiative"
    assert named[0]["conversation_id"] == group
    scene.cap[0] = 2 * 1024**3
    async with database.read_session() as reader:
        open_issues = set(await reader.scalars(select(watchdog_issues.c.issue_key)))
    assert "protocol_storage_near_cap" in open_issues

    # F1: the owner's private turn reads it and abandons it.
    before = await _row(database, held)
    read, cancelled = await _turn(
        scene,
        "abandon",
        [
            ("get_tasks", {"id": held}),
            ("manage_task", {"action": "cancel", "id": held, "text": "SECRET-TEXT 主人同意"}),
        ],
    )
    assert read["status"] == "failed", read
    assert cancelled == {"id": held, "status": "cancelled"}, cancelled
    after = await _row(database, held)
    assert after["state"] == "cancelled"
    assert after["revision"] == before["revision"] + 1
    # F13: fixed reason code; the original source and owner are kept.
    assert after["reason"] == "superuser_abandoned"
    for key in ("source_json", "conversation_id", "generation", "parent_work_id", "goal"):
        assert after[key] == before[key], key
    assert await _failure(database, held) == "LLMInvalidRequestError"
    # F12: its refs are released; the live Work keeps the shared one.
    assert await _refs(database, held) == set()
    assert await _refs(database, live) == {shared}

    store = ProtocolStore(database)
    used = await _usage(database)
    while await store.cleanup():
        pass
    assert await _usage(database) == used and store._path(pad).exists()
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
    while await store.cleanup():
        pass
    dropped = used - await _usage(database)
    assert pad in {row.sha256 for row in unowned}
    assert dropped == sum(row.byte_size for row in unowned)
    assert not store._path(pad).exists() and store._path(shared).exists()

    RESULTS["S1_old_group_self_failed_work"] = {
        "generation_moved_on": current_generation - old_generation,
        "watchdog_named_it": {"origin": named[0]["origin"], "same_conversation": True},
        "owner_private_turn": {
            "get_tasks_status": read["status"],
            "manage_task_result": {"id": "SELF", "status": cancelled["status"]},
            "revision_step": after["revision"] - before["revision"],
            "reason": after["reason"],
            "model_text_written": "SECRET-TEXT" in str(after["reason"]),
            "source_owner_conversation_generation_kept": True,
            "failure_code_kept": await _failure(database, held),
        },
        "refs": {"target_after": 0, "live_keeps_shared": True},
        "gc": {
            "reclaimed_inside_grace": False,
            "usage_drop_equals_unowned_bytes": True,
            "target_object_removed": True,
            "shared_object_kept": True,
        },
    }


# --------------------------------------------------------------------------- S2


async def test_owner_abandons_own_old_generation_work_worker_and_others_record(database, tmp_path):
    scene = await Scene(database, tmp_path).build()
    private = await scene.conversation()
    group = scene.env.context.conversation_id

    # F2: the owner's own failed Work of an older private generation (seeded as the
    # private Host admission writes it), after the private conversation's /ai new.
    own = str(uuid.uuid4())
    now = time.time()
    private_generation = await _generation(database, private)
    async with database.immediate_session() as writer:
        await writer.execute(
            work.insert().values(
                id=own,
                conversation_id=private,
                generation=private_generation,
                source_key=f"private:{own}",
                source_json=json.dumps(
                    {
                        "origin": "user_message",
                        "principal_kind": "person",
                        "actor_person_id": scene.env.person,
                    }
                ),
                goal="SECRET-GOAL 本人旧任务",
                state="failed",
                reason="RemoteProtocolError",
                created=now,
                updated=now,
            )
        )
    await _attach(database, own, "pad-own", 64 * KB)
    await _new_generation(database, private)

    # F11: another person's failed delegation record of an older group generation,
    # and a failed worker under a suspended SELF parent of the current one.
    async with database.immediate_session() as writer:
        other = await ensure_person(writer, "10002")
    delegated = await _failed_record(database, group, other)
    await _new_generation(database, group)
    parent = await _self_work(scene, state="suspended")
    worker = await _self_work(scene, parent=parent, worker=True)
    await _attach(database, worker, "pad-worker", 32 * KB)
    recorder = Recorder()
    database.external_agents = recorder
    operations = await _operations(database)

    def mail_for(identity: str):
        return select(func.count()).select_from(inputs).where(inputs.c.work_id == identity)

    async with database.read_session() as reader:
        parent_mail = int(await reader.scalar(mail_for(parent)))
    first, second, third = await _turn(
        scene,
        "mine",
        [
            ("manage_task", {"action": "cancel", "id": own}),
            ("manage_task", {"action": "cancel", "id": worker}),
            ("manage_task", {"action": "cancel", "id": delegated["record"]}),
        ],
    )
    assert first == {"id": own, "status": "cancelled"}, first
    assert second == {"id": worker, "status": "cancelled"}, second
    assert third == {"id": delegated["record"], "status": "cancelled"}, third
    for identity in (own, worker, delegated["record"]):
        row = await _row(database, identity)
        assert row["state"] == "cancelled" and row["reason"] == "superuser_abandoned"
        assert await _refs(database, identity) == set()
    # The parent gets the stop evidence without being revived.
    async with database.read_session() as reader:
        parent_mail_after = int(await reader.scalar(mail_for(parent)))
    assert parent_mail_after == parent_mail + 1
    assert (await _row(database, parent))["state"] == "suspended"
    # The record's ended native run is not touched; no operation is registered.
    assert recorder.calls == []
    assert await _operations(database) == operations
    record_source = json.loads((await _row(database, delegated["record"]))["source_json"])
    assert record_source["actor_person_id"] == other

    RESULTS["S2_own_old_generation_worker_and_record"] = {
        "own_old_private_generation": first["status"],
        "worker_under_suspended_self_parent": {
            "status": second["status"],
            "parent_worker_result_inputs": parent_mail_after - parent_mail,
            "parent_state": "suspended",
        },
        "other_persons_delegation_record": {
            "status": third["status"],
            "native_calls": len(recorder.calls),
            "new_operations": await _operations(database) - operations,
            "owner_kept": True,
        },
        "reason": "superuser_abandoned",
    }


# --------------------------------------------------------------------------- S3


async def test_refusals_write_nothing(database, tmp_path, monkeypatch):
    """Non-superuser, group, schedule, actions other than cancel, revocation in between."""
    scene = await Scene(database, tmp_path).build()
    group = scene.env.context.conversation_id
    target = await _self_work(scene)
    await _attach(database, target, "pad-target", 16 * KB)
    async with database.immediate_session() as writer:
        other = await ensure_person(writer, "10002")
    delegated = await _failed_record(database, group, other)
    database.external_agents = Recorder()
    await _new_generation(database, group)
    watched = (target, delegated["record"])
    baseline = await _facts(database, *watched)
    outcomes: dict[str, Any] = {}

    # F3: a non-superuser gets today's errors.
    await _set_superuser(database, scene.env.person, False)
    plain, record = await _turn(
        scene,
        "plain",
        [
            ("manage_task", {"action": "cancel", "id": target}),
            ("manage_task", {"action": "cancel", "id": delegated["record"]}),
        ],
    )
    assert _code(plain, DENIED), plain
    assert _code(record, "subtask_not_owned"), record
    assert await _facts(database, *watched) == baseline
    outcomes["non_superuser"] = {"work": DENIED, "record": "subtask_not_owned"}
    await _set_superuser(database, scene.env.person, True)

    # F5: the superuser's group turn.
    (in_group,) = await _turn(
        scene, "group", [("manage_task", {"action": "cancel", "id": target})], group=True
    )
    assert _code(in_group, PRIVATE_ONLY), in_group
    assert await _facts(database, *watched) == baseline
    outcomes["superuser_group_turn"] = PRIVATE_ONLY

    # F8: only cancel is opened; resume, fail and release keep today's errors.
    resume, fail, release = await _turn(
        scene,
        "other-actions",
        [
            ("manage_task", {"action": "resume", "id": target}),
            ("manage_task", {"action": "fail", "id": target}),
            ("manage_task", {"action": "release", "id": delegated["record"]}),
        ],
    )
    assert _code(resume, "resume_work_not_authorized"), resume
    assert _code(fail, DENIED), fail
    assert _code(release, "subtask_not_owned"), release
    assert await _facts(database, *watched) == baseline
    outcomes["other_actions"] = {
        "resume": "resume_work_not_authorized",
        "fail": DENIED,
        "release": "subtask_not_owned",
    }

    # F4: qualification revoked after the reader check, before the writer.
    real = getattr(work_control_module, "person_is_superuser", None)
    revoked: list[int] = []

    async def revoke_after_read(session, person_id):
        allowed = await real(session, person_id)
        if not revoked:
            revoked.append(1)
            await _set_superuser(database, scene.env.person, False)
        return allowed

    monkeypatch.setattr(
        work_control_module, "person_is_superuser", revoke_after_read, raising=False
    )
    (raced,) = await _turn(scene, "revoked", [("manage_task", {"action": "cancel", "id": target})])
    monkeypatch.setattr(work_control_module, "person_is_superuser", real, raising=False)
    assert revoked == [1]
    assert _code(raced, DENIED), raced
    assert await _facts(database, *watched) == baseline
    outcomes["revoked_between_reader_and_writer"] = DENIED
    await _set_superuser(database, scene.env.person, True)

    # F5: the superuser's own scheduled occurrence in their private conversation.
    settings = scene.chat._settings.model_copy(update={"automation_enabled": True})
    clock = FakeClock(datetime.now(UTC))
    repository = AutomationRepository(database)
    time_service = TimeContextService(default_timezone=settings.default_timezone, clock=clock)
    service = AutomationService(settings=settings, repository=repository, time_service=time_service)
    scene.chat.set_automation_tools(AutomationToolService(service))
    automation_worker = AutomationWorker(
        settings=settings,
        repository=repository,
        time_service=time_service,
        generate=scene.chat.generate_origin_turn,
        runtime_config=scene.chat._runtime_config,
        conversation_scopes=scene.chat._conversation_scopes,
        turns=scene.chat._turn_coordinator,
        concurrency=scene.chat._concurrency,
        work_repository=scene.chat._work_repository,
        bindings=scene.chat.runtime.bindings,
    )
    (created,) = await _turn(
        scene,
        "schedule",
        [
            (
                "manage_schedule",
                {
                    "action": "create",
                    "name": "放弃",
                    "goal": "放弃那个失败的任务",
                    "trigger": {"type": "after", "seconds": 1},
                },
            )
        ],
    )
    assert "error" not in _error_text(created).casefold(), created
    occurrence: list[Any] = []

    def scheduled(request):
        control = current_work_control.get()
        assert control is not None and control.source["origin"] == "scheduled_automation"
        assert control.source["actor_person_id"] == scene.env.person
        occurrence.append(request)
        if len(occurrence) == 1:
            return _call("manage_task", {"action": "cancel", "id": target}, "scheduled-0")
        return "done"

    scene.ordinary = scheduled
    clock.advance(2)
    async for claimed in repository.claim_due(
        worker_id="e2e-owner",
        now=clock.now(),
        lease_seconds=settings.automation_lease_seconds,
        clock=clock.now,
    ):
        await automation_worker._process_guarded(claimed)
    scene.ordinary = lambda request: "好"
    async with database.read_session() as reader:
        runs = list(await reader.scalars(select(AutomationRunModel)))
    assert runs, "the occurrence did not run"
    answers: dict[str, Any] = {}
    for request in occurrence:
        answers.update(_tool_results(request))
    scheduled_result = answers.get("scheduled-0")
    assert _code(scheduled_result, PRIVATE_ONLY), answers
    assert await _facts(database, *watched) == baseline
    outcomes["superuser_scheduled_occurrence"] = PRIVATE_ONLY

    RESULTS["S3_refusals_write_nothing"] = outcomes


# --------------------------------------------------------------------------- S4


async def test_only_failed_targets(database, tmp_path):
    """Targets that are not failed are refused before any write."""
    scene = await Scene(database, tmp_path).build()
    group = scene.env.context.conversation_id
    states = ("queued", "running", "suspended", "completed", "cancelled")
    targets = {state: await _self_work(scene, state=state) for state in states}
    await _new_generation(database, group)
    baseline = await _facts(database, *targets.values())
    results = await _turn(
        scene,
        "states",
        [("manage_task", {"action": "cancel", "id": targets[state]}) for state in states],
    )
    for state, result in zip(states, results, strict=True):
        assert _code(result, FAILED_ONLY), (state, result)
    assert await _facts(database, *targets.values()) == baseline
    RESULTS["S4_only_failed_targets"] = {state: FAILED_ONLY for state in states}


# --------------------------------------------------------------- real deployment


class Deployment:
    """The full ApplicationContainer: Desktop App and ACP turns, admin HTTP, scheduler.

    The superuser's own paired chat device is both the Desktop/ACP sender and the
    real resume entry (``POST /admin/v1/works/{id}/resume`` with its chat token).
    Failed Works come from real ACP turns whose model rejects the request.
    """

    def __init__(self, env, database) -> None:
        self.env, self.database = env, database
        self.app, self.http = env.app, env.http
        attach_admin_api(env.http_app, lambda: env.app.admin_api)
        self.token = env.device["token"]
        self.answers: list[Any] = []
        self.hook = None
        self.failing: set[str] = set()
        self._in_hook = False
        self.hook_errors: list[BaseException] = []
        original = env.provider._complete

        async def complete(request):
            if self.hook is not None and not self._in_hook:
                self._in_hook = True
                try:
                    await self.hook(len(self.answers))
                except BaseException as exc:
                    # Raised again by turn(): a hook's own assertion must fail the test.
                    self.hook_errors.append(exc)
                    raise
                finally:
                    self._in_hook = False
            control = current_work_control.get()
            current = control.current["id"] if control and control.current else None
            if current in self.failing:
                raise LLMInvalidRequestError("SECRET-FAILURE provider rejected the request")
            return await original(request)

        env.provider._complete = complete

    async def setup(self) -> Deployment:
        async with self.database.read_session() as reader:
            self.person = await reader.scalar(
                select(ClientDeviceModel.person_id).where(
                    ClientDeviceModel.label == "ACP integration device"
                )
            )
        await _set_superuser(self.database, self.person, True)
        return self

    def headers(self, session: dict[str, Any] | None) -> dict[str, str]:
        return selected(self.env, session) if session else bearer(self.token)

    async def turn(
        self,
        key: str,
        calls: list[tuple[str, dict[str, Any]]],
        *,
        session: dict[str, Any] | None = None,
        hook=None,
    ) -> list[Any]:
        """One direct input of the superuser: Desktop App, or the ACP ``session``."""
        answered: list[Any] = []

        def respond(request):
            answered.append(request)
            index = len(answered) - 1
            if index < len(calls):
                name, arguments = calls[index]
                return _call(name, arguments, f"{key}-{index}")
            return _call("send", {"text": "好"}, f"{key}-send") if index == len(calls) else "好"

        self.answers = answered
        self.env.provider._responder = respond
        self.hook = hook
        request_id = str(uuid.uuid4())
        try:
            submitted = await self.http.post(
                "/client/v1/messages",
                headers=self.headers(session),
                json={"request_id": request_id, "text": f"{key} 处理一下"},
            )
            assert submitted.status_code == 200, submitted.text
            async with asyncio.timeout(15):
                while True:
                    response = await self.http.get(
                        f"/client/v1/requests/{request_id}", headers=self.headers(session)
                    )
                    assert response.status_code == 200, response.text
                    if response.json()["data"].get("turn", {}).get("state") == "ended":
                        break
                    await asyncio.sleep(0.01)
        finally:
            self.hook = None
        if self.hook_errors:
            raise self.hook_errors.pop()
        results: dict[str, Any] = {}
        for request in answered:
            results.update(_tool_results(request))
        return [results.get(f"{key}-{index}") for index in range(len(calls))]

    async def failed_in_acp(self, session: dict[str, Any], goal: str) -> str:
        """A real ACP turn registers the task, then the model rejects the request."""
        async with self.database.read_session() as reader:
            before = set(await reader.scalars(select(work.c.id)))
        holder: list[str] = []

        async def fail_after_start(index: int) -> None:
            if index == 1:
                async with self.database.read_session() as reader:
                    created = set(await reader.scalars(select(work.c.id))) - before
                assert len(created) == 1, created
                holder.extend(created)
                self.failing |= created

        await self.turn(
            f"fail-{goal}",
            [("manage_task", {"action": "start", "goal": f"SECRET-GOAL {goal}"})],
            session=session,
            hook=fail_after_start,
        )
        (identity,) = holder
        self.failing.discard(identity)
        row = await _row(self.database, identity)
        assert row["state"] == "failed", row
        return identity

    async def resume(self, identity: str, revision: int):
        """The real resume entry: the superuser's chat device on the admin API."""
        return await self.http.post(
            f"/admin/v1/works/{identity}/resume",
            headers=bearer(self.token),
            json=admin_command(revision),
        )


async def _snapshot(database, identity: str) -> dict[str, Any]:
    """Every fact one Work owns: row, refs, mailbox, waits, recovery, journal, effects."""
    async with database.read_session() as reader:

        async def rows(table, column):
            result = await reader.execute(select(table).where(column == identity))
            return sorted(json.dumps(dict(row), default=str) for row in result.mappings())

        return {
            "work": await rows(work, work.c.id),
            "refs": await rows(refs, refs.c.work_id),
            "inputs": await rows(inputs, inputs.c.work_id),
            "waits": await rows(waits, waits.c.work_id),
            "recovery": await rows(recovery, recovery.c.work_id),
            "journal": await rows(journal, journal.c.work_id),
            "effects": await rows(effects, effects.c.work_id),
        }


# --------------------------------------------------------------------------- S5


async def test_desktop_app_and_acp_direct_input_abandon(acp_runtime, database):
    """Q5: the Desktop App and an ACP session are the superuser's direct input too."""
    deployment = await Deployment(acp_runtime, database).setup()
    origin_session = await new_session(acp_runtime)
    from_desktop = await deployment.failed_in_acp(origin_session, "A")
    from_acp = await deployment.failed_in_acp(origin_session, "B")
    in_work = await deployment.failed_in_acp(origin_session, "C")
    pad = await _attach(database, from_desktop, "pad-desktop", 32 * KB)

    # The Desktop App conversation is a different private conversation: the
    # origin session's Work is outside its own scope.
    read, abandoned = await deployment.turn(
        "desktop",
        [
            ("get_tasks", {"id": from_desktop}),
            ("manage_task", {"action": "cancel", "id": from_desktop, "text": "SECRET-TEXT"}),
        ],
    )
    assert read["status"] == "failed", read
    assert abandoned == {"id": from_desktop, "status": "cancelled"}, abandoned
    row = await _row(database, from_desktop)
    assert row["reason"] == "superuser_abandoned" and await _refs(database, from_desktop) == set()
    assert pad not in await _refs(database, from_desktop)

    # Another ACP session of the same person.
    other_session = await new_session(acp_runtime)
    (from_other,) = await deployment.turn(
        "acp", [("manage_task", {"action": "cancel", "id": from_acp})], session=other_session
    )
    assert from_other == {"id": from_acp, "status": "cancelled"}, from_other
    assert (await _row(database, from_acp))["reason"] == "superuser_abandoned"

    # Inside a Work (this turn first registers its own task) the call is refused and
    # tells Yuki to have the person say it directly in a new message.
    before = await _snapshot(database, in_work)
    started, refused = await deployment.turn(
        "inside-work",
        [
            ("manage_task", {"action": "start", "goal": "先登记一个任务"}),
            ("manage_task", {"action": "cancel", "id": in_work}),
        ],
    )
    assert "error" not in json.dumps(started), started
    assert _code(refused, DIRECT_ONLY), refused
    assert "另起一句直接说" in refused["message"], refused
    assert await _snapshot(database, in_work) == before

    RESULTS["S5_desktop_app_and_acp"] = {
        "desktop_app_turn": {"read": read["status"], "result": abandoned["status"]},
        "other_acp_session_turn": from_other["status"],
        "reason": "superuser_abandoned",
        "inside_a_work": {"error": DIRECT_ONLY, "says_new_direct_message": True},
        "refused_call_wrote_nothing": True,
    }


# --------------------------------------------------------------------------- S6


async def test_real_resume_entry_races_the_abandon(acp_runtime, database, monkeypatch):
    """The real resume entry and the abandon act on the same read revision."""
    deployment = await Deployment(acp_runtime, database).setup()
    session = await new_session(acp_runtime)
    outcomes: dict[str, Any] = {}

    # R1: Desktop reads it failed at r; the resume entry wins first; the abandon and a
    # retry without a new read are version_conflict and write nothing.
    resumed = await deployment.failed_in_acp(session, "R1")
    read_revision = (await _row(database, resumed))["revision"]
    after_resume: dict[str, Any] = {}

    async def resume_first(index: int) -> None:
        if index == 1:
            response = await deployment.resume(resumed, read_revision)
            assert response.status_code == 200, response.text
            after_resume.update(await _snapshot(database, resumed))

    read, conflict, retry = await deployment.turn(
        "r1",
        [
            ("get_tasks", {"id": resumed}),
            ("manage_task", {"action": "cancel", "id": resumed}),
            ("manage_task", {"action": "cancel", "id": resumed}),
        ],
        hook=resume_first,
    )
    assert read["status"] == "failed" and read.get("id", resumed) == resumed
    assert _code(conflict, "version_conflict"), conflict
    assert _code(retry, "version_conflict"), retry
    assert await _snapshot(database, resumed) == after_resume
    row = await _row(database, resumed)
    assert row["state"] == "queued" and row["revision"] == read_revision + 1
    outcomes["R1_resume_wins"] = {
        "resume": "queued",
        "abandon": "version_conflict",
        "retry_without_read": "version_conflict",
        "abandon_wrote_nothing": True,
    }

    # R2: resumed by the real entry and run by the real scheduler, it fails again:
    # still failed, at a newer revision. The read revision still guards.
    refailed = await deployment.failed_in_acp(session, "R2")
    read_revision = (await _row(database, refailed))["revision"]
    after_refail: dict[str, Any] = {}
    checked: list[int] = []

    async def resume_and_fail_again(index: int) -> None:
        if index == 1:
            response = await deployment.resume(refailed, read_revision)
            assert response.status_code == 200, response.text
            # The scheduler takes one Work of a conversation at a time, R1's queued
            # one first; both meet the failing model.
            deployment.failing |= {refailed, resumed}
            for _ in range(5):
                if (await _row(database, refailed))["state"] != "queued":
                    break
                for owner in await deployment.app.work_scheduler.dispatch_once():
                    await owner
            deployment.failing -= {refailed, resumed}
            row = await _row(database, refailed)
            assert row["state"] == "failed" and row["revision"] > read_revision + 1, row
            after_refail.update(await _snapshot(database, refailed))
        if index == 3:
            # Both conflicting calls are behind us: nothing they touched moved.
            assert await _snapshot(database, refailed) == after_refail
            checked.append(index)

    read, conflict, retry, reread, abandoned = await deployment.turn(
        "r2",
        [
            ("get_tasks", {"id": refailed}),
            ("manage_task", {"action": "cancel", "id": refailed}),
            ("manage_task", {"action": "cancel", "id": refailed}),
            ("get_tasks", {"id": refailed}),
            ("manage_task", {"action": "cancel", "id": refailed}),
        ],
        hook=resume_and_fail_again,
    )
    assert read["status"] == "failed"
    assert _code(conflict, "version_conflict"), conflict
    assert _code(retry, "version_conflict"), retry
    assert reread["status"] == "failed"
    assert abandoned == {"id": refailed, "status": "cancelled"}, abandoned
    assert checked == [3]
    outcomes["R2_resumed_and_failed_again"] = {
        "abandon_on_old_read": "version_conflict",
        "retry_without_read": "version_conflict",
        "conflicts_wrote_nothing": True,
        "after_new_read": abandoned["status"],
    }

    # R3: both act on the same read revision at once; the abandon holds the writer
    # when the resume entry arrives, so the resume is the loser.
    raced = await deployment.failed_in_acp(session, "R3")
    read_revision = (await _row(database, raced))["revision"]
    real_manage = work_control_module.manage_work
    real_writer = database.immediate_session
    queued = asyncio.Event()
    loser: list[asyncio.Task] = []

    def writer(*args, **kwargs):
        if loser and asyncio.current_task() is loser[0]:
            queued.set()
        return real_writer(*args, **kwargs)

    async def manage_holding_writer(session_, identity, *args, **kwargs):
        if identity == raced and not loser:
            loser.append(asyncio.create_task(deployment.resume(raced, read_revision)))
            await asyncio.wait_for(queued.wait(), 10)
        return await real_manage(session_, identity, *args, **kwargs)

    monkeypatch.setattr(database, "immediate_session", writer)
    monkeypatch.setattr(work_control_module, "manage_work", manage_holding_writer)
    read, abandoned = await deployment.turn(
        "r3",
        [
            ("get_tasks", {"id": raced}),
            ("manage_task", {"action": "cancel", "id": raced}),
        ],
    )
    monkeypatch.setattr(work_control_module, "manage_work", real_manage)
    monkeypatch.setattr(database, "immediate_session", real_writer)
    assert queued.is_set() and len(loser) == 1
    resume_response = await loser[0]
    assert abandoned == {"id": raced, "status": "cancelled"}, abandoned
    assert resume_response.status_code == 409, resume_response.text
    assert "version_conflict" in resume_response.text
    row = await _row(database, raced)
    assert row["state"] == "cancelled" and row["revision"] == read_revision + 1
    assert row["reason"] == "superuser_abandoned"
    outcomes["R3_same_revision_abandon_wins"] = {
        "abandon": "cancelled",
        "resume_entry": {"status": 409, "problem": "version_conflict"},
        "revision_step": 1,
    }

    # R4: no prior read; the resume entry lands between the reader check and the
    # writer. The abandon meets a queued Work and writes nothing.
    between = await deployment.failed_in_acp(session, "R4")
    read_revision = (await _row(database, between))["revision"]
    real_check = work_control_module.person_is_superuser
    landed: list[Any] = []

    async def resume_after_read(session_, person_id):
        allowed = await real_check(session_, person_id)
        if not landed:
            landed.append(await deployment.resume(between, read_revision))
            landed.append(await _snapshot(database, between))
        return allowed

    monkeypatch.setattr(work_control_module, "person_is_superuser", resume_after_read)
    (refused,) = await deployment.turn("r4", [("manage_task", {"action": "cancel", "id": between})])
    monkeypatch.setattr(work_control_module, "person_is_superuser", real_check)
    assert landed and landed[0].status_code == 200, landed
    assert _code(refused, FAILED_ONLY), refused
    assert await _snapshot(database, between) == landed[1]
    outcomes["R4_resume_between_reader_and_writer"] = {
        "resume": "queued",
        "abandon": FAILED_ONLY,
        "abandon_wrote_nothing": True,
    }

    RESULTS["S6_real_resume_races"] = outcomes
