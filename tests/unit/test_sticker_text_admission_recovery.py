"""Text dispatch and its optional illustration share the original durable claim."""

import asyncio
import sqlite3
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy import update
from sqlalchemy.exc import OperationalError
from tests.support.social_identity_cases import social_env
from tests.support.sticker_delivery import make_delivery, settle

from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.identity.db_models import PresenceModel
from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_schema_v1 import scope
from yuki.social.db_models import SocialEmojiIntentModel
from yuki.social.repository import sequence_prefix


def split_context(context, *, hard_max_messages=4):
    return replace(
        context,
        runtime_snapshot=SimpleNamespace(
            reply=SimpleNamespace(
                hard_max_messages=hard_max_messages,
                max_qq_message_chars=200,
                delay_min_seconds=0,
                delay_max_seconds=0,
            )
        ),
    )


def messages(env):
    return [payload["message"] for action, payload in env.bot.calls if action == "send_group_msg"]


@pytest.mark.parametrize("split", [False, True])
async def test_plain_text_never_wakes_optional_illustration_queue(
    database, tmp_path, monkeypatch, split
):
    env = await social_env(database, tmp_path)
    delivery, library = make_delivery(env)
    wake = Mock(wraps=delivery.wake)
    monkeypatch.setattr(delivery, "wake", wake)
    context = split_context(env.context, hard_max_messages=4 if split else 1)
    result = await env.service.execute("send", {"text": "first\nsecond"}, context)
    assert result["status"] == "succeeded"
    assert len(messages(env)) == (2 if split else 1)
    wake.assert_not_called()
    assert library.select_calls == 0


async def test_failed_atomic_admission_does_not_dispatch_text_or_leave_orphan_intent(
    database, tmp_path, monkeypatch
):
    env = await social_env(database, tmp_path)
    delivery, _ = make_delivery(env)
    admit = delivery.admit

    async def fail_before_claim_commit(*args, **kwargs):
        await admit(*args, **kwargs)
        raise OperationalError("uncommitted admission", (), sqlite3.OperationalError("lost"))

    arguments = {"text": "first", "emoji": {"goal": "happy"}}
    with monkeypatch.context() as patch:
        patch.setattr(delivery, "admit", fail_before_claim_commit)
        with pytest.raises(OperationalError):
            await env.service.execute("send", arguments, env.context)
    assert not messages(env)
    assert await delivery.intents.find(env.context.turn_id, env.context.call_id) is None
    result = await env.service.execute("send", arguments, env.context)
    assert result["status"] == "succeeded" and result["emoji"]["status"] == "accepted"
    await delivery.start()
    try:
        assert (await settle(delivery, env.context))["status"] == "succeeded"
        assert len(messages(env)) == 2
    finally:
        await delivery.close()


@pytest.mark.parametrize("split", [False, True])
@pytest.mark.parametrize("revocation", [None, "generation", "cancel"])
async def test_confirmed_text_interruption_recovers_only_original_illustration(
    database, tmp_path, monkeypatch, split, revocation
):
    env = await social_env(database, tmp_path)
    delivery, library = make_delivery(env)
    context = split_context(env.context) if split else env.context
    arguments = {"text": "first\nsecond" if split else "first", "emoji": {"goal": "happy"}}
    sequence = env.service._send_message_sequence

    async def stop_after_confirmed_text(*args, **kwargs):
        await sequence(*args, **kwargs)
        raise asyncio.CancelledError

    with monkeypatch.context() as patch:
        patch.setattr(env.service, "_send_message_sequence", stop_after_confirmed_text)
        with pytest.raises(asyncio.CancelledError):
            await env.service.execute("send", arguments, context)
    original = await delivery.intents.find(context.turn_id, context.call_id)
    assert original is not None, "confirmed text lost its illustration before admission"
    assert original.source["generation"] == 1
    if revocation is not None:
        async with database.immediate_session() as session:
            if revocation == "generation":
                await session.execute(
                    update(CanonicalConversationModel)
                    .where(CanonicalConversationModel.id == context.conversation_id)
                    .values(generation=2)
                )
            else:
                await session.execute(
                    scope.insert().values(
                        conversation_id=context.conversation_id, generation=1, cancel_epoch=1
                    )
                )
    replay = await env.service.execute("send", arguments, context)
    assert replay["status"] == "succeeded"
    assert replay["emoji"]["status"] == "accepted"
    assert len(messages(env)) == (2 if split else 1)
    await delivery.start()
    try:
        result = await settle(delivery, context)
        current = await delivery.intents.find(context.turn_id, context.call_id)
        assert current.source == original.source
        assert result["status"] == ("failed" if revocation else "succeeded")
        assert len(messages(env)) == (2 if split else 1) + int(revocation is None)
        assert library.select_calls == int(revocation is None)
    finally:
        await delivery.close()


async def test_inflight_text_does_not_release_image_or_block_another_intent(
    database, tmp_path, monkeypatch
):
    env = await social_env(database, tmp_path)
    delivery, _ = make_delivery(env)
    context = split_context(env.context)
    first, second, release_first, release_second = (asyncio.Event() for _ in range(4))
    call_api = env.bot.call_api

    async def hold_text(action, **params):
        parts = params.get("message", ())
        text = next((part["data"]["text"] for part in parts if part["type"] == "text"), "")
        if text == "first":
            first.set()
            await release_first.wait()
        elif text == "second":
            second.set()
            await release_second.wait()
        return await call_api(action, **params)

    monkeypatch.setattr(env.bot, "call_api", hold_text)
    await delivery.start()
    sending = asyncio.create_task(
        env.service.execute("send", {"text": "first\nsecond", "emoji": {"goal": "slow"}}, context)
    )
    try:
        await asyncio.wait_for(first.wait(), 3)
        intent = await delivery.intents.find(context.turn_id, context.call_id)
        assert intent is not None
        async with database.immediate_session() as session:
            original = await session.get(SocialEmojiIntentModel, intent.id)
            for index in range(99):
                session.add(
                    SocialEmojiIntentModel(
                        id=str(uuid4()),
                        source_turn_id=f"pending-text-{index}",
                        tool_call_id="image",
                        source_conversation_id=original.source_conversation_id,
                        payload_hash=original.payload_hash,
                        target_kind=original.target_kind,
                        target_id=original.target_id,
                        source_json=original.source_json,
                        intent_json=original.intent_json,
                        closed=False,
                        created_at=original.created_at,
                        updated_at=original.updated_at,
                    )
                )
        independent = replace(context, call_id="independent")
        await env.service.execute("send", {"emoji": {"goal": "fast"}}, independent)
        assert (await settle(delivery, independent))["status"] == "succeeded"
        assert len(messages(env)) == 1  # Only the independent image.
        from sqlalchemy import delete

        async with database.immediate_session() as session:
            await session.execute(
                delete(SocialEmojiIntentModel).where(
                    SocialEmojiIntentModel.source_turn_id.startswith("pending-text-")
                )
            )
        release_first.set()
        await asyncio.wait_for(second.wait(), 3)
        assert not (await delivery.intents.find(context.turn_id, context.call_id)).closed
        assert len(messages(env)) == 2  # Independent image, then first text.
        release_second.set()
        assert (await sending)["emoji"]["status"] == "accepted"
        assert (await settle(delivery, context))["status"] == "succeeded"
        assert len(messages(env)) == 4
        assert [part["type"] for part in messages(env)[-1]] == ["image"]
    finally:
        release_first.set()
        release_second.set()
        await asyncio.gather(sending, return_exceptions=True)
        await delivery.close()


@pytest.mark.parametrize("uncertain", [False, True])
async def test_text_failure_ends_its_dispatch_without_requiring_success_for_image(
    database, tmp_path, monkeypatch, uncertain
):
    env = await social_env(database, tmp_path)
    delivery, _ = make_delivery(env)
    context = split_context(env.context)
    call_api = env.bot.call_api

    class TextFailure(OSError):
        pass

    async def fail_text(action, **params):
        if any(part["type"] == "text" for part in params.get("message", ())):
            error = TextFailure("text outcome")
            error.uncertain = uncertain
            raise error
        return await call_api(action, **params)

    monkeypatch.setattr(env.bot, "call_api", fail_text)
    result = await env.service.execute(
        "send", {"text": "first\nsecond", "emoji": {"goal": "happy"}}, context
    )
    assert result["emoji"]["status"] == "accepted"
    await delivery.start()
    try:
        assert (await settle(delivery, context))["status"] == "succeeded"
        assert len(messages(env)) == 1 and messages(env)[0][0]["type"] == "image"
    finally:
        await delivery.close()


async def test_later_text_preflight_exit_does_not_leave_image_pending_forever(
    database, tmp_path, monkeypatch
):
    env = await social_env(database, tmp_path)
    delivery, _ = make_delivery(env)
    context = split_context(env.context)
    call_api = env.bot.call_api
    discovered = asyncio.Event()
    pending = delivery.intents.pending
    validate_route = env.router.validate_prepared_send
    validations = 0

    async def observe_pending(after=None):
        result = await pending(after)
        if not result and await delivery.intents.find(context.turn_id, context.call_id) is not None:
            discovered.set()
        return result

    async def preflight_after_discovery(*args, **kwargs):
        nonlocal validations
        validations += 1
        if validations == 2:
            await asyncio.wait_for(discovered.wait(), 3)
        return await validate_route(*args, **kwargs)

    async def disable_presence_after_first_text(action, **params):
        result = await call_api(action, **params)
        if any(
            part["type"] == "text" and part["data"]["text"] == "first"
            for part in params.get("message", ())
        ):
            async with database.immediate_session() as session:
                await session.execute(
                    update(PresenceModel)
                    .where(PresenceModel.id == env.presence)
                    .values(status="disabled")
                )
        return result

    monkeypatch.setattr(env.bot, "call_api", disable_presence_after_first_text)
    monkeypatch.setattr(delivery.intents, "pending", observe_pending)
    monkeypatch.setattr(env.router, "validate_prepared_send", preflight_after_discovery)
    await delivery.start()
    try:
        result = await env.service.execute(
            "send", {"text": "first\nsecond", "emoji": {"goal": "happy"}}, context
        )
        assert result["emoji"]["status"] == "accepted"
        assert len(messages(env)) == 1
        result = await settle(delivery, context)
        assert result["status"] == "failed"
        assert result["error"]
        assert len(messages(env)) == 1
    finally:
        await delivery.close()


async def test_obsolete_source_closes_while_its_text_transport_is_still_pending(
    database, tmp_path, monkeypatch
):
    env = await social_env(database, tmp_path)
    delivery, library = make_delivery(env)
    entered, release = asyncio.Event(), asyncio.Event()
    call_api = env.bot.call_api

    async def pending_text(action, **params):
        entered.set()
        await release.wait()
        return await call_api(action, **params)

    monkeypatch.setattr(env.bot, "call_api", pending_text)
    sending = asyncio.create_task(
        env.service.execute(
            "send", {"text": "pending text", "emoji": {"goal": "happy"}}, env.context
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 3)
        async with database.immediate_session() as session:
            await session.execute(
                update(CanonicalConversationModel)
                .where(CanonicalConversationModel.id == env.context.conversation_id)
                .values(generation=2)
            )
        await delivery.start()
        result = await settle(delivery, env.context)
        assert result["status"] == "failed" and result["error"] == "emoji_source_obsolete"
        assert library.select_calls == 0 and not messages(env)
    finally:
        release.set()
        await asyncio.gather(sending, return_exceptions=True)
        await delivery.close()


@pytest.mark.parametrize("cancel_source", [False, True])
@pytest.mark.parametrize("clean_shutdown", [False, True])
async def test_shutdown_between_text_parts_preserves_original_image_without_resending_text(
    database, tmp_path, monkeypatch, cancel_source, clean_shutdown
):
    env = await social_env(database, tmp_path)
    delivery, _ = make_delivery(env)
    repository = WorkRepository(database)
    lease = await repository.acquire(env.context.conversation_id, 1)
    await repository.release(lease)
    context = split_context(env.context)
    context.runtime_snapshot.reply.delay_min_seconds = 60
    context.runtime_snapshot.reply.delay_max_seconds = 60
    first_finished = asyncio.Event()
    effect = env.service._effect

    async def observe_first_effect(*args, **kwargs):
        result = await effect(*args, **kwargs)
        if args[2].sequence_part_index == 0:
            asyncio.get_running_loop().call_soon(first_finished.set)
        return result

    monkeypatch.setattr(env.service, "_effect", observe_first_effect)
    arguments = {"text": "first\nsecond", "emoji": {"goal": "happy"}}
    sending = asyncio.create_task(env.service.execute("send", arguments, context))
    await asyncio.wait_for(first_finished.wait(), 3)
    original = await delivery.intents.find(context.turn_id, context.call_id)
    assert original is not None
    if cancel_source:
        await repository.cancel(context.conversation_id)
    with monkeypatch.context() as patch:
        if not clean_shutdown:
            # A hard-killed owner has no chance to persist its cancellation cleanup.
            patch.setattr(env.service.receipts, "stop_sequence", AsyncMock())
        sending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sending
    assert await env.service.receipts.recover_interrupted() == (0 if clean_shutdown else 1)
    parent = await env.service.receipts.get(original.source["after_operation_id"])
    assert parent.status == "failed"
    assert parent.error_category == ("CancelledError" if clean_shutdown else "process_interrupted")
    suffix = sequence_prefix(context.call_id)
    first = await env.service.receipts.find(context.turn_id, f"{suffix}0")
    second = await env.service.receipts.find(context.turn_id, f"{suffix}1")
    assert first.status == "succeeded" and second.status == "prepared"
    await delivery.close()
    restarted, _ = make_delivery(env)
    await restarted.start()
    try:
        result = await settle(restarted, context)
        current = await restarted.intents.find(context.turn_id, context.call_id)
        assert current.source == original.source
        assert result["status"] == ("failed" if cancel_source else "succeeded")
        assert len(messages(env)) == 1 + int(not cancel_source)
        replay = await env.service.execute("send", arguments, context)
        assert replay["status"] == "uncertain"  # The second text was never dispatched.
        assert len(messages(env)) == 1 + int(not cancel_source)
    finally:
        await restarted.close()


async def test_startup_keeps_fully_confirmed_text_sequence_parent_facts(database, tmp_path):
    env = await social_env(database, tmp_path)
    delivery, _ = make_delivery(env)
    context = split_context(env.context)
    arguments = {"text": "first\nsecond", "emoji": {"goal": "happy"}}
    result = await env.service.execute("send", arguments, context)
    assert result["status"] == "succeeded"
    original = await delivery.intents.find(context.turn_id, context.call_id)
    parent_id = original.source["after_operation_id"]
    assert (await env.service.receipts.get(parent_id)).status == "prepared"
    assert await env.service.receipts.recover_interrupted() == 0
    assert (await env.service.receipts.get(parent_id)).status == "prepared"
    await delivery.start()
    try:
        assert (await settle(delivery, context))["status"] == "succeeded"
        assert len(messages(env)) == 3
        replay = await env.service.execute("send", arguments, context)
        assert replay["status"] == "succeeded" and replay["emoji"]["status"] == "succeeded"
        assert len(messages(env)) == 3
    finally:
        await delivery.close()
