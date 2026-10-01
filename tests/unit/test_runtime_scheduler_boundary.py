"""Runtime drives accepted waits independently of automation or new admission."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from tests.support.social_identity_cases import social_env

from qq_ai_bot.adapters.onebot.sender import OneBotRouteSender
from qq_ai_bot.domain.messages import AttachmentKind, OutboundMedia, OutboundMessage
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.runtime.work_scheduler import WorkScheduler
from qq_ai_bot.runtime.work_schema_v1 import inputs
from qq_ai_bot.runtime.work_wait import WorkWaitRepository


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_admission_enabled", [False, True])
async def test_scheduler_delivers_original_time_wait_without_automation(
    database, tmp_path, chat_admission_enabled
):
    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    lease = await repository.acquire(env.context.conversation_id, 1)
    assert lease
    async with database.sessions() as session:
        original = await session.scalar(select(ChatEventModel))
    source = {
        "principal_kind": "person",
        "actor_person_id": env.person,
        "actor_user_id": original.sender_user_id,
        "origin": "user_message",
        "trigger_event_id": original.id,
        "conversation_id": env.context.conversation_id,
        "generation": 1,
        "presence_id": env.presence,
        "bot_user_id": original.bot_user_id,
    }
    item = await repository.accept(lease, source_key="timer", source=source, goal="wait")
    await WorkWaitRepository(repository).register(
        lease,
        work_id=item["id"],
        source=source,
        call_key="original-time-wait",
        mode="any",
        conditions=[{"kind": "time_due", "at": "2000-01-01T00:00:00+00:00"}],
        deadline_at=None,
    )
    await repository.transition(lease, item["id"], item["revision"], "waiting_external")
    await repository.release(lease)
    resumer = SimpleNamespace(resume=AsyncMock(), last_error=None)
    scheduler = WorkScheduler(repository, resumer, chat_admission_enabled=chat_admission_enabled)
    await scheduler.drain_once()
    await scheduler.drain_once()
    assert resumer.resume.await_count == 2
    assert all(call.args[0]["id"] == item["id"] for call in resumer.resume.await_args_list)
    current = await repository.get(item["id"])
    assert current["state"] == "queued"
    assert current["model_requests"] == current["tool_calls"] == 0
    assert current["source_key"] == "timer"
    async with database.sessions() as session:
        mailbox = list(
            await session.scalars(select(inputs.c.id).where(inputs.c.work_id == item["id"]))
        )
    assert len(mailbox) == 1


@pytest.mark.asyncio
async def test_failed_wait_poll_does_not_block_unrelated_queued_work(
    database, tmp_path, monkeypatch
):
    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    lease = await repository.acquire(env.context.conversation_id, 1)
    assert lease
    item = await repository.accept(
        lease, source_key="unrelated-root", source={"origin": "user_message"}, goal="continue"
    )
    await repository.release(lease)
    resumer = SimpleNamespace(resume=AsyncMock(), last_error=None)
    scheduler = WorkScheduler(repository, resumer, chat_admission_enabled=False)
    monkeypatch.setattr(
        WorkWaitRepository, "deliver_due", AsyncMock(side_effect=RuntimeError("wait unavailable"))
    )
    await scheduler.drain_once()
    resumer.resume.assert_awaited_once()
    assert resumer.resume.await_args.args[0]["id"] == item["id"]
    assert (await scheduler.health())["wait_error_category"] == "RuntimeError"
    current = await repository.get(item["id"])
    assert current["model_requests"] == current["tool_calls"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("group", [False, True])
async def test_persisted_delivery_keeps_original_text_media_and_verified_connection(group):
    bot = SimpleNamespace(call_api=AsyncMock(return_value={"message_id": 901}))
    sender = OneBotRouteSender(bot, group=group, target_id="42")
    receipt = await sender.send(
        OutboundMessage(
            text="旧交付",
            reply_to_message_id="17",
            media=(
                OutboundMedia(AttachmentKind.IMAGE, content=b"image"),
                OutboundMedia(AttachmentKind.AUDIO, content=b"audio"),
            ),
        )
    )
    assert receipt.platform_message_id == "901"
    bot.call_api.assert_awaited_once_with(
        "send_group_msg" if group else "send_private_msg",
        **{
            "group_id" if group else "user_id": 42,
            "message": [
                {"type": "reply", "data": {"id": "17"}},
                {"type": "text", "data": {"text": "旧交付"}},
                {"type": "image", "data": {"file": "base64://aW1hZ2U="}},
                {"type": "record", "data": {"file": "base64://YXVkaW8="}},
            ],
        },
    )
