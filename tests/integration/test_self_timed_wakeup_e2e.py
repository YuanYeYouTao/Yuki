"""E2E: one SELF wake chain, now also on an explicit timer and without a group (#541 P3).

Owner decisions of 2026-11-23 (research doc §8): every autonomous wake uses the existing
``self_initiative`` chain (initiative admission, SELF Work, the fixed Main tools, Social
targets, authorization and receipts); SELF no longer has to wake in a group; a timer at an
explicit cadence, off by default, wakes SELF and its trigger text names the concerns file
once (path only). Real owner private chat, group, Main, Social, ``PresenceRouter`` with the
owner's route, the participation host tick, its outbox and the WorkScheduler; only model
replies and the timer clock are scripted. Facts (IDs replaced by labels) go to
``test-results/self-timed-wakeup-e2e.json``.

Failure cases, written before the implementation:

W1  off by default (interval 0): a tick admits no run, creates no Work, pays no request;
W2  ``conversation.proactive_wakeup_enabled=false`` or ``conversation.autonomous_enabled=false``
    (global) still lets the timer wake SELF; a paused owner route still wakes it;
W3  the production overrides (``semantic_participation_enabled=false``,
    ``auto_reply_enabled=false``) block the timer;
W4  a wake without a group cannot be admitted or executed (``space_id`` NOT NULL, owner
    check, the autonomy-binding foreign key, Work recovery, ToolActor, Social or Memory
    insisting on a group);
W5  the trigger text lacks the concerns path, or carries anything besides path and one line;
W6  ``send`` without target misses the owner, ``send`` with a group target fails, or the
    sends are not settled as this run's receipts (Social rows, run feedback, run state);
W7  the same slot (again, or after a restart) admits a second run, Work or model request;
W8  a run admitted before the process died loses its wake (no Work after restart);
W9  the existing group SELF changes: trigger text gains the path, default send leaves the group;
W10 an ordinary chat turn carries the reminder;
W11 the wake's SELF memory write is rejected, or is visible wider than the owner's chat;
W12 SELF cannot schedule its own follow-up from the private wake, or the due follow-up
    sends somewhere other than the original private chat;
W13 an unchanged tick takes the writer or its statements grow with runs; the new admission
    queries scan instead of using an index.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event as sa_event
from sqlalchemy import select, text, update
from tests.integration.test_watchdog_capacity_incident_e2e import Scene, _plain, _tool_results
from tests.support.automation_runtime_helpers import FakeClock
from tests.support.runtime_execution import make_work_resumer

from yuki.admin.config_service import RuntimeConfigService
from yuki.automation.repository import AutomationRepository
from yuki.automation.service import AutomationService
from yuki.automation.tools import AutomationToolService
from yuki.automation.worker import AutomationWorker
from yuki.conversation.autonomy_db_models import InitiativeFeedbackModel, InitiativeRunModel
from yuki.conversation.autonomy_repository import AutonomyRepository
from yuki.conversation.canonical_db_models import PersonActiveRouteModel
from yuki.domain.messages import ChatResponse, ToolCall, ToolFunction
from yuki.identity.db_models import CanonicalSpaceModel
from yuki.persistence.models import MemoryFactModel, MemoryToolReceiptModel
from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_scheduler import WorkScheduler
from yuki.runtime.work_schema_v1 import work
from yuki.services.main_agent_contract import MainAgentContract
from yuki.services.semantic_participation import SemanticParticipationService
from yuki.social.db_models import SocialOperationModel

pytestmark = pytest.mark.asyncio

ARTIFACT = Path(__file__).resolve().parents[2] / "test-results" / "self-timed-wakeup-e2e.json"
RESULTS: dict[str, Any] = {}
INTERVAL = 3600
TIMER_MARK = "定时自主唤醒"
GROUP_MARK = "自主工作唤醒"
CONCERNS_PATH = "/workspace/notes/concerns.md"
SCHEDULE_GOAL = "SELF-FOLLOW-UP: 明天再看一眼清单里的第一件事"


@pytest.fixture(scope="module", autouse=True)
def _write_artifact():
    yield
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).read_bytes()
    ARTIFACT.write_text(
        json.dumps(
            {
                "suite": "tests/integration/test_self_timed_wakeup_e2e.py",
                "generated_at": datetime.now(UTC).isoformat(),
                "suite_sha256": hashlib.sha256(source).hexdigest(),
                "scenarios": RESULTS,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=str,
        )
        + "\n",
        encoding="utf-8",
    )


class TimerClock:
    """The wake timer's clock, aligned inside one slot so no test straddles a boundary."""

    def __init__(self) -> None:
        self.now = float((int(time.time()) // INTERVAL + 1) * INTERVAL + 10)

    def __call__(self) -> float:
        return self.now


def _trigger(request) -> str:
    return next(
        (
            str(message.content or "")
            for message in request.messages
            if message.role == "user"
            and (TIMER_MARK in str(message.content) or GROUP_MARK in str(message.content))
        ),
        "",
    )


def _calls(*calls: tuple[str, str, dict[str, Any]]) -> ChatResponse:
    return ChatResponse(
        "",
        0,
        tool_calls=tuple(
            ToolCall(identity, ToolFunction(name, json.dumps(arguments, ensure_ascii=False)))
            for identity, name, arguments in calls
        ),
    )


class WakeScene(Scene):
    """The owner's private chat and group, plus the SELF host, outbox and Work scheduler."""

    def __init__(self, database, tmp_path) -> None:
        super().__init__(database, tmp_path)
        self.self_requests: list[Any] = []
        self.ordinary_requests: list[Any] = []
        self.script: Any = None
        self.timer = TimerClock()

    def respond(self, request):
        text_ = _plain(request)
        if _trigger(request) or SCHEDULE_GOAL in text_:
            self.self_requests.append(request)
            return self.script(request)
        self.ordinary_requests.append(request)
        return "好"

    async def build(self) -> WakeScene:  # type: ignore[override]
        await super().build()
        from tests.unit.test_self_initiative_memory import _service

        mutations, facts, _, _ = _service(self.database, self_memory_enabled=True)
        self.chat._tools._memory_mutations = mutations
        self.facts = facts
        # Main declares save_memory only when the mutation service is wired (as the
        # container does at startup); freeze the fixed contract again with it.
        self.chat.runtime.runner.main_contract = MainAgentContract(
            self.chat, self.chat._tools.short_state
        )
        self.chat._settings.self_memory_enabled = True
        self.config = RuntimeConfigService(database=self.database, settings=self.chat._settings)
        self.chat._runtime_config = self.config
        self.env.service.runtime_config = self.config
        self.works = WorkRepository(self.database)
        self.restart_host()
        return self

    def restart_host(self) -> None:
        """A new process for the SELF host and Work scheduler over the same database."""
        self.host = SemanticParticipationService(
            database=self.database,
            settings=self.chat._settings,
            ledger=self.harness.ledger,
            runtime_config=self.config,
            validate_turn_snapshot=self.chat.validate_turn_snapshot,
            promote=AsyncMock(),
            model_config_path=self.tmp_path / "autonomous-model.json",
            clock=self.timer,
        )
        resumer = make_work_resumer(
            self.works,
            ledger=self.harness.ledger,
            scopes=self.chat._conversation_scopes,
            turns=self.chat._turn_coordinator,
            router=self.env.router,
            config=self.config,
            generate_self=self.chat.generate_self_initiative,
            generate_wakeup=AsyncMock(side_effect=AssertionError("no inbound wakeup")),
            bindings=self.chat.runtime.bindings,
            concurrency=self.chat._concurrency,
        )
        self.scheduler = WorkScheduler(self.works, resumer.resume, chat_admission_enabled=True)

    async def switch(self, key: str, value: object) -> None:
        change = await self.config.set_override(
            f"conversation.{key}",
            value,
            scope_type="global",
            scope_id="",
            actor_user_id="operator",
            trigger_message_id=f"self-wakeup-e2e-{key}-{value}",
        )
        assert change.success, change

    async def wake(self) -> None:
        """One host tick (admission + outbox), the scheduler, then the host settles feedback."""
        await self.host.tick()
        await self.scheduler.drain_once()
        await self.host.tick()

    async def runs(self) -> list[InitiativeRunModel]:
        async with self.database.read_session() as reader:
            return list(
                await reader.scalars(
                    select(InitiativeRunModel).order_by(InitiativeRunModel.created_at)
                )
            )

    async def works_rows(self) -> list[dict[str, Any]]:
        async with self.database.read_session() as reader:
            return [
                dict(row)
                for row in (
                    await reader.execute(
                        select(
                            work.c.id,
                            work.c.source_key,
                            work.c.state,
                            work.c.goal,
                            work.c.conversation_id,
                            work.c.source_json,
                        )
                        .where(work.c.source_key.like("initiative:%"))
                        .order_by(work.c.created)
                    )
                ).mappings()
            ]

    def group_sends(self) -> list[dict[str, Any]]:
        return [params for action, params in self.env.bot.calls if action == "send_group_msg"]


async def _scene(database, tmp_path) -> WakeScene:
    return await WakeScene(database, tmp_path).build()


def _send_both(scene: WakeScene):
    def script(request):
        results = _tool_results(request)
        if not results:
            return _calls(
                ("owner", "send", {"text": "给主人：清单里的事有进展"}),
                (
                    "group",
                    "send",
                    {"text": "群里说一声：有进展", "target": {"space_id": scene.env.space}},
                ),
            )
        return "完成"

    return script


async def test_w1_w2_w3_switches(database, tmp_path):
    scene = await _scene(database, tmp_path)
    scene.script = _send_both(scene)
    record: dict[str, Any] = {}
    # W1: off by default.
    await scene.wake()
    record["default_off"] = {"runs": len(await scene.runs()), "requests": len(scene.self_requests)}
    assert await scene.runs() == [] and scene.self_requests == []
    await scene.switch("self_wakeup_interval_seconds", INTERVAL)
    # W2: the existing SELF switches still apply to the timer.
    await scene.switch("proactive_wakeup_enabled", False)
    await scene.wake()
    record["proactive_wakeup_off"] = len(await scene.runs())
    assert await scene.runs() == []
    await scene.switch("proactive_wakeup_enabled", True)
    await scene.switch("autonomous_enabled", False)
    scene.timer.now += INTERVAL
    await scene.wake()
    record["autonomous_off"] = len(await scene.runs())
    assert await scene.runs() == []
    await scene.switch("autonomous_enabled", True)
    async with database.immediate_session() as writer:
        await writer.execute(update(PersonActiveRouteModel).values(paused=True))
    scene.timer.now += INTERVAL
    await scene.wake()
    record["route_paused"] = len(await scene.runs())
    assert await scene.runs() == []
    async with database.immediate_session() as writer:
        await writer.execute(update(PersonActiveRouteModel).values(paused=False))
    # W3: production overrides of semantic participation and auto reply do not block it.
    await scene.switch("semantic_participation_enabled", False)
    await scene.switch("auto_reply_enabled", False)
    scene.timer.now += INTERVAL
    await scene.wake()
    runs = await scene.runs()
    record["production_overrides"] = {
        "runs": len(runs),
        "owner": runs[0].owner if runs else None,
        "requests": len(scene.self_requests),
    }
    RESULTS["W1_W2_W3"] = record
    assert len(runs) == 1 and runs[0].owner == "timer"
    assert scene.self_requests


async def test_w4_w5_w6_w11_w13_private_wake_sends_and_remembers(database, tmp_path):
    scene = await _scene(database, tmp_path)
    owner_conversation = await scene.conversation()
    saved: dict[str, Any] = {}

    def script(request):
        results = _tool_results(request)
        if not results:
            return _calls(
                ("owner", "send", {"text": "给主人：清单里的事有进展"}),
                (
                    "group",
                    "send",
                    {"text": "群里说一声：有进展", "target": {"space_id": scene.env.space}},
                ),
                ("contacts", "find_contacts", {"type": "group"}),
            )
        if "contacts" in results and "memory" not in results:
            saved["sends"] = {key: results[key] for key in ("owner", "group")}
            saved["contacts"] = results["contacts"]
            return _calls(
                (
                    "memory",
                    "save_memory",
                    {
                        "target": {"self": True},
                        "content": "定时醒来时给主人和群都说了进展。",
                        "kind": "episode",
                        "quote": "20001",
                    },
                )
            )
        if "memory" in results and "read" not in results:
            saved["memory"] = results["memory"]
            fact_id = results["memory"].get("fact_id")
            return _calls(("read", "search_memory", {"fact_id": fact_id}))
        saved["read"] = results.get("read")
        return "完成"

    scene.script = script
    await scene.switch("self_wakeup_interval_seconds", INTERVAL)
    statements: list[str] = []

    def count(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    await scene.wake()
    runs = await scene.runs()
    assert len(runs) == 1, runs
    run = runs[0]
    trigger = _trigger(scene.self_requests[0])
    tasks = await scene.works_rows()
    source = json.loads(tasks[0]["source_json"])
    async with database.read_session() as reader:
        social = list(
            await reader.scalars(
                select(SocialOperationModel).where(
                    SocialOperationModel.source_turn_id
                    == f"{owner_conversation}:initiative:{run.id}"
                )
            )
        )
        feedback = list(
            await reader.scalars(
                select(InitiativeFeedbackModel).where(InitiativeFeedbackModel.run_id == run.id)
            )
        )
        receipts = [
            (row.tool_name, row.canonical_person_id == scene.env.person, row.canonical_space_id)
            for row in await reader.scalars(
                select(MemoryToolReceiptModel).where(
                    MemoryToolReceiptModel.initiative_run_id == run.id
                )
            )
        ]
        fact = (
            await reader.scalar(
                select(MemoryFactModel).where(MemoryFactModel.id == saved["memory"]["fact_id"])
            )
            if saved.get("memory", {}).get("fact_id")
            else None
        )
    # W13: an unchanged tick of the timer: constant statements, no writer.
    engine = database.engine.sync_engine
    sa_event.listen(engine, "before_cursor_execute", count)
    try:
        from yuki.services.self_wakeup import timer_wakeup

        await timer_wakeup(scene.host)
    finally:
        sa_event.remove(engine, "before_cursor_execute", count)
    plans = {}
    async with database.read_session() as reader:
        from yuki.services.self_wakeup import ADMITTED_SQL, SCENES_SQL

        for label, statement, parameters in (
            ("scenes", SCENES_SQL, {}),
            ("admitted", ADMITTED_SQL, {"scenes": "[]", "proposal": "timer:1:1"}),
        ):
            rows = (await reader.execute(text("EXPLAIN QUERY PLAN " + statement), parameters)).all()
            plans[label] = [str(row[-1]) for row in rows]
    RESULTS["W4_W5_W6_W11_W13"] = {
        "run": {
            "owner": run.owner,
            "space_id": run.space_id,
            "conversation": "owner_private" if run.conversation_id == owner_conversation else "?",
            "state": run.state,
            "trigger_kind": run.trigger_kind,
        },
        "work": {"state": tasks[0]["state"], "origin": source["origin"]},
        "send_results": saved.get("sends"),
        "tool_receipts": receipts,
        "contacts_excerpt": str(saved.get("contacts"))[:400],
        "trigger_has_path": CONCERNS_PATH in trigger,
        "trigger_text": trigger,
        "private_sends": scene.sends(),
        "group_sends": len(scene.group_sends()),
        "social": sorted((row.target_kind, row.status) for row in social),
        "feedback": [(row.sequence, row.outcome) for row in feedback],
        "memory": {
            "result": saved.get("memory"),
            "visibility_type": fact.visibility_type if fact else None,
            "visibility_owner": (fact.canonical_visibility_person_id == scene.env.person)
            if fact
            else None,
            "visibility_space": fact.canonical_visibility_space_id if fact else None,
            "read_back": saved.get("read"),
        },
        "unchanged_tick_statements": [" ".join(s.split())[:160] for s in statements],
        "query_plans": plans,
    }
    # W4/W5
    assert run.owner == "timer" and run.space_id is None
    assert run.conversation_id == owner_conversation
    assert source["origin"] == "self_initiative" and source["principal_kind"] == "self"
    assert CONCERNS_PATH in trigger
    # W6
    assert any("给主人" in json.loads(sent)["message"][0]["data"]["text"] for sent in scene.sends())
    assert len(scene.group_sends()) == 1
    assert sorted((row.target_kind, row.status) for row in social) == [
        ("person", "succeeded"),
        ("space", "succeeded"),
    ]
    assert tasks[0]["state"] == "completed"
    assert run.state == "completed" and feedback and feedback[-1].outcome == "completed"
    # W11
    assert saved["memory"]["outcome"] == "committed", saved["memory"]
    assert fact.visibility_type == "private" and fact.canonical_visibility_person_id == (
        scene.env.person
    )
    assert fact.canonical_visibility_space_id is None
    assert saved["read"]["memories"][0]["content"] == "定时醒来时给主人和群都说了进展。"
    # W13
    assert not any(
        s.lstrip().upper().startswith(("INSERT", "UPDATE", "BEGIN IMMEDIATE")) for s in statements
    )
    assert len(statements) <= 4
    # The scene query reads the (one row per person) route table once per slot; the
    # Conversation and the slot's prior run are index lookups.
    assert any("uq_canonical_conversations_binding" in step for step in plans["scenes"])
    assert any(
        "autoindex_autonomy_initiative_runs" in step and "proposal_id=?" in step
        for step in plans["admitted"]
    )


async def test_w7_w8_same_slot_restart_and_lost_dispatch(database, tmp_path):
    scene = await _scene(database, tmp_path)
    scene.script = _send_both(scene)
    await scene.switch("self_wakeup_interval_seconds", INTERVAL)
    await scene.wake()
    first = (len(await scene.runs()), len(await scene.works_rows()), len(scene.self_requests))
    await scene.wake()
    again = (len(await scene.runs()), len(await scene.works_rows()), len(scene.self_requests))
    scene.restart_host()
    await scene.wake()
    restarted = (len(await scene.runs()), len(await scene.works_rows()), len(scene.self_requests))
    # W8: the next slot's run is admitted, then the process dies before its Work exists.
    scene.timer.now += INTERVAL
    original = scene.host._dispatch

    async def die(run):
        raise RuntimeError("process lost before the Work was created")

    scene.host._dispatch = die
    with pytest.raises(RuntimeError):
        from yuki.services.self_wakeup import timer_wakeup

        await timer_wakeup(scene.host)
    scene.host._dispatch = original
    lost = (len(await scene.runs()), len(await scene.works_rows()))
    scene.restart_host()
    await scene.wake()
    recovered = (len(await scene.runs()), len(await scene.works_rows()), len(scene.self_requests))
    states = [(row.state, row.owner) for row in await scene.runs()]
    RESULTS["W7_W8"] = {
        "first": first,
        "same_slot_again": again,
        "after_restart": restarted,
        "admitted_without_work": lost,
        "after_recovery": recovered,
        "run_states": states,
        "private_sends": len(scene.sends()),
    }
    assert first[0] == first[1] == 1 and first[2] >= 1
    assert again == first and restarted == first
    assert lost == (2, 1)
    assert recovered[:2] == (2, 2) and recovered[2] > first[2]
    assert states == [("completed", "timer"), ("completed", "timer")]
    assert len(scene.sends()) == 2


async def test_w9_w10_group_self_and_ordinary_chat_unchanged(database, tmp_path):
    scene = await _scene(database, tmp_path)

    def group_script(request):
        if not _tool_results(request):
            return _calls(("g", "send", {"text": "群里自己醒来的进展"}))
        return "完成"

    scene.script = group_script
    async with database.immediate_session() as writer:
        space = await writer.get(CanonicalSpaceModel, scene.env.space)
        space.enabled = space.autonomous_enabled = True
    admissions = AutonomyRepository(database)
    binding = await admissions.ensure_binding(scene.env.context.conversation_id, 1)
    binding = await admissions.transition(binding, master_enabled=True, external_enabled=True)
    accepted = await admissions.accept_host_proposal(
        proposal_id="group-intrinsic",
        binding=binding,
        owner=binding.effective_owner,
        space_id=scene.env.space,
        presence_id=scene.env.presence,
        sources=(),
        trigger_kind="intrinsic",
    )
    assert accepted.run is not None
    await scene.host._dispatch(accepted.run)
    await scene.scheduler.drain_once()
    await scene.host.tick()
    group_trigger = _trigger(scene.self_requests[0])
    group_work = next(
        row
        for row in await scene.works_rows()
        if row["source_key"] == f"initiative:{accepted.run.run_id}"
    )
    group_source = json.loads(group_work["source_json"])
    # W10: an ordinary owner chat turn after a timer wake carries no reminder.
    scene.script = _send_both(scene)
    await scene.switch("self_wakeup_interval_seconds", INTERVAL)
    await scene.wake()
    result = await scene.harness.processor.handle(scene.message("最近怎么样", "later"), _sender())
    ordinary = _plain(scene.ordinary_requests[-1])
    RESULTS["W9_W10"] = {
        "group_trigger_has_path": CONCERNS_PATH in group_trigger,
        "group_trigger_has_timer_note": TIMER_MARK in group_trigger,
        "group_source_space": group_source["space_id"] == scene.env.space,
        "group_sends": len(scene.group_sends()),
        "group_work_state": group_work["state"],
        "ordinary_reason": result.reason,
        "ordinary_has_path": CONCERNS_PATH in ordinary,
    }
    assert GROUP_MARK in group_trigger and TIMER_MARK not in group_trigger
    assert CONCERNS_PATH not in group_trigger
    assert group_source["space_id"] == scene.env.space and group_source["group_id"] == "20001"
    assert len(scene.group_sends()) >= 1 and scene.group_sends()[0]["message"]
    assert group_work["state"] == "completed"
    assert result.reason == "chat"
    assert CONCERNS_PATH not in ordinary


def _sender():
    from tests.conftest import MemorySender

    return MemorySender()


async def test_w12_private_self_schedules_its_follow_up(database, tmp_path):
    scene = await _scene(database, tmp_path)
    clock = FakeClock(datetime.now(UTC))
    settings = scene.chat._settings
    settings.automation_enabled = True
    repository = AutomationRepository(database)
    from yuki.time.service import TimeContextService

    time_service = TimeContextService(default_timezone=settings.default_timezone, clock=clock)
    automations = AutomationService(
        settings=settings, repository=repository, time_service=time_service
    )
    scene.chat.set_automation_tools(AutomationToolService(automations))
    created: dict[str, Any] = {}

    def script(request):
        results = _tool_results(request)
        if SCHEDULE_GOAL in _plain(request) and not _trigger(request):
            if not results:
                return _calls(("follow", "send", {"text": "到点了，再看一眼清单"}))
            return "完成"
        if not results:
            return _calls(
                (
                    "schedule",
                    "manage_schedule",
                    {
                        "action": "create",
                        "goal": SCHEDULE_GOAL,
                        "trigger": {"type": "after", "seconds": 60},
                    },
                )
            )
        created.update(results["schedule"])
        return "完成"

    scene.script = script
    await scene.switch("self_wakeup_interval_seconds", INTERVAL)
    await scene.wake()
    worker = AutomationWorker(
        settings=settings,
        repository=repository,
        time_service=time_service,
        generate=scene.chat.generate_origin_turn,
        runtime_config=scene.config,
        conversation_scopes=scene.chat._conversation_scopes,
        turns=scene.chat._turn_coordinator,
        concurrency=scene.chat._concurrency,
        work_repository=scene.works,
        bindings=scene.chat.runtime.bindings,
    )
    clock.advance(120)
    async for claimed in repository.claim_due(
        worker_id="self-wakeup-e2e",
        now=clock.now(),
        lease_seconds=settings.automation_lease_seconds,
        clock=clock.now,
    ):
        await worker._process_guarded(claimed)
    owner_conversation = await scene.conversation()
    rows = [await repository.get(created["id"])] if created.get("id") else []
    history = await repository.run_history(rows[0].id) if rows else []
    RESULTS["W12"] = {
        "create_result": {k: created.get(k) for k in ("ok", "error", "status")},
        "creator_kind": rows[0].creator_kind if rows else None,
        "conversation": "owner_private"
        if rows and rows[0].canonical_conversation_id == owner_conversation
        else None,
        "run_status": history[0].status.value if history else None,
        "private_sends": [json.loads(sent)["message"][0]["data"]["text"] for sent in scene.sends()],
    }
    assert rows and rows[0].creator_kind == "self", created
    assert rows[0].canonical_conversation_id == owner_conversation
    assert history and history[0].status.value == "succeeded"
    assert any("到点了" in json.loads(sent)["message"][0]["data"]["text"] for sent in scene.sends())
    assert not scene.group_sends()
