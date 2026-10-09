"""Caller completion carries its result in complete(result) and never replays sends."""

import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select, update
from tests.conftest import build_harness, make_settings
from tests.support.social_identity_cases import social_env
from tests.unit.test_work_delivery_ownership import DeliveryBackend, call
from yuki_participation.self_report import extract_tail

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.domain.messages import ChatMessage, ChatResponse
from qq_ai_bot.llm.base import LLMEmptyResponseError
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.runtime.work_activation import current_work_control
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_repository import WorkConflict, WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import effects, work
from qq_ai_bot.services.agent_runner import AgentRuntime


class CallerBackend(DeliveryBackend):
    def finalize(self, text, runtime):
        return extract_tail(text)[0]

    def has_visible_effects(self):
        return bool(self.owners)


async def caller_case(database, tmp_path, responses):
    env = await social_env(database, tmp_path)
    scripted = iter(responses)
    provider = FakeLLMProvider(lambda _: next(scripted))
    chat = build_harness(
        database, make_settings(database.url, runtime_work_enabled=True), provider
    ).processor._chat
    runtime = AgentRuntime(
        origin=TurnOrigin.SCHEDULED_AUTOMATION,
        actor_user_id="10001",
        actor_is_superuser=False,
        delegated_authority=None,
        conversation_key="caller-completion",
        current_group_id="20001",
        bot_user_id="80001",
        gateway=None,
        runtime_config=await chat._runtime_config.snapshot(),
        current_time=chat._time.current_default(),
        allowed_capabilities=frozenset(),
        max_tool_calls=2,
        max_model_requests=2,
        canonical_conversation_id=env.context.conversation_id,
        execution_id="original-caller-execution",
        invocation_goal="send once and finish",
        invocation_source={
            "actor_person_id": env.person,
            "principal_kind": "person",
            "presence_id": env.presence,
            "bot_user_id": "80001",
            "conversation_id": env.context.conversation_id,
            "generation": 1,
        },
    )
    return env, provider, chat.runtime.main_turns, runtime


async def sends(env):
    return sum(action == "send_group_msg" for action, _ in env.bot.calls)


async def save_accepted(database, runtime, work_id, payload):
    """Fixture for a crash after acceptance but before the writer committed it."""
    repository = WorkRepository(database)
    lease = await repository.acquire(runtime.canonical_conversation_id, 1)
    assert lease is not None
    try:
        await repository.accept_control(lease, work_id, payload)
    finally:
        await repository.release(lease)


async def mark_receipt(database, work_id, **outcome):
    async with database.immediate_session() as writer:
        row = (
            (await writer.execute(select(effects).where(effects.c.work_id == work_id)))
            .mappings()
            .one()
        )
        receipt = json.loads(row["receipt_json"])
        receipt["outcome"].update(outcome)
        await writer.execute(
            update(effects)
            .where(effects.c.effect_key == row["effect_key"])
            .values(receipt_json=json.dumps(receipt))
        )


def checkpoint(row):
    return json.loads(row["checkpoint_json"])


@pytest.mark.asyncio
async def test_complete_result_ends_activation_and_reentry_returns_committed_result(
    database, tmp_path
):
    env, provider, service, runtime = await caller_case(
        database,
        tmp_path,
        [call("task_control", {"action": "complete", "result": "internal result"}, "finish")],
    )
    messages = (ChatMessage("user", "finish original task"),)
    # Even a one-request segment completes: the accepted call ends the activation.
    first = await service.run(messages, replace(runtime, max_model_requests=1), CallerBackend(env))
    assert first.work_state == "completed" and first.text == "internal result"
    assert first.model_requests == 1 and len(provider.requests) == 1
    # The internal result returns to the caller; it is never sent by itself.
    assert await sends(env) == 0
    row = await WorkRepository(database).get(first.work_id)
    assert checkpoint(row)["sync_result"] == "internal result"
    assert "accepted_control" not in checkpoint(row)
    repeated = await service.run(messages, runtime, CallerBackend(env))
    assert repeated.work_id == first.work_id and repeated.work_state == "completed"
    assert repeated.text == "internal result" and repeated.model_requests == 0
    assert len(provider.requests) == 1
    row = await WorkRepository(database).get(first.work_id)
    assert row["model_requests"] == 1


@pytest.mark.asyncio
async def test_ordinary_final_uses_same_completion_as_caller_result(database, tmp_path):
    env, provider, service, runtime = await caller_case(
        database, tmp_path, [ChatResponse("internal result", 0)]
    )
    messages = (ChatMessage("user", "answer the caller"),)
    first = await service.run(messages, runtime, CallerBackend(env))
    assert first.work_state == "completed" and first.text == "internal result"
    assert len(provider.requests) == 1 and await sends(env) == 0
    row = await WorkRepository(database).get(first.work_id)
    assert checkpoint(row)["sync_result"] == "internal result"


@pytest.mark.asyncio
async def test_confirmed_send_then_empty_result_completion_never_resends(database, tmp_path):
    env, provider, service, runtime = await caller_case(
        database,
        tmp_path,
        [
            call("send_message", {"text": "synthetic confirmed reply"}, "send"),
            call("task_control", {"action": "complete"}, "finish"),
        ],
    )
    messages = (ChatMessage("user", "send once and finish"),)
    # The send exhausts the first segment; completion arrives in the next one.
    first = await service.run(messages, replace(runtime, max_model_requests=1), CallerBackend(env))
    assert first.work_state == "queued" and first.model_requests == 1
    assert await sends(env) == 1
    second = await service.run(messages, runtime, CallerBackend(env))
    assert second.work_id == first.work_id
    assert second.work_state == "completed" and second.text == ""
    assert second.model_requests == 1 and len(provider.requests) == 2
    row = await WorkRepository(database).get(first.work_id)
    # Budgets accumulate on the original Work; nothing is reset by the segment.
    assert row["model_requests"] == 2 and row["sent_messages"] == 1
    assert checkpoint(row)["sync_result"] == ""
    repeated = await service.run(messages, runtime, CallerBackend(env))
    assert repeated.work_state == "completed" and repeated.model_requests == 0
    assert len(provider.requests) == 2 and await sends(env) == 1


@pytest.mark.asyncio
async def test_no_confirmed_effect_rejects_empty_caller_result(database, tmp_path):
    env, provider, service, runtime = await caller_case(
        database,
        tmp_path,
        [call("task_control", {"action": "complete"}, "finish"), ChatResponse("", 0)],
    )
    messages = (ChatMessage("user", "return a real result"),)
    result = await service.run(messages, runtime, CallerBackend(env))
    # The rejected complete is visible to the model; the empty final runs the
    # same preparation and its stable code becomes the pause reason.
    assert result.work_state == "suspended" and result.text == ""
    assert '"work_completion_requires_result"' in provider.requests[1].messages[-1].content
    assert len(provider.requests) == 2 and await sends(env) == 0
    async with database.sessions() as reader:
        rows = (await reader.execute(select(work))).mappings().all()
    assert len(rows) == 1 and rows[0]["model_requests"] == 2
    assert rows[0]["state"] == "suspended"
    assert rows[0]["reason"] == "work_completion_requires_result"
    assert "sync_result" not in checkpoint(rows[0])


@pytest.mark.asyncio
async def test_new_ready_input_revokes_saved_completion(database, tmp_path):
    env, provider, service, runtime = await caller_case(
        database,
        tmp_path,
        [call("send_message", {"text": "original confirmed reply"}, "send")],
    )
    messages = (ChatMessage("user", "original task"),)
    first = await service.run(messages, replace(runtime, max_model_requests=1), CallerBackend(env))
    assert first.work_state == "queued"
    await save_accepted(
        database,
        runtime,
        first.work_id,
        {"action": "complete", "call_key": "finish", "result": "stale result"},
    )
    repository = WorkRepository(database)
    identity = await repository.enqueue(
        runtime.canonical_conversation_id,
        1,
        "new-input",
        kind="message",
        work_id=first.work_id,
        ready=False,
    )
    await repository.prepare_input(identity, {"text": "new independent requirement"})
    second = await service.run(messages, runtime, CallerBackend(env))
    # The writer sees the admitted input: the stale candidate is consumed
    # without publishing its result, and the Work stays live for the input.
    assert second.work_id == first.work_id and second.work_state == "queued"
    assert second.text == "" and second.model_requests == 0
    row = await repository.get(first.work_id)
    assert row["state"] == "queued"
    assert "sync_result" not in checkpoint(row) and "accepted_control" not in checkpoint(row)

    def respond(request):
        control = current_work_control.get()
        assert control.accepted is None and control.ending is None
        assert any("new independent requirement" in (m.content or "") for m in request.messages)
        return call("task_control", {"action": "complete", "result": "fresh result"}, "fresh")

    provider._responder = respond
    third = await service.run(messages, runtime, CallerBackend(env))
    assert third.work_state == "completed" and third.text == "fresh result"
    assert len(provider.requests) == 2 and await sends(env) == 1
    row = await repository.get(first.work_id)
    assert row["model_requests"] == 2 and checkpoint(row)["sync_result"] == "fresh result"


@pytest.mark.asyncio
async def test_new_input_during_complete_request_keeps_work_live(database, tmp_path):
    env, provider, service, runtime = await caller_case(
        database,
        tmp_path,
        [
            call("send_message", {"text": "original confirmed reply"}, "send"),
            call("task_control", {"action": "complete", "result": "too early"}, "finish"),
        ],
    )
    repository = WorkRepository(database)
    original_complete = provider.complete

    async def complete(request):
        response = await original_complete(request)
        if len(provider.requests) == 2:
            control = current_work_control.get()
            identity = await repository.enqueue(
                runtime.canonical_conversation_id,
                1,
                "during-http",
                kind="message",
                work_id=control.current["id"],
                ready=False,
            )
            await repository.prepare_input(identity, {"text": "new requirement during HTTP"})
        return response

    provider.complete = complete
    result = await service.run((ChatMessage("user", "original task"),), runtime, CallerBackend(env))
    assert result.work_state == "queued" and result.text == ""
    assert result.model_requests == 2 and len(provider.requests) == 2
    row = await repository.get(result.work_id)
    assert row["state"] == "queued" and row["reason"] == "work_input_arrived"
    assert "sync_result" not in checkpoint(row) and "accepted_control" not in checkpoint(row)
    assert row["sent_messages"] == 1 and await sends(env) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "receipt_state,code",
    [
        ("missing", "work_completion_requires_result"),
        ("failed", "work_completion_requires_result"),
        ("unknown", "work_has_unresolved_execution"),
    ],
)
async def test_empty_internal_result_requires_original_confirmed_fact(
    database, tmp_path, receipt_state, code
):
    env, provider, service, runtime = await caller_case(
        database,
        tmp_path,
        [
            call("send_message", {"text": "original confirmed reply"}, "send"),
            call("task_control", {"action": "complete"}, "finish"),
            ChatResponse("", 0),
        ],
    )
    messages = (ChatMessage("user", "original task"),)
    first = await service.run(messages, replace(runtime, max_model_requests=1), CallerBackend(env))
    assert first.work_state == "queued"
    if receipt_state == "missing":
        async with database.immediate_session() as writer:
            await writer.execute(delete(effects).where(effects.c.work_id == first.work_id))
    else:
        await mark_receipt(
            database,
            first.work_id,
            ok=False,
            status=receipt_state,
            delivered_message=False,
            uncertain=receipt_state == "unknown",
        )
    second = await service.run(messages, runtime, CallerBackend(env))
    assert second.work_state == "suspended" and second.text == ""
    assert f'"{code}"' in provider.requests[2].messages[-1].content
    row = await WorkRepository(database).get(first.work_id)
    assert row["state"] == "suspended" and row["reason"] == code
    assert row["model_requests"] == 3 and row["sent_messages"] == 1
    assert "sync_result" not in checkpoint(row)
    assert len(provider.requests) == 3 and await sends(env) == 1


@pytest.mark.asyncio
async def test_restored_accepted_completion_settles_without_model_request(database, tmp_path):
    env, provider, service, runtime = await caller_case(
        database,
        tmp_path,
        [call("send_message", {"text": "original confirmed reply"}, "send")],
    )
    messages = (ChatMessage("user", "original task"),)
    first = await service.run(messages, replace(runtime, max_model_requests=1), CallerBackend(env))
    await save_accepted(
        database,
        runtime,
        first.work_id,
        {"action": "complete", "call_key": "finish", "result": "saved result"},
    )
    second = await service.run(messages, runtime, CallerBackend(env))
    assert second.work_id == first.work_id and second.work_state == "completed"
    assert second.text == "saved result" and second.model_requests == 0
    assert len(provider.requests) == 1 and await sends(env) == 1
    row = await WorkRepository(database).get(first.work_id)
    assert row["model_requests"] == 1 and row["sent_messages"] == 1
    assert checkpoint(row)["sync_result"] == "saved result"
    assert "accepted_control" not in checkpoint(row)


@pytest.mark.asyncio
async def test_source_revalidation_failure_cannot_use_saved_completion(database, tmp_path):
    env, provider, service, runtime = await caller_case(
        database,
        tmp_path,
        [call("send_message", {"text": "original confirmed reply"}, "send")],
    )
    messages = (ChatMessage("user", "original task"),)
    first = await service.run(messages, replace(runtime, max_model_requests=1), CallerBackend(env))
    saved = {"action": "complete", "call_key": "finish", "result": "saved result"}
    await save_accepted(database, runtime, first.work_id, saved)

    async def changed():
        raise WorkConflict("invocation_authority_changed")

    with pytest.raises(WorkConflict, match="invocation_authority_changed"):
        await service.run(
            messages, replace(runtime, before_model_request=changed), CallerBackend(env)
        )
    row = await WorkRepository(database).get(first.work_id)
    assert row["state"] == "queued" and row["model_requests"] == 1
    assert checkpoint(row)["accepted_control"] == saved
    assert "sync_result" not in checkpoint(row)
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_empty_provider_after_send_is_ordinary_failure_not_completion(database, tmp_path):
    env, provider, service, runtime = await caller_case(
        database,
        tmp_path,
        [call("send_message", {"text": "original confirmed reply"}, "send")],
    )
    messages = (ChatMessage("user", "original task"),)
    first = await service.run(messages, replace(runtime, max_model_requests=1), CallerBackend(env))

    def empty(_):
        raise LLMEmptyResponseError("synthetic empty provider")

    provider._responder = empty
    second = await service.run(messages, replace(runtime, max_model_requests=1), CallerBackend(env))
    assert second.work_id == first.work_id
    assert second.work_state != "completed" and second.text == ""
    assert second.outcome.failure.code == "LLMEmptyResponseError"
    row = await WorkRepository(database).get(first.work_id)
    assert row["state"] != "completed" and "sync_result" not in checkpoint(row)
    assert row["model_requests"] == 2 and row["sent_messages"] == 1
    assert await sends(env) == 1


@pytest.mark.asyncio
async def test_conversation_generation_change_does_not_restore_completion(database, tmp_path):
    env, provider, service, runtime = await caller_case(
        database,
        tmp_path,
        [call("send_message", {"text": "original confirmed reply"}, "send")],
    )
    messages = (ChatMessage("user", "original task"),)
    first = await service.run(messages, replace(runtime, max_model_requests=1), CallerBackend(env))
    await save_accepted(
        database,
        runtime,
        first.work_id,
        {"action": "complete", "call_key": "finish", "result": "saved result"},
    )
    async with database.immediate_session() as writer:
        await writer.execute(
            update(CanonicalConversationModel)
            .where(CanonicalConversationModel.id == runtime.canonical_conversation_id)
            .values(generation=2)
        )
    second = await service.run(messages, runtime, CallerBackend(env))
    assert second.work_state == "cancelled" and second.model_requests == 0
    assert second.text == ""
    row = await WorkRepository(database).get(first.work_id)
    assert row["model_requests"] == 1 and len(provider.requests) == 1
    assert row["state"] != "completed" and "sync_result" not in checkpoint(row)


@pytest.mark.asyncio
async def test_goal_update_retires_accepted_completion(database, tmp_path):
    env, _provider, service, runtime = await caller_case(
        database,
        tmp_path,
        [call("send_message", {"text": "original confirmed reply"}, "send")],
    )
    messages = (ChatMessage("user", "original task"),)
    first = await service.run(messages, replace(runtime, max_model_requests=1), CallerBackend(env))
    repository = WorkRepository(database)
    lease = await repository.acquire(runtime.canonical_conversation_id, 1)
    assert lease is not None
    try:
        row = await repository.get(first.work_id)
        control = WorkControl(
            repository,
            lease,
            row["source_key"],
            json.loads(row["source_json"]),
            AsyncMock(),
        )
        control.current = row
        await control._accept("complete", "finish", result="stale result")
        assert control.accepted_ending() == "completed"
        updated = json.loads(
            await control.execute(
                "task_control", {"action": "update", "goal": "new goal"}, "update"
            )
        )
        assert updated["ok"] is True and updated["ending_proposed"] is None
        assert control.accepted is None and control.accepted_ending() is None
        await control.settle(pending_inputs=False)
    finally:
        await repository.release(lease)
    row = await repository.get(first.work_id)
    assert row["goal"] == "new goal" and row["state"] == "suspended"
    assert "accepted_control" not in checkpoint(row) and "sync_result" not in checkpoint(row)
    assert await sends(env) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["", "actual internal result"])
async def test_receipt_becoming_unknown_during_model_cannot_complete_result(
    database, tmp_path, body
):
    env, provider, service, runtime = await caller_case(
        database,
        tmp_path,
        [
            call("send_message", {"text": "original confirmed reply"}, "send"),
            call("task_control", {"action": "complete", "result": body}, "finish"),
        ],
    )
    messages = (ChatMessage("user", "original task"),)
    first = await service.run(messages, replace(runtime, max_model_requests=1), CallerBackend(env))
    original_complete = provider.complete

    async def complete(request):
        result = await original_complete(request)
        # Preserve the old delivered bit: a newly unknown outcome must
        # invalidate completion even with a surviving positive send fact.
        await mark_receipt(database, first.work_id, uncertain=True)
        return result

    provider.complete = complete
    second = await service.run(messages, replace(runtime, max_model_requests=1), CallerBackend(env))
    assert second.work_state != "completed" and second.text == ""
    row = await WorkRepository(database).get(first.work_id)
    assert row["model_requests"] == 2 and row["sent_messages"] == 1
    assert row["state"] != "completed"
    assert "sync_result" not in checkpoint(row) and "accepted_control" not in checkpoint(row)
    assert len(provider.requests) == 2 and await sends(env) == 1


@pytest.mark.asyncio
async def test_writer_rechecks_receipt_that_turns_unknown_after_acceptance(
    database, tmp_path, monkeypatch
):
    env, provider, service, runtime = await caller_case(
        database,
        tmp_path,
        [
            call("send_message", {"text": "original confirmed reply"}, "send"),
            call("task_control", {"action": "complete", "result": "accepted result"}, "finish"),
        ],
    )
    original_accept = WorkRepository.accept_control

    async def accept_then_unknown(self, lease, identity, payload):
        row = await original_accept(self, lease, identity, payload)
        if payload is not None:
            await mark_receipt(database, identity, uncertain=True)
        return row

    monkeypatch.setattr(WorkRepository, "accept_control", accept_then_unknown)
    result = await service.run((ChatMessage("user", "original task"),), runtime, CallerBackend(env))
    assert result.work_state == "suspended" and result.text == ""
    assert len(provider.requests) == 2
    row = await WorkRepository(database).get(result.work_id)
    assert row["state"] == "suspended" and row["reason"] == "work_completion_facts_changed"
    assert "sync_result" not in checkpoint(row) and "accepted_control" not in checkpoint(row)
    assert row["sent_messages"] == 1 and await sends(env) == 1


@pytest.mark.asyncio
async def test_empty_provider_with_turn_local_visible_send_is_not_completion(database, tmp_path):
    env, provider, service, runtime = await caller_case(
        database,
        tmp_path,
        [call("send_message", {"text": "original confirmed reply"}, "send")],
    )
    backend = CallerBackend(env)
    original_complete = provider.complete

    async def complete(request):
        if len(provider.requests) < 1:
            return await original_complete(request)
        provider.requests.append(request)
        control = current_work_control.get()
        assert control.accepted is None and backend.has_visible_effects()
        await mark_receipt(database, control.current["id"], uncertain=True)
        raise LLMEmptyResponseError("synthetic empty provider with original send now unknown")

    provider.complete = complete
    result = await service.run(
        (ChatMessage("user", "original task"),), replace(runtime, max_model_requests=3), backend
    )
    # A turn-local visible send grants no completion shortcut to an active Work.
    assert result.work_state != "completed" and result.text == ""
    assert result.outcome.failure.code == "LLMEmptyResponseError"
    row = await WorkRepository(database).get(result.work_id)
    assert row["state"] != "completed" and "sync_result" not in checkpoint(row)
    assert row["model_requests"] == 3 and row["sent_messages"] == 1
    assert len(provider.requests) == 3 and await sends(env) == 1
