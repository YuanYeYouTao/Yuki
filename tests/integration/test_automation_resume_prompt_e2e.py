"""Automation Work resume trigger must only describe what actually exists (#730).

Real SQLite database (migrated to head), real ``AutomationWorker``, real
``WorkScheduler`` + ``WorkResumer._resume_automation``, real ``MainAgentTurnService``
/ ``TurnExecution`` / ``WorkSession`` / ``WorkJournal`` and real ``WorkWaitRepository``;
only the model provider and OneBot gateway are faked so every submitted request is
captured verbatim. Reproducible, deterministic facts go to
``test-results/issue-730-automation-resume-e2e.json``.

Failure and regression cases, written before the fix:

F1  Chat Completions exact recovery (no native continuation, #725):
    The first due activation carries ``<runtime_context>`` and the schedule goal.
    After ``manage_task(start/update)`` + ``wait`` and wake-up via
    ``WorkResumer._resume_automation``, the resumed request appends to the paid chain
    without re-preparing ``<runtime_context>``; no message in the resumed request may
    point at "本轮运行资料", and the appended trigger text must only state what exists.
F2  Responses native continuation exact recovery (production protocol):
    Same wait-and-resume flow with a ``ProviderContinuation``. The resumed request
    keeps the continuation, adds no ``<runtime_context>`` in the resume delta, and
    never mentions "本轮运行资料".
F3  SELF-owned scheduled automation exact recovery:
    A ``principal="self"`` scheduled Work resumed through ``_resume_automation``
    follows the same rule: appends to the paid chain, no second ``<runtime_context>``,
    and no reference to "本轮运行资料".
F4  First due activation regression guard:
    A fresh due occurrence still prepares ``<runtime_context>`` (``origin``,
    ``automation_id``, ``run_id``, ``created_at``, ``scheduled_for``, ``current_time``)
    ahead of the schedule goal.
F5  New-chain resume when recovery contract changes:
    When the stored journal contract no longer matches and a fresh chain is started
    (``uses_recovery_transcript=False``), ``_finalize_initial_layout`` prepares a
    fresh ``<runtime_context>`` with the current Work goal and the trigger text still
    avoids the stale "见本轮运行资料" phrasing while finishing cleanly.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import select, update
from tests.integration.test_automation_activation_context import runtime_blocks
from tests.integration.test_automation_lifecycle import calls, resumer
from tests.support.automation_unified_delivery_helpers import sent, setup_run

from yuki.automation.models import RunStatus
from yuki.domain.messages import ChatMessage, FunctionCallOutput, ProviderContinuation
from yuki.runtime.work_scheduler import WorkScheduler
from yuki.runtime.work_schema_v1 import journal, work
from yuki.runtime.work_wait import WorkWaitRepository

ARTIFACT = (
    Path(__file__).resolve().parents[2] / "test-results" / "issue-730-automation-resume-e2e.json"
)
RESULTS: dict[str, object] = {}
FORBIDDEN_PHRASE = "本轮运行资料"


@pytest.fixture(scope="module", autouse=True)
def _write_artifact():
    RESULTS.clear()
    yield
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(
        json.dumps(RESULTS, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


async def _run_due_then_wait_and_resume(
    database,
    tmp_path,
    *,
    principal: str = "person",
    continuation: ProviderContinuation | None = None,
    mutate_contract_before_resume: bool = False,
):
    goal = "原来安排的完整目标"
    current_goal = "已经核实原资料；继续更新后的工作目标"
    case = await setup_run(database, tmp_path, principal=principal, mode="silent", goal=goal)

    def first_turn(_request):
        response = calls(
            ("accept", "manage_task", {"action": "start", "goal": "最初接纳的目标"}),
            ("update", "manage_task", {"action": "update", "goal": current_goal}),
            ("wait", "wait", {"for": [{"seconds": 0}]}),
        )
        if continuation is not None:
            return replace(response, continuation=continuation)
        return response

    case.provider._responder = first_turn
    case.clock.advance(2)
    original_run = await case.dispatch()
    async with database.read_session() as reader:
        original_work = dict((await reader.execute(select(work))).mappings().one())

    if mutate_contract_before_resume:
        async with database.immediate_session() as writer:
            await writer.execute(
                update(journal)
                .where(journal.c.work_id == original_work["id"])
                .values(contract="outdated-contract-for-730-e2e")
            )

    case.provider._responder = lambda _: calls(
        ("complete", "manage_task", {"action": "finish", "result": "完成"})
    )
    waits = WorkWaitRepository(case.works)
    binding = await waits.describe(original_work["id"])
    assert await waits.deliver_due(now=binding["registered_at"] + 1) == 1

    scheduler = WorkScheduler(case.works, resumer(case).resume, chat_admission_enabled=True)
    scheduler._last_reclaim = time.monotonic()
    await scheduler.drain_once()

    finished_work = await case.works.get(original_work["id"])
    finished_run = (await case.repository.run_history(case.row.id))[0]
    return case, original_run, original_work, finished_work, finished_run, current_goal


def _delta_messages(first_request, resumed_request) -> list[ChatMessage]:
    if resumed_request.continuation is not None:
        return [
            item for item in resumed_request.continuation_items if isinstance(item, ChatMessage)
        ]
    prefix_len = (
        len(first_request.messages)
        if resumed_request.messages[: len(first_request.messages)] == first_request.messages
        else 0
    )
    return list(resumed_request.messages[prefix_len:])


def _all_request_messages(request) -> list[ChatMessage]:
    return [
        *request.messages,
        *(item for item in request.continuation_items if isinstance(item, ChatMessage)),
    ]


def _delta_user_contents(first_request, resumed_request) -> list[str]:
    return [
        message.content or ""
        for message in _delta_messages(first_request, resumed_request)
        if message.role == "user"
    ]


def _delta_has_runtime_context(first_request, resumed_request) -> bool:
    return any(
        "<runtime_context>\n" in (message.content or "")
        for message in _delta_messages(first_request, resumed_request)
    )


def _record_resume_scenario(
    name: str,
    *,
    principal: str,
    first_request,
    resumed_request,
    finished_work: dict[str, object],
    finished_run,
) -> dict[str, object]:
    prefix_preserved = (
        resumed_request.messages[: len(first_request.messages)] == first_request.messages
    )
    delta_users = _delta_user_contents(first_request, resumed_request)
    # Exclude volatile timestamps/IDs from wait-signal JSON while preserving exact
    # non-JSON trigger texts for deterministic artifact comparison.
    stable_delta_users = [
        text if not text.lstrip().startswith("[") else "<structured-signal>" for text in delta_users
    ]
    all_messages = _all_request_messages(resumed_request)
    mentions_absent = any(
        FORBIDDEN_PHRASE in (message.content or "")
        for message in all_messages
        if message.role == "user"
    )
    entry = {
        "principal": principal,
        "continuation_protocol": (
            resumed_request.continuation.protocol if resumed_request.continuation else None
        ),
        "turn1_runtime_context_count": len(runtime_blocks(first_request)),
        "resume_runtime_context_count": len(runtime_blocks(resumed_request))
        + sum(
            1
            for item in resumed_request.continuation_items
            if isinstance(item, ChatMessage) and "<runtime_context>\n" in (item.content or "")
        ),
        "new_runtime_context_in_resume_delta": _delta_has_runtime_context(
            first_request, resumed_request
        ),
        "chain_prefix_preserved": prefix_preserved,
        "resume_delta_user_texts": stable_delta_users,
        "mentions_absent_runtime_material": mentions_absent,
        "final_run_status": finished_run.status.value,
        "model_requests": finished_work["model_requests"],
    }
    RESULTS[name] = entry
    return entry


async def test_f1_chat_completions_automation_resume_omits_absent_runtime_material(
    database, tmp_path
):
    (
        case,
        original_run,
        _orig_work,
        finished_work,
        finished_run,
        current_goal,
    ) = await _run_due_then_wait_and_resume(database, tmp_path, principal="person")
    first_request, resumed_request = case.provider.requests[0], case.provider.requests[-1]
    entry = _record_resume_scenario(
        "F1_chat_completions_exact_recovery",
        principal="person",
        first_request=first_request,
        resumed_request=resumed_request,
        finished_work=finished_work,
        finished_run=finished_run,
    )

    assert entry["chain_prefix_preserved"] is True
    assert entry["turn1_runtime_context_count"] == 1
    assert entry["resume_runtime_context_count"] == 1
    assert entry["new_runtime_context_in_resume_delta"] is False
    assert entry["mentions_absent_runtime_material"] is False
    assert any(text.endswith("继续原 Work。") for text in entry["resume_delta_user_texts"])
    goals = [
        json.loads(call.function.arguments).get("goal")
        for message in resumed_request.messages
        for call in message.tool_calls
    ]
    assert current_goal in goals
    assert finished_work["goal"] == current_goal
    assert finished_run.id == original_run.id
    assert finished_run.status is RunStatus.SUCCEEDED
    assert finished_work["model_requests"] == finished_run.llm_calls == 2
    assert not sent(case.env)


async def test_f2_responses_continuation_automation_resume_omits_absent_runtime_material(
    database, tmp_path
):
    continuation = ProviderContinuation(
        provider="openai",
        protocol="openai_responses",
        payload={"previous_response_id": "resp_due_1"},
    )
    (
        case,
        original_run,
        _orig_work,
        finished_work,
        finished_run,
        current_goal,
    ) = await _run_due_then_wait_and_resume(
        database, tmp_path, principal="person", continuation=continuation
    )
    first_request, resumed_request = case.provider.requests[0], case.provider.requests[-1]
    entry = _record_resume_scenario(
        "F2_responses_continuation_exact_recovery",
        principal="person",
        first_request=first_request,
        resumed_request=resumed_request,
        finished_work=finished_work,
        finished_run=finished_run,
    )

    assert resumed_request.continuation == continuation
    assert entry["chain_prefix_preserved"] is True
    assert entry["turn1_runtime_context_count"] == 1
    assert entry["resume_runtime_context_count"] == 1
    assert entry["new_runtime_context_in_resume_delta"] is False
    assert entry["mentions_absent_runtime_material"] is False
    assert any(text.endswith("继续原 Work。") for text in entry["resume_delta_user_texts"])
    paired_call_ids = {
        item.call_id
        for item in resumed_request.continuation_items
        if isinstance(item, FunctionCallOutput)
    }
    assert paired_call_ids == {"accept", "update", "wait"}
    assert finished_work["goal"] == current_goal
    assert finished_run.id == original_run.id
    assert finished_run.status is RunStatus.SUCCEEDED
    assert finished_work["model_requests"] == finished_run.llm_calls == 2


async def test_f3_self_scheduled_automation_resume_omits_absent_runtime_material(
    database, tmp_path
):
    (
        case,
        original_run,
        _orig_work,
        finished_work,
        finished_run,
        _current_goal,
    ) = await _run_due_then_wait_and_resume(database, tmp_path, principal="self")
    first_request, resumed_request = case.provider.requests[0], case.provider.requests[-1]
    entry = _record_resume_scenario(
        "F3_self_automation_exact_recovery",
        principal="self",
        first_request=first_request,
        resumed_request=resumed_request,
        finished_work=finished_work,
        finished_run=finished_run,
    )

    assert entry["chain_prefix_preserved"] is True
    assert entry["turn1_runtime_context_count"] == 1
    assert entry["resume_runtime_context_count"] == 1
    assert entry["new_runtime_context_in_resume_delta"] is False
    assert entry["mentions_absent_runtime_material"] is False
    assert any(text.endswith("继续原 Work。") for text in entry["resume_delta_user_texts"])
    assert finished_run.id == original_run.id
    assert finished_run.status is RunStatus.SUCCEEDED
    assert finished_work["model_requests"] == finished_run.llm_calls == 2


async def test_f4_first_due_activation_still_carries_runtime_context_and_goal(database, tmp_path):
    goal = "到期后检查并记录完成"
    case = await setup_run(database, tmp_path, principal="person", mode="silent", goal=goal)
    case.provider._responder = lambda _: "内部完成"
    case.clock.advance(2)
    result = await case.dispatch()

    first_request = case.provider.requests[0]
    blocks = runtime_blocks(first_request)
    assert len(blocks) == 1
    block = blocks[0]
    assert block["origin"] == "scheduled_automation"
    assert (block["automation_id"], block["run_id"]) == (case.row.id, result.id)
    assert block["created_at"] is not None and block["scheduled_for"] is not None
    assert block["current_time"]
    assert first_request.messages[-1].content.endswith(goal)
    assert result.status is RunStatus.SUCCEEDED

    RESULTS["F4_first_due_activation"] = {
        "runtime_context_count": len(blocks),
        "origin": block["origin"],
        "has_created_at": block["created_at"] is not None,
        "has_scheduled_for": block["scheduled_for"] is not None,
        "has_current_time": bool(block["current_time"]),
        "ends_with_goal": first_request.messages[-1].content.endswith(goal),
        "final_run_status": result.status.value,
    }


async def test_f5_contract_changed_new_chain_resume_prepares_fresh_runtime_context(
    database, tmp_path
):
    (
        case,
        original_run,
        _orig_work,
        finished_work,
        finished_run,
        current_goal,
    ) = await _run_due_then_wait_and_resume(
        database,
        tmp_path,
        principal="person",
        mutate_contract_before_resume=True,
    )
    first_request, resumed_request = case.provider.requests[0], case.provider.requests[-1]
    resumed_blocks = runtime_blocks(resumed_request)
    assert len(resumed_blocks) == 1
    block = resumed_blocks[0]
    assert block["origin"] == "scheduled_automation"
    assert block["goal"] == current_goal

    user_texts = [
        message.content or "" for message in resumed_request.messages if message.role == "user"
    ]
    entry = {
        "chain_prefix_preserved": (
            resumed_request.messages[: len(first_request.messages)] == first_request.messages
        ),
        "resume_runtime_context_count": len(resumed_blocks),
        "fresh_runtime_context_has_current_goal": block["goal"] == current_goal,
        "mentions_absent_runtime_material": any(FORBIDDEN_PHRASE in text for text in user_texts),
        "trigger_present": any(text.endswith("继续原 Work。") for text in user_texts),
        "final_run_status": finished_run.status.value,
        "model_requests": finished_work["model_requests"],
    }
    RESULTS["F5_contract_changed_new_chain"] = entry

    assert entry["mentions_absent_runtime_material"] is False
    assert entry["trigger_present"] is True
    assert entry["chain_prefix_preserved"] is False
    assert finished_run.id == original_run.id
    assert finished_run.status is RunStatus.SUCCEEDED
    assert finished_work["model_requests"] == finished_run.llm_calls == 2
