"""E2E: a tool's post-call failure settles the same in a serial and a parallel batch (#669).

A tool's own failure becomes its failed result inside ``MainAgentBackend.execute_call``.
What raises after it -- tool receipt, result source fence, result rendering, journal --
aborts the whole turn. ``ToolInvocationCoordinator`` runs a parallel-safe stretch in an
``asyncio.TaskGroup``, so the same abort surfaced there as an ``ExceptionGroup`` and was
settled and logged as ``ExceptionGroup`` instead of its own class (production proactive
turn 9ba28996, 2026-11-20).

The scene is the watchdog E2E scene (isolated SQLite, fake Provider, the real Main,
Social, receipt recorder and result artifacts). The fault is raised by a real post-call
step after the tool ran (see ``Fault``). Only the batch shape differs: ``serial``
declares the tools not parallel-safe, ``parallel`` uses the catalog (one TaskGroup
stretch). Facts go to ``test-results/tool-batch-failure-settlement-e2e.json``; the
settlement table is deterministic (``scenarios_sha256``), so a rerun reproduces it.

Failure cases, written before the implementation. For each origin (a proactive private
turn without a Work, an ordinary private turn in its owned Work) and each fault, serial
and parallel must give the same settlement (proactive turn state/reason; processor
result, Work state, recovery exit and failure code) and the same warning/error lines:

F1  one failing call (MemoryPartitionResolutionError, the production case);
F2  two calls failing together with the same class: that class, not a group;
F3  two calls failing together with different classes: the first failure decides (it
    stops the stretch as it stops a serial batch), not a group of both;
F4  ``ValueError("tool_result_source_changed")`` (the result source fence);
F5  ``HistorySourceChangedError`` keeps its class;
F6  ``WorkBudgetExceeded`` keeps the root budget exit;
F7  ``WorkCapacityError`` keeps the capacity suspension of an owned Work;
F8  ``WorkConflict`` keeps its class and code;
F9  a call cancelled from inside (``CancelledError``) ends the turn cancelled; the
    parallel batch must not drop it as a missing result and continue the turn;
F10 the turn is cancelled from outside while it awaits the batch and a call fails in
    that window: the turn ends cancelled, as a serial call awaited at that moment is
    cancelled; neither the cancellation nor its owner's handling of it is lost;
F11 which faults abort the turn is unchanged: after every fault above the model sees
    no batch result.

Added in the review of #672 (each failed on the first version, which raised the
failure and delivered the cancellation again after it):

F12 the Work lease heartbeat fails while the turn awaits the batch and a call fails in
    that window (private turn only; a proactive turn holds no Work lease): the lease
    supervisor receives its own cancellation inside its scope and raises the
    heartbeat failure, as when a serial call is interrupted.

The artifact records the hash of the code under test and, per scenario, whether
serial and parallel matched, so a table produced from other code identifies itself.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from tests.conftest import MemorySender
from tests.integration.test_watchdog_capacity_incident_e2e import (
    _runtime_context,
    _scene,
    _tool_results,
)

from yuki.capabilities.coordinator import ToolInvocationCoordinator
from yuki.domain.messages import ChatResponse, ToolCall, ToolFunction
from yuki.memory.partition import MemoryPartitionResolutionError
from yuki.persistence.database import Database
from yuki.persistence.event_repository import ConversationReadVersion
from yuki.runtime import lease_heartbeat
from yuki.runtime.proactive_schema import proactive_turns
from yuki.runtime.work_budget import WorkBudgetExceeded
from yuki.runtime.work_journal import WorkJournal
from yuki.runtime.work_recovery_schema import recovery
from yuki.runtime.work_repository import WorkCapacityError, WorkConflict
from yuki.runtime.work_schema_v1 import work
from yuki.services.main_agent_backend import MainAgentBackend
from yuki.services.turn_coordinator import HistorySourceChangedError

ARTIFACT = (
    Path(__file__).resolve().parents[2] / "test-results" / "tool-batch-failure-settlement-e2e.json"
)
RESULTS: dict[str, Any] = {}
DIAGNOSTICS: dict[str, Any] = {}
# Three parallel-safe catalog tools that succeed in this scene.
TOOLS = ("get_environment", "get_schedules", "get_members")
ROOT = Path(__file__).resolve().parents[2]
CODE_UNDER_TEST = ("src/yuki/capabilities/coordinator.py", "src/yuki/services/agent_runner.py")


@pytest.fixture(scope="module", autouse=True)
def _write_artifact():
    yield
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).read_bytes()
    table = json.dumps(RESULTS, ensure_ascii=False, indent=2, sort_keys=True)
    ARTIFACT.write_text(
        json.dumps(
            {
                "suite": "tests/integration/test_tool_batch_failure_settlement_e2e.py",
                "generated_at": datetime.now(UTC).isoformat(),
                "suite_sha256": hashlib.sha256(source).hexdigest(),
                "code_sha256": {
                    path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
                    for path in CODE_UNDER_TEST
                },
                "scenarios_sha256": hashlib.sha256(table.encode()).hexdigest(),
                "scenarios": RESULTS,
                # Timing-dependent statement diagnostics, kept out of the hashed table.
                "database_diagnostics": DIAGNOSTICS,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _version() -> ConversationReadVersion:
    return ConversationReadVersion(
        scope=None,  # type: ignore[arg-type]
        conversation_id=None,
        generation=0,
        starts_after_event_id=0,
    )


# fault name -> one exception per failing call, in call order
FAULTS: dict[str, Any] = {
    "F1_single": lambda: [MemoryPartitionResolutionError("self_execution_source_missing")],
    "F2_two_same": lambda: [
        MemoryPartitionResolutionError("self_execution_source_missing"),
        MemoryPartitionResolutionError("self_execution_source_missing"),
    ],
    "F3_two_mixed": lambda: [
        MemoryPartitionResolutionError("self_execution_source_missing"),
        ValueError("tool_result_source_changed"),
    ],
    "F4_source_fence": lambda: [ValueError("tool_result_source_changed")],
    "F5_history_source": lambda: [HistorySourceChangedError(_version())],
    "F6_budget": lambda: [WorkBudgetExceeded("work_total_budget_exhausted")],
    "F7_capacity": lambda: [WorkCapacityError("work_protocol_storage_capacity")],
    "F8_conflict": lambda: [WorkConflict("work_activation_obsolete")],
    "F9_inner_cancel": lambda: [asyncio.CancelledError()],
    "F10_outer_cancel": lambda: [MemoryPartitionResolutionError("self_execution_source_missing")],
    "F12_lease_lost": lambda: [MemoryPartitionResolutionError("self_execution_source_missing")],
}
_IDS = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|\b[0-9a-f]{32}\b")


class Fault:
    """Fail a real post-call step after the tool ran.

    ``render``: the result renderer of ``MainAgentBackend.execute_call`` (a turn without
    a Work, as the proactive turn). ``journal``: the accepted-result write of an owned
    Work; a render failure there is already the call's own receipt
    (``tool_result_publication_failed``) in both batch shapes and aborts nothing.
    """

    def __init__(self, monkeypatch, fault: str, mode: str, where: str) -> None:
        self.fault = fault
        self.mode = mode
        self.errors = FAULTS[fault]()
        self.failing = TOOLS[: len(self.errors)]
        self.outer: asyncio.Task[Any] | None = None
        self.turn: asyncio.Task[Any] | None = None  # the task that awaits the batch
        self.lease_lost = asyncio.Event()
        # F2/F3: in a parallel stretch both failing calls reach their failing step
        # together; the second fails in the same loop iteration right after the first
        # (a bare TaskGroup collected both into one group). Serially it never runs.
        self.arrived = asyncio.Barrier(len(self.errors))
        self.first_failed = asyncio.Event()
        execute_batch = ToolInvocationCoordinator.execute_batch
        render = MainAgentBackend.render_result
        record_effect = WorkJournal.record_effect
        parallel_safe = MainAgentBackend.parallel_safe
        fixture = self

        async def fail(name: str) -> None:
            if name not in fixture.failing:
                return
            position = fixture.failing.index(name)
            error = fixture.errors[position]
            if fixture.mode == "parallel" and len(fixture.errors) > 1:
                await fixture.arrived.wait()
                if position:
                    await fixture.first_failed.wait()
            fixture.first_failed.set()
            if fixture.fault in {"F10_outer_cancel", "F12_lease_lost"}:
                # The turn's cancellation arrives while it awaits this call; the call
                # fails at its next step. A serial call is awaited by the turn itself,
                # so it receives the cancellation there and never gets to fail.
                turn = fixture.turn
                assert turn is not None
                if fixture.fault == "F10_outer_cancel":
                    asyncio.get_running_loop().call_soon(turn.cancel)
                else:
                    fixture.lease_lost.set()
                for _ in range(100):
                    await asyncio.sleep(0)
                    if turn.cancelling():
                        break
                assert turn.cancelling(), "the turn was never cancelled"
            raise error

        async def capturing_batch(self, *args, **kwargs):
            fixture.turn = asyncio.current_task()
            return await execute_batch(self, *args, **kwargs)

        async def lost_lease(renew, expiry, **kwargs):
            await fixture.lease_lost.wait()
            raise RuntimeError("lease_renew_failed")

        async def failing_render(self, outcome):
            text = await render(self, outcome)
            await fail(outcome.tool_name)
            return text

        async def failing_record(self, key, state, receipt, **kwargs):
            if state == "accepted" and isinstance(receipt.get("outcome"), dict):
                await fail(receipt["outcome"].get("tool"))
            await record_effect(self, key, state, receipt, **kwargs)

        def serial_or_catalog(self, name, runtime):
            return fixture.mode == "parallel" and parallel_safe(self, name, runtime)

        if where == "render":
            monkeypatch.setattr(MainAgentBackend, "render_result", failing_render)
        else:
            monkeypatch.setattr(WorkJournal, "record_effect", failing_record)
        monkeypatch.setattr(MainAgentBackend, "parallel_safe", serial_or_catalog)
        monkeypatch.setattr(ToolInvocationCoordinator, "execute_batch", capturing_batch)
        if fault == "F12_lease_lost":
            monkeypatch.setattr(lease_heartbeat, "lease_heartbeat", lost_lease)


def _batch() -> ChatResponse:
    return ChatResponse(
        "",
        0,
        tool_calls=tuple(ToolCall(f"r-{name}", ToolFunction(name, "{}")) for name in TOOLS),
    )


# Statement diagnostics: a sibling's in-flight statement cancelled by the aborting
# stretch (a serial batch never started that sibling), or a writer that waited out the
# busy timeout. Their number depends on timing; kept in the artifact, not compared.
STATEMENT_DIAGNOSTIC = "WARNING yuki.persistence.sqlite_diagnostics sqlite_statement_failed"


def _logs(caplog) -> list[str]:
    return [
        f"{record.levelname} {record.name} {_IDS.sub('<id>', record.getMessage())}"
        for record in caplog.records
        if record.levelno >= logging.WARNING
    ]


def _settlement(observed: dict[str, Any]) -> dict[str, Any]:
    return {
        **observed,
        "logs": [line for line in observed["logs"] if not line.startswith(STATEMENT_DIAGNOSTIC)],
    }


async def _outcome(scene, run, fault: Fault, caplog) -> dict[str, Any]:
    caplog.clear()
    task = asyncio.ensure_future(run())
    fault.outer = task
    try:
        returned: Any = await task
    except asyncio.CancelledError:
        returned = "CancelledError"
    return {"returned": returned, "logs": _logs(caplog)}


async def _proactive(database, tmp_path, monkeypatch, caplog, fault_name, mode):
    scene = await _scene(database, tmp_path)
    fault = Fault(monkeypatch, fault_name, mode, "render")
    after_batch: list[dict[str, Any]] = []

    def respond(request):
        context = _runtime_context(request)
        if context is None or "watchdog" not in context:
            return "好"
        replies = _tool_results(request)
        if not replies:
            return _batch()
        after_batch.append(replies)
        return "说完了"

    scene.respond = respond
    scene.provider._responder = respond
    scene.loops = {"runtime_work": {"running": False, "errors": {}}}
    outcome = await _outcome(scene, scene.tick, fault, caplog)
    turns = await scene.rows(proactive_turns)
    return {
        "returned": outcome["returned"],
        "turns": [(row["state"], row["reason"]) for row in turns],
        "model_saw_batch_result": bool(after_batch),
        "logs": outcome["logs"],
    }


async def _private(database, tmp_path, monkeypatch, caplog, fault_name, mode):
    scene = await _scene(database, tmp_path)
    fault = Fault(monkeypatch, fault_name, mode, "journal")
    after_batch: list[dict[str, Any]] = []

    def ordinary(request):
        replies = _tool_results(request)
        if not replies:
            return _batch()
        after_batch.append(replies)
        return "好"

    scene.ordinary = ordinary

    async def handle():
        result = await scene.harness.processor.handle(
            scene.message("帮我看看环境", f"turn-{fault_name}-{mode}"), MemorySender()
        )
        return result.reason

    outcome = await _outcome(scene, handle, fault, caplog)
    works = await scene.rows(work)
    recoveries = await scene.rows(recovery)
    return {
        "returned": outcome["returned"],
        "works": sorted((row["state"], row["reason"] or "") for row in works),
        "recoveries": sorted(
            (row["exit_reason"], json.loads(row["failure_json"]).get("code")) for row in recoveries
        ),
        "model_saw_batch_result": bool(after_batch),
        "logs": outcome["logs"],
    }


ORIGINS = {"proactive": _proactive, "private": _private}


@pytest.mark.parametrize(
    ("origin", "fault"),
    [
        (origin, fault)
        for origin in ORIGINS
        for fault in FAULTS
        if (origin, fault) != ("proactive", "F12_lease_lost")
    ],
)
async def test_serial_and_parallel_batches_settle_a_post_call_failure_the_same(
    database, _database_template, tmp_path, monkeypatch, caplog, origin, fault
):
    caplog.set_level(logging.WARNING)
    # The parallel run gets its own copy of the same seeded template.
    (tmp_path / "parallel").mkdir()
    copy = tmp_path / "parallel" / "test.db"
    await asyncio.to_thread(shutil.copyfile, _database_template, copy)
    databases = {"serial": database, "parallel": Database(f"sqlite+aiosqlite:///{copy}")}
    observed = {}
    try:
        for mode in ("serial", "parallel"):
            with monkeypatch.context() as patch:
                observed[mode] = await ORIGINS[origin](
                    databases[mode], tmp_path / mode, patch, caplog, fault, mode
                )
    finally:
        await databases["parallel"].close()
    settled = {mode: _settlement(value) for mode, value in observed.items()}
    RESULTS[f"{origin}:{fault}"] = {
        **settled,
        "serial_equals_parallel": settled["serial"] == settled["parallel"],
    }
    DIAGNOSTICS[f"{origin}:{fault}"] = {
        mode: [line for line in value["logs"] if line.startswith(STATEMENT_DIAGNOSTIC)]
        for mode, value in observed.items()
    }
    assert settled["parallel"] == settled["serial"]
    # F11: every fault still aborts the turn.
    assert observed["serial"]["model_saw_batch_result"] is False
    if fault == "F9_inner_cancel":
        # F9: the cancellation ends the turn: the proactive turn task is cancelled
        # before it settles (left for the lost-attempt path); the private turn raises it.
        if origin == "proactive":
            assert observed["parallel"]["turns"] == [("running", None)]
        else:
            assert observed["parallel"]["returned"] == "CancelledError"
