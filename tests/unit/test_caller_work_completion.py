"""Caller completion survives segmentation without replaying confirmed sends."""

import json
from dataclasses import replace

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


@pytest.mark.asyncio
async def test_complete_at_segment_end_preserves_proposal_until_real_caller_result(
    database, tmp_path
):
    env, provider, service, runtime = await caller_case(
        database,
        tmp_path,
        [
            call("task_control", {"action": "complete"}, "finish"),
            ChatResponse("internal result", 0),
        ],
    )
    messages = (ChatMessage("user", "finish original task"),)
    first = await service.run(messages, replace(runtime, max_model_requests=1), CallerBackend(env))
    assert first.work_state == "queued" and first.model_requests == 1
    original_responder = provider._responder

    def respond(request):
        control = current_work_control.get()
        assert control.ending == "completed"
        assert control.session.recovered_phase == "paired"
        assert control.session.progress["caller_completion_pending_result"]["action"] == "complete"
        return original_responder(request)

    provider._responder = respond
    second = await service.run(messages, replace(runtime, max_model_requests=1), CallerBackend(env))
    assert second.work_id == first.work_id
    assert second.work_state == "completed" and second.text == "internal result"
    assert len(provider.requests) == 2 and not any(
        action == "send_group_msg" for action, _ in env.bot.calls
    )
    repeated = await service.run(messages, runtime, CallerBackend(env))
    assert repeated.text == "internal result" and repeated.model_requests == 0
    assert len(provider.requests) == 2
    row = await WorkRepository(database).get(first.work_id)
    assert row["model_requests"] == 2 and row["tool_calls"] == 0


@pytest.mark.asyncio
async def test_confirmed_send_complete_segment_then_empty_internal_result_never_resends(
    database, tmp_path
):
    env, provider, service, runtime = await caller_case(
        database,
        tmp_path,
        [
            call("send_message", {"text": "synthetic confirmed reply"}, "send"),
            call("task_control", {"action": "complete"}, "finish"),
            call("task_control", {"action": "complete"}, "finish-resume"),
            ChatResponse('<yuki-state>{"engage":"quiet"}</yuki-state>', 0),
        ],
    )
    messages = (ChatMessage("user", "send once and finish"),)
    first = await service.run(messages, runtime, CallerBackend(env))
    assert first.work_state == "queued" and first.model_requests == 2
    assert sum(action == "send_group_msg" for action, _ in env.bot.calls) == 1
    second = await service.run(messages, runtime, CallerBackend(env))
    assert second.work_state == "completed" and second.text == ""
    assert second.model_requests == 2 and second.work_id == first.work_id
    row = await WorkRepository(database).get(first.work_id)
    assert row["model_requests"] == 4 and row["tool_calls"] == 1 and row["sent_messages"] == 1
    repeated = await service.run(messages, runtime, CallerBackend(env))
    assert repeated.work_state == "completed" and repeated.model_requests == 0
    assert len(provider.requests) == 4
    assert sum(action == "send_group_msg" for action, _ in env.bot.calls) == 1


@pytest.mark.asyncio
async def test_no_confirmed_effect_does_not_make_empty_caller_result_success(database, tmp_path):
    env, provider, service, runtime = await caller_case(
        database,
        tmp_path,
        [call("task_control", {"action": "complete"}, "finish"), ChatResponse("", 0)],
    )
    messages = (ChatMessage("user", "return a real result"),)
    first = await service.run(messages, replace(runtime, max_model_requests=1), CallerBackend(env))
    second = await service.run(messages, replace(runtime, max_model_requests=1), CallerBackend(env))
    assert second.work_id == first.work_id and second.work_state == "suspended"
    assert second.outcome.failure.code == "LLMEmptyResponseError"
    assert len(provider.requests) == 2 and not any(
        action == "send_group_msg" for action, _ in env.bot.calls
    )
    async with database.sessions() as reader:
        rows = (await reader.execute(select(work))).mappings().all()
    assert len(rows) == 1 and rows[0]["model_requests"] == 2


@pytest.mark.asyncio
async def test_new_ready_input_revokes_saved_completion_proposal(database, tmp_path):
    env, provider, service, runtime = await caller_case(
        database,
        tmp_path,
        [
            call("send_message", {"text": "original confirmed reply"}, "send"),
            call("task_control", {"action": "complete"}, "finish"),
        ],
    )
    messages = (ChatMessage("user", "original task"),)
    first = await service.run(messages, runtime, CallerBackend(env))
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

    def respond(request):
        control = current_work_control.get()
        assert control.ending is None
        assert "caller_completion_pending_result" not in control.session.progress
        assert any("new independent requirement" in (m.content or "") for m in request.messages)
        return ChatResponse("", 0)

    provider._responder = respond
    second = await service.run(messages, replace(runtime, max_model_requests=1), CallerBackend(env))
    assert (
        second.work_state == "suspended" and second.outcome.failure.code == "LLMEmptyResponseError"
    )
    assert sum(action == "send_group_msg" for action, _ in env.bot.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt_state", ["missing", "failed", "unknown"])
async def test_empty_internal_result_requires_original_confirmed_fact(
    database, tmp_path, receipt_state
):
    env, _provider, service, runtime = await caller_case(
        database,
        tmp_path,
        [
            call("send_message", {"text": "original confirmed reply"}, "send"),
            call("task_control", {"action": "complete"}, "finish"),
            ChatResponse("", 0),
        ],
    )
    messages = (ChatMessage("user", "original task"),)
    first = await service.run(messages, runtime, CallerBackend(env))
    async with database.immediate_session() as writer:
        if receipt_state == "missing":
            await writer.execute(delete(effects).where(effects.c.work_id == first.work_id))
        else:
            row = (
                (await writer.execute(select(effects).where(effects.c.work_id == first.work_id)))
                .mappings()
                .one()
            )
            receipt = json.loads(row["receipt_json"])
            receipt["outcome"].update(
                ok=False,
                status=receipt_state,
                delivered_message=False,
                uncertain=receipt_state == "unknown",
            )
            await writer.execute(
                update(effects)
                .where(effects.c.effect_key == row["effect_key"])
                .values(receipt_json=json.dumps(receipt))
            )
    second = await service.run(messages, replace(runtime, max_model_requests=1), CallerBackend(env))
    assert second.work_state != "completed"
    assert second.outcome.failure.code == "LLMEmptyResponseError"
    row = await WorkRepository(database).get(first.work_id)
    assert row["model_requests"] == 3 and row["sent_messages"] == 1
    assert sum(action == "send_group_msg" for action, _ in env.bot.calls) == 1


@pytest.mark.asyncio
async def test_source_revalidation_failure_cannot_use_saved_completion(database, tmp_path):
    env, provider, service, runtime = await caller_case(
        database,
        tmp_path,
        [call("task_control", {"action": "complete"}, "finish")],
    )
    messages = (ChatMessage("user", "original task"),)
    first = await service.run(messages, replace(runtime, max_model_requests=1), CallerBackend(env))

    async def changed():
        raise WorkConflict("invocation_authority_changed")

    with pytest.raises(WorkConflict, match="invocation_authority_changed"):
        await service.run(
            messages, replace(runtime, before_model_request=changed), CallerBackend(env)
        )
    row = await WorkRepository(database).get(first.work_id)
    assert row["state"] == "queued" and row["model_requests"] == 1
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_empty_provider_after_verified_completion_uses_original_receipt(database, tmp_path):
    env, provider, service, runtime = await caller_case(
        database,
        tmp_path,
        [
            call("send_message", {"text": "original confirmed reply"}, "send"),
            call("task_control", {"action": "complete"}, "finish"),
        ],
    )
    messages = (ChatMessage("user", "original task"),)
    first = await service.run(messages, runtime, CallerBackend(env))

    def empty(_):
        raise LLMEmptyResponseError("synthetic empty provider")

    provider._responder = empty
    second = await service.run(messages, replace(runtime, max_model_requests=1), CallerBackend(env))
    assert (
        second.work_id == first.work_id and second.work_state == "completed" and second.text == ""
    )
    row = await WorkRepository(database).get(first.work_id)
    assert row["model_requests"] == 3 and row["sent_messages"] == 1


@pytest.mark.asyncio
async def test_conversation_generation_change_does_not_restore_completion(database, tmp_path):
    env, provider, service, runtime = await caller_case(
        database, tmp_path, [call("task_control", {"action": "complete"}, "finish")]
    )
    messages = (ChatMessage("user", "original task"),)
    first = await service.run(messages, replace(runtime, max_model_requests=1), CallerBackend(env))
    async with database.immediate_session() as writer:
        await writer.execute(
            update(CanonicalConversationModel)
            .where(CanonicalConversationModel.id == runtime.canonical_conversation_id)
            .values(generation=2)
        )
    second = await service.run(messages, runtime, CallerBackend(env))
    assert second.work_state == "cancelled" and second.model_requests == 0
    row = await WorkRepository(database).get(first.work_id)
    assert row["model_requests"] == 1 and len(provider.requests) == 1


@pytest.mark.asyncio
async def test_goal_update_after_restored_completion_revokes_old_proposal(database, tmp_path):
    env, provider, service, runtime = await caller_case(
        database,
        tmp_path,
        [
            call("send_message", {"text": "original confirmed reply"}, "send"),
            call("task_control", {"action": "complete"}, "finish"),
            call("task_control", {"action": "update", "goal": "new goal"}, "update"),
        ],
    )
    messages = (ChatMessage("user", "original task"),)
    first = await service.run(messages, runtime, CallerBackend(env))
    second = await service.run(messages, replace(runtime, max_model_requests=1), CallerBackend(env))
    assert second.work_id == first.work_id and second.work_state == "queued"

    def respond(_):
        control = current_work_control.get()
        assert control.ending is None
        assert "caller_completion_pending_result" not in control.session.progress
        return ChatResponse("", 0)

    provider._responder = respond
    third = await service.run(messages, replace(runtime, max_model_requests=1), CallerBackend(env))
    assert third.work_state == "suspended" and third.outcome.failure.code == "LLMEmptyResponseError"
    row = await WorkRepository(database).get(first.work_id)
    assert row["goal"] == "new goal" and row["model_requests"] == 4
    assert sum(action == "send_group_msg" for action, _ in env.bot.calls) == 1


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
            call("task_control", {"action": "complete"}, "finish"),
            ChatResponse(body, 0),
        ],
    )
    messages = (ChatMessage("user", "original task"),)
    first = await service.run(messages, runtime, CallerBackend(env))
    original_complete = provider.complete

    async def complete(request):
        control = current_work_control.get()
        assert control.ending == "completed"
        result = await original_complete(request)
        async with database.immediate_session() as writer:
            row = (
                (await writer.execute(select(effects).where(effects.c.work_id == first.work_id)))
                .mappings()
                .one()
            )
            receipt = json.loads(row["receipt_json"])
            # Preserve the old delivered bit: a newly unknown outcome must
            # invalidate completion even with a surviving positive send fact.
            receipt["outcome"]["uncertain"] = True
            await writer.execute(
                update(effects)
                .where(effects.c.effect_key == row["effect_key"])
                .values(receipt_json=json.dumps(receipt))
            )
        return result

    provider.complete = complete
    second = await service.run(messages, replace(runtime, max_model_requests=1), CallerBackend(env))
    assert second.work_state == "queued" and second.text == ""
    row = await WorkRepository(database).get(first.work_id)
    assert row["model_requests"] == 3 and row["sent_messages"] == 1
    assert row["state"] == "queued" and "sync_result" not in json.loads(row["checkpoint_json"])
    assert sum(action == "send_group_msg" for action, _ in env.bot.calls) == 1


@pytest.mark.asyncio
async def test_empty_provider_with_new_ready_input_does_not_finish_caller(database, tmp_path):
    env, provider, service, runtime = await caller_case(
        database,
        tmp_path,
        [
            call("send_message", {"text": "original confirmed reply"}, "send"),
            call("task_control", {"action": "complete"}, "finish"),
        ],
    )
    messages = (ChatMessage("user", "original task"),)
    first = await service.run(messages, runtime, CallerBackend(env))
    repository = WorkRepository(database)

    async def complete(_):
        control = current_work_control.get()
        assert control.ending == "completed"
        identity = await repository.enqueue(
            runtime.canonical_conversation_id,
            1,
            "during-http",
            kind="message",
            work_id=first.work_id,
            ready=False,
        )
        await repository.prepare_input(identity, {"text": "new requirement during HTTP"})
        raise LLMEmptyResponseError("synthetic empty response after new input")

    provider.complete = complete
    second = await service.run(messages, replace(runtime, max_model_requests=1), CallerBackend(env))
    assert second.work_state == "queued" and second.model_requests == 1
    row = await repository.get(first.work_id)
    assert row["state"] == "queued" and "sync_result" not in json.loads(row["checkpoint_json"])
    assert row["model_requests"] == 3 and row["sent_messages"] == 1
    assert sum(action == "send_group_msg" for action, _ in env.bot.calls) == 1


@pytest.mark.asyncio
async def test_empty_provider_with_turn_local_visible_send_still_revalidates_unknown(
    database, tmp_path
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
    backend = CallerBackend(env)
    original_complete = provider.complete

    async def complete(request):
        response = await original_complete(request)
        if len(provider.requests) != 3:
            return response
        control = current_work_control.get()
        assert control.ending == "completed" and backend.has_visible_effects()
        async with database.immediate_session() as writer:
            row = (
                (
                    await writer.execute(
                        select(effects).where(effects.c.work_id == control.current["id"])
                    )
                )
                .mappings()
                .one()
            )
            receipt = json.loads(row["receipt_json"])
            receipt["outcome"]["uncertain"] = True
            await writer.execute(
                update(effects)
                .where(effects.c.effect_key == row["effect_key"])
                .values(receipt_json=json.dumps(receipt))
            )
        raise LLMEmptyResponseError("synthetic empty provider with original send now unknown")

    provider.complete = complete
    result = await service.run(
        (ChatMessage("user", "original task"),), replace(runtime, max_model_requests=3), backend
    )
    assert result.work_state == "queued" and result.model_requests == 3
    row = await WorkRepository(database).get(result.work_id)
    assert row["state"] == "queued" and "sync_result" not in json.loads(row["checkpoint_json"])
    assert row["model_requests"] == 3 and row["sent_messages"] == 1
    assert sum(action == "send_group_msg" for action, _ in env.bot.calls) == 1
