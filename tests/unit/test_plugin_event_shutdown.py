"""Cancellation-resistant Hook cleanup must not strand the EventBus lifecycle."""

from __future__ import annotations

import asyncio

import pytest

from qq_ai_bot.plugin_host.event_bus import PluginEventBus
from yuki_plugin_sdk.events import EventEnvelope, EventName


def _subscribe(bus: PluginEventBus, handler) -> None:
    bus.subscribe(
        plugin_id="observer",
        hook_id="observe",
        event=EventName.TURN_ADMITTED,
        handler=handler,
    )


@pytest.mark.asyncio
async def test_shutdown_timeout_drains_old_notifications_and_can_restart_after_cleanup():
    bus = PluginEventBus(default_timeout_seconds=60)
    entered, cancelling, cleanup_finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    seen: list[int] = []

    async def handler(event):
        sequence = event.payload["sequence"]
        seen.append(sequence)
        if sequence != 1:
            return
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelling.set()
            # A real Hook may catch cancellation to finish its cleanup after
            # the Host's bounded shutdown deadline; it eventually returns.
            await cleanup_finished.wait()

    _subscribe(bus, handler)
    await bus.start()
    consumer = bus._consumer
    assert consumer is not None
    try:
        assert bus.enqueue_notification(
            EventEnvelope(name=EventName.TURN_ADMITTED, payload={"sequence": 1})
        )
        await entered.wait()
        assert bus.enqueue_notification(
            EventEnvelope(name=EventName.TURN_ADMITTED, payload={"sequence": 2})
        )
        await asyncio.wait_for(bus.close(), timeout=3)
        assert cancelling.is_set() and not consumer.done()
        assert not bus.enqueue_notification(
            EventEnvelope(name=EventName.TURN_ADMITTED, payload={"sequence": 99})
        )
        cleanup_finished.set()
        await asyncio.wait_for(asyncio.gather(consumer, return_exceptions=True), timeout=1)
        await asyncio.wait_for(bus._queue.join(), timeout=1)
        assert seen == [1]
        assert (await bus.health())["pending"] == 0
        await bus.start()
        assert bus.enqueue_notification(
            EventEnvelope(name=EventName.TURN_ADMITTED, payload={"sequence": 3})
        )
        await asyncio.wait_for(bus._queue.join(), timeout=1)
        assert seen == [1, 3]
    finally:
        cleanup_finished.set()
        await asyncio.gather(consumer, return_exceptions=True)
        await bus.close()


@pytest.mark.asyncio
async def test_unsubscribed_hook_cleanup_cannot_report_success_to_waited_sdk_publisher():
    bus = PluginEventBus(default_timeout_seconds=60)
    entered, cancelling, cleanup_finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def handler(_event):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelling.set()
            await cleanup_finished.wait()

    _subscribe(bus, handler)
    publisher = asyncio.create_task(bus.publish(EventEnvelope(name=EventName.TURN_ADMITTED)))
    try:
        await entered.wait()
        assert bus.unsubscribe_plugin("observer") == 1
        await cancelling.wait()
        assert not publisher.done()
        cleanup_finished.set()
        executions = await asyncio.wait_for(publisher, timeout=1)
        assert len(executions) == 1
        assert not executions[0].success
        assert executions[0].error_category == "hook_unsubscribed"
    finally:
        cleanup_finished.set()
        await asyncio.gather(publisher, return_exceptions=True)
