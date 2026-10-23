from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError
from subscription_monitor.config import MonitorConfig, load_config
from subscription_monitor.plugin import SubscriptionMonitorPlugin
from subscription_monitor.polling import (
    OBSERVED_NAMESPACE,
    STATE_NAMESPACE,
    FeedPoller,
    FeedState,
    StateConflict,
)

from yuki_plugin_sdk.models import NotificationTarget, RestartPolicy
from yuki_plugin_sdk.results import PluginResult
from yuki_plugin_sdk.testing import FakePluginContext
from yuki_plugin_sdk.testing.fake_services import FakeStorage


async def scene(count=201, targets=4):
    context = FakePluginContext("subscription-monitor")
    subscription = {
        "id": "news",
        "url": "https://example.com/feed.json",
        "targets": [
            NotificationTarget(target_type="group", target_id=str(uuid4())).model_dump(mode="json")
            for _ in range(targets)
        ],
    }
    await context.config.set("subscriptions", [subscription])
    await context.config.set("max_notifications_per_poll", 100)
    body = json.dumps(
        {
            "version": "https://jsonfeed.org/version/1",
            "items": [{"id": str(index), "content_text": "new event"} for index in range(count)],
        }
    )

    async def request(*args, **kwargs):
        return PluginResult(data={"status_code": 200, "body": body})

    context.http.request = request
    return context, await load_config(context)


def reopen_storage(context):
    # Reconstruct JSON values as a cold SDK consumer would receive them.
    saved = json.loads(
        json.dumps([(*key, value) for key, value in context.storage._values.items()])
    )
    storage = FakeStorage()
    storage._values = {(namespace, key): value for namespace, key, value in saved}
    context.storage = storage


async def test_large_pending_survives_cold_load_and_drains_original_requests_in_rounds():
    context, config = await scene()
    initial = FeedState(initialized=True)
    raw = initial.model_dump(mode="json")
    await context.storage.set(STATE_NAMESPACE, "news", raw)
    poller = FeedPoller(context, asyncio.Event())
    await poller._fetch(config.subscriptions[0], raw, initial, datetime.now(UTC))
    reopen_storage(context)
    poller = FeedPoller(context, asyncio.Event())
    _, state = await poller.load_state("news")
    assert len(state.pending) == 804 and len(state.unmarked) == 201
    publish = context.notifications.publish

    async def interrupted(request):
        if len(context.notifications.published) == 19:
            raise ConnectionError("fixture before next publication")
        return await publish(request)

    context.notifications.publish = interrupted
    with pytest.raises(ConnectionError):
        await poller.poll("news", config)
    reopen_storage(context)
    poller = FeedPoller(context, asyncio.Event())
    _, state = await poller.load_state("news")
    assert len(state.pending) == 785
    context.notifications.publish = publish
    while state.pending:
        before = len(context.notifications.published)
        await poller.poll("news", config)
        assert len(context.notifications.published) - before <= config.max_notifications_per_poll
        _, state = await poller.load_state("news")
    accepted = [(item.event_key, item.target.target_id) for item in context.notifications.published]
    assert len(accepted) == len(set(accepted)) == 804
    assert len(await context.storage.list(OBSERVED_NAMESPACE)) == 201
    await poller.poll("news", config)
    assert len(context.notifications.published) == 804


async def test_failed_batch_cas_does_not_advance_observed_or_publish_and_baseline_stays_quiet():
    context, config = await scene()
    poller = FeedPoller(context, asyncio.Event())
    compare_and_set = context.storage.compare_and_set

    async def fail(*args, **kwargs):
        return False

    context.storage.compare_and_set = fail
    with pytest.raises(StateConflict):
        await poller.poll("news", config)
    assert await context.storage.get(STATE_NAMESPACE, "news") is None
    assert not await context.storage.list(OBSERVED_NAMESPACE)
    assert not context.notifications.published
    context.storage.compare_and_set = compare_and_set
    await poller.poll("news", config)
    _, state = await poller.load_state("news")
    assert state.initialized and not state.pending
    assert len(await context.storage.list(OBSERVED_NAMESPACE)) == 201
    assert not context.notifications.published


async def test_invalid_config_fails_once_and_explicit_lifecycle_resumes_original_pending():
    context, config = await scene(1, 1)
    poller = FeedPoller(context, asyncio.Event())
    await context.storage.set(
        STATE_NAMESPACE, "news", FeedState(initialized=True).model_dump(mode="json")
    )
    await poller.poll("news", config)
    original = context.notifications.published.pop()
    stored = FeedState(
        initialized=True,
        pending=(original,),
        next_poll_at=datetime.now(UTC) + timedelta(hours=1),
    ).model_dump(mode="json")
    await context.storage.set(STATE_NAMESPACE, "news", stored)
    await context.config.set("poll_interval_seconds", 0)
    registrations = []
    registrar = SimpleNamespace(
        register_config_schema=lambda schema: None,
        register_command=lambda command: None,
        register_background_service=registrations.append,
    )
    plugin = SubscriptionMonitorPlugin()
    await plugin.register(registrar)
    assert registrations[0].metadata.restart_policy is RestartPolicy.NEVER
    await plugin.start(context)
    with pytest.raises(ValidationError):
        await plugin.run()
    assert await context.storage.get(STATE_NAMESPACE, "news") == stored
    assert not context.notifications.published
    await context.config.set("poll_interval_seconds", MonitorConfig().poll_interval_seconds)
    await plugin.stop()
    await plugin.start(context)
    assert plugin._poller is not None
    await plugin._poller.poll("news", await load_config(context))
    assert context.notifications.published == [original]
    await plugin.stop()


async def test_transient_source_failure_uses_existing_domain_poll_loop(monkeypatch):
    context, config = await scene(1, 1)
    await context.storage.set(
        STATE_NAMESPACE, "news", FeedState(initialized=True).model_dump(mode="json")
    )
    stop = asyncio.Event()
    poller = FeedPoller(context, stop)
    request, publish = context.http.request, context.notifications.publish
    attempts, intervals = [], []

    async def temporarily_unavailable(*args, **kwargs):
        attempts.append(args)
        if len(attempts) == 1:
            return PluginResult(ok=False, error_code="source_unavailable")
        return await request(*args, **kwargs)

    async def accepted(value):
        result = await publish(value)
        stop.set()
        return result

    async def next_domain_tick(waiting, **options):
        intervals.append(options["timeout"])
        if stop.is_set():
            return await waiting
        waiting.close()
        raise TimeoutError

    context.http.request = temporarily_unavailable
    context.notifications.publish = accepted
    monkeypatch.setattr("subscription_monitor.polling.asyncio.wait_for", next_domain_tick)
    await poller.run()
    assert len(attempts) == 2 and len(context.notifications.published) == 1
    assert intervals == [config.poll_interval_seconds, config.poll_interval_seconds]
