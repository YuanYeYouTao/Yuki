"""Core notifications are bounded observations, not awaited decisions."""

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from qq_ai_bot.application.lifecycle import LifecycleRegistry
from qq_ai_bot.application.modules.plugins import PluginModule
from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.plugin_host.admission_adapter import PluginAdmissionSignalAdapter
from qq_ai_bot.plugin_host.event_bus import PluginEventBus
from qq_ai_bot.plugin_host.extension_registry import ExtensionRegistry
from qq_ai_bot.plugin_host.manager import PluginManager
from qq_ai_bot.services.plugin_events import publish_notification
from yuki_plugin_sdk.events import EventEnvelope, EventName
from yuki_plugin_sdk.registrar import AdmissionSignalRegistration


def _subscribe(bus, handler, *, plugin_id="observer", event=EventName.TURN_ADMITTED):
    bus.subscribe(
        plugin_id=plugin_id, hook_id=f"observe.{event.value}", event=event, handler=handler
    )


@pytest.mark.asyncio
async def test_metadata_notification_returns_before_a_blocked_hook_and_preserves_order():
    bus = PluginEventBus(default_timeout_seconds=60)
    entered, release = asyncio.Event(), asyncio.Event()
    seen = []

    async def observe(event):
        seen.append((event.name, event.payload["stage"][0]))
        if event.name is EventName.MESSAGE_NORMALIZED:
            entered.set()
            await release.wait()

    stages = (EventName.MESSAGE_NORMALIZED, EventName.MESSAGE_TRIGGERED, EventName.TURN_ADMITTED)
    for stage in stages:
        _subscribe(bus, observe, event=stage)
    await bus.start()
    try:
        first_payload = {"stage": ["normalized"]}
        await asyncio.wait_for(publish_notification(bus, stages[0], first_payload), timeout=0.2)
        first_payload["stage"][0] = "mutated after admission"
        await entered.wait()
        for stage in stages[1:]:
            await publish_notification(bus, stage, {"stage": [stage.value]})
        assert seen == [(stages[0], "normalized")]
        assert (await bus.health())["pending"] == 3
        release.set()
        await bus._queue.join()
        assert seen == [(stages[0], "normalized"), *[(stage, stage.value) for stage in stages[1:]]]
        assert (await bus.health())["pending_bytes"] == 0
    finally:
        release.set()
        await bus.close()


@pytest.mark.asyncio
async def test_explicit_sdk_publish_still_waits_for_hook_outcomes():
    bus = PluginEventBus()
    entered, release = asyncio.Event(), asyncio.Event()

    async def observe(_event):
        entered.set()
        await release.wait()

    _subscribe(bus, observe)
    publishing = asyncio.create_task(bus.publish(EventEnvelope(name=EventName.TURN_ADMITTED)))
    try:
        await entered.wait()
        assert not publishing.done()
        release.set()
        result = await publishing
        assert len(result) == 1 and result[0].success
    finally:
        release.set()
        await publishing


@pytest.mark.asyncio
async def test_cancelling_explicit_publisher_still_cancels_its_handlers():
    bus = PluginEventBus(default_timeout_seconds=60)
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def observe(_event):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    _subscribe(bus, observe)
    publishing = asyncio.create_task(bus.publish(EventEnvelope(name=EventName.TURN_ADMITTED)))
    await entered.wait()
    publishing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await publishing
    assert cancelled.is_set()
    assert not bus._active_hooks


@pytest.mark.asyncio
async def test_queue_bounds_include_active_delivery_and_do_not_log_payload(caplog):
    bus = PluginEventBus(queue_capacity=1, queue_max_bytes=512)
    entered, release = asyncio.Event(), asyncio.Event()

    async def observe(_event):
        entered.set()
        await release.wait()

    _subscribe(bus, observe)
    await bus.start()
    try:
        event = EventEnvelope(name=EventName.TURN_ADMITTED)
        assert bus.enqueue_notification(event)
        await entered.wait()
        assert not bus.enqueue_notification(event)
        assert (await bus.health())["pending"] == 1
        release.set()
        await bus._queue.join()
        assert not bus.enqueue_notification(
            EventEnvelope(name=event.name, payload={"private": "PRIVATE_SENTINEL" * 100})
        )
        health = await bus.health()
        assert health["pending"] == health["pending_bytes"] == 0
        assert health["dropped"] == 2
        assert "PRIVATE_SENTINEL" not in caplog.text
    finally:
        release.set()
        await bus.close()


@pytest.mark.asyncio
async def test_unsubscribe_cancels_active_hook_and_old_snapshot_cannot_reach_replacement():
    bus = PluginEventBus(default_timeout_seconds=60)
    entered, cancelled = asyncio.Event(), asyncio.Event()
    seen = []

    async def original(event):
        seen.append(("original", event.payload["sequence"]))
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def replacement(event):
        seen.append(("replacement", event.payload["sequence"]))

    _subscribe(bus, original)
    await bus.start()
    try:
        assert bus.enqueue_notification(
            EventEnvelope(name=EventName.TURN_ADMITTED, payload={"sequence": 1})
        )
        await entered.wait()
        assert bus.enqueue_notification(
            EventEnvelope(name=EventName.TURN_ADMITTED, payload={"sequence": 2})
        )
        assert bus.unsubscribe_plugin("observer") == 1
        _subscribe(bus, replacement)
        assert bus.enqueue_notification(
            EventEnvelope(name=EventName.TURN_ADMITTED, payload={"sequence": 3})
        )
        await cancelled.wait()
        await bus._queue.join()
        assert seen == [("original", 1), ("replacement", 3)]
        health = await bus.health()
        assert health["cancelled_hooks"] == 1
        assert health["dropped"] == 1
        assert health["stale_hooks"] == 1
    finally:
        await bus.close()


@pytest.mark.asyncio
async def test_slow_and_failed_hooks_do_not_stop_following_notifications():
    bus = PluginEventBus(default_timeout_seconds=0.01)
    seen = []

    async def observe(event):
        sequence = event.payload["sequence"]
        if sequence == 1:
            await asyncio.Event().wait()
        elif sequence == 2:
            raise RuntimeError("do not log this private payload")
        elif sequence == 3:
            raise asyncio.CancelledError
        else:
            seen.append(sequence)

    _subscribe(bus, observe)
    await bus.start()
    try:
        for sequence in (1, 2, 3, 4):
            assert bus.enqueue_notification(
                EventEnvelope(name=EventName.TURN_ADMITTED, payload={"sequence": sequence})
            )
        await bus._queue.join()
        assert seen == [4]
        assert (await bus.health())["pending"] == 0
        assert (await bus.health())["cancelled_hooks"] == 1
    finally:
        await bus.close()


@pytest.mark.asyncio
async def test_consumer_does_not_inherit_request_context_and_close_discards_pending():
    request_actor = ContextVar("notification_test_actor", default="none")
    bus = PluginEventBus(default_timeout_seconds=60)
    entered, cancelled = asyncio.Event(), asyncio.Event()
    actors = []

    async def observe(_event):
        actors.append(request_actor.get())
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    _subscribe(bus, observe)
    token = request_actor.set("foreground_privileged_actor")
    try:
        await bus.start()
        assert bus.enqueue_notification(EventEnvelope(name=EventName.TURN_ADMITTED))
        await entered.wait()
        assert bus.enqueue_notification(EventEnvelope(name=EventName.TURN_ADMITTED))
        await bus.close()
        assert cancelled.is_set()
        assert actors == ["none"]
        assert not bus.enqueue_notification(EventEnvelope(name=EventName.TURN_ADMITTED))
        health = await bus.health()
        assert not health["running"]
        assert health["pending"] == health["pending_bytes"] == 0
        assert health["dropped"] == 3
    finally:
        request_actor.reset(token)
        await bus.close()


@pytest.mark.asyncio
async def test_plugin_stop_revokes_hooks_before_waiting_for_stop_callback():
    bus = PluginEventBus(default_timeout_seconds=60)
    entered, cancelled, stopping, release_stop = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    seen = []

    async def observe(event):
        seen.append(event.name)
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def stop():
        stopping.set()
        await release_stop.wait()

    manager = PluginManager(
        enabled=True,
        discovery=Mock(),
        installations=SimpleNamespace(set_status=AsyncMock()),
        loader=Mock(),
        extensions=ExtensionRegistry(),
        event_bus=bus,
        context_factory=Mock(),
    )
    manager._running["observer"] = SimpleNamespace(
        loaded=SimpleNamespace(instance=SimpleNamespace(stop=stop)),
        context=None,
        background_tasks={},
    )
    _subscribe(bus, observe)
    await bus.start()
    stop_task = None
    try:
        assert bus.enqueue_notification(EventEnvelope(name=EventName.TURN_ADMITTED))
        await entered.wait()
        stop_task = asyncio.create_task(
            manager._stop_one_unlocked("observer", final_status="disabled")
        )
        await stopping.wait()
        await asyncio.wait_for(cancelled.wait(), timeout=0.2)
        assert not stop_task.done()
        assert bus.enqueue_notification(EventEnvelope(name=EventName.TURN_ADMITTED))
        await bus._queue.join()
        assert seen == [EventName.TURN_ADMITTED]
    finally:
        release_stop.set()
        if stop_task is not None:
            await stop_task
        await bus.close()


@pytest.mark.asyncio
async def test_admission_signal_still_waits_for_its_permission_scope():
    checking, approved = asyncio.Event(), asyncio.Event()
    provider = AsyncMock(return_value=None)

    @asynccontextmanager
    async def permission_scope(*_args):
        checking.set()
        await approved.wait()
        yield

    adapter = PluginAdmissionSignalAdapter(ExtensionRegistry(), invocation_scope=permission_scope)
    task = asyncio.create_task(
        adapter._collect_one(
            "observer",
            AdmissionSignalRegistration(name="admission", provider=provider),
            message=SimpleNamespace(),
            origin=TurnOrigin.USER_MESSAGE,
            runtime=SimpleNamespace(),
            signal_context=SimpleNamespace(),
        )
    )
    try:
        await checking.wait()
        assert not task.done()
        provider.assert_not_awaited()
        approved.set()
        assert await task is None
        provider.assert_awaited_once()
    finally:
        approved.set()
        await task


@pytest.mark.asyncio
async def test_application_lifecycle_starts_consumer_before_plugins_and_closes_it_after_stop():
    bus = PluginEventBus()
    order = []

    async def start_plugins():
        assert (await bus.health())["running"]
        order.append("start")

    async def stop_plugins():
        assert (await bus.health())["running"]
        order.append("stop")

    bundle = SimpleNamespace(
        http=SimpleNamespace(close=AsyncMock()),
        session_repository=SimpleNamespace(delete_ephemeral=AsyncMock()),
        events=bus,
        manager=SimpleNamespace(start=start_plugins, stop=stop_plugins),
    )
    lifecycle = LifecycleRegistry()
    PluginModule.register_lifecycle(bundle, lifecycle)
    try:
        await lifecycle.start()
        assert (await lifecycle.health())["plugin_events"]["running"]
    finally:
        await lifecycle.close()
    assert order == ["start", "stop"]
    assert not (await bus.health())["running"]
