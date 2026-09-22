"""Durable delivery recovery must never spend another model request or resend acceptance."""

import time

import pytest
from sqlalchemy import select, update
from tests.support.social_identity_cases import social_env

from qq_ai_bot.domain.messages import ChatMessage, OutboundMessage, OutboundSendReceipt
from qq_ai_bot.runtime.delivery_intents import record, reserve
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_delivery import WorkDeliverySender, resume_delivery_plan
from qq_ai_bot.runtime.work_journal import JournalUnavailable
from qq_ai_bot.runtime.work_recovery_schema import deliveries, quota
from qq_ai_bot.runtime.work_repository import WorkConflict, WorkRepository
from qq_ai_bot.runtime.work_session import WorkSession
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
        from qq_ai_bot.runtime.work_schema_v1 import work

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
