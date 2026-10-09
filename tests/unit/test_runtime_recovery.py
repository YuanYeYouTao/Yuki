"""Durable recovery must never spend another model request or resend acceptance."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update
from tests.support.social_identity_cases import social_env
from tests.support.work_session import WorkSession, invoke_tool

from qq_ai_bot.capabilities.results import ToolExecutionResult, ToolResultBudgeter
from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.domain.messages import ChatMessage, ToolCall, ToolFunction
from qq_ai_bot.gateway.registry import RegistryClosed
from qq_ai_bot.identity.routing import RouteSendError
from qq_ai_bot.llm.base import LLMUnavailableError
from qq_ai_bot.runtime.activation_outcome import ExitReason
from qq_ai_bot.runtime.delivery_intents import record, reserve
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_journal import JournalUnavailable
from qq_ai_bot.runtime.work_recovery_schema import deliveries, recovery
from qq_ai_bot.runtime.work_repository import WorkConflict, WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import effects, scope
from qq_ai_bot.runtime.work_supervisor import recover_failure
from qq_ai_bot.services.concurrency import RequestCancelledError
from qq_ai_bot.services.turn_transcript import TurnTranscript
from qq_ai_bot.tool_results.artifacts import ToolArtifactRepository


async def setup(database, tmp_path, *, output_kind="state_change"):
    env = await social_env(database, tmp_path)
    repo = WorkRepository(database)
    lease = await repo.acquire(env.context.conversation_id, 1)

    async def validate():
        assert await repo.valid(lease)

    control = WorkControl(repo, lease, "delivery-test", {"trigger_event_id": 1}, validate)
    control.current = await repo.accept(
        lease, source_key="delivery-test", source={}, goal="deliver", output_kind=output_kind
    )
    control.session = WorkSession(control, "contract")
    await control.session.restore(TurnTranscript((ChatMessage("user", "deliver"),)))
    return control


@pytest.mark.asyncio
async def test_disconnected_presence_queues_original_work_without_error_notice(database, tmp_path):
    control = await setup(database, tmp_path)
    try:
        raise RouteSendError("original_presence_unavailable") from RegistryClosed("disconnected")
    except RouteSendError as disconnected:
        outcome = await recover_failure(control, disconnected)
    assert outcome.reason is ExitReason.RETRY
    assert outcome.failure and outcome.failure.code == "gateway_disconnected"
    assert control.current and control.current["state"] == "queued"
    async with database.sessions() as session:
        saved = (
            await session.execute(
                select(recovery.c.exit_reason, recovery.c.attempts).where(
                    recovery.c.work_id == control.current["id"]
                )
            )
        ).one()
        notices = await session.scalar(
            select(deliveries.c.id).where(
                deliveries.c.work_id == control.current["id"], deliveries.c.kind == "notice"
            )
        )
    assert saved == ("retry", 1)
    assert notices is None


@pytest.mark.parametrize(
    "failure", [LLMUnavailableError, asyncio.CancelledError, RequestCancelledError]
)
async def test_repeated_transient_failure_preserves_original_work_and_can_complete(
    database, tmp_path, failure
):
    control = await setup(database, tmp_path, output_kind="answer")
    identity = control.current["id"]
    await control.repository.checkpoint(control.lease, identity, None, models=1)
    control.current = await control.repository.get(identity)
    await control.session.save("dispatched")
    for _ in range(5):
        outcome = await recover_failure(control, failure("temporary interruption"))
        assert outcome.reason is ExitReason.RETRY
        assert control.current["id"] == identity
        assert control.current["state"] == "queued"
        assert control.current["model_requests"] == 1
    async with database.sessions() as session:
        assert (
            await session.scalar(select(recovery.c.attempts).where(recovery.c.work_id == identity))
            == 5
        )
        assert (
            await session.scalar(select(deliveries.c.id).where(deliveries.c.work_id == identity))
            is None
        )
    resumed = WorkSession(control, "contract")
    await resumed.restore(TurnTranscript((ChatMessage("user", "current source"),)))
    control.session = resumed
    control.settled = False
    await control.complete_final("finished", "recovered-final")
    await control.settle(pending_inputs=False)
    assert control.current["id"] == identity and control.current["state"] == "completed"
    assert control.current["model_requests"] == 1


@pytest.mark.parametrize("failure", ["capacity", "uncertain", "cancel"])
async def test_artifact_publication_failure_preserves_typed_effect_without_replaying_business(
    database, tmp_path, failure
):
    control = await setup(database, tmp_path)
    identity = control.current["id"]
    call = ToolCall("mutate-once", ToolFunction("fixture_mutation", "{}"))
    session = control.session
    session.transcript.append(ChatMessage("assistant", "", tool_calls=(call,)))
    await session.save("response", (call,))
    key = session.call_key(call.id)
    artifacts = ToolArtifactRepository(
        database, tmp_path / "artifacts", retention_seconds=60, max_artifact_bytes=1
    )
    budgeter = ToolResultBudgeter(max_characters=1, artifacts=artifacts)
    executions = 0

    async def business():
        nonlocal executions
        executions += 1
        outcome = ToolExecutionResult(
            ok=True,
            mutation_committed=True,
            uncertain=failure == "uncertain",
            provider_id="fixture",
            tool_name="fixture_mutation",
            data={"actual_result": "already committed"},
        )
        if failure == "cancel":
            from qq_ai_bot.runtime.effect_outcomes import current_result_capture

            current_result_capture.get().outcome = outcome
            raise asyncio.CancelledError("cancelled after confirmed effect")
        return (await budgeter.render(outcome)).text

    if failure == "capacity":
        result = await invoke_tool(session, call, business)
        assert json.loads(result)["result_unavailable"] is True
        assert json.loads(result)["mutation_committed"] is True
        session.transcript.append_result(call.id, result)
        await session.save("paired")
        await control.complete_final("business effect completed", "completed-after-artifact-error")
        await control.settle(pending_inputs=False)
        assert control.current["state"] == "completed"
    else:
        with pytest.raises(asyncio.CancelledError if failure == "cancel" else ValueError):
            await invoke_tool(session, call, business)
        assert await control.has_unresolved_effects() is (failure == "uncertain")
    assert executions == 1
    if failure != "capacity":
        replayed = (
            await session.journal.effect_result(key)
            if failure == "uncertain"
            else await invoke_tool(session, call, business)
        )
        assert json.loads(replayed)["replay_forbidden"] is True
        assert executions == 1
    async with database.sessions() as reader:
        receipt = (
            await reader.execute(
                select(effects.c.state, effects.c.receipt_json).where(effects.c.effect_key == key)
            )
        ).one()
    assert receipt[0] == "accepted"
    original = json.loads(receipt[1])["outcome"]
    assert original["mutation_committed"] is True
    assert original["uncertain"] is (failure == "uncertain")
    assert (await control.repository.get(identity))["tool_calls"] == 1


async def change_prompt_source(database, conversation_id):
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(CanonicalConversationModel)
            .where(CanonicalConversationModel.id == conversation_id)
            .values(prompt_source_revision=CanonicalConversationModel.prompt_source_revision + 1)
        )


@pytest.mark.asyncio
async def test_source_change_retries_original_work_before_any_effect(database, tmp_path):
    control = await setup(database, tmp_path)
    assert control.session is not None and control.current is not None
    original_id = control.current["id"]
    original_chain = control.session.transcript.chain_id
    await control.session.save("dispatched")
    await control.repository.checkpoint(control.lease, original_id, None, models=1)
    control.current = await control.repository.get(original_id)
    await change_prompt_source(database, control.lease.conversation_id)

    with pytest.raises(WorkConflict, match="work_journal_source_changed") as caught:
        await control.session.save("response")
    outcome = await recover_failure(control, caught.value)
    assert outcome.reason is ExitReason.RETRY
    assert outcome.failure and outcome.failure.code == "work_journal_source_changed"
    assert control.current["id"] == original_id and control.current["state"] == "queued"
    async with database.sessions() as session:
        row = (
            await session.execute(
                select(recovery.c.failure_json, recovery.c.exit_reason).where(
                    recovery.c.work_id == original_id
                )
            )
        ).one()
        notice = await session.scalar(
            select(deliveries.c.id).where(deliveries.c.work_id == original_id)
        )
    assert row[1] == "retry" and json.loads(row[0])["code"] == "work_journal_source_changed"
    assert notice is None

    resumed = WorkSession(control, "contract")
    await resumed.restore(TurnTranscript((ChatMessage("user", "current source"),)))
    assert resumed.transcript.chain_id != original_chain
    assert resumed.progress["chain_links"][0]["reason"] == "source_changed"
    assert resumed.transcript.request().messages[0].content == "current source"


@pytest.mark.asyncio
async def test_source_change_after_accepted_automation_preserves_receipt_without_replay(
    database, tmp_path
):
    control = await setup(database, tmp_path)
    assert control.session is not None and control.current is not None
    original_id = control.current["id"]
    call = ToolCall("create-once", ToolFunction("automation_create", "{}"))
    control.session.transcript.append(ChatMessage("assistant", "", tool_calls=(call,)))
    await control.session.save("response", (call,))
    invoke = AsyncMock(return_value='{"ok":true,"automation_id":93}')
    original_key = control.session.call_key(call.id)
    await invoke_tool(control.session, call, invoke)
    await change_prompt_source(database, control.lease.conversation_id)

    with pytest.raises(WorkConflict, match="work_journal_source_changed") as caught:
        await control.session.save("response")
    outcome = await recover_failure(control, caught.value)
    assert outcome.reason is ExitReason.RETRY
    assert outcome.failure and outcome.failure.code == "work_journal_source_changed"
    assert outcome.failure.retryable is True
    assert control.current["id"] == original_id and control.current["state"] == "queued"
    resumed = WorkSession(control, "contract")
    await resumed.restore(TurnTranscript((ChatMessage("user", "current source"),)))
    assert any(
        item.get("ok") and item.get("effect_key") == original_key for item in control.known_effects
    )
    invoke.assert_awaited_once()
    async with database.sessions() as session:
        saved = (
            await session.execute(
                select(recovery.c.failure_json, recovery.c.exit_reason).where(
                    recovery.c.work_id == original_id
                )
            )
        ).one()
        notice = await session.scalar(
            select(deliveries.c.payload_json).where(
                deliveries.c.work_id == original_id, deliveries.c.kind == "notice"
            )
        )
        effect = await session.scalar(
            select(effects.c.state).where(effects.c.effect_key == original_key)
        )
    assert saved[1] == "retry"
    assert json.loads(saved[0])["code"] == "work_journal_source_changed"
    assert notice is None
    assert effect == "accepted"
    control.session = resumed
    control.settled = False
    await control.complete_final("automation created", "recovered-final")
    await control.settle(pending_inputs=False)
    assert control.current["id"] == original_id and control.current["state"] == "completed"
    invoke.assert_awaited_once()


@pytest.mark.asyncio
async def test_source_change_never_lets_expired_owner_commit_recovery(database, tmp_path):
    control = await setup(database, tmp_path)
    assert control.current is not None
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(scope)
            .where(scope.c.conversation_id == control.lease.conversation_id)
            .values(lease_until=0)
        )
    with pytest.raises(WorkConflict, match="work_journal_source_changed"):
        await recover_failure(control, WorkConflict("work_journal_source_changed"))
    async with database.sessions() as session:
        assert (
            await session.scalar(
                select(recovery.c.work_id).where(recovery.c.work_id == control.current["id"])
            )
            is None
        )


@pytest.mark.asyncio
async def test_committed_cancellation_is_read_before_lease_guarded_effects(database, tmp_path):
    control = await setup(database, tmp_path)
    identity = control.current["id"]
    await control.repository.cancel(control.lease.conversation_id)
    assert not await control.repository.valid(control.lease)
    outcome = await recover_failure(control, WorkConflict("work_journal_source_changed"))
    assert outcome.reason is ExitReason.CANCELLED
    assert outcome.work_id == identity
    assert control.current["state"] == "cancelled"
    async with database.sessions() as session:
        assert (
            await session.scalar(select(recovery.c.work_id).where(recovery.c.work_id == identity))
        ) is None
        assert (
            await session.scalar(select(deliveries.c.id).where(deliveries.c.work_id == identity))
        ) is None


@pytest.mark.asyncio
async def test_delivery_reservations_are_idempotent_and_fence_replay(database, tmp_path):
    control = await setup(database, tmp_path)
    await reserve(control, "previous", "final", {}, count=16)
    await reserve(control, "previous", "final", {}, count=16)
    assert control.current["sent_messages"] == 16
    with pytest.raises(WorkConflict, match="intent_conflict"):
        await reserve(control, "previous", "final", {}, count=17)
    for state in ("dispatching", "unknown", "accepted"):
        key = f"guard-{state}"
        await reserve(control, key, "final", {})
        await record(control, key, state, {})
        with pytest.raises(WorkConflict, match="replay_forbidden"):
            await reserve(control, key, "final", {})
    for count in (0, -1, True):
        with pytest.raises(WorkConflict, match="message_count_invalid"):
            await reserve(control, "invalid", "final", {}, count=count)


@pytest.mark.asyncio
async def test_missing_used_history_is_not_a_fresh_chain(database, tmp_path):
    control = await setup(database, tmp_path)
    await control.repository.checkpoint(control.lease, control.current["id"], None, models=1)
    with pytest.raises(JournalUnavailable, match="work_journal_missing"):
        await WorkSession(control, "contract").restore(TurnTranscript(()))


@pytest.mark.asyncio
async def test_prepared_input_wins_race_with_wait_commit(database, tmp_path):
    control = await setup(database, tmp_path)
    identity = control.current["id"]
    await control.repository.enqueue(
        control.lease.conversation_id,
        control.lease.generation,
        "late-ready-input",
        kind="message",
        work_id=identity,
        ready=True,
    )
    # The supervisor inspected the mailbox before preparation completed.
    control.ending = "waiting_external"
    await control.settle(pending_inputs=False)
    assert control.current["state"] == "queued"
    assert control.outcome.reason.value == "waiting_input"
