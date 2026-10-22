"""Open groups use the same implicit qualification while semantic participation is enabled."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import update
from tests.conftest import MemorySender
from tests.unit.test_auto_reply_merge import _admissions, _open_group_scene, _wait_for_worker

from yuki.conversation.initiative_sources import source_revision
from yuki.conversation.ordinary_admission import OrdinaryParticipationBinding
from yuki.identity.db_models import CanonicalSpaceModel
from yuki.llm.fake import FakeLLMProvider


async def _scene(database, tmp_path, *, semantic=True):
    provider = FakeLLMProvider(lambda _: "NO_REPLY")
    env, harness, worker, message = await _open_group_scene(
        database,
        tmp_path,
        provider,
        debounce=0.01,
        conversation_semantic_participation_enabled=semantic,
    )
    classify = AsyncMock(return_value=None)
    participation = SimpleNamespace(
        continuation_for_event=classify,
        binding_for_event=AsyncMock(return_value=None),
        continuation_current=AsyncMock(return_value=True),
        on_ordinary_admitted=AsyncMock(),
    )
    harness.processor.set_participation(participation)
    return env, harness, worker, message, provider, participation


@pytest.mark.parametrize("require_mention", [False, True])
async def test_open_group_does_not_bypass_closed_implicit_relationship(
    database, tmp_path, require_mention
):
    env, harness, worker, message, provider, participation = await _scene(database, tmp_path)
    async with database.immediate_session() as writer:
        await writer.execute(
            update(CanonicalSpaceModel)
            .where(CanonicalSpaceModel.id == env.space)
            .values(require_mention=require_mention)
        )
    sender = MemorySender()
    try:
        result = await harness.processor.handle(message(1, "大家继续聊"), sender)
        assert result.reason == "group_observed"
        participation.continuation_for_event.assert_awaited_once()
        assert not await _admissions(database)
        assert not provider.requests
        assert not sender.messages
        assert not any(
            action in {"send_group_msg", "send_private_msg"} for action, _ in env.bot.calls
        )
    finally:
        await worker.close()


@pytest.mark.parametrize("reason", ["continuation", "semantic_invitation"])
async def test_open_group_accepts_real_continuation_or_new_invitation(database, tmp_path, reason):
    env, harness, worker, message, provider, participation = await _scene(database, tmp_path)

    async def classify(event_id):
        event = await harness.ledger.get_event(event_id)
        return (
            OrdinaryParticipationBinding(
                env.context.conversation_id,
                1,
                event_id,
                str(source_revision(event)),
                "existing-discussion",
                env.person,
            ),
            reason,
        )

    participation.continuation_for_event.side_effect = classify
    try:
        result = await harness.processor.handle(message(1, "那这个问题呢"), MemorySender())
        assert result.reason == "ordinary_deferred"
        await _wait_for_worker(worker, next(iter(worker._states)))
        admitted = await _admissions(database)
        assert len(admitted) == 1
        assert len(provider.requests) == 1
        participation.on_ordinary_admitted.assert_awaited_once()
    finally:
        await worker.close()


async def test_direct_mention_stays_immediate_when_implicit_relationship_is_closed(
    database, tmp_path
):
    _env, harness, worker, message, provider, participation = await _scene(database, tmp_path)
    try:
        await harness.processor.handle(message(1, "现在问你", mention=True), MemorySender())
        assert len(provider.requests) == 1
        participation.continuation_for_event.assert_not_awaited()
    finally:
        await worker.close()


async def test_semantic_disabled_keeps_open_group_behavior(database, tmp_path):
    _env, harness, worker, message, provider, participation = await _scene(
        database, tmp_path, semantic=False
    )
    try:
        result = await harness.processor.handle(message(1, "开放群普通消息"), MemorySender())
        assert result.reason == "ordinary_deferred"
        await _wait_for_worker(worker, next(iter(worker._states)))
        assert len(provider.requests) == 1
        participation.continuation_for_event.assert_not_awaited()
    finally:
        await worker.close()


async def test_queued_semantic_invitation_is_rechecked_after_correction(database, tmp_path):
    env, harness, worker, message, provider, participation = await _scene(database, tmp_path)
    closed = False

    async def classify(event_id):
        if closed:
            return None
        event = await harness.ledger.get_event(event_id)
        return (
            OrdinaryParticipationBinding(
                env.context.conversation_id,
                1,
                event_id,
                str(source_revision(event)),
                "original-invitation",
                env.person,
            ),
            "semantic_invitation",
        )

    participation.continuation_for_event.side_effect = classify
    try:
        result = await harness.processor.handle(message(1, "原来的邀请"), MemorySender())
        assert result.reason == "ordinary_deferred"
        closed = True
        await _wait_for_worker(worker, next(iter(worker._states)))
        assert not await _admissions(database)
        assert not provider.requests
    finally:
        await worker.close()
