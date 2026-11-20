"""E2E: the watchdog tells the owner once, in Yuki's own words, and only reports.

Replays the 2026-11-20 incidents with an isolated SQLite, a fake Provider and the
real Main (``generate_origin_turn``), Social, ``PresenceRouter`` with the owner's
proactive route, real Work capacity suspension and real ``ProtocolStore`` files.
Only time is moved (an offset clock). Facts go to
``test-results/watchdog-capacity-incident-e2e.json``.

Failure cases written before the implementation (task book §5):

F1  the same occurrence is told twice (across ticks or a restart);
F2  a problem that cleared beyond the window and came back is not told again;
F3  a notice is sent about a healthy state (incl. cleared before the turn started);
F4  a stuck state of I1-I5 is not detected (capacity, waiting parent, unknown
    receipt, orphaned record, silent executor);
F5  the notice turn carries management authority (Work control, mutations);
F6  the material carries someone's content (goal, brief, native request args);
F7  an unchanged tick still takes the writer;
F8  the notice turn itself needs protocol storage and is suspended by the very
    capacity problem it reports, or creates a Work;
F9  a failed notice is retried every tick (more than once per occurrence);
F10 a turn lost with its process is resent although its send was claimed;
F11 a paused / missing route still pays a model call or picks another entry;
F12 after a restart, an in-memory health that is still debouncing deletes the row
    and the same occurrence is told again;
F13 a busy conversation blocks the checker or the turn joins the owner's Main;
F14 a revoked superuser is still told; a new superuser is not told current issues;
F15 each newly capacity-suspended Work causes another notice;
F16 a failing check is notified or stops the loop;
F17 the material enters the public ledger or a later ordinary request;
F18 a key that clears and comes back within ``no_progress_seconds`` is told again;
F19 several notices start for one person within ``notify_min_interval_seconds``.

Added after the independent review of #664 (each failed on the first version):

F20 the notice turn sends with ``target`` to a group or another person;
F21 an ACP tool (a whole ToolCallUpdate: title, rawInput) reaches the facts;
F22 a send claimed with an unknown result, then a model failure, is retried and
    the owner gets the message twice;
F23 an issue undetermined at the due tick, or cleared before the start and back
    within the window, is never told;
F24 a lost attempt leaves the turn running and blocks the person forever.

Added after the second review (each failed before its fix):

F25 a turn that ends without a successful send (no send call, or a refused send)
    is recorded as told and the issue is swallowed;
F26 forgetting a person who was notified fails on the turn's foreign key;
F27 the route's Presence is gone and another Presence of the same Binding is used;
F28 a failing config read ends the loop; a changed error category keeps the old
    first-seen time; outcomes grow without bound.
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
from tests.conftest import MemorySender, build_harness, make_settings
from tests.support.social_identity_cases import social_env

from yuki.admin.models import WatchdogRuntimeConfig, WorkStorageRuntimeConfig
from yuki.application.watchdog import Watchdog
from yuki.automation.repository import AutomationRepository
from yuki.automation.service import AutomationService
from yuki.automation.tools import AutomationToolService
from yuki.client_api.db_models import ClientDeviceModel
from yuki.conversation.canonical_db_models import PersonActiveRouteModel
from yuki.domain.conversations import ScopeType
from yuki.domain.messages import (
    ChatResponse,
    InboundMessage,
    SenderIdentity,
    ToolCall,
    ToolFunction,
)
from yuki.external_agents.db_models import (
    AgentEventModel,
    AgentInstanceModel,
    AgentOperationModel,
)
from yuki.identity.db_models import CanonicalPersonModel
from yuki.llm.base import LLMUnavailableError
from yuki.llm.fake import FakeLLMProvider
from yuki.persistence.models import ChatEventModel
from yuki.runtime.proactive_schema import proactive_turns, watchdog_issues
from yuki.runtime.protocol_schema import objects
from yuki.runtime.subagent_schema import children
from yuki.runtime.work_schema_v1 import work
from yuki.services.main_agent_contract import MainAgentContract
from yuki.services.proactive_turns import ProactivePrivateTurns, turn_source_key
from yuki.services.rate_limit import SlidingWindowRateLimiter
from yuki.social.db_models import SocialOperationModel
from yuki.time.service import TimeContextService
from yuki.workspace.short_state import ShortState
from yuki.workspace.store import WorkspaceStore

ARTIFACT = (
    Path(__file__).resolve().parents[2] / "test-results" / "watchdog-capacity-incident-e2e.json"
)
RESULTS: dict[str, Any] = {}
CHECK = 300.0
WINDOW = 900.0
MIN_INTERVAL = 1800.0


@pytest.fixture(scope="module", autouse=True)
def _write_artifact():
    yield
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).read_bytes()
    ARTIFACT.write_text(
        json.dumps(
            {
                "suite": "tests/integration/test_watchdog_capacity_incident_e2e.py",
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


def _call(name: str, arguments: dict[str, Any], identity: str) -> ChatResponse:
    return ChatResponse(
        "", 0, tool_calls=(ToolCall(identity, ToolFunction(name, json.dumps(arguments))),)
    )


def _plain(request) -> str:
    return "\n".join(str(message.content or "") for message in request.messages)


def _runtime_context(request) -> dict[str, Any] | None:
    text = _plain(request)
    if "<runtime_context>\n" not in text:
        return None
    start = text.index("<runtime_context>\n") + len("<runtime_context>\n")
    return json.loads(text[start : text.index("\n</runtime_context>", start)])


def _tool_results(request) -> dict[str, Any]:
    results = {}
    for message in request.messages:
        if message.role == "tool" and message.tool_call_id:
            try:
                results[message.tool_call_id] = json.loads(message.content or "null")
            except ValueError:
                results[message.tool_call_id] = message.content
    return results


class Clock:
    def __init__(self) -> None:
        self.offset = 0.0

    def __call__(self) -> float:
        return time.time() + self.offset


class PaceConfig:
    """Hot config as the watchdog reads it; the protocol cap shares one source."""

    def __init__(self, inner, cap: list[int]) -> None:
        self.inner = inner
        self.cap = cap
        self.pace = WatchdogRuntimeConfig(
            check_interval_seconds=CHECK,
            no_progress_seconds=WINDOW,
            notify_min_interval_seconds=MIN_INTERVAL,
            protocol_storage_warn_ratio=0.8,
        )

    async def snapshot(self, **kwargs):
        snapshot = await self.inner.snapshot(**kwargs)
        return replace(
            snapshot,
            work_storage=WorkStorageRuntimeConfig(total_max_bytes=self.cap[0]),
            watchdog=self.pace,
        )


class Scene:
    """Owner, private conversation, real Main and a restartable watchdog."""

    def __init__(self, database, tmp_path) -> None:
        self.database = database
        self.tmp_path = tmp_path
        self.clock = Clock()
        self.cap = [2 * 1024**3]
        self.mode = "send"
        self.notice_requests: list[Any] = []
        self.other_requests: list[Any] = []
        self.loops: dict[str, dict[str, Any]] = {}
        self.components: dict[str, dict[str, Any]] = {}
        self.online = True
        self.fail_loops = False

    def respond(self, request):
        context = _runtime_context(request)
        if context is not None and "watchdog" in context:
            self.notice_requests.append(request)
            if self.mode == "fail":
                raise LLMUnavailableError("provider down")
            if any(message.role == "tool" for message in request.messages):
                return "说完了"
            if self.mode == "manage":
                calls = {
                    "m1": ("manage_task", {"action": "start", "goal": "恢复挂起的任务"}),
                    "m1b": ("manage_task", {"action": "resume", "id": str(uuid.uuid4())}),
                    "m2": ("set_short_note", {"slot": 1, "text": "x"}),
                    "m4": (
                        "message_task",
                        {"id": str(uuid.uuid4()), "reply_to": "1", "answer": "allow"},
                    ),
                    "m5": ("manage_schedule", {"action": "pause", "id": 1}),
                    "m6": ("run_command", {"command": "echo hi"}),
                    "m7": ("send", {"text": "发到群", "target": {"space_id": self.env.space}}),
                    "m3": ("send", {"text": "只汇报"}),
                }
                return ChatResponse(
                    "",
                    0,
                    tool_calls=tuple(
                        ToolCall(identity, ToolFunction(name, json.dumps(arguments)))
                        for identity, (name, arguments) in calls.items()
                    ),
                )
            keys = ",".join(issue["key"] for issue in context["watchdog"]["issues"])
            return _call("send", {"text": f"看门狗提醒：{keys}"}, "notice")
        self.other_requests.append(request)
        return self.ordinary(request)

    def ordinary(self, request):
        return "好"

    async def build(self) -> Scene:
        self.provider = FakeLLMProvider(self.respond)
        self.env = await social_env(self.database, self.tmp_path)
        settings = make_settings(
            self.database.url, runtime_work_enabled=True, enabled_groups_csv="20001"
        )
        self.harness = build_harness(self.database, settings, self.provider)
        self.chat = self.harness.processor._chat
        state = ShortState(WorkspaceStore(self.tmp_path / "state"))
        self.chat.runtime.runner.main_contract = MainAgentContract(self.chat, state)
        self.chat.runtime.runner.code_mode_settings = settings
        self.chat._tools.short_state = state
        self.chat._tools.social_service = self.env.service
        self.env.service.runtime_config = self.chat._runtime_config
        automations = AutomationService(
            settings=settings,
            repository=AutomationRepository(self.database),
            time_service=TimeContextService(default_timezone=settings.default_timezone),
        )
        self.chat.set_automation_tools(AutomationToolService(automations))
        self.harness.processor._rate_limiter = SlidingWindowRateLimiter(
            per_user=1000, per_group=1000
        )
        self.config = PaceConfig(self.chat._runtime_config, self.cap)

        async def policy():
            return WorkStorageRuntimeConfig(total_max_bytes=self.cap[0])

        self.database.protocol_storage_policy = policy
        assert await self.env.router.cas_takeover_person(self.env.person) == "taken"
        async with self.database.immediate_session() as writer:
            # One owner: the seeded operator superuser has no route in this scene.
            await writer.execute(update(CanonicalPersonModel).values(is_superuser=False))
            await writer.execute(
                update(CanonicalPersonModel)
                .where(CanonicalPersonModel.id == self.env.person)
                .values(is_superuser=True)
            )
        # The owner has chatted privately before: the route's Conversation exists.
        result = await self.harness.processor.handle(self.message("在吗", "hello"), MemorySender())
        assert result.reason == "chat", result
        self.restart()
        return self

    def restart(self) -> None:
        """A new process: fresh in-memory state over the same database."""
        self.turns = ProactivePrivateTurns(
            self.database,
            router=self.env.router,
            scopes=self.chat._conversation_scopes,
            turn_coordinator=self.chat._turn_coordinator,
            concurrency=self.chat._concurrency,
            runtime_config=self.config,
            generate=self.chat.generate_origin_turn,
            clock=self.clock,
        )

        async def loops():
            if self.fail_loops:
                raise RuntimeError("health read failed")
            return self.loops

        async def components():
            return self.components

        self.watchdog = Watchdog(
            self.database,
            self.turns,
            self.config,
            loops=loops,
            components=components,
            device_online=lambda device: self.online,
            clock=self.clock,
        )

    def message(self, text: str, key: str) -> InboundMessage:
        return InboundMessage(
            ingress_key=key,
            native_message_id=key,
            event_type="message:test",
            scope_type=ScopeType.PRIVATE,
            sender=SenderIdentity("10001", nickname="本人"),
            text=text,
            bot_user_id="80001",
            person_id=self.env.person,
            presence_id=self.env.presence,
        )

    async def tick(self, advance: float = 0.0) -> list[str]:
        self.clock.offset += advance
        await self.watchdog.tick()
        return await self.watchdog.drain()

    def sends(self) -> list[str]:
        return [
            params["message"] if isinstance(params.get("message"), str) else json.dumps(params)
            for action, params in self.env.bot.calls
            if action == "send_private_msg"
        ]

    async def rows(self, table, *where) -> list[dict[str, Any]]:
        async with self.database.read_session() as reader:
            return [
                dict(row) for row in (await reader.execute(select(table).where(*where))).mappings()
            ]

    async def count(self, table) -> int:
        async with self.database.read_session() as reader:
            return int(await reader.scalar(select(func.count()).select_from(table)) or 0)

    async def conversation(self) -> str:
        async with self.database.read_session() as reader:
            return str(
                await reader.scalar(
                    select(ChatEventModel.canonical_conversation_id).where(
                        ChatEventModel.native_message_id == "hello"
                    )
                )
            )


async def _scene(database, tmp_path) -> Scene:
    return await Scene(database, tmp_path).build()


def _secret_free(payload: Any) -> None:
    text = json.dumps(payload, ensure_ascii=False)
    for secret in ("SECRET-GOAL", "SECRET-CMD", "SECRET-BRIEF", "SECRET-OWNER"):
        assert secret not in text, secret


async def _delegation(
    scene: Scene,
    *,
    state: str,
    reason: str,
    parent: str | None = None,
    receipt: dict[str, Any] | None,
    request: bool = False,
    age: float = 0.0,
) -> dict[str, str]:
    """One delegation record with one operation, written as its tables hold it."""
    now = time.time() - age
    created = datetime.fromtimestamp(now, UTC)
    conversation = await scene.conversation()
    record = str(uuid.uuid4())
    device = str(uuid.uuid4())
    instance = str(uuid.uuid4())
    operation = f"prompt:{uuid.uuid4()}"
    async with scene.database.immediate_session() as writer:
        writer.add(
            ClientDeviceModel(
                id=device,
                person_id=scene.env.person,
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
                parent_work_id=parent,
                conversation_id=conversation,
                generation=1,
                source_key=f"executor:{record}",
                source_json=json.dumps({"origin": "user_message", "worker": True}),
                goal="SECRET-GOAL 修复构建",
                state=state,
                reason=reason,
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
                    {"relationship": "delegate", "device_id": device, "instance_id": instance}
                ),
            )
        )
        writer.add(
            AgentOperationModel(
                id=operation,
                instance_id=instance,
                session_id=None,
                actor_person_id=scene.env.person,
                work_id=record,
                source_json="{}",
                action="prompt",
                payload_hash="0" * 64,
                command_json=json.dumps({"prompt": "SECRET-CMD"}),
                result_json=json.dumps(receipt) if receipt is not None else None,
                created_at=created,
            )
        )
        await writer.flush()
        if request:
            writer.add(
                AgentEventModel(
                    device_id=device,
                    event_id=f"perm-{record}",
                    operation_id=operation,
                    kind="permission",
                    payload_json=json.dumps(
                        {
                            "payload": {
                                "native_request_id": "native-1",
                                "tool": "Bash",
                                "input": {"command": "SECRET-CMD"},
                            }
                        }
                    ),
                    created_at=created,
                )
            )
    return {"record": record, "operation": operation}


async def _capacity_suspended_work(scene: Scene) -> str:
    """A real private execution suspended by ``work_protocol_storage_capacity``."""

    def ordinary(request):
        number = len(scene.other_requests)
        if number == 2:
            return _call("set_short_note", {"slot": 1, "text": "SECRET-OWNER 第一步"}, "s1")
        if number == 3:
            scene.cap[0] = 1  # the 2 GiB budget is full from here on
            return _call("set_short_note", {"slot": 1, "text": "第二步"}, "s2")
        return "好"

    scene.ordinary = ordinary
    result = await scene.harness.processor.handle(
        scene.message("SECRET-OWNER 帮我整理一下", "incident"), MemorySender()
    )
    scene.ordinary = lambda request: "好"
    suspended = await scene.rows(work, work.c.state == "suspended")
    assert result.reason == "chat", result
    assert [row["reason"] for row in suspended] == ["work_protocol_storage_capacity"]
    return str(suspended[0]["id"])


# --------------------------------------------------------------------------- S1


async def test_capacity_incident_is_told_once_and_again_after_it_recurs(database, tmp_path):
    scene = await _scene(database, tmp_path)
    parent = await _capacity_suspended_work(scene)
    delegated = await _delegation(
        scene,
        state="waiting_user",
        reason="native_request",
        parent=parent,
        receipt={"status": "accepted", "pending": True},
        request=True,
    )
    objects_before = await scene.count(objects)
    works_before = await scene.count(work)

    # A permission card younger than the window is not yet a problem.
    outcomes = await scene.tick()
    turns = await scene.rows(proactive_turns)
    issues = {row["issue_key"] for row in await scene.rows(watchdog_issues)}
    assert outcomes == ["finished"]
    assert issues == {"protocol_storage_capacity", "protocol_storage_near_cap"}
    assert [row["state"] for row in turns] == ["finished"]
    assert len(scene.notice_requests) == 2  # send, then the final after its result
    context = _runtime_context(scene.notice_requests[0])
    assert context is not None and context["origin"] == "proactive_private"
    said = {issue["key"]: issue["facts"] for issue in context["watchdog"]["issues"]}
    assert said["protocol_storage_capacity"]["suspended_work_ids"] == [parent]
    _secret_free(context)
    # F8: told during the capacity incident, without a Work or a protocol object.
    sends = scene.sends()
    assert len(sends) == 1 and "protocol_storage_capacity" in sends[0]
    assert await scene.count(objects) == objects_before
    assert await scene.count(work) == works_before
    async with database.read_session() as reader:
        outbound = list(
            await reader.scalars(
                select(ChatEventModel.origin).where(ChatEventModel.direction == "outbound")
            )
        )
        ledger = " ".join(
            str(text) for text in await reader.scalars(select(ChatEventModel.content))
        )
    assert outbound.count("proactive_private") == 1
    # The real send's receipt is found by the key recovery reads (F10).
    receipts = await scene.rows(
        SocialOperationModel.__table__,
        SocialOperationModel.source_turn_id
        == turn_source_key(await scene.conversation(), turns[0]["id"]),
    )
    assert [row["status"] for row in receipts] == ["succeeded"]
    # F17: neither the instruction nor the material is in the ledger.
    assert "看门狗发现" not in ledger and '"watchdog"' not in ledger

    # F1/F7: unchanged ticks and a restart neither write nor tell again.
    writes = scene.watchdog.writes
    assert await scene.tick(CHECK) == []
    assert scene.watchdog.writes == writes
    scene.restart()
    assert await scene.tick(CHECK) == []
    assert scene.watchdog.writes == 0
    assert len(scene.sends()) == 1

    # F4 (I2): the card is still unanswered past the window: told, but not before
    # the person's minimum interval (F19); F15: more capacity suspensions add nothing.
    await scene.tick(WINDOW)
    assert len(scene.sends()) == 1
    pending = await scene.rows(proactive_turns, proactive_turns.c.state == "pending")
    assert len(pending) == 1
    await scene.tick(MIN_INTERVAL)
    sends = scene.sends()
    assert len(sends) == 2 and f"delegation_waiting_parent:{delegated['record']}" in sends[1]
    second = _runtime_context(scene.notice_requests[-2])
    assert second is not None
    assert [issue["key"] for issue in second["watchdog"]["issues"]] == [
        f"delegation_waiting_parent:{delegated['record']}"
    ]
    _secret_free(second)
    async with database.read_session() as reader:
        facts = " ".join(await reader.scalars(select(watchdog_issues.c.facts_json)))
    _secret_free(facts)

    # F17: the owner's next ordinary turn carries no watchdog material.
    before = len(scene.other_requests)
    await scene.harness.processor.handle(scene.message("知道了", "ack"), MemorySender())
    later = scene.other_requests[before:]
    assert later and all("watchdog" not in (_runtime_context(r) or {}) for r in later)

    # Cleared: cap raised, the parent resumed (cancelled here), the card answered.
    scene.cap[0] = 2 * 1024**3
    async with database.immediate_session() as writer:
        await writer.execute(
            update(work).where(work.c.id == parent).values(state="cancelled", updated=time.time())
        )
        await writer.execute(
            update(work)
            .where(work.c.id == delegated["record"])
            .values(state="cancelled", updated=time.time())
        )
    await scene.tick(CHECK)
    assert {row["issue_key"] for row in await scene.rows(watchdog_issues)} == issues | {
        f"delegation_waiting_parent:{delegated['record']}"
    }
    await scene.tick(WINDOW)  # clear for the whole window: rows deleted
    assert await scene.rows(watchdog_issues) == []

    # F2: it recurs after the window: told again (after the min interval).
    scene.cap[0] = 1
    await scene.tick(MIN_INTERVAL)
    sends = scene.sends()
    assert len(sends) == 3 and "protocol_storage_capacity" in sends[2]
    RESULTS["S1_capacity_incident"] = {
        "suspended_parent": parent,
        "delegation": delegated["record"],
        "first_runtime_context": context,
        "second_runtime_context": second,
        "sends": sends,
        "turns": [
            {key: row[key] for key in ("state", "reason", "attempts", "result_json")}
            for row in await scene.rows(proactive_turns)
        ],
        "protocol_objects_before_and_after_notice": objects_before,
        "writes_on_unchanged_ticks": 0,
    }


# --------------------------------------------------------------------------- S2


async def test_delegation_stuck_shapes_are_detected(database, tmp_path):
    scene = await _scene(database, tmp_path)
    unknown = await _delegation(
        scene, state="waiting_external", reason="native_executor", receipt=None, age=WINDOW + 60
    )
    refused = await _delegation(
        scene,
        state="waiting_external",
        reason="native_executor",
        receipt={"status": "failed", "admitted": False, "error": "native_start_failed"},
        age=WINDOW + 60,
    )
    silent = await _delegation(
        scene,
        state="waiting_external",
        reason="device_offline",
        receipt={"status": "accepted", "pending": True},
        age=WINDOW + 60,
    )
    fresh = await _delegation(
        scene, state="waiting_external", reason="native_executor", receipt=None
    )
    await scene.tick()
    keys = {row["issue_key"] for row in await scene.rows(watchdog_issues)}
    assert f"operation_unconfirmed:{unknown['operation']}" in keys  # I3
    assert f"delegation_orphaned:{refused['record']}" in keys  # I4
    assert f"delegation_silent:{silent['record']}:offline" in keys
    assert not any(fresh["record"] in key or fresh["operation"] in key for key in keys)
    assert len(scene.sends()) == 1
    _secret_free(_runtime_context(scene.notice_requests[0]))
    RESULTS["S2_delegation_shapes"] = sorted(keys)


# --------------------------------------------------------------------------- S3 F3


async def test_cleared_before_start_says_nothing(database, tmp_path):
    scene = await _scene(database, tmp_path)
    scene.loops = {"runtime_work": {"running": False, "errors": {}}}
    await scene.tick()
    assert len(scene.sends()) == 1
    scene.loops = {"runtime_work": {"running": True, "errors": {}}}
    scene.components = {"gateway:qq": {"healthy": False, "state": "unhealthy", "durable": True}}
    scene.watchdog._present.clear()  # the new turn will find its issue already gone
    await scene.watchdog.tick()
    scene.components = {"gateway:qq": {"healthy": True}}
    scene.watchdog._present.clear()
    outcomes = await scene.watchdog.drain()
    assert outcomes in ([], ["cleared_before_start"])
    scene.clock.offset += MIN_INTERVAL
    await scene.watchdog.tick()
    scene.watchdog._present.clear()
    outcomes = await scene.watchdog.drain()
    assert outcomes == ["cleared_before_start"]
    assert len(scene.sends()) == 1
    assert len(scene.notice_requests) == 2
    RESULTS["S3_cleared_before_start"] = outcomes


# --------------------------------------------------------------------------- S4 F5


async def test_notice_turn_only_reports(database, tmp_path):
    scene = await _scene(database, tmp_path)
    scene.mode = "manage"
    scene.loops = {"runtime_work": {"running": False, "errors": {}}}
    works_before = await scene.count(work)
    assert await scene.tick() == ["finished"]
    results = _tool_results(scene.notice_requests[-1])
    refused = {key: value.get("error") for key, value in results.items() if key != "m3"}
    assert refused == {
        "m1": "work_control_unavailable",
        "m1b": "work_control_unavailable",
        "m4": "work_control_unavailable",
        "m2": "report_only_source",
        "m5": "report_only_source",
        "m6": "report_only_source",
        "m7": "permission_denied",
    }, results
    assert all(value.get("executed") is not True for value in results.values())
    assert len(scene.sends()) == 1
    assert not any(action == "send_group_msg" for action, _ in scene.env.bot.calls)
    assert await scene.count(work) == works_before
    RESULTS["S4_report_only"] = refused


# --------------------------------------------------------------------------- S5 F9


async def test_failed_notice_is_retried_once_then_waits_for_next_batch(database, tmp_path):
    scene = await _scene(database, tmp_path)
    scene.mode = "fail"
    scene.loops = {"runtime_work": {"running": False, "errors": {}}}
    assert await scene.tick() == ["failed"]
    assert await scene.tick(CHECK) == []  # retry waits for the min interval
    assert await scene.tick(MIN_INTERVAL) == ["failed"]  # the one retry
    calls = len(scene.notice_requests)
    for _ in range(3):
        assert await scene.tick(MIN_INTERVAL) == []
    assert len(scene.notice_requests) == calls
    scene.mode = "send"
    scene.loops["maintenance"] = {"running": False, "errors": {}}
    assert await scene.tick(MIN_INTERVAL) == ["finished"]
    context = _runtime_context(scene.notice_requests[-2])
    assert context is not None
    assert {issue["key"] for issue in context["watchdog"]["issues"]} == {
        "loop_stopped:runtime_work",
        "loop_stopped:maintenance",
    }
    RESULTS["S5_retry"] = [
        {key: row[key] for key in ("state", "reason", "attempts")}
        for row in await scene.rows(proactive_turns)
    ]


# --------------------------------------------------------------------------- S6 F10


async def test_lost_turn_is_settled_from_its_receipts(database, tmp_path):
    scene = await _scene(database, tmp_path)
    conversation = await scene.conversation()
    now = time.time()
    ids = {}
    async with database.immediate_session() as writer:
        for label in ("sent", "claimed", "none"):
            ids[label] = str(uuid.uuid4())
            await writer.execute(
                proactive_turns.insert().values(
                    id=ids[label],
                    source_kind="watchdog",
                    person_id=scene.env.person,
                    conversation_id=conversation,
                    presence_id=scene.env.presence,
                    state="running",
                    attempts=1,
                    created=now,
                    started=now,
                    updated=now,
                )
            )
        for label, status in (("sent", "succeeded"), ("claimed", "executing")):
            writer.add(
                SocialOperationModel(
                    id=str(uuid.uuid4()),
                    source_turn_id=turn_source_key(conversation, ids[label]),
                    tool_call_id="c",
                    source_conversation_id=conversation,
                    action="send_message",
                    payload_hash="0" * 64,
                    target_kind="person",
                    target_id=scene.env.person,
                    status=status,
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
    await scene.turns.recover()
    states = {
        label: (await scene.rows(proactive_turns, proactive_turns.c.id == identity))[0]["state"]
        for label, identity in ids.items()
    }
    assert states == {"sent": "finished", "claimed": "unknown", "none": "failed"}
    RESULTS["S6_recover"] = states


# --------------------------------------------------------------------------- S7 F11


async def test_paused_route_pays_nothing_and_resumes(database, tmp_path):
    scene = await _scene(database, tmp_path)
    async with database.immediate_session() as writer:
        await writer.execute(
            update(PersonActiveRouteModel)
            .where(PersonActiveRouteModel.person_id == scene.env.person)
            .values(paused=True)
        )
    scene.loops = {"runtime_work": {"running": False, "errors": {}}}
    assert await scene.tick() == ["route_paused"]
    writes = scene.watchdog.writes
    assert await scene.tick(CHECK) == ["route_paused"]
    assert scene.watchdog.writes == writes
    assert scene.notice_requests == [] and scene.sends() == []
    async with database.immediate_session() as writer:
        await writer.execute(
            update(PersonActiveRouteModel)
            .where(PersonActiveRouteModel.person_id == scene.env.person)
            .values(paused=False)
        )
    assert await scene.tick(CHECK) == ["finished"]
    assert len(scene.sends()) == 1
    RESULTS["S7_paused_route"] = "pending while paused, delivered after"


# --------------------------------------------------------------------------- S8 F12 F18


async def test_debouncing_health_after_restart_and_flaps_are_one_occurrence(database, tmp_path):
    scene = await _scene(database, tmp_path)
    error = {"runtime_work": {"running": True, "errors": {"maintenance": "OperationalError"}}}
    scene.loops = error
    assert await scene.tick() == []  # observed, not yet persistent
    assert await scene.tick(WINDOW) == ["finished"]
    scene.restart()
    assert await scene.tick(CHECK) == []  # still debouncing after restart: kept
    assert len(await scene.rows(watchdog_issues)) == 1
    # Flap: clears and comes back inside the window: the same occurrence (F18).
    scene.loops = {"runtime_work": {"running": True, "errors": {"maintenance": None}}}
    await scene.tick(CHECK)
    scene.loops = error
    await scene.tick(CHECK)
    await scene.tick(WINDOW)
    await scene.tick(MIN_INTERVAL)
    assert len(scene.sends()) == 1
    RESULTS["S8_restart_and_flap"] = len(scene.sends())


# --------------------------------------------------------------------------- S9 F13


async def test_busy_conversation_keeps_the_turn_pending(database, tmp_path):
    scene = await _scene(database, tmp_path)
    scene.loops = {"runtime_work": {"running": False, "errors": {}}}
    conversation = await scene.conversation()
    held = asyncio.Event()
    release = asyncio.Event()

    async def hold():
        async with scene.chat._turn_coordinator.background_turn(
            f"conversation:{conversation}"
        ) as token:
            assert token is not None
            held.set()
            await release.wait()

    holder = asyncio.create_task(hold())
    await held.wait()
    outcome = await asyncio.wait_for(scene.tick(), 10)
    release.set()
    await holder
    assert outcome == ["conversation_busy"]
    assert scene.notice_requests == []
    assert await scene.tick(CHECK) == ["finished"]
    RESULTS["S9_busy"] = outcome


# --------------------------------------------------------------------------- S10 F14


async def test_superuser_changes(database, tmp_path):
    scene = await _scene(database, tmp_path)
    async with database.immediate_session() as writer:
        await writer.execute(
            update(CanonicalPersonModel)
            .where(CanonicalPersonModel.id == scene.env.person)
            .values(is_superuser=False)
        )
    scene.loops = {"runtime_work": {"running": False, "errors": {}}}
    assert await scene.tick() == []
    assert await scene.rows(watchdog_issues) == []
    async with database.immediate_session() as writer:
        await writer.execute(
            update(CanonicalPersonModel)
            .where(CanonicalPersonModel.id == scene.env.person)
            .values(is_superuser=True)
        )
    assert await scene.tick(CHECK) == ["finished"]
    async with database.immediate_session() as writer:
        await writer.execute(
            update(CanonicalPersonModel)
            .where(CanonicalPersonModel.id == scene.env.person)
            .values(is_superuser=False)
        )
    scene.loops["maintenance"] = {"running": False, "errors": {}}
    assert await scene.tick(MIN_INTERVAL) == []
    assert await scene.rows(watchdog_issues) == []
    assert len(scene.sends()) == 1
    RESULTS["S10_superuser"] = "revoked: nothing; granted: told current issues"


# --------------------------------------------------------------------------- S11 F16


async def test_failing_check_is_only_health(database, tmp_path):
    scene = await _scene(database, tmp_path)
    scene.loops = {"runtime_work": {"running": False, "errors": {}}}
    await scene.tick()
    scene.fail_loops = True
    assert await scene.tick(WINDOW * 2) == []
    health = await scene.watchdog.health()
    assert health["check_errors"] == {"loops": "RuntimeError"}
    assert len(await scene.rows(watchdog_issues)) == 1  # undetermined: kept
    scene.fail_loops = False
    assert await scene.tick(CHECK) == []
    assert len(scene.sends()) == 1
    RESULTS["S11_failing_check"] = health["check_errors"]


# --------------------------------------------------------------------------- S12 F19


async def test_new_issues_inside_the_min_interval_are_merged(database, tmp_path):
    scene = await _scene(database, tmp_path)
    scene.loops = {"runtime_work": {"running": False, "errors": {}}}
    assert await scene.tick() == ["finished"]
    for name in ("subagents", "sandbox_completions", "maintenance"):
        scene.loops[name] = {"running": False, "errors": {}}
        assert await scene.tick(CHECK) == []
    assert await scene.tick(MIN_INTERVAL) == ["finished"]
    context = _runtime_context(scene.notice_requests[-2])
    assert context is not None and context["watchdog"]["issues_total"] == 3
    assert len(scene.sends()) == 2
    RESULTS["S12_min_interval"] = [issue["key"] for issue in context["watchdog"]["issues"]]


# --------------------------------------------------------------------------- review F21-F24


async def test_acp_tool_payload_never_reaches_the_facts(database, tmp_path):
    scene = await _scene(database, tmp_path)
    delegated = await _delegation(
        scene,
        state="waiting_user",
        reason="native_request",
        receipt={"status": "accepted", "pending": True},
        request=True,
        age=WINDOW + 60,
    )
    async with database.immediate_session() as writer:
        await writer.execute(
            update(AgentEventModel)
            .where(AgentEventModel.operation_id == delegated["operation"])
            .values(
                payload_json=json.dumps(
                    {
                        "payload": {
                            "native_request_id": "native-1",
                            "tool": {
                                "toolCallId": "t1",
                                "title": "Run SECRET-CMD rm -rf /",
                                "kind": "execute",
                                "rawInput": {"command": "SECRET-CMD"},
                            },
                            "options": [{"name": "SECRET-CMD"}],
                        }
                    }
                )
            )
        )
    assert await scene.tick() == ["finished"]
    async with database.read_session() as reader:
        facts = " ".join(await reader.scalars(select(watchdog_issues.c.facts_json)))
    context = _runtime_context(scene.notice_requests[0])
    _secret_free(facts)
    _secret_free(context)
    assert context is not None
    requests = context["watchdog"]["issues"][0]["facts"]["requests"]
    assert [item["tool"] for item in requests] == ["execute"]
    RESULTS["S13_acp_tool"] = requests


async def test_unknown_send_then_model_failure_is_not_resent(database, tmp_path):
    scene = await _scene(database, tmp_path)
    bot = scene.env.bot
    original = bot.call_api
    first = {"pending": True}

    async def call_api(action, **params):
        if action == "send_private_msg" and first["pending"]:
            first["pending"] = False
            bot.calls.append((action, params))
            raise RuntimeError("transport timeout after dispatch")
        return await original(action, **params)

    bot.call_api = call_api

    def respond(request):
        context = _runtime_context(request)
        if context is not None and "watchdog" in context:
            scene.notice_requests.append(request)
            if any(message.role == "tool" for message in request.messages):
                raise LLMUnavailableError("provider down")
            return _call("send", {"text": "notice"}, "n1")
        return "好"

    scene.respond = respond
    scene.provider._responder = respond
    scene.loops = {"runtime_work": {"running": False, "errors": {}}}
    assert await scene.tick() == ["unknown"]
    for _ in range(3):
        assert await scene.tick(MIN_INTERVAL) == []
    private = [call for call in bot.calls if call[0] == "send_private_msg"]
    assert len(private) == 1
    RESULTS["S14_unknown_not_resent"] = [
        {key: row[key] for key in ("state", "reason", "attempts")}
        for row in await scene.rows(proactive_turns)
    ]


async def test_undetermined_at_the_due_tick_is_still_told(database, tmp_path):
    scene = await _scene(database, tmp_path)
    scene.loops = {"runtime_work": {"running": False, "errors": {}}}
    assert await scene.tick() == ["finished"]
    scene.components = {"gateway:qq": {"healthy": False, "state": "unhealthy", "durable": True}}
    assert await scene.tick(CHECK) == []  # inside the minimum interval
    scene.components = {"gateway:qq": {"healthy": None}}  # momentarily undetermined
    assert await scene.tick(MIN_INTERVAL) == ["finished"]
    assert len(scene.sends()) == 2 and "component:gateway:qq:unhealthy" in scene.sends()[1]
    RESULTS["S15_undetermined_due"] = scene.sends()


async def test_cleared_before_start_and_back_within_the_window_is_told(database, tmp_path):
    scene = await _scene(database, tmp_path)
    scene.loops = {"runtime_work": {"running": False, "errors": {}}}
    assert await scene.tick() == ["finished"]
    scene.loops["maintenance"] = {"running": False, "errors": {}}
    assert await scene.tick(CHECK) == []  # pending: inside the minimum interval
    scene.loops["maintenance"] = {"running": True, "errors": {}}
    assert await scene.tick(MIN_INTERVAL) == ["cleared_before_start"]
    issue = await scene.rows(
        watchdog_issues, watchdog_issues.c.issue_key == "loop_stopped:maintenance"
    )
    assert issue and issue[0]["turn_id"] is None  # not told, row kept for the window
    scene.loops["maintenance"] = {"running": False, "errors": {}}  # back within the window
    assert await scene.tick(CHECK) == ["finished"]
    assert len(scene.sends()) == 2 and "loop_stopped:maintenance" in scene.sends()[1]
    RESULTS["S16_back_within_window"] = scene.sends()


async def test_a_lost_attempt_does_not_block_the_person(database, tmp_path):
    scene = await _scene(database, tmp_path)
    scene.loops = {"runtime_work": {"running": False, "errors": {}}}
    original = scene.turns._run

    async def lost(*args, **kwargs):
        raise asyncio.CancelledError  # the attempt dies without settling its turn

    scene.turns._run = lost
    await scene.tick()
    turns = await scene.rows(proactive_turns)
    assert [row["state"] for row in turns] == ["running"]
    scene.turns._run = original
    await scene.tick(CHECK)  # no live attempt owns it: settled from its receipts
    turns = await scene.rows(proactive_turns)
    assert [row["state"] for row in turns] == ["failed"]
    assert await scene.tick(MIN_INTERVAL) == ["finished"]
    assert len(scene.sends()) == 1
    RESULTS["S17_lost_attempt"] = [row["reason"] for row in await scene.rows(proactive_turns)]


# --------------------------------------------------------------------------- round 2 F25-F28


def _silent(scene: Scene, first):
    def respond(request):
        context = _runtime_context(request)
        if context is not None and "watchdog" in context:
            scene.notice_requests.append(request)
            if any(message.role == "tool" for message in request.messages):
                return "发不出去"
            return first()
        return "好"

    scene.respond = respond
    scene.provider._responder = respond


@pytest.mark.parametrize("shape", ["no_send", "refused_send"])
async def test_a_turn_without_a_delivered_send_is_not_told(database, tmp_path, shape):
    scene = await _scene(database, tmp_path)
    if shape == "no_send":
        _silent(scene, lambda: "看门狗提醒：runtime_work 停了")  # final text only
    else:
        _silent(scene, lambda: _call("send", {"text": "提醒", "reply_to": 999999}, "n1"))
    scene.loops = {"runtime_work": {"running": False, "errors": {}}}
    assert await scene.tick() == ["failed"]
    assert await scene.tick(MIN_INTERVAL) == ["failed"]  # the one retry
    assert await scene.tick(MIN_INTERVAL) == []  # waits for the next batch
    scene.mode = "send"
    scene.respond = Scene.respond.__get__(scene)
    scene.provider._responder = scene.respond
    scene.loops["maintenance"] = {"running": False, "errors": {}}
    assert await scene.tick(MIN_INTERVAL) == ["finished"]
    context = _runtime_context(scene.notice_requests[-2])
    assert context is not None
    assert {issue["key"] for issue in context["watchdog"]["issues"]} == {
        "loop_stopped:runtime_work",
        "loop_stopped:maintenance",
    }
    RESULTS[f"S18_undelivered_{shape}"] = [
        {key: row[key] for key in ("state", "reason", "attempts")}
        for row in await scene.rows(proactive_turns)
    ]


async def test_forgetting_a_notified_person_removes_the_watchdog_rows(database, tmp_path):
    from yuki.persistence.people_repository import PeopleRepository

    scene = await _scene(database, tmp_path)
    scene.loops = {"runtime_work": {"running": False, "errors": {}}}
    assert await scene.tick() == ["finished"]
    assert await PeopleRepository(database).delete_person_id(scene.env.person) is True
    assert await scene.rows(proactive_turns) == []
    assert await scene.rows(watchdog_issues) == []
    RESULTS["S19_forget"] = "notified person forgotten; turns and issues removed"


async def test_a_route_whose_presence_is_gone_is_not_rerouted(database, tmp_path):
    from tests.support.canonical_identity import ensure_presence

    scene = await _scene(database, tmp_path)
    async with database.immediate_session() as writer:
        other = await ensure_presence(writer, "80009")
    async with database.immediate_session() as writer:
        await writer.execute(
            update(PersonActiveRouteModel)
            .where(PersonActiveRouteModel.person_id == scene.env.person)
            .values(presence_id=other)
        )
    scene.loops = {"runtime_work": {"running": False, "errors": {}}}
    outcome = await scene.tick()
    assert outcome == ["route_presence_unavailable"]
    assert scene.notice_requests == [] and scene.sends() == []
    turns = await scene.rows(proactive_turns)
    assert [row["state"] for row in turns] == ["pending"]
    RESULTS["S20_presence_gone"] = outcome


async def test_the_loop_survives_a_config_failure_and_state_stays_bounded(database, tmp_path):
    scene = await _scene(database, tmp_path)
    failures = {"left": 0}
    inner = scene.config.snapshot

    async def flaky(**kwargs):
        if failures["left"]:
            failures["left"] -= 1
            raise RuntimeError("config read failed")
        snapshot = await inner(**kwargs)
        return replace(snapshot, watchdog=replace(snapshot.watchdog, check_interval_seconds=0.01))

    scene.config.snapshot = flaky
    task = asyncio.create_task(scene.watchdog._loop())

    async def until(condition) -> None:
        for _ in range(500):
            if condition():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("condition not reached")

    try:
        await until(lambda: scene.watchdog._last_check is not None)
        failures["left"] = 1  # the next config read fails
        await until(lambda: scene.watchdog._last_error == "RuntimeError")
        before = scene.watchdog._last_check
        await until(lambda: scene.watchdog._last_check != before)  # and it goes on
        assert not task.done()
        assert scene.watchdog.status()["enabled"] is True
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    # A category change restarts its persistence clock.
    scene.loops = {"runtime_work": {"running": True, "errors": {"maintenance": "A"}}}
    await scene.tick()
    scene.loops = {"runtime_work": {"running": True, "errors": {"maintenance": "B"}}}
    await scene.tick(WINDOW / 2)
    assert not any(key.endswith(":A") for key in scene.watchdog._first_seen)
    assert scene.watchdog.outcomes.maxlen is not None
    RESULTS["S21_loop_and_bounds"] = "loop survived a config failure"
