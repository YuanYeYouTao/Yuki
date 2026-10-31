"""Real Main delivery survives automatic compact and its following request.

Failure cases are specified before runtime changes: a public prefix just below
the soft trigger becomes eligible after a real successful send; the following
normal Main must receive its original call and receipt, and covered public text
must leave that request. A background Work must not replace the ordinary goal.
Known and unknown hard capacity retain the same execution facts. The model and
QQ transport are scripted boundaries; Main, Social, SQLite and projection are real.
"""

import json
import re
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from tests.conftest import MemorySender, build_harness, make_settings
from tests.support.commands_and_chat_helpers import inbound
from tests.support.compact_delivery_evidence import capture_compact_facts
from tests.support.compact_delivery_evidence import compact_evidence as evidence_capture
from tests.support.correctness_wire import wire
from tests.support.event_seed import seed_scoped_event
from tests.support.fixed_contract_fixture import bind_main_contract
from tests.support.social_identity_cases import social_env

from yuki.domain.conversations import ConversationScope
from yuki.domain.messages import ChatResponse, ToolCall, ToolFunction
from yuki.identity.db_models import CanonicalPersonModel
from yuki.llm.fake import FakeLLMProvider
from yuki.model_runtime.capacity import ModelCapacity, estimate_request_tokens
from yuki.runtime.work_activation import current_work_control
from yuki.runtime.work_journal import decode_transcript
from yuki.runtime.work_repository import WorkCapacityError
from yuki.runtime.work_schema_v1 import journal, work
from yuki.services.context_compaction import (
    EXECUTION_COMPACT_INSTRUCTION,
    PUBLIC_COMPACT_INSTRUCTION,
)


@pytest.fixture
def compact_evidence(request):
    """Keep actual requests and durable facts even when a red assertion fails."""
    with evidence_capture(request) as captured:
        yield captured


def _kind(request):
    content = request.messages[-1].content or ""
    if content.startswith(PUBLIC_COMPACT_INSTRUCTION):
        return "public_compact"
    if content.startswith(EXECUTION_COMPACT_INSTRUCTION):
        return "execution_compact"
    return "normal"


def _tool(name, arguments, call_id):
    return ChatResponse(
        "", 0, tool_calls=(ToolCall(call_id, ToolFunction(name, json.dumps(arguments))),)
    )


def _record_responses(respond, evidence):
    def complete(request):
        response = respond(request)
        evidence.setdefault("model_responses", []).append(asdict(response))
        return response

    return complete


@pytest.mark.parametrize(
    ("hard_capacity", "next_step"),
    [
        ("unknown", "finish"),
        ("known", "finish"),
        ("unknown", "batch"),
        ("unknown", "read_then_send"),
        ("unknown", "p_refused"),
    ],
)
async def test_real_ordinary_send_keeps_receipt_and_replaces_public_prefix(
    database, tmp_path, monkeypatch, compact_evidence, hard_capacity, next_step
):
    directory, evidence = compact_evidence
    question = "CURRENT_ORDINARY_GOAL: explain fanout once and finish."
    old_public = ("PUBLIC_OLD_CAN_DROP " + "historical decision. " * 9500).rstrip()
    answer = "REAL_DELIVERED_FANOUT " + "independent branch; " * 480
    call_id = "compact-real-send"
    normal_after_send = []
    issued = False
    requested_read = False
    requested_second = False
    prior_write_requested = False
    private_fact = "PRIVATE_TOOL_RESULT_MUST_NOT_ENTER_PUBLIC_CHECKPOINT"
    expected_calls = {call_id}
    if next_step == "batch":
        expected_calls.update({"original-private-write", "original-failed-read"})

    def respond(request):
        nonlocal issued, requested_read, requested_second, prior_write_requested
        kind = _kind(request)
        evidence["requests"].append(
            {
                "kind": kind,
                "estimated_tokens": estimate_request_tokens(request),
                "request": asdict(request),
            }
        )
        if kind == "public_compact":
            return ChatResponse("PUBLIC_CHECKPOINT: preserve the earlier decision.", 0)
        if kind == "execution_compact":
            return ChatResponse(
                "PRIVATE_CHECKPOINT: fanout was answered successfully; no remaining action.", 0
            )
        if not issued:
            if next_step == "p_refused" and not prior_write_requested:
                prior_write_requested = True
                return _tool(
                    "update_short_state", {"slot": 1, "text": private_fact}, "prior-observed-write"
                )
            issued = True
            if next_step == "batch":
                return ChatResponse(
                    "",
                    0,
                    tool_calls=(
                        ToolCall(call_id, ToolFunction("send", json.dumps({"text": answer}))),
                        ToolCall(
                            "original-private-write",
                            ToolFunction(
                                "update_short_state", json.dumps({"slot": 1, "text": private_fact})
                            ),
                        ),
                        ToolCall(
                            "original-failed-read",
                            ToolFunction("query_chat_history", '{"view":"unsupported-view"}'),
                        ),
                    ),
                )
            return _tool("send", {"text": answer}, call_id)
        normal_after_send.append(request)
        if next_step == "read_then_send" and not requested_read:
            requested_read = True
            return _tool(
                "query_chat_history", {"view": "recent", "limit": 1}, "actual-followup-read"
            )
        if next_step == "read_then_send" and not requested_second:
            requested_second = True
            return _tool(
                "send",
                {"text": "SECOND_LEGITIMATE_DELIVERY: followup complete."},
                "actual-second-send",
            )
        return ChatResponse("NO_REPLY", 0)

    env = await social_env(database, tmp_path)
    provider = FakeLLMProvider(_record_responses(respond, evidence))
    settings = make_settings(
        database.url,
        runtime_work_enabled=False,
        enabled_groups_csv="20001",
        context_compact_trigger_tokens=96000,
    )
    harness = build_harness(database, settings, provider)
    bind_main_contract(harness, tmp_path)
    chat = harness.processor._chat
    chat._tools.social_service = env.service
    env.service.runtime_config = chat._runtime_config
    client, payloads = wire(SimpleNamespace(provider=provider, runner=chat.runtime.runner), "chat")
    evidence["wire_requests"] = payloads
    if next_step == "p_refused":
        import yuki.services.context_compaction as compaction

        def refuse_public(*args, **kwargs):
            evidence.setdefault("refused_public_plans", []).append(
                {"fragments": len(args[0].fragments), "request": asdict(args[2])}
            )
            raise WorkCapacityError("model_request_capacity")

        monkeypatch.setattr(compaction, "public_compact_plan", refuse_public)
    if hard_capacity == "known":
        monkeypatch.setattr(
            chat.runtime.runner._models,
            "capacity",
            lambda _: ModelCapacity(input_tokens=180000, context_tokens=200000),
        )
    await seed_scoped_event(
        harness.scoped_events,
        scope=ConversationScope.group("80001", "20001"),
        origin_key="compact-old-public",
        sender_user_id="10001",
        direction="inbound",
        content=old_public,
    )
    message = replace(
        inbound(
            question,
            message_id="compact-real-question",
            user_id="10001",
            group_id="20001",
            mentions_bot=True,
        ),
        bot_user_id="80001",
        conversation_id=env.context.conversation_id,
        person_id=env.person,
        space_id=env.space,
        presence_id=env.presence,
    )
    evidence.update(
        {
            "question": question,
            "old_public": old_public,
            "answer": answer,
            "call_id": call_id,
            "hard_capacity": hard_capacity,
            "trigger": 96000,
            "next_step": next_step,
        }
    )
    captured_facts = False
    try:
        result = await harness.processor.handle(message, MemorySender())
        evidence["result"] = asdict(result)
        evidence["gateway_calls"] = env.bot.calls
        await capture_compact_facts(database, directory, evidence)
        captured_facts = True
        assert result.reason == "chat", result
        normal = [item for item in evidence["requests"] if item["kind"] == "normal"]
        compact = [item for item in evidence["requests"] if item["kind"] != "normal"]
        assert evidence["requests"][0]["kind"] == "normal", (
            "The prefix must not compact before send"
        )
        assert normal[0]["estimated_tokens"] < settings.context_compact_trigger_tokens
        assert old_public in "\n".join(item.content or "" for item in provider.requests[0].messages)
        assert compact, "The successful send must cross the shared trigger in this fixture"
        originals = [
            decode_transcript(item["payload"]["original"]).request()
            for item in evidence["compact_receipts"]
            if item["payload"]["phase"] == "dispatched"
        ]
        assert originals
        original = originals[0]
        original_tokens = estimate_request_tokens(
            replace(
                provider.requests[0],
                messages=original.messages,
                continuation=original.continuation,
                continuation_items=original.items,
            )
        )
        evidence["precompact_original_tokens"] = original_tokens
        assert original_tokens >= settings.context_compact_trigger_tokens
        assert normal_after_send, "Capture the real normal Main following compact"
        following = normal_after_send[0]
        receipts = [item for item in following.messages if item.tool_call_id == call_id]
        assert receipts, "Normal Main lost the original successful send receipt during compact"
        receipt = json.loads(receipts[0].content)
        assert receipt["ok"] is True, receipt
        assert receipt["data"]["status"] == "succeeded", receipt
        assert receipt["data"]["operation_id"]
        event_by_id = {row["id"]: row for row in evidence["events"]}
        delivered = receipt["data"]["parts"]
        assert delivered and receipt["data"]["sent_messages"] == len(delivered)
        assert all(part["status"] == "succeeded" and part["operation_id"] for part in delivered)
        for part in delivered:
            assert event_by_id[part["event_id"]]["content"] == part["delivered_text"]
            assert event_by_id[part["event_id"]]["direction"] == "outbound"
        received = {
            item.tool_call_id: json.loads(item.content)
            for item in following.messages
            if item.tool_call_id in expected_calls
        }
        assert set(received) == expected_calls, "Every original tool outcome must reach normal Main"
        call_ids = {call.id for item in following.messages for call in item.tool_calls}
        assert expected_calls <= call_ids
        if next_step == "batch":
            assert received["original-private-write"]["ok"] is True
            assert received["original-failed-read"]["ok"] is False
        assert question in str(following.messages)
        if next_step == "p_refused":
            assert evidence["refused_public_plans"]
            assert any(item["kind"] == "execution_compact" for item in compact)
            assert old_public in str(following.messages), "E does not cover the public prefix"
            assert "PRIVATE_CHECKPOINT" in str(following.messages)
        else:
            assert old_public not in str(following.messages), (
                "Covered public text stayed in the next normal Main"
            )
            assert "PUBLIC_CHECKPOINT" in str(following.messages)
        for item in compact:
            if item["kind"] == "public_compact":
                text = json.dumps(item["request"]["messages"])
                assert answer not in text and private_fact not in text
                assert not any(message["role"] == "tool" for message in item["request"]["messages"])
        if next_step == "read_then_send":
            assert requested_read and requested_second
            final_ids = {item.tool_call_id for item in normal_after_send[-1].messages}
            assert {"actual-followup-read", "actual-second-send"} <= final_ids
            assert any("SECOND_LEGITIMATE_DELIVERY" in str(params) for _, params in env.bot.calls)
    finally:
        if not captured_facts:
            await capture_compact_facts(database, directory, evidence)
        await client.aclose()


async def test_ordinary_compact_keeps_its_goal_beside_real_waiting_work(
    database, tmp_path, compact_evidence
):
    """A real accepted background Work is visible, but owns no ordinary input."""
    directory, evidence = compact_evidence
    env = await social_env(database, tmp_path)
    background_goal = "BACKGROUND_ONLY: monitor astronomy updates after the timer."
    question = "CURRENT_ORDINARY_GOAL: explain fanout and then stop."
    answer = "ORDINARY_ANSWER " + "independent branch; " * 480
    background_phase = True
    sent = False
    read_requested = False
    background_id = None
    execution_requests = []
    after_execution = []
    ordinary_ownership = []

    def respond(request):
        nonlocal sent, read_requested
        kind = _kind(request)
        control = current_work_control.get()
        active = control.current if control is not None else None
        evidence["requests"].append(
            {
                "kind": kind,
                "phase": "background" if background_phase else "ordinary",
                "request": asdict(request),
                "estimated_tokens": estimate_request_tokens(request),
                "current_work_id": active["id"] if active is not None else None,
            }
        )
        if background_phase:
            if active is None:
                return _tool(
                    "task_control",
                    {"action": "accept", "goal": background_goal},
                    "accept-background",
                )
            return _tool(
                "task_control",
                {
                    "action": "wait",
                    "conditions": [{"kind": "time_due", "after_seconds": 3600}],
                },
                "wait-background",
            )
        ordinary_ownership.append(active["id"] if active is not None else None)
        if kind == "public_compact":
            return ChatResponse("PUBLIC_CHECKPOINT: earlier astronomy work is separate.", 0)
        if kind == "execution_compact":
            execution_requests.append(request)
            return ChatResponse(
                f"WRONG_SUMMARY_GOAL: resume background Work {background_id}: {background_goal} "
                "The ordinary fanout answer was already sent.",
                0,
            )
        states = [
            json.loads(
                item.content.split("<runtime_context>\n", 1)[1].split("\n</runtime_context>", 1)[0]
            )
            for item in request.messages
            if item.role == "user" and (item.content or "").lstrip().startswith("<runtime_context>")
        ]
        assert states and states[-1]["state"] == "no_active_work"
        assert states[-1]["state_scope"] == "current_activation"
        assert "work_id" not in states[-1]
        headers = [
            match.group(1)
            for item in request.messages
            if item.role == "user"
            for match in re.finditer(r"#(\d+)>" + re.escape(question), item.content or "")
        ]
        assert headers, "Normal Main must retain the actual activation source and its question"
        evidence.setdefault("normal_sources", []).append([int(identity) for identity in headers])
        if not sent:
            sent = True
            return _tool("send", {"text": answer}, "ordinary-original-send")
        if execution_requests:
            after_execution.append(request)
        if not read_requested:
            original_receipt = next(
                item for item in request.messages if item.tool_call_id == "ordinary-original-send"
            )
            original_receipt = json.loads(original_receipt.content)
            evidence["observed_original_send"] = original_receipt
            assert original_receipt["ok"] and original_receipt["data"]["status"] == "succeeded"
            read_requested = True
            return _tool(
                "query_chat_history", {"view": "recent", "limit": 1}, "ordinary-followup-read"
            )
        latest_read = next(
            item for item in request.messages if item.tool_call_id == "ordinary-followup-read"
        )
        evidence["observed_latest_read"] = json.loads(latest_read.content)
        assert evidence["observed_latest_read"]["ok"] is True
        return ChatResponse("NO_REPLY", 0)

    provider = FakeLLMProvider(_record_responses(respond, evidence))
    settings = make_settings(
        database.url,
        runtime_work_enabled=True,
        enabled_groups_csv="20001",
        context_compact_trigger_tokens=30000,
    )
    harness = build_harness(database, settings, provider)
    bind_main_contract(harness, tmp_path)
    chat = harness.processor._chat
    chat._tools.social_service = env.service
    env.service.runtime_config = chat._runtime_config
    client, payloads = wire(SimpleNamespace(provider=provider, runner=chat.runtime.runner), "chat")
    evidence["wire_requests"] = payloads

    def message(text, identity):
        return replace(
            inbound(
                text, message_id=identity, user_id="10001", group_id="20001", mentions_bot=True
            ),
            bot_user_id="80001",
            conversation_id=env.context.conversation_id,
            person_id=env.person,
            space_id=env.space,
            presence_id=env.presence,
        )

    try:
        background_result = await harness.processor.handle(
            message(background_goal, "actual-background-goal"), MemorySender()
        )
        evidence["background_result"] = asdict(background_result)
        await capture_compact_facts(database, directory, evidence)
        background = next(row for row in evidence["works"] if row["goal"] == background_goal)
        background_id = background["id"]
        assert background["state"] == "waiting_external", background
        before = {
            key: background[key]
            for key in (
                "id",
                "goal",
                "source_json",
                "model_requests",
                "tool_calls",
                "sent_messages",
            )
        }
        evidence["background_before"] = before
        background_phase = False
        result = await harness.processor.handle(
            message(question, "ordinary-beside-background"), MemorySender()
        )
        evidence["ordinary_result"] = asdict(result)
        evidence["gateway_calls"] = env.bot.calls
        await capture_compact_facts(database, directory, evidence)
        assert result.reason == "chat", result
        assert execution_requests, "Exercise E after actual P maintenance and private tool progress"
        question_event = next(row["id"] for row in evidence["events"] if row["content"] == question)
        assert all(question_event in identities for identities in evidence["normal_sources"])
        for request in execution_requests:
            focus_line = request.messages[-1].content.rsplit("\n", 1)[-1]
            focus = json.loads(focus_line[focus_line.index("{") :])
            assert focus["current_work"] is None
            assert focus["source_event_id"] == question_event
        assert after_execution
        assert all(identity is None for identity in ordinary_ownership)
        following = after_execution[-1]
        assert question in str(following.messages)
        assert "WRONG_SUMMARY_GOAL" in str(following.messages)
        assert any(item.tool_call_id == "ordinary-followup-read" for item in following.messages)
        after = next(row for row in evidence["works"] if row["id"] == background_id)
        assert {key: after[key] for key in before} == before
        assert after["state"] == "waiting_external"
        assert len(evidence["works"]) == 1
    finally:
        await capture_compact_facts(database, directory, evidence)
        await client.aclose()


async def test_manual_public_planning_failure_never_falls_back_to_private_execution(
    database, tmp_path, monkeypatch, compact_evidence
):
    """Manual P failure cannot purchase E or change an existing Work's private journal."""
    import yuki.services.context_compaction as compaction

    directory, evidence = compact_evidence
    env = await social_env(database, tmp_path)
    private_fact = "PRIVATE_WORK_FACT_MUST_NOT_ENTER_MANUAL_PUBLIC_REQUEST"
    private_requested = False
    manual_phase = False

    def respond(request):
        nonlocal private_requested
        evidence["requests"].append({"kind": _kind(request), "request": asdict(request)})
        assert not manual_phase, "A failed manual public plan must buy no model request"
        current = current_work_control.get().current
        if current is None:
            return _tool(
                "task_control",
                {"action": "accept", "goal": "retain private evidence"},
                "accept-private",
            )
        if not private_requested:
            private_requested = True
            return _tool("update_short_state", {"slot": 1, "text": private_fact}, "private-write")
        return _tool(
            "task_control",
            {
                "action": "wait",
                "conditions": [{"kind": "time_due", "after_seconds": 3600}],
            },
            "private-wait",
        )

    provider = FakeLLMProvider(_record_responses(respond, evidence))
    harness = build_harness(
        database,
        make_settings(
            database.url,
            runtime_work_enabled=True,
            enabled_groups_csv="20001",
        ),
        provider,
    )
    bind_main_contract(harness, tmp_path)
    chat = harness.processor._chat
    chat._tools.social_service = env.service
    env.service.runtime_config = chat._runtime_config
    client, payloads = wire(SimpleNamespace(provider=provider, runner=chat.runtime.runner), "chat")
    evidence["wire_requests"] = payloads

    def message(text, identity):
        return replace(
            inbound(
                text, message_id=identity, user_id="10001", group_id="20001", mentions_bot=True
            ),
            bot_user_id="80001",
            conversation_id=env.context.conversation_id,
            person_id=env.person,
            space_id=env.space,
            presence_id=env.presence,
        )

    async def private_facts():
        async with database.read_session() as reader:
            return {
                "work": [dict(row) for row in (await reader.execute(select(work))).mappings()],
                "journal": [
                    dict(row) for row in (await reader.execute(select(journal))).mappings()
                ],
            }

    try:
        original = await harness.processor.handle(
            message("Keep this private investigation pending.", "private-work-before-manual"),
            MemorySender(),
        )
        assert original.reason == "chat", original
        before = await private_facts()
        evidence["private_before"] = before
        assert before["work"][0]["state"] == "waiting_external"
        assert before["journal"]
        assert any(private_fact in str(item.messages) for item in provider.requests)
        async with database.immediate_session() as writer:
            (await writer.get(CanonicalPersonModel, env.person)).is_superuser = True
        original_plan = compaction.public_compact_plan

        def fail_after_public_plan(*args, **kwargs):
            plan = original_plan(*args, **kwargs)
            evidence["manual_public_plan"] = asdict(plan.request)
            raise WorkCapacityError("model_request_capacity")

        monkeypatch.setattr(compaction, "public_compact_plan", fail_after_public_plan)
        manual_phase = True
        original_requests = len(provider.requests)
        with pytest.raises(WorkCapacityError, match="model_request_capacity") as failure:
            await harness.processor.handle(
                message("/ai compact", "manual-public-only"), MemorySender()
            )
        evidence["manual_failure"] = {
            "category": type(failure.value).__name__,
            "error": str(failure.value),
        }
        assert len(provider.requests) == original_requests
        assert private_fact not in json.dumps(evidence["manual_public_plan"]["messages"])
        after = await private_facts()
        evidence["private_after"] = after
        assert after == before
        assert not any(action == "send_group_msg" for action, _ in env.bot.calls)
    finally:
        await capture_compact_facts(database, directory, evidence)
        await client.aclose()
