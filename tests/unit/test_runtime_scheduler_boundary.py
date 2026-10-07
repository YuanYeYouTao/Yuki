"""Runtime drives accepted waits independently of automation or new admission."""

import asyncio
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
async def test_time_wait_is_delivered_while_another_root_activation_is_blocked(
    database, tmp_path, monkeypatch
):
    from datetime import UTC, datetime

    import qq_ai_bot.runtime.work_scheduler as scheduler_module
    import qq_ai_bot.runtime.work_wait as wait_module

    clock = [datetime(2030, 1, 1, tzinfo=UTC).timestamp()]
    monkeypatch.setattr(wait_module, "time", SimpleNamespace(time=lambda: clock[0]))
    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    lease = await repository.acquire(env.context.conversation_id, 1)
    assert lease
    source = {
        "origin": "user_message",
        "principal_kind": "person",
        "actor_person_id": env.person,
    }
    blocked = await repository.accept(lease, source_key="blocked-root", source=source, goal="run")
    timer = await repository.accept(lease, source_key="later-timer", source=source, goal="wait")
    await WorkWaitRepository(repository).register(
        lease,
        work_id=timer["id"],
        source=source,
        call_key="timer-during-model",
        mode="any",
        conditions=[{"kind": "time_due", "after_seconds": 5}],
        deadline_at=None,
    )
    await repository.transition(lease, timer["id"], timer["revision"], "waiting_external")
    await repository.release(lease)
    entered, release, tick, delivered = (asyncio.Event() for _ in range(4))

    async def resume(item):
        assert item["id"] == blocked["id"]
        entered.set()
        await release.wait()

    async def sleep(_seconds):
        await tick.wait()
        tick.clear()

    original_poll = WorkWaitRepository.deliver_due

    async def observe_poll(self):
        result = await original_poll(self)
        if result:
            delivered.set()
        return result

    monkeypatch.setattr(WorkWaitRepository, "deliver_due", observe_poll)
    monkeypatch.setattr(
        scheduler_module,
        "asyncio",
        SimpleNamespace(
            create_task=asyncio.create_task,
            sleep=sleep,
            wait=asyncio.wait,
            gather=asyncio.gather,
            shield=asyncio.shield,
            FIRST_COMPLETED=asyncio.FIRST_COMPLETED,
            CancelledError=asyncio.CancelledError,
        ),
    )
    scheduler = WorkScheduler(
        repository, SimpleNamespace(resume=resume, last_error=None), chat_admission_enabled=False
    )
    await scheduler.start()
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        clock[0] += 6
        tick.set()
        await asyncio.wait_for(delivered.wait(), timeout=0.5)
        assert not release.is_set()
        assert (await scheduler.health())["wait_running"] is True
        current = await repository.get(timer["id"])
        assert current["state"] == "queued"
        assert current["model_requests"] == current["tool_calls"] == 0
        async with database.sessions() as session:
            mailbox = list(
                await session.scalars(select(inputs.c.id).where(inputs.c.work_id == timer["id"]))
            )
        assert len(mailbox) == 1
    finally:
        workers = (scheduler._worker, scheduler._selection_worker, scheduler._wait_worker)
        await scheduler.close()
    assert all(task is not None and task.done() for task in workers)
    assert scheduler.running is False


@pytest.mark.asyncio
async def test_wait_loop_exit_is_visible_and_collects_blocked_selection(database, monkeypatch):
    scheduler = WorkScheduler(
        WorkRepository(database),
        SimpleNamespace(resume=AsyncMock(), last_error=None),
        chat_admission_enabled=False,
    )
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def selection():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def failed_wait_loop():
        await entered.wait()
        raise RuntimeError("wait maintenance unexpectedly stopped")

    monkeypatch.setattr(scheduler, "_selection_loop", selection)
    monkeypatch.setattr(scheduler, "_wait_loop", failed_wait_loop)
    await scheduler.start()
    try:
        await asyncio.wait_for(asyncio.shield(scheduler._worker), timeout=1)
        assert cancelled.is_set()
        health = await scheduler.health()
        assert health["running"] is health["wait_running"] is False
        assert health["last_error_category"] == health["wait_error_category"] == "RuntimeError"
    finally:
        await scheduler.close()


@pytest.mark.asyncio
async def test_close_during_failed_wait_loop_cleanup_does_not_cancel_selection_twice(
    database, monkeypatch
):
    scheduler = WorkScheduler(
        WorkRepository(database),
        SimpleNamespace(resume=AsyncMock(), last_error=None),
        chat_admission_enabled=False,
    )
    entered, cleanup_entered, release_cleanup = (asyncio.Event() for _ in range(3))
    close_entered, cleaned, interrupted = (asyncio.Event() for _ in range(3))

    async def selection():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_entered.set()
            try:
                await release_cleanup.wait()
                cleaned.set()
            except asyncio.CancelledError:
                interrupted.set()
                raise

    async def failed_wait_loop():
        await entered.wait()
        raise RuntimeError("wait maintenance unexpectedly stopped")

    async def close():
        close_entered.set()
        await scheduler.close()

    monkeypatch.setattr(scheduler, "_selection_loop", selection)
    monkeypatch.setattr(scheduler, "_wait_loop", failed_wait_loop)
    await scheduler.start()
    shutdown = None
    try:
        await asyncio.wait_for(cleanup_entered.wait(), 1)
        selection_task = scheduler._selection_worker
        shutdown = asyncio.create_task(close())
        await asyncio.wait_for(close_entered.wait(), 1)
        assert not shutdown.done()
        release_cleanup.set()
        await asyncio.wait_for(shutdown, 1)
        assert cleaned.is_set() and not interrupted.is_set()
        assert selection_task is not None and selection_task.done()
        assert scheduler._selection_worker is scheduler._wait_worker is None
        health = await scheduler.health()
        assert health["running"] is health["wait_running"] is False
        assert health["last_error_category"] == health["wait_error_category"] == "RuntimeError"
    finally:
        release_cleanup.set()
        if shutdown is not None:
            await shutdown
        else:
            await scheduler.close()


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
    await scheduler.poll_waits_once()
    await scheduler.drain_once()
    await scheduler.poll_waits_once()
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
    await scheduler.poll_waits_once()
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
            media=(OutboundMedia(AttachmentKind.IMAGE, content=b"image"),),
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
            ],
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("group", [False, True])
async def test_persisted_audio_is_rejected_before_gateway_dispatch(group):
    bot = SimpleNamespace(call_api=AsyncMock(return_value={"message_id": 901}))
    sender = OneBotRouteSender(bot, group=group, target_id="42")
    with pytest.raises(ValueError, match="unsupported_persisted_delivery_media"):
        await sender.send(
            OutboundMessage(
                text="not a text fallback",
                media=(
                    OutboundMedia(AttachmentKind.IMAGE, content=b"image"),
                    OutboundMedia(AttachmentKind.AUDIO, content=b"audio"),
                ),
            )
        )
    bot.call_api.assert_not_awaited()
