"""E2E (#675): a Work stopped by a runtime exception is told once; a model's own end is not.

Real private turns of the owner, the real Main, Work admission, ``recover_failure``,
``WorkScheduler``/``WorkResumer``, Social and the owner's proactive route (the scene of
``test_watchdog_capacity_incident_e2e.py``). Runtime failures are real: the fake
Provider raises ``httpx.RemoteProtocolError`` (classified ``activation``, not
retryable) or ``LLMUnavailableError`` (retryable). Only time is moved. Facts go to
``test-results/watchdog-failed-work-e2e.json`` with every internal ID replaced by a
stable label, so two runs of one commit give the same bytes.

Failure cases written before the implementation (task book §6):

F1  a root Work that a real runtime exception left failed is not reported;
F2  a Work the model ended with ``manage_task(fail)`` is reported;
F3  a retryable failure, then the model's own fail, is reported;
F4  a runtime failure, a resume, then the model's own fail, is reported (F-a1);
F5  a decided fail followed by an auxiliary exception (P3) is reported (Q1b);
F6  a management ``fail`` (admin/client/manage_task on another target) is reported;
F7  a failed worker or a failed delegation record is reported on its own (Q5);
F8  a retryable failure (queued) or a capacity suspension enters this check;
F9  the same failure is told twice (ticks, restart);
F10 a Work resumed and failing again inside ``no_progress_seconds`` is not told (Q6);
F11 resume or abandon does not close the issue;
F12 the facts carry the goal, diagnostics, a message or free text; or miss a Q7 field;
F13 N stock failures at first deployment start N turns instead of one per person;
F14 a non-superuser is told; a new superuser is not told the open failures;
F15 a failing read of this check is reported or clears the issue;
F16 an unchanged tick takes the writer;
F17 the storage holder list and the failed issue swallow each other;
F18 a SELF Work's facts carry group content.

The artifact lists the scenarios whose assertions all passed (each records its facts
last) and ``all_scenarios_passed``; CI checks that flag.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, update
from tests.conftest import MemorySender
from tests.integration.test_watchdog_capacity_incident_e2e import (
    CHECK,
    MIN_INTERVAL,
    WINDOW,
    Scene,
    _call,
    _delegation,
    _runtime_context,
    _secret_free,
    _tool_results,
)
from tests.support.runtime_execution import make_work_resumer

import yuki.application.watchdog as watchdog_module
from yuki.identity.db_models import CanonicalPersonModel
from yuki.llm.base import LLMUnavailableError
from yuki.runtime.proactive_schema import proactive_turns, watchdog_issues
from yuki.runtime.protocol_store import usage_bytes
from yuki.runtime.work_management import manage_work
from yuki.runtime.work_recovery_schema import recovery
from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_scheduler import WorkScheduler
from yuki.runtime.work_schema_v1 import work

ARTIFACT = Path(__file__).resolve().parents[2] / "test-results" / "watchdog-failed-work-e2e.json"
RESULTS: dict[str, Any] = {}
EXPECTED = (
    "S1_runtime_failure_told_once",
    "S2_model_endings_not_told",
    "S3_exclusions",
    "S4_resume_refail_abandon",
    "S5_stock_and_recipients",
    "S6_failing_check",
    "S7_storage_holders_separate",
)
FACT_FIELDS = {
    "work_id",
    "revision",
    "origin",
    "owner",
    "conversation_id",
    "stage",
    "code",
    "failed_at",
    "model_requests",
    "resume_blocker",
}
PREFIX = "work_failed:"


@pytest.fixture(scope="module", autouse=True)
def _write_artifact():
    yield
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    scenarios = json.dumps(RESULTS, ensure_ascii=False, indent=2, sort_keys=True)
    ARTIFACT.write_text(
        json.dumps(
            {
                "suite": "tests/integration/test_watchdog_failed_work_e2e.py",
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


class FailScene(Scene):
    """The watchdog scene plus scripted private turns and the real scheduler."""

    script: list[Any]
    labels: dict[str, str]

    async def build(self) -> FailScene:
        await super().build()
        self.script = []
        self.labels = {self.env.person: "<owner>"}
        self.labels[await self.conversation()] = "<owner_private>"
        self.ordinary = self._scripted
        return self

    def _scripted(self, request):
        if not self.script:
            return "好"
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step(request) if callable(step) else step

    def label(self, identity: str, name: str) -> str:
        self.labels[identity] = f"<{name}>"
        return identity

    def norm(self, value: Any) -> Any:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        for identity, name in sorted(self.labels.items(), key=lambda item: -len(item[0])):
            text = text.replace(identity, name)
        return json.loads(text)

    async def turn(self, text: str, key: str, script: list[Any]) -> None:
        self.script = list(script)
        result = await self.harness.processor.handle(self.message(text, key), MemorySender())
        assert result.reason == "chat", result
        assert self.script == [], self.script
        self.script = []

    async def drain(self, script: list[Any]) -> None:
        """The real scheduler runs every queued Work once (original execution chain)."""
        self.script = list(script)
        repository = WorkRepository(self.database)
        resumer = make_work_resumer(
            repository,
            ledger=self.harness.ledger,
            scopes=self.chat._conversation_scopes,
            turns=self.chat._turn_coordinator,
            router=self.env.router,
            config=self.chat._runtime_config,
            generate_self=self.chat.generate_self_initiative,
            generate_wakeup=self.chat.generate_main_agent_wakeup,
            bindings=self.chat.runtime.bindings,
            concurrency=self.chat._concurrency,
        )
        scheduler = WorkScheduler(repository, resumer.resume, chat_admission_enabled=True)
        scheduler._last_reclaim = time.monotonic()
        await scheduler.drain_once()
        assert self.script == [], self.script
        self.script = []

    async def work(self, identity: str) -> dict[str, Any]:
        rows = await self.rows(work, work.c.id == identity)
        return rows[0]

    async def recovery(self, identity: str) -> dict[str, Any]:
        rows = await self.rows(recovery, recovery.c.work_id == identity)
        return rows[0]

    async def work_ids(self) -> set[str]:
        async with self.database.read_session() as reader:
            return set(await reader.scalars(select(work.c.id)))

    async def new_work(self, text: str, key: str, script: list[Any]) -> str:
        before = await self.work_ids()
        await self.turn(text, key, script)
        created = (await self.work_ids()) - before
        assert len(created) == 1, created
        return created.pop()

    async def issue_keys(self, person: str | None = None) -> set[str]:
        rows = await self.rows(watchdog_issues)
        return {row["issue_key"] for row in rows if person is None or row["person_id"] == person}

    async def failed_keys(self, person: str | None = None) -> set[str]:
        return {key for key in await self.issue_keys(person) if key.startswith(PREFIX)}


def _note(identity: str) -> Any:
    return _call("set_short_note", {"slot": 1, "text": "SECRET-OWNER 进度"}, identity)


def _protocol_error() -> httpx.RemoteProtocolError:
    return httpx.RemoteProtocolError("SECRET-CMD peer closed connection without response")


async def _runtime_failed(scene: FailScene, name: str) -> str:
    """A real private Work: one step, then the connection drops mid-reply (P1)."""
    identity = await scene.new_work(
        f"SECRET-GOAL 帮我整理{name}", name, [_note(name), _protocol_error()]
    )
    row = await scene.work(identity)
    assert (row["state"], row["reason"]) == ("failed", "RemoteProtocolError")
    failure = json.loads((await scene.recovery(identity))["failure_json"])
    assert (failure["code"], failure["stage"], failure["retryable"]) == (
        "RemoteProtocolError",
        "activation",
        False,
    )
    return scene.label(identity, name)


async def _model_failed(scene: FailScene, name: str) -> str:
    """A real private Work the model itself ends with ``manage_task(fail)`` (P2)."""
    identity = await scene.new_work(
        f"帮我做{name}",
        name,
        [_note(name), _call("manage_task", {"action": "fail", "text": "做不了"}, "stop")],
    )
    assert (await scene.work(identity))["reason"] == "failed"
    return scene.label(identity, name)


async def _resume(scene: FailScene, identity: str, key: str) -> None:
    """The owner says continue; Yuki resumes the original Work in an ordinary turn."""
    await scene.turn(
        "继续刚才那个",
        key,
        # The resume hands the turn to the original chain: no further request here.
        [_call("manage_task", {"action": "resume", "id": identity}, key)],
    )
    assert (await scene.work(identity))["state"] == "queued"


async def _abandon(scene: FailScene, identity: str, key: str) -> None:
    """The owner agrees; Yuki gives up recovery with the existing tool (#666/#674)."""
    await scene.turn(
        "不要了，放弃吧",
        key,
        [_call("manage_task", {"action": "cancel", "id": identity}, key), "好"],
    )
    assert (await scene.work(identity))["state"] == "cancelled"


def _key(identity: str, revision: int) -> str:
    return f"{PREFIX}{identity}:{revision}"


def _notice_issues(request) -> dict[str, dict[str, Any]]:
    context = _runtime_context(request)
    assert context is not None and context["origin"] == "proactive_private"
    return {issue["key"]: issue["facts"] for issue in context["watchdog"]["issues"]}


def _first_requests(scene: FailScene) -> list[Any]:
    """The first request of each notice turn (the one carrying the material)."""
    return [request for request in scene.notice_requests if not _tool_results(request)]


async def _scene(database, tmp_path) -> FailScene:
    return await FailScene(database, tmp_path).build()


# --------------------------------------------------------------------------- S1


async def test_runtime_failure_is_told_once(database, tmp_path):
    scene = await _scene(database, tmp_path)
    identity = await _runtime_failed(scene, "S1.private")
    row = await scene.work(identity)
    key = _key(identity, row["revision"])

    # F1: the first tick opens the issue and the owner is told in one private turn.
    assert await scene.tick() == ["finished"]
    assert await scene.failed_keys() == {key}
    requests = _first_requests(scene)
    assert len(requests) == 1
    said = _notice_issues(requests[0])
    facts = said[key]
    # F12: exactly the Q7 fields; codes only; no goal, diagnostics or message.
    assert set(facts) == FACT_FIELDS
    assert facts["work_id"] == identity and facts["revision"] == row["revision"]
    assert (facts["stage"], facts["code"]) == ("activation", "RemoteProtocolError")
    assert facts["origin"] == "user_message" and facts["owner"] is None
    assert facts["conversation_id"] == await scene.conversation()
    assert facts["model_requests"] == row["model_requests"] == 2
    assert facts["resume_blocker"] is None  # the original chain can still continue
    _secret_free(_runtime_context(requests[0]))
    async with database.read_session() as reader:
        stored = " ".join(await reader.scalars(select(watchdog_issues.c.facts_json)))
    _secret_free(stored)
    assert "peer closed" not in stored and "diagnostics" not in stored
    sends = scene.sends()
    assert len(sends) == 1 and key in sends[0]

    # F9/F16: unchanged ticks and a restart neither write nor tell again.
    writes = scene.watchdog.writes
    assert await scene.tick(CHECK) == []
    assert scene.watchdog.writes == writes
    scene.restart()
    assert await scene.tick(MIN_INTERVAL) == []
    assert scene.watchdog.writes == 0
    assert len(scene.sends()) == 1
    assert await scene.failed_keys() == {key}

    RESULTS["S1_runtime_failure_told_once"] = scene.norm(
        {
            "work": {key: row[key] for key in ("state", "reason", "revision", "model_requests")},
            "recovery_failure": json.loads((await scene.recovery(identity))["failure_json"]),
            "open_keys": sorted(await scene.failed_keys()),
            "told_facts": {name: value for name, value in facts.items() if name != "failed_at"},
            "failed_at_is_iso": isinstance(facts["failed_at"], str) and "T" in facts["failed_at"],
            "sends": len(scene.sends()),
            "send_names_the_key": key in sends[0],
            "writes_after_restart_ticks": scene.watchdog.writes,
        }
    )


# --------------------------------------------------------------------------- S2


async def test_model_decided_endings_are_not_told(database, tmp_path):
    scene = await _scene(database, tmp_path)
    shapes: dict[str, str] = {}

    # F2: P2, the model's own fail.
    shapes["P2_model_fail"] = await _model_failed(scene, "S2.p2")

    # F4 (F-a1): a real runtime failure, the owner resumes it, then the model fails it.
    a1 = await _runtime_failed(scene, "S2.a1")
    await _resume(scene, a1, "S2.a1.resume")
    await scene.drain([_call("manage_task", {"action": "fail", "text": "做不了"}, "a1")])
    shapes["Fa1_runtime_resume_model_fail"] = a1

    # F3 (F-a2): a retryable failure (queued, retried), then the model fails it.
    a2 = scene.label(
        await scene.new_work(
            "帮我做 S2.a2",
            "S2.a2",
            [_note("S2.a2"), LLMUnavailableError("upstream unavailable")],
        ),
        "S2.a2",
    )
    assert (await scene.work(a2))["state"] == "queued"
    async with database.immediate_session() as writer:  # only time is moved
        await writer.execute(update(recovery).where(recovery.c.work_id == a2).values(not_before=0))
    await scene.drain([_call("manage_task", {"action": "fail", "text": "做不了"}, "a2")])
    shapes["Fa2_retry_then_model_fail"] = a2

    # F6 (P4): a runtime failure, resumed, then ended by the shared management fail.
    p4 = await _runtime_failed(scene, "S2.p4")
    await _resume(scene, p4, "S2.p4.resume")
    async with database.immediate_session() as writer:
        await manage_work(writer, p4, (await scene.work(p4))["revision"], "fail")
    shapes["P4_management_fail"] = p4

    # F5 (P3, data shape): a decided fail whose settlement met an auxiliary exception.
    # The current tool loop ends at ``fail`` and runs nothing after it, so the shape
    # ``recover_failure`` writes (reason NULL, failure_json present) is written here.
    p3 = await _model_failed(scene, "S2.p3")
    async with database.immediate_session() as writer:
        await writer.execute(update(work).where(work.c.id == p3).values(reason=None))
        await writer.execute(
            update(recovery)
            .where(recovery.c.work_id == p3)
            .values(
                failure_json=json.dumps(
                    {"code": "RuntimeError", "stage": "activation", "retryable": False}
                )
            )
        )
    shapes["P3_decided_then_auxiliary_shape"] = p3

    facts: dict[str, Any] = {}
    issue_direction = set()
    for name, identity in shapes.items():
        row = await scene.work(identity)
        found = await scene.recovery(identity)
        failure = json.loads(found["failure_json"])
        assert row["state"] == "failed", name
        facts[name] = {
            "reason": row["reason"],
            "exit_reason": found["exit_reason"],
            "failure_code": failure.get("code"),
        }
        if found["exit_reason"] == "failed" and failure:
            issue_direction.add(name)
    # The issue's first direction (exit_reason + failure_json) would report these two.
    # F-a1/F-a2 are not among them: every persisted model response clears the earlier
    # failure (work_journal.py), so the fail decision's own response already did.
    assert issue_direction == {"P4_management_fail", "P3_decided_then_auxiliary_shape"}
    assert facts["Fa1_runtime_resume_model_fail"]["failure_code"] is None
    assert facts["P4_management_fail"]["failure_code"] == "RemoteProtocolError"

    # Two runtime failures are still told, so the check is really running; none of
    # the model-decided or management endings is.
    told = await _runtime_failed(scene, "S2.control")
    assert await scene.tick() == ["finished"]
    keys = await scene.failed_keys()
    assert keys == {_key(told, (await scene.work(told))["revision"])}
    RESULTS["S2_model_endings_not_told"] = scene.norm(
        {
            "shapes": facts,
            "issue_direction_would_report": sorted(issue_direction),
            "open_failed_keys": sorted(keys),
        }
    )


# --------------------------------------------------------------------------- S3


async def test_workers_records_retries_and_suspensions_are_excluded(database, tmp_path):
    scene = await _scene(database, tmp_path)
    # F8: a retryable failure is queued for retry, not failed.
    retried = scene.label(
        await scene.new_work(
            "帮我做 S3.retry",
            "S3.retry",
            [_note("S3.retry"), LLMUnavailableError("upstream unavailable")],
        ),
        "S3.retry",
    )
    assert (await scene.work(retried))["state"] == "queued"
    # F7 (worker, data shape): a real runtime-failed Work placed under another root.
    parent = await _runtime_failed(scene, "S3.parent")
    child = await _runtime_failed(scene, "S3.child")
    async with database.immediate_session() as writer:
        await writer.execute(update(work).where(work.c.id == child).values(parent_work_id=parent))
    # F7: a failed delegation record (root, no Yuki loop, no recovery row).
    record = await _delegation(
        scene,
        state="failed",
        reason="native_error",
        receipt={"status": "failed", "pending": False, "error": "native_error"},
    )
    scene.label(record["record"], "S3.record")

    # F8: a real capacity suspension (the 2 GiB budget fills mid-work) stays with C5.
    def fill(request):
        scene.cap[0] = 1
        return _note("S3.full")

    suspended = scene.label(
        await scene.new_work("帮我做 S3.suspended", "S3.suspended", [_note("S3.s"), fill]),
        "S3.suspended",
    )
    assert (await scene.work(suspended))["reason"] == "work_protocol_storage_capacity"

    await scene.tick()
    keys = await scene.failed_keys()
    assert keys == {_key(parent, (await scene.work(parent))["revision"])}
    assert "protocol_storage_capacity" in await scene.issue_keys()
    RESULTS["S3_exclusions"] = scene.norm(
        {
            "open_failed_keys": sorted(keys),
            "states": {
                name: (await scene.work(identity))["state"]
                for name, identity in (
                    ("retry", retried),
                    ("parent", parent),
                    ("child", child),
                    ("record", record["record"]),
                    ("suspended", suspended),
                )
            },
        }
    )


# --------------------------------------------------------------------------- S4


async def test_resume_closes_refailure_is_told_again_and_abandon_closes(database, tmp_path):
    scene = await _scene(database, tmp_path)
    identity = await _runtime_failed(scene, "S4.private")
    first = _key(identity, (await scene.work(identity))["revision"])
    assert await scene.tick() == ["finished"]
    assert await scene.failed_keys() == {first}

    # F11: the owner resumes it; the Work leaves failed.
    await _resume(scene, identity, "S4.resume")
    await scene.tick(CHECK)
    assert await scene.failed_keys() == {first}  # absent less than the window

    # F10: resumed by the real scheduler, it takes one step (consuming the owner's
    # input) and the connection drops again: a new occurrence of the same Work.
    await scene.drain([_note("S4.again"), _protocol_error()])
    again = await scene.work(identity)
    assert (again["state"], again["reason"]) == ("failed", "RemoteProtocolError")
    second = _key(identity, again["revision"])
    assert second != first
    await scene.tick(CHECK)
    assert second in await scene.failed_keys()
    assert len(scene.sends()) == 1  # inside the person's minimum interval
    await scene.tick(MIN_INTERVAL)
    assert await scene.failed_keys() == {second}
    sends = scene.sends()
    assert len(sends) == 2 and second in sends[1]
    assert list(_notice_issues(_first_requests(scene)[-1])) == [second]

    # F11: the owner abandons it (#674 path): the issue closes after the window.
    await _abandon(scene, identity, "S4.abandon")
    await scene.tick(CHECK)
    await scene.tick(WINDOW)
    assert await scene.failed_keys() == set()
    assert len(scene.sends()) == 2
    RESULTS["S4_resume_refail_abandon"] = scene.norm(
        {
            "first_key": first,
            "second_key": second,
            "sends": len(sends),
            "second_send_names_second_key": second in sends[1],
            "after_abandon": {
                "state": (await scene.work(identity))["state"],
                "open_failed_keys": sorted(await scene.failed_keys()),
            },
        }
    )


# --------------------------------------------------------------------------- S5


async def test_stock_is_told_once_per_person_and_only_to_superusers(database, tmp_path):
    scene = await _scene(database, tmp_path)
    stock = [await _runtime_failed(scene, f"S5.stock{index}") for index in range(3)]
    # F18 (data shape): the third becomes a SELF Work (classification keys only).
    async with database.immediate_session() as writer:
        await writer.execute(
            update(work)
            .where(work.c.id == stock[2])
            .values(
                source_json=func.json_set(
                    work.c.source_json,
                    "$.origin",
                    "self_initiative",
                    "$.principal_kind",
                    "self",
                )
            )
        )
    keys = {_key(identity, (await scene.work(identity))["revision"]) for identity in stock}

    # F13: first deployment: all stock failures, one turn and one message per person.
    assert await scene.tick() == ["finished"]
    owner = scene.env.person
    assert await scene.failed_keys(owner) == keys
    turns = await scene.rows(proactive_turns)
    assert len(turns) == 1 and turns[0]["person_id"] == owner
    said = _notice_issues(_first_requests(scene)[0])
    assert set(said) == keys
    assert said[_key(stock[2], (await scene.work(stock[2]))["revision"])]["origin"] == (
        "self_initiative"
    )
    assert len(scene.sends()) == 1

    # F14: another person becomes a superuser: told the open failures (route none here).
    async with database.read_session() as reader:
        other = await reader.scalar(
            select(CanonicalPersonModel.id).where(CanonicalPersonModel.id != owner).limit(1)
        )
    assert other is not None
    scene.label(other, "other_person")
    async with database.immediate_session() as writer:
        await writer.execute(
            update(CanonicalPersonModel)
            .where(CanonicalPersonModel.id == other)
            .values(is_superuser=True)
        )
    await scene.tick(CHECK)
    assert await scene.failed_keys(other) == keys
    other_turns = await scene.rows(proactive_turns, proactive_turns.c.person_id == other)
    assert [row["state"] for row in other_turns] == ["pending"]
    async with database.immediate_session() as writer:
        await writer.execute(
            update(CanonicalPersonModel)
            .where(CanonicalPersonModel.id == other)
            .values(is_superuser=False)
        )
    await scene.tick(CHECK)
    assert await scene.failed_keys(other) == set()
    persons = {row["person_id"] for row in await scene.rows(watchdog_issues)}
    assert persons == {owner}
    assert len(scene.sends()) == 1
    RESULTS["S5_stock_and_recipients"] = scene.norm(
        {
            "stock_keys": sorted(scene.norm(list(keys))),
            "owner_turns": len(turns),
            "owner_sends": len(scene.sends()),
            "self_origin_told_as": "self_initiative",
            "new_superuser_rows": sorted(scene.norm(list(keys))),
            "new_superuser_turn": [row["state"] for row in other_turns],
            "after_revoke_persons": sorted(persons),
        }
    )


# --------------------------------------------------------------------------- S6


async def test_a_failing_read_is_undetermined(database, tmp_path, monkeypatch):
    scene = await _scene(database, tmp_path)
    identity = await _runtime_failed(scene, "S6.private")
    key = _key(identity, (await scene.work(identity))["revision"])
    assert await scene.tick() == ["finished"]

    async def broken(reader, **_: Any):
        raise RuntimeError("read failed")

    monkeypatch.setattr(watchdog_module, "runtime_failed_works", broken)
    # F15: neither reported nor cleared while the read fails, even past the window.
    assert await scene.tick(WINDOW) == []
    assert await scene.tick(WINDOW) == []
    assert await scene.failed_keys() == {key}
    health = await scene.watchdog.health()
    assert health["check_errors"] == {"works": "RuntimeError"}
    monkeypatch.undo()
    assert await scene.tick(CHECK) == []
    assert await scene.failed_keys() == {key}
    assert len(scene.sends()) == 1
    RESULTS["S6_failing_check"] = scene.norm(
        {"open_failed_keys": sorted(await scene.failed_keys()), "sends": len(scene.sends())}
    )


# --------------------------------------------------------------------------- S7


async def test_storage_holders_and_failed_issue_are_separate(database, tmp_path):
    scene = await _scene(database, tmp_path)
    identity = await _runtime_failed(scene, "S7.private")
    key = _key(identity, (await scene.work(identity))["revision"])
    async with database.read_session() as reader:
        used = await usage_bytes(reader)
    assert used > 0
    scene.cap[0] = int(used / 0.9)  # above the warning line, below the cap

    # F17: both issues open; the holder list names the same failed Work.
    assert await scene.tick() == ["finished"]
    keys = await scene.issue_keys()
    assert {key, "protocol_storage_near_cap"} <= keys
    assert "protocol_storage_capacity" not in keys
    said = _notice_issues(_first_requests(scene)[0])
    holders = said["protocol_storage_near_cap"]["failed_holders"]
    assert [holder["work_id"] for holder in holders] == [identity]

    # Abandoning closes the failed issue; the storage issue keeps its own condition.
    await _abandon(scene, identity, "S7.abandon")
    await scene.tick(CHECK)
    await scene.tick(WINDOW)
    keys_after = await scene.issue_keys()
    assert key not in keys_after
    assert "protocol_storage_near_cap" in keys_after
    RESULTS["S7_storage_holders_separate"] = scene.norm(
        {
            "opened": sorted(keys),
            "holders": [holder["work_id"] for holder in holders],
            "after_abandon": sorted(keys_after),
        }
    )
