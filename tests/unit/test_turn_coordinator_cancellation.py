"""Task cancellation keeps its real source across the shared turn stages."""

import asyncio

import pytest

from yuki.automation.models import TurnOrigin
from yuki.services.turn_coordinator import (
    ConversationTurnCoordinator,
    TurnInterruptedError,
    TurnSupersededError,
)


@pytest.mark.parametrize("stage", ["admission", "generation"])
@pytest.mark.parametrize("source", ["external", "new_message", "stop", "boundary"])
async def test_external_cancellation_is_not_a_coordinator_supersession(stage, source):
    coordinator = ConversationTurnCoordinator()
    token = await coordinator.notify_message("original-conversation")
    entered = asyncio.Event()

    async def original():
        async with coordinator.track(token, stage):
            entered.set()
            await asyncio.Event().wait()

    owner = asyncio.create_task(original())
    await entered.wait()
    if source == "external":
        owner.cancel()
    elif source == "new_message":
        await coordinator.notify_message("original-conversation", preempt_private=True)
    elif source == "stop":
        assert await coordinator.cancel_interruptible("original-conversation")
    else:
        assert await coordinator.cancel_running_before_boundary("original-conversation")
    expected = (
        asyncio.CancelledError
        if source == "external"
        else TurnInterruptedError
        if stage == "admission"
        else TurnSupersededError
    )
    with pytest.raises(expected):
        await owner
    assert owner.cancelled() == (source == "external")


@pytest.mark.parametrize("stage", ["admission", "generation"])
@pytest.mark.parametrize("source", ["stop", "boundary", "new_message", "autonomous_message"])
async def test_self_skipped_cancellation_does_not_mask_a_later_external_cancel(stage, source):
    coordinator = ConversationTurnCoordinator()
    origin = (
        TurnOrigin.AUTONOMOUS_GROUP if source == "autonomous_message" else TurnOrigin.USER_MESSAGE
    )
    token = await coordinator.notify_message("original-conversation", origin=origin)
    entered = asyncio.Event()

    async def original():
        async with coordinator.track(token, stage):
            if source == "stop":
                assert not await coordinator.cancel_interruptible("original-conversation")
            elif source == "boundary":
                assert not await coordinator.cancel_running_before_boundary("original-conversation")
            else:
                await coordinator.notify_message("original-conversation", preempt_private=True)
            entered.set()
            await asyncio.Event().wait()

    owner = asyncio.create_task(original())
    await entered.wait()
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    assert owner.cancelled()
