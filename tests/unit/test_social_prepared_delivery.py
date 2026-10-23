"""Selected routes send once; batched replay preserves original confirmed content."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.support.social_identity_cases import social_env

from yuki.admin.models import ReplyRuntimeConfig
from yuki.identity.routing import RouteSendError
from yuki.persistence.models import ChatEventModel
from yuki.social.db_models import SocialOperationModel
from yuki.social.models import OperationStatus, SocialTarget
from yuki.social.repository import payload_hash


@pytest.mark.parametrize("sequence", [False, True])
async def test_original_delivery_payload_hashes_are_prepared_before_writer(
    database, tmp_path, monkeypatch, sequence
):
    import yuki.social.repository as social_repository

    env = await social_env(database, tmp_path)
    original_hash = social_repository.payload_hash
    original_receipt = social_repository.SocialOperationRepository._receipt
    hashed = []

    def prepare_hash(*args):
        assert not database._writer.locked(), "body hashing must precede writer admission"
        hashed.append(args[0])
        return original_hash(*args)

    monkeypatch.setattr(social_repository, "payload_hash", prepare_hash)

    def decode_receipt(row, *, include_prepared=True):
        if include_prepared and row.prepared_json is not None:
            assert not database._writer.locked(), "original send plans are decoded after the writer"
        return original_receipt(row, include_prepared=include_prepared)

    monkeypatch.setattr(
        social_repository.SocialOperationRepository, "_receipt", staticmethod(decode_receipt)
    )
    context = replace(
        env.context,
        runtime_snapshot=SimpleNamespace(reply=ReplyRuntimeConfig(0, 0, 1800, 20)),
    )
    args = {"text": "first\nsecond" if sequence else "one original message"}
    sent = await env.service.execute("send", args, context)
    assert sent["status"] == "succeeded" and hashed
    before = tuple(env.bot.calls)
    assert await env.service.execute("send", args, context) == sent
    assert tuple(env.bot.calls) == before


@pytest.mark.parametrize("current_entry", [False, True])
async def test_known_group_route_sends_without_remote_membership_preflight(
    database, tmp_path, current_entry
):
    env = await social_env(database, tmp_path)
    async with database.read_session() as reader:
        source = await reader.scalar(select(ChatEventModel))
    context = replace(
        env.context,
        trigger_event_id=source.id if current_entry else None,
        presence_id=env.presence if current_entry else None,
    )
    env.bot.missing.add(env.bot.self_id)
    env.bot.calls.clear()
    result = await env.service.execute("send", {"text": "actual delivery"}, context)
    assert result["status"] == "succeeded"
    assert [action for action, _ in env.bot.calls] == ["send_group_msg"]
    # Multi-candidate discovery still evaluates reachability; send does not invent a fallback.
    with pytest.raises(RouteSendError, match="group_unavailable"):
        await env.router.accessible_group_connections(env.space)


async def test_sequence_replay_preserves_original_parts_and_origin_privacy(database, tmp_path):
    env = await social_env(database, tmp_path)
    context = replace(
        env.context,
        call_id="sequence",
        runtime_snapshot=SimpleNamespace(reply=ReplyRuntimeConfig(0, 0, 1800, 20)),
    )
    args = {"text": "first\nsecond"}
    sent = await env.service.execute("send", args, context)
    assert sent["status"] == "succeeded" and sent["sent_messages"] == 2
    replay = await env.service.execute("send", args, context)
    assert replay == sent
    assert [part["delivered_text"] for part in replay["parts"]] == ["first", "second"]
    isolated = await env.service.execute("send", args, replace(context, origin="another_source"))
    assert isolated["status"] == "succeeded"
    assert all(not part["delivered_text_available"] for part in isolated["parts"])


async def test_sequence_keeps_configured_delay_between_confirmed_parts(
    database, tmp_path, monkeypatch
):
    env = await social_env(database, tmp_path)
    context = replace(
        env.context,
        runtime_snapshot=SimpleNamespace(reply=ReplyRuntimeConfig(1.5, 1.5, 1800, 10)),
    )
    sends_at_sleep = []

    async def delay(seconds):
        assert seconds == 1.5
        sends_at_sleep.append(sum(action == "send_group_msg" for action, _ in env.bot.calls))

    sleeper = AsyncMock(side_effect=delay)
    monkeypatch.setattr("yuki.social.service.asyncio.sleep", sleeper)
    result = await env.service.execute("send", {"text": "first\nsecond\nthird"}, context)
    assert result["status"] == "succeeded"
    assert sends_at_sleep == [1, 2]


async def test_unmigrated_old_sequence_payload_cannot_acquire_a_new_plan(database, tmp_path):
    env = await social_env(database, tmp_path)
    env.bot.calls.clear()
    context = replace(
        env.context,
        runtime_snapshot=SimpleNamespace(reply=ReplyRuntimeConfig(0, 0, 1800, 20)),
    )
    args = {"text": "first\nsecond"}
    parent = await env.service.receipts.prepare(
        source_turn_id=context.turn_id,
        tool_call_id=context.call_id,
        source_conversation_id=context.conversation_id,
        action="send_message_sequence",
        target=SocialTarget(kind="space", id=UUID(env.space)),
        payload={"original": args, "chunks": ("first", "second")},
        planned_parts=2,
    )
    from yuki.social.models import SocialError

    with pytest.raises(SocialError, match="idempotency_conflict"):
        await env.service.execute("send", args, context)
    current = await env.service.receipts.get(parent.operation_id)
    assert current.payload_hash == payload_hash(
        "send_message_sequence",
        parent.target,
        {"original": args, "chunks": ("first", "second")},
        context.conversation_id,
    )
    assert current.status is OperationStatus.PREPARED
    assert env.bot.calls == []


async def test_cancel_after_ack_and_receipt_commit_replays_without_another_send(
    database, tmp_path, monkeypatch
):
    env = await social_env(database, tmp_path)
    env.bot.calls.clear()
    finish, commit = env.service.receipts.finish, AsyncSession.commit

    async def mark_receipt(*args, **kwargs):
        receipt = await finish(*args, **kwargs)
        if kwargs["status"] is OperationStatus.SUCCEEDED:
            kwargs["session"].info["cancel_after_social_commit"] = True
        return receipt

    async def cancel_after_commit(session):
        await commit(session)
        if session.info.pop("cancel_after_social_commit", False):
            raise asyncio.CancelledError("caller cancelled after confirmed receipt commit")

    monkeypatch.setattr(env.service.receipts, "finish", mark_receipt)
    monkeypatch.setattr(AsyncSession, "commit", cancel_after_commit)
    args = {"text": "confirmed before cancellation"}
    with pytest.raises(asyncio.CancelledError):
        await env.service.execute("send", args, env.context)
    result = await env.service.execute("send", args, env.context)
    assert result["status"] == "succeeded"
    assert result["delivered_text"] == args["text"]
    assert [action for action, _ in env.bot.calls] == ["send_group_msg"]
    async with database.read_session() as reader:
        receipt = await reader.get(SocialOperationModel, result["operation_id"])
        outgoing = list(
            await reader.scalars(
                select(ChatEventModel).where(ChatEventModel.direction == "outbound")
            )
        )
        assert len(outgoing) == 1 and receipt.event_id == outgoing[0].id == result["event_id"]


async def test_failed_work_delivery_rolls_back_normal_settlement_without_resending(
    database, tmp_path, monkeypatch
):
    from tests.support.work_runner_helpers import case

    import yuki.runtime.delivery_intents as delivery_intents
    from yuki.runtime.work_activation import current_work_control
    from yuki.runtime.work_recovery_schema import deliveries

    probe = await case(database, tmp_path, [])
    env = probe.env
    original = delivery_intents.record
    attempts = 0

    async def fail_first_record(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("work receipt unavailable after the acknowledged send")
        await original(*args, **kwargs)

    monkeypatch.setattr(delivery_intents, "record", fail_first_record)
    env.bot.calls.clear()
    token = current_work_control.set(probe.control)
    try:
        args = {"text": "one acknowledged transport effect"}
        result = await env.service.execute("send", args, env.context)
        assert result["status"] == "succeeded"
        async with database.read_session() as reader:
            outgoing = list(
                await reader.scalars(
                    select(ChatEventModel.id).where(ChatEventModel.direction == "outbound")
                )
            )
            receipt = await reader.get(SocialOperationModel, result["operation_id"])
            delivery = (
                (await reader.execute(select(deliveries).where(deliveries.c.id == receipt.id)))
                .mappings()
                .one()
            )
        # The first normal ledger/Social/Work transaction rolled back as one unit.
        # Reconciliation retains the actual send receipt without inventing its lost ledger row.
        assert outgoing == [] and receipt.event_id is None
        assert delivery["state"] == "accepted"
        assert await env.service.execute("send", args, env.context) == result
        assert [action for action, _ in env.bot.calls] == ["send_group_msg"]
    finally:
        current_work_control.reset(token)
        await probe.repository.release(probe.control.lease)
