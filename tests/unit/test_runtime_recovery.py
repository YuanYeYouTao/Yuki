"""Durable delivery recovery must never spend another model request or resend acceptance."""

import json
import time

import pytest
from sqlalchemy import select, update
from tests.support.social_identity_cases import social_env

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.domain.messages import ChatMessage, OutboundMessage, OutboundSendReceipt
from qq_ai_bot.gateway.registry import RegistryClosed
from qq_ai_bot.identity.routing import RouteSendError
from qq_ai_bot.runtime.activation_outcome import ExitReason
from qq_ai_bot.runtime.delivery_intents import record, reserve
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_delivery import WorkDeliverySender, resume_delivery_plan
from qq_ai_bot.runtime.work_journal import JournalUnavailable
from qq_ai_bot.runtime.work_recovery_schema import deliveries, quota, recovery
from qq_ai_bot.runtime.work_repository import WorkConflict, WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import effects, scope, work
from qq_ai_bot.runtime.work_session import WorkSession
from qq_ai_bot.runtime.work_supervisor import recover_failure
from qq_ai_bot.services.turn_transcript import TurnTranscript


async def setup(database, tmp_path):
    env = await social_env(database, tmp_path)
    repo = WorkRepository(database)
    lease = await repo.acquire(env.context.conversation_id, 1)

    async def validate():
        assert await repo.valid(lease)

    control = WorkControl(repo, lease, "delivery-test", {"trigger_event_id": 1}, validate)
    control.current = await repo.accept(
        lease, source_key="delivery-test", source={}, goal="deliver"
    )
    control.session = WorkSession(control, "contract")
    await control.session.restore(TurnTranscript((ChatMessage("user", "deliver"),)))
    return control


class Sender:
    def __init__(self, fail_at=0):
        self.messages = []
        self.fail_at = fail_at

    async def send(self, message):
        self.messages.append(message.text)
        if len(self.messages) == self.fail_at:
            raise TimeoutError("gateway outcome unknown")
        return OutboundSendReceipt(str(len(self.messages)))


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
    assert await control.repository.prepare_effect(
        control.lease, original_id, "create-once", "tool"
    )
    await control.repository.record_effect("create-once", "accepted", {"automation_id": 93})
    control.known_effects.append(
        {"tool": "automation_create", "side_effecting": True, "ok": True, "run_id": None}
    )
    await control.session.save("dispatched")
    await change_prompt_source(database, control.lease.conversation_id)

    with pytest.raises(WorkConflict, match="work_journal_source_changed") as caught:
        await control.session.save("response")
    outcome = await recover_failure(control, caught.value)
    assert outcome.reason is ExitReason.PAUSED
    assert outcome.failure and outcome.failure.code == "work_journal_source_changed"
    assert outcome.failure.retryable is False
    assert outcome.failure.diagnostics["effect_receipt_recorded"] is True
    assert control.current["id"] == original_id and control.current["state"] == "suspended"
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
            select(effects.c.state).where(effects.c.effect_key == "create-once")
        )
    assert saved[1] == "paused"
    assert json.loads(saved[0])["code"] == "work_journal_source_changed"
    assert "已执行的操作和回执已保留" in json.loads(notice)["text"]
    assert effect == "accepted"


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
async def test_unthrottled_plan_recovers_without_model_and_preserves_replay_guards(
    database, tmp_path
):
    control = await setup(database, tmp_path)
    await reserve(control, "previous", "final", {}, count=16)
    sender = Sender()
    wrapped = WorkDeliverySender(sender, control)
    messages = [OutboundMessage(f"part-{index}") for index in range(20)]
    await wrapped.plan(messages)
    assert control.current["sent_messages"] == 36 and not sender.messages
    # A reservation is idempotent, including plans larger than the retired window.
    await wrapped.plan(messages)
    assert control.current["sent_messages"] == 36
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(deliveries)
            .where(deliveries.c.id == control.session.call_key("final-plan"))
            .values(state="blocked", not_before=time.time() + 3600)
        )
        # Represent an old, never-reserved blocked plan; no send has occurred.
        await session.execute(
            update(work).where(work.c.id == control.current["id"]).values(sent_messages=16)
        )
    resumed = WorkSession(control, "contract")
    await resumed.restore(TurnTranscript((ChatMessage("user", "do not replace"),)))
    control.session = resumed
    assert await resume_delivery_plan(control, sender)
    assert sender.messages == [message.text for message in messages]
    assert control.current["sent_messages"] == 36
    assert control.requests_started == 0
    assert await resume_delivery_plan(control, sender)
    assert sender.messages == [message.text for message in messages]
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
    async with database.sessions() as session:
        assert await session.scalar(select(quota.c.bytes)) > 0


@pytest.mark.asyncio
async def test_accepted_fragment_and_unknown_next_fragment_are_never_replayed(database, tmp_path):
    control = await setup(database, tmp_path)
    sender = Sender(fail_at=2)
    wrapped = WorkDeliverySender(sender, control)
    await wrapped.plan([OutboundMessage("first"), OutboundMessage("second")])
    await wrapped.send(OutboundMessage("first"))
    with pytest.raises(TimeoutError):
        await wrapped.send(OutboundMessage("second"))
    control.session = WorkSession(control, "contract")
    await control.session.restore(TurnTranscript(()))
    with pytest.raises(WorkConflict, match="requires_reconciliation"):
        await resume_delivery_plan(control, sender)
    assert sender.messages == ["first", "second"]


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
    await control.settle(delivered=False, pending_inputs=False)
    assert control.current["state"] == "queued"
    assert control.outcome.reason.value == "waiting_input"
