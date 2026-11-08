"""E2E: ordinary private execution continuity, interjection and automation queueing.

Replays the 2026-11-07 QQ private incident shape with an isolated SQLite, a fake
Provider and the real processor / Main / Work / Social paths. Every scenario
records its facts; the module writes them to
``test-results/execution-continuity-e2e.json`` (see ``_write_artifact``).

Failure cases written before the implementation (task book §8):

C02 admission
- the first non-send tool batch of a private Main runs without an owning Work;
- admission commits without the paid request count or without the first
  ``phase=response`` journal (a crash then loses the response / tail calls);
- a crash after the admission commit and before the first tool repurchases the
  model response, drops a pending call, resets the budget or runs a call twice;
- calls of one parallel batch are bound to different owners (accept barrier split);
- ``execute_code`` in the first batch is refused with ``accept_work_before_execution``;
- an explicit ``accept`` after the Host admission creates a second Work;
- ``RUNTIME_WORK_ENABLED=false`` still creates a Work, or is truncated at 24;
- a group direct turn is admitted as Work.
C03 segments
- more than 24 model requests / 32 tool calls ends with
  ``agent_segment_budget_exhausted`` and no reply;
- a plugin's explicit limit no longer bounds the root Work.
C04 interjection
- a message during an admitted execution supersedes it, opens a second Main,
  is dropped, or a later side-effecting call of the old batch still runs;
- the interjected text never reaches the next model request.
C05 automation
- a user message during an automation run is dropped as
  ``conversation_activation_busy``;
- the automation run is superseded by the user's input version;
- an exception path records ``llm_calls=0`` after paid requests.
C06 recent work
- the next activation (user or automation) cannot see the previous execution's
  real tool facts.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from tests.conftest import MemorySender, build_harness, make_settings
from tests.support.automation_runtime_helpers import FakeClock
from tests.support.social_identity_cases import social_env
from tests.support.work_runner_helpers import case, tool

from yuki.automation.repository import AutomationRepository
from yuki.automation.service import AutomationService
from yuki.automation.tools import AutomationToolService
from yuki.automation.worker import AutomationWorker
from yuki.codemode.contract import EXECUTE_CODE_NAME
from yuki.domain.conversations import ScopeType
from yuki.domain.messages import (
    ChatMessage,
    ChatResponse,
    InboundMessage,
    SenderIdentity,
    ToolCall,
    ToolFunction,
)
from yuki.llm.base import LLMUnavailableError
from yuki.llm.fake import FakeLLMProvider
from yuki.persistence.models import AutomationRunModel, ChatEventModel
from yuki.runtime.work_activation import current_work_control
from yuki.runtime.work_budget_schema import budgets
from yuki.runtime.work_control import WorkControl
from yuki.runtime.work_schema_v1 import effects, journal, work
from yuki.services.main_agent_contract import MainAgentContract
from yuki.services.rate_limit import SlidingWindowRateLimiter
from yuki.time.service import TimeContextService
from yuki.workspace.short_state import ShortState
from yuki.workspace.store import WorkspaceStore

ARTIFACT = Path(__file__).resolve().parents[2] / "test-results" / "execution-continuity-e2e.json"
RESULTS: dict[str, Any] = {}


@pytest.fixture(scope="module", autouse=True)
def _write_artifact():
    yield
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(
        json.dumps(
            {
                "suite": "tests/integration/test_execution_continuity_e2e.py",
                "generated_at": datetime.now(UTC).isoformat(),
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


class ScriptedProvider(FakeLLMProvider):
    """Fake Provider that records Work ownership per request and can pause."""

    def __init__(self, responder, *, pauses=()):
        self.ownership: list[str | None] = []
        self.entered: list[Any] = []
        self.arrived = {number: asyncio.Event() for number in pauses}
        self.release = {number: asyncio.Event() for number in pauses}
        super().__init__(responder)

    async def _complete(self, request):
        control = current_work_control.get()
        self.ownership.append(
            control.current["id"] if control is not None and control.current else None
        )
        self.entered.append(request)
        number = len(self.ownership)
        if number in self.arrived:
            self.arrived[number].set()
            await self.release[number].wait()
        return await super()._complete(request)

    async def wait(self, number: int) -> None:
        await asyncio.wait_for(self.arrived[number].wait(), 10)


def _plain(request) -> str:
    return "\n".join(str(message.content or "") for message in request.messages)


async def _private_scene(database, tmp_path, provider, *, work_enabled=True, code_enabled=False):
    env = await social_env(database, tmp_path)
    settings = make_settings(
        database.url,
        runtime_work_enabled=work_enabled,
        enabled_groups_csv="20001",
        code_mode_enabled=code_enabled,
        automation_enabled=True,
    )
    harness = build_harness(database, settings, provider)
    chat = harness.processor._chat
    state = ShortState(WorkspaceStore(tmp_path / "state"))
    chat.runtime.runner.main_contract = MainAgentContract(chat, state, code_enabled=code_enabled)
    chat.runtime.runner.code_mode_settings = settings
    chat._tools.short_state = state
    chat._tools.social_service = env.service
    env.service.runtime_config = chat._runtime_config
    harness.processor._rate_limiter = SlidingWindowRateLimiter(per_user=1000, per_group=1000)
    assert await env.router.cas_takeover_person(env.person) == "taken"
    return env, harness, chat, settings


def _private(env, text: str, key: str) -> InboundMessage:
    return InboundMessage(
        ingress_key=key,
        native_message_id=key,
        event_type="message:test",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity("10001", nickname="本人"),
        text=text,
        bot_user_id="80001",
        person_id=env.person,
        presence_id=env.presence,
    )


def _group(env, text: str, key: str) -> InboundMessage:
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
        conversation_id=env.context.conversation_id,
        person_id=env.person,
        presence_id=env.presence,
        space_id=env.space,
    )


async def _rows(database, table, *where):
    async with database.read_session() as reader:
        return [dict(row) for row in (await reader.execute(select(table).where(*where))).mappings()]


async def _event_id(database, key: str) -> int:
    async with database.read_session() as reader:
        return int(
            await reader.scalar(select(ChatEventModel.id).where(ChatEventModel.origin_key == key))
            or await reader.scalar(
                select(ChatEventModel.id).where(ChatEventModel.native_message_id == key)
            )
        )


def _runtime_context(request) -> dict[str, Any]:
    text = _plain(request)
    start = text.index("<runtime_context>\n") + len("<runtime_context>\n")
    return json.loads(text[start : text.index("\n</runtime_context>", start)])


def _private_sends(env) -> list[dict[str, Any]]:
    return [params for action, params in env.bot.calls if action == "send_private_msg"]


def _tool_results(request) -> dict[str, Any]:
    results = {}
    for message in request.messages:
        if message.role == "tool" and message.tool_call_id:
            try:
                results[message.tool_call_id] = json.loads(message.content or "null")
            except ValueError:
                results[message.tool_call_id] = message.content
    return results


# --------------------------------------------------------------------------- C02/C03


async def test_incident_private_execution_beyond_24_requests_completes_in_host_work(
    database, tmp_path
):
    steps = 30

    def respond(request):
        number = len(provider.requests)
        if number <= steps:
            return _call("update_short_state", {"slot": 1, "text": f"step-{number}"}, f"s{number}")
        if number == steps + 1:
            return _call("send", {"text": "报告已经传到电脑上了"}, "deliver")
        return "执行完成"

    provider = ScriptedProvider(respond)
    env, harness, _chat, _ = await _private_scene(database, tmp_path, provider)
    message = _private(env, "把那份报告放到这个电脑里", "incident-135131")
    result = await harness.processor.handle(message, MemorySender())
    trigger = await _event_id(database, "incident-135131")
    works = await _rows(database, work)
    assert result.reason == "chat", result
    assert len(provider.requests) == steps + 2 > 24
    assert len(works) == 1
    admitted = works[0]
    source = json.loads(admitted["source_json"])
    assert admitted["state"] == "completed"
    assert source["trigger_event_id"] == trigger and source["origin"] == "user_message"
    assert f"event:{trigger}" in admitted["goal"]
    assert "报告" not in admitted["goal"]
    assert admitted["model_requests"] == len(provider.requests)
    # The first request is ordinary chat; admission precedes its tool batch.
    assert provider.ownership[0] is None
    assert set(provider.ownership[1:]) == {admitted["id"]}
    owned = await _rows(database, effects, effects.c.work_id == admitted["id"])
    owners = {json.loads(row["receipt_json"])["invocation"]["owner_execution_id"] for row in owned}
    assert owners == {admitted["id"]}
    assert len(owned) == steps + 1
    journals = await _rows(database, journal, journal.c.work_id == admitted["id"])
    assert len(journals) == 1
    assert len(_private_sends(env)) == 1
    RESULTS["incident_private_execution_beyond_24"] = {
        "processor_reason": result.reason,
        "model_requests_observed": len(provider.requests),
        "work": {
            key: admitted[key]
            for key in ("id", "state", "goal", "model_requests", "tool_calls", "sent_messages")
        },
        "first_request_owner": provider.ownership[0],
        "later_request_owners": sorted(set(provider.ownership[1:])),
        "effect_owner_ids": sorted(owners),
        "effects": len(owned),
        "journal_phase": journals[0]["phase"],
        "private_sends": len(_private_sends(env)),
    }


async def test_work_disabled_private_execution_is_not_truncated_and_creates_no_work(
    database, tmp_path
):
    def respond(request):
        number = len(provider.requests)
        if number <= 26:
            return _call("update_short_state", {"slot": 1, "text": f"step-{number}"}, f"s{number}")
        if number == 27:
            return _call("send", {"text": "完成"}, "deliver")
        return "done"

    provider = ScriptedProvider(respond)
    env, harness, _, _ = await _private_scene(database, tmp_path, provider, work_enabled=False)
    result = await harness.processor.handle(
        _private(env, "长任务", "disabled-long"), MemorySender()
    )
    assert result.reason == "chat", result
    assert len(provider.requests) == 28
    assert await _rows(database, work) == []
    assert len(_private_sends(env)) == 1
    RESULTS["runtime_work_disabled_private"] = {
        "processor_reason": result.reason,
        "model_requests_observed": len(provider.requests),
        "works": 0,
        "private_sends": len(_private_sends(env)),
    }


async def test_group_direct_turn_is_not_admitted_and_runs_beyond_24(database, tmp_path):
    def respond(request):
        number = len(provider.requests)
        if number <= 26:
            return _call("update_short_state", {"slot": 1, "text": f"step-{number}"}, f"s{number}")
        if number == 27:
            return _call("send", {"text": "完成"}, "deliver")
        return "done"

    provider = ScriptedProvider(respond)
    env, harness, _, _ = await _private_scene(database, tmp_path, provider)
    result = await harness.processor.handle(_group(env, "群里长任务", "group-long"), MemorySender())
    assert result.reason == "chat", result
    assert len(provider.requests) == 28
    assert set(provider.ownership) == {None}
    assert await _rows(database, work) == []
    RESULTS["group_direct_not_admitted"] = {
        "processor_reason": result.reason,
        "model_requests_observed": len(provider.requests),
        "works": 0,
    }


async def test_explicit_accept_after_host_admission_does_not_create_second_work(database, tmp_path):
    def respond(request):
        number = len(provider.requests)
        if number == 1:
            return _call("update_short_state", {"slot": 1, "text": "first"}, "first")
        if number == 2:
            return _call("task_control", {"action": "accept", "goal": "模型自己的目标"}, "late")
        if number == 3:
            return _call(
                "task_control", {"action": "update", "goal": "传输报告到本人电脑"}, "rename"
            )
        return "done"

    provider = ScriptedProvider(respond)
    env, harness, _, _ = await _private_scene(database, tmp_path, provider)
    result = await harness.processor.handle(_private(env, "开始", "late-accept"), MemorySender())
    works = await _rows(database, work)
    late = _tool_results(provider.requests[2])["late"]
    assert result.reason == "chat"
    assert len(works) == 1 and works[0]["state"] == "completed"
    assert late["ok"] is False and late["error"] == "work_already_active_use_update"
    assert works[0]["goal"] == "传输报告到本人电脑"
    RESULTS["accept_after_host_admission"] = {
        "works": len(works),
        "late_accept_result": late,
        "goal_after_update": works[0]["goal"],
    }


async def test_parallel_batch_and_execute_code_share_the_admitted_work(database, tmp_path):
    def respond(request):
        number = len(provider.requests)
        if number == 1:
            return ChatResponse(
                "",
                0,
                tool_calls=(
                    ToolCall("read-a", ToolFunction("query_chat_history", '{"view":"recent"}')),
                    ToolCall("read-b", ToolFunction("query_chat_history", '{"view":"recent"}')),
                    ToolCall(
                        "code",
                        ToolFunction(EXECUTE_CODE_NAME, json.dumps({"code": "result = 1"})),
                    ),
                ),
            )
        return "done"

    provider = ScriptedProvider(respond)
    env, harness, _, _ = await _private_scene(database, tmp_path, provider, code_enabled=True)
    result = await harness.processor.handle(_private(env, "并行读", "parallel"), MemorySender())
    works = await _rows(database, work)
    assert result.reason == "chat"
    assert len(works) == 1
    owned = await _rows(database, effects, effects.c.work_id == works[0]["id"])
    receipts = [json.loads(row["receipt_json"]) for row in owned]
    owners = {receipt["invocation"]["owner_execution_id"] for receipt in receipts}
    sequences = {receipt["invocation"]["request_sequence"] for receipt in receipts}
    assert owners == {works[0]["id"]} and len(sequences) == 1
    code = _tool_results(provider.requests[1])["code"]
    assert "accept_work_before_execution" not in json.dumps(code, ensure_ascii=False)
    RESULTS["parallel_batch_and_execute_code"] = {
        "works": len(works),
        "effect_owner_ids": sorted(owners),
        "request_sequences": sorted(sequences),
        "execute_code_result_error": code.get("error") if isinstance(code, dict) else code,
    }


class HardExit(BaseException):
    """Process death after a committed transaction: no Python cleanup writes."""


async def test_crash_after_admission_before_first_tool_resumes_original_response(
    database, tmp_path, monkeypatch
):
    batch = ChatResponse(
        "",
        0,
        tool_calls=(
            tool("write_fixture", {"step": 1}, "w1"),
            tool("read_fixture", {}, "r1"),
            tool("send", {"text": "原批次发送"}, "s1"),
        ),
    )
    probe = await case(database, tmp_path, [batch, "完成"])
    probe.control.current = None
    probe.control.source_key = "private-admission-crash"
    probe.control.source = {**probe.control.source, "trigger_event_id": 1}
    execute = probe.runner._tool_coordinator.execute_batch
    armed = True

    async def crash(*args, **kwargs):
        nonlocal armed
        if armed:
            armed = False
            raise HardExit()
        return await execute(*args, **kwargs)

    monkeypatch.setattr(probe.runner._tool_coordinator, "execute_batch", crash)
    with pytest.raises(HardExit):
        await probe.runner.run((ChatMessage("user", "请修复并报告"),), probe.runtime, probe.backend)
    admitted = [
        row for row in await _rows(database, work) if row["source_key"] == "private-admission-crash"
    ]
    assert len(admitted) == 1
    crashed = admitted[0]
    journals = await _rows(database, journal, journal.c.work_id == crashed["id"])
    assert crashed["model_requests"] == 1
    assert journals and journals[0]["phase"] == "response"
    assert probe.observed == []
    assert await _rows(database, effects, effects.c.work_id == crashed["id"]) == []

    recovered = WorkControl(
        probe.repository,
        probe.control.lease,
        probe.control.source_key,
        probe.control.source,
        probe.control.validate,
        current=await probe.repository.get(crashed["id"]),
    )
    result = await probe.runner.run(
        (ChatMessage("user", "请修复并报告"),),
        replace(probe.runtime, work_control=recovered),
        probe.backend,
    )
    final = await probe.repository.get(crashed["id"])
    owned = await _rows(database, effects, effects.c.work_id == crashed["id"])
    # The runner has no activation owner here; the accepted final decision is
    # committed on the original Work and its supervisor settles it.
    assert result.text == "完成" and recovered.accepted_ending() == "completed"
    assert probe.observed == ["write_fixture", "read_fixture", "send"]
    assert len(probe.provider.requests) == 2
    assert final["model_requests"] == 2
    owners = {json.loads(row["receipt_json"])["invocation"]["owner_execution_id"] for row in owned}
    assert owners == {crashed["id"]}
    async with database.read_session() as reader:
        usage = (
            (await reader.execute(select(budgets).where(budgets.c.root_id == crashed["id"])))
            .mappings()
            .one()
        )
    assert usage["models"] == 2
    RESULTS["crash_after_admission_before_first_tool"] = {
        "work_id": crashed["id"],
        "after_crash": {
            "state": crashed["state"],
            "model_requests": crashed["model_requests"],
            "journal_phase": journals[0]["phase"],
            "effects": 0,
        },
        "after_recovery": {
            "accepted_ending": recovered.accepted_ending(),
            "model_requests": final["model_requests"],
            "budget_models": usage["models"],
            "executed_calls": probe.observed,
            "provider_requests": len(probe.provider.requests),
            "effect_owner_ids": sorted(owners),
        },
    }


# --------------------------------------------------------------------------- C04


async def test_interjection_joins_admitted_execution_without_second_main(database, tmp_path):
    def respond(request):
        number = len(provider.requests)
        if number == 1:
            return _call("update_short_state", {"slot": 1, "text": "base64-read"}, "read")
        if number == 2:
            return _call("update_short_state", {"slot": 1, "text": "mac-write"}, "write")
        if number == 3:
            return _call("send", {"text": "已按补充改用正确路径"}, "deliver")
        return "done"

    provider = ScriptedProvider(respond, pauses=(2,))
    env, harness, _, _ = await _private_scene(database, tmp_path, provider)
    first = asyncio.create_task(
        harness.processor.handle(_private(env, "把报告放到电脑里", "interject-1"), MemorySender())
    )
    await provider.wait(2)
    second = await asyncio.wait_for(
        harness.processor.handle(
            _private(env, "放到 Documents 目录，不要覆盖原文件", "interject-2"), MemorySender()
        ),
        10,
    )
    provider.release[2].set()
    result = await asyncio.wait_for(first, 10)
    works = await _rows(database, work)
    refused = _tool_results(provider.requests[2])["write"]
    assert result.reason == "chat"
    assert second.reason == "work_input_queued", second
    assert len(works) == 1 and works[0]["state"] == "completed"
    assert len(provider.requests) == 4
    assert "放到 Documents 目录" in _plain(provider.requests[2])
    assert refused["error_code"] == "new_input_before_execution"
    assert refused["executed"] is False and refused["mutation_committed"] is False
    assert len(_private_sends(env)) == 1
    RESULTS["interjection_joins_admitted_execution"] = {
        "first_reason": result.reason,
        "second_reason": second.reason,
        "works": len(works),
        "provider_requests": len(provider.requests),
        "interjection_in_request": 3,
        "stale_side_effect_result": refused,
        "private_sends": len(_private_sends(env)),
    }


async def test_input_before_admission_keeps_the_version_fence(database, tmp_path):
    def respond(request):
        number = len(provider.entered)
        if number == 1:
            return _call("update_short_state", {"slot": 1, "text": "stale"}, "stale")
        if number == 2:
            return _call("send", {"text": "按新消息处理"}, "deliver")
        return "done"

    provider = ScriptedProvider(respond, pauses=(1,))
    env, harness, _, _ = await _private_scene(database, tmp_path, provider)
    first = asyncio.create_task(
        harness.processor.handle(_private(env, "先做旧的", "fence-1"), MemorySender())
    )
    await provider.wait(1)
    second = asyncio.create_task(
        harness.processor.handle(_private(env, "改成新的", "fence-2"), MemorySender())
    )
    await asyncio.sleep(0.2)
    provider.release[1].set()
    old, new = await asyncio.wait_for(asyncio.gather(first, second), 10)
    works = await _rows(database, work)
    assert old.reason == "turn_interrupted", old
    assert new.reason == "chat", new
    # The superseded response was never admitted nor executed.
    assert len(provider.entered) == 3 and len(provider.requests) == 2
    assert works == []
    assert "stale" not in str(_tool_results(provider.requests[-1]))
    RESULTS["input_before_admission_keeps_version_fence"] = {
        "first_reason": old.reason,
        "second_reason": new.reason,
        "works": len(works),
        "requests_entered": len(provider.entered),
        "requests_answered": len(provider.requests),
    }


# --------------------------------------------------------------------------- C05/C06


async def _automation(env, chat, settings, database):
    repository = AutomationRepository(database)
    clock = FakeClock(datetime.now(UTC))
    time_service = TimeContextService(default_timezone=settings.default_timezone, clock=clock)
    service = AutomationService(settings=settings, repository=repository, time_service=time_service)
    chat.set_automation_tools(AutomationToolService(service))
    worker = AutomationWorker(
        settings=settings,
        repository=repository,
        time_service=time_service,
        generate=chat.generate_origin_turn,
        runtime_config=chat._runtime_config,
        conversation_scopes=chat._conversation_scopes,
        turns=chat._turn_coordinator,
        concurrency=chat._concurrency,
        work_repository=chat._work_repository,
        bindings=chat.runtime.bindings,
    )

    async def dispatch():
        async for claimed in repository.claim_due(
            worker_id="e2e-owner",
            now=clock.now(),
            lease_seconds=settings.automation_lease_seconds,
            clock=clock.now,
        ):
            await worker._process_guarded(claimed)

    return repository, clock, dispatch


async def _create_reminder(env, harness, key: str) -> None:
    """The private Main creates the reminder itself, as automation 161/162 did."""
    result = await harness.processor.handle(_private(env, "五分钟后提醒我", key), MemorySender())
    assert result.reason == "chat", result


def _automation_create(identity: str) -> ChatResponse:
    return _call(
        "automation_manage",
        {
            "action": "create",
            "task": {
                "name": "提醒",
                "goal": "提醒本人检查报告",
                "trigger": {"type": "after", "seconds": 1},
            },
        },
        identity,
    )


async def test_automation_sees_previous_private_execution_and_user_message_queues(
    database, tmp_path
):
    phase = {"name": "private"}

    def respond(request):
        number = len(provider.requests)
        control = current_work_control.get()
        if control is not None and control.source.get("origin") == "scheduled_automation":
            if phase.setdefault("automation_first", number) == number:
                return _call("send", {"text": "提醒：检查报告"}, "remind-send")
            return "automation done"
        if phase["name"] == "private":
            if number == 1:
                return _call("update_short_state", {"slot": 1, "text": "copied"}, "copy")
            if number == 2:
                return _automation_create("remind")
            if number == 3:
                return _call("send", {"text": "已复制，也设好提醒了"}, "deliver")
            return "done"
        if phase.setdefault("user_first", number) == number:
            return _call("send", {"text": "收到，提醒已发"}, "reply")
        return "ok"

    provider = ScriptedProvider(respond)
    env, harness, chat, settings = await _private_scene(database, tmp_path, provider)
    _, clock, dispatch = await _automation(env, chat, settings, database)
    await _create_reminder(env, harness, "private-before-reminder")
    previous = (await _rows(database, work))[0]
    assert previous["state"] == "completed"
    private_requests = len(provider.requests)

    phase["name"] = "user"
    pause_at = private_requests + 1
    provider.arrived[pause_at] = asyncio.Event()
    provider.release[pause_at] = asyncio.Event()
    clock.advance(2)
    automation = asyncio.create_task(dispatch())
    await provider.wait(pause_at)
    automation_first = provider.entered[pause_at - 1]
    user = asyncio.create_task(
        harness.processor.handle(_private(env, "现在进度如何", "during-automation"), MemorySender())
    )
    await asyncio.sleep(0.3)
    assert not user.done(), user.result() if user.done() else None
    provider.release[pause_at].set()
    await asyncio.wait_for(automation, 10)
    user_result = await asyncio.wait_for(user, 10)
    async with database.read_session() as reader:
        runs = list(await reader.scalars(select(AutomationRunModel)))
    automation_recent = _runtime_context(automation_first).get("recent_work", {})
    assert runs and runs[0].status == "succeeded", runs[0].__dict__ if runs else None
    assert runs[0].llm_calls == 2
    assert user_result.reason == "chat", user_result
    assert automation_recent["work_id"] == previous["id"]
    assert {step["tool"] for step in automation_recent["recent_steps"]} >= {
        "update_short_state",
        "automation_manage",
        "send",
    }
    # The queued Main starts only after the occurrence's two requests.
    assert phase["user_first"] > pause_at + 1
    RESULTS["automation_sees_previous_execution_and_user_queues"] = {
        "previous_work_id": previous["id"],
        "automation_first_request_recent_work": automation_recent,
        "automation_run": {
            "status": runs[0].status,
            "llm_calls": runs[0].llm_calls,
            "error_category": runs[0].error_category,
        },
        "user_message_reason": user_result.reason,
        "user_message_first_request_number": phase["user_first"],
        "automation_pause_request_index": pause_at,
    }


async def test_automation_exception_records_actual_llm_calls(database, tmp_path):
    phase = {"name": "private"}

    def respond(request):
        number = len(provider.requests)
        if phase["name"] == "private":
            if number == 1:
                return _automation_create("remind")
            if number == 2:
                return _call("send", {"text": "好的"}, "deliver")
            return "done"
        if number == phase["start"] + 1:
            return _call("update_short_state", {"slot": 1, "text": "checked"}, "check")
        raise LLMUnavailableError("synthetic provider outage after a paid request")

    provider = ScriptedProvider(respond)
    env, harness, chat, settings = await _private_scene(database, tmp_path, provider)
    _, clock, dispatch = await _automation(env, chat, settings, database)
    await _create_reminder(env, harness, "private-reminder-failure")
    phase.update(name="automation", start=len(provider.requests))
    clock.advance(2)
    await dispatch()
    async with database.read_session() as reader:
        run = (await reader.scalars(select(AutomationRunModel))).one()
    attempted = len(provider.requests) - phase["start"]
    assert run.status == "uncertain"
    assert run.llm_calls == attempted == 2
    RESULTS["automation_exception_llm_calls"] = {
        "status": run.status,
        "llm_calls": run.llm_calls,
        "provider_requests_during_run": attempted,
        "error_category": run.error_category,
    }


async def test_next_private_turn_reads_previous_execution_facts(database, tmp_path):
    phase = {"name": "first"}

    def respond(request):
        number = len(provider.requests)
        if phase["name"] == "first":
            if number == 1:
                return _call("update_short_state", {"slot": 1, "text": "copied"}, "copy")
            if number == 2:
                return _call("send", {"text": "好了"}, "deliver")
            return "done"
        if number == phase["start"] + 1:
            phase["context"] = _runtime_context(request)
            work_id = phase["context"]["recent_work"]["work_id"]
            return _call("task_control", {"action": "get", "work_id": work_id}, "lookup")
        if number == phase["start"] + 2:
            return _call("send", {"text": "刚才写入了一次状态并回复了你"}, "answer")
        return "done"

    provider = ScriptedProvider(respond)
    env, harness, _, _ = await _private_scene(database, tmp_path, provider)
    await harness.processor.handle(_private(env, "复制一下", "facts-first"), MemorySender())
    previous = (await _rows(database, work))[0]
    phase.update(name="second", start=len(provider.requests))
    result = await harness.processor.handle(
        _private(env, "刚才做了什么", "facts-second"), MemorySender()
    )
    lookup = _tool_results(provider.requests[phase["start"] + 1])["lookup"]
    tools = [step.get("tool") for step in lookup["work"]["steps"]]
    assert result.reason == "chat"
    recent = phase["context"]["recent_work"]
    assert recent["work_id"] == previous["id"]
    assert {step["tool"] for step in recent["recent_steps"]} >= {"update_short_state", "send"}
    assert lookup["ok"] is True and lookup["work"]["work_id"] == previous["id"]
    assert "update_short_state" in tools and "send" in tools
    RESULTS["next_private_turn_reads_previous_facts"] = {
        "previous_work_id": previous["id"],
        "recent_work_in_first_request": recent,
        "task_control_get_tools": tools,
    }


async def test_plugin_explicit_limit_is_written_to_root_work_budget(database, tmp_path):
    from tests.integration.test_plugin_agent_domain_grants import source_host

    from yuki.runtime.work_repository import WorkRepository

    provider = FakeLLMProvider(lambda _: _call("environment_status", {}, "status"))
    _, _, chat, source, host = await source_host(database, tmp_path, provider, ())
    try:
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        chat._tools.sandbox_client = SimpleNamespace(
            execute=AsyncMock(return_value={"ready": True})
        )
        with host.invocation_scope(host.plugin_id, source):
            result = await host.agent.run("Inspect", max_model_requests=2, max_tool_calls=5)
        identity = result.data["work_id"]
        row = await WorkRepository(database).get(identity)
        async with database.read_session() as reader:
            budget = (
                (await reader.execute(select(budgets).where(budgets.c.root_id == identity)))
                .mappings()
                .one()
            )
        assert (budget["model_limit"], budget["tool_limit"]) == (2, 5)
        assert row["model_requests"] == 2 and row["state"] == "suspended"
        RESULTS["plugin_explicit_limit_root_budget"] = {
            "work_state": row["state"],
            "model_requests": row["model_requests"],
            "budget": {
                key: budget[key] for key in ("models", "tools", "model_limit", "tool_limit")
            },
        }
    finally:
        await host.close_host_resources()
