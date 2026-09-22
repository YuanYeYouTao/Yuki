from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from subscription_monitor.config import MonitorConfig, Subscription, save_subscriptions
from subscription_monitor.feeds import FeedEntry, FeedParseError
from subscription_monitor.plugin import CommandArguments, SubscriptionMonitorPlugin
from subscription_monitor.polling import STATE_NAMESPACE, FeedPoller, FeedState, prepare_request

from yuki_plugin_sdk.models import NotificationTarget
from yuki_plugin_sdk.results import PluginResult
from yuki_plugin_sdk.testing import FakePluginContext, run_plugin_contract_tests

TARGET = NotificationTarget(target_type="group", target_id="123456")


def subscription(**updates: object) -> Subscription:
    return Subscription.model_validate(
        {
            "id": "updates",
            "url": "https://example.com/feed.json",
            "targets": [TARGET],
            **updates,
        }
    )


def response(*entries: tuple[str, str, str], etag: str = "v1") -> PluginResult:
    return PluginResult(
        data={
            "status_code": 200,
            "headers": {"etag": etag},
            "body": json.dumps(
                {
                    "version": "https://jsonfeed.org/version/1.1",
                    "title": "updates",
                    "items": [
                        {
                            "id": id_,
                            "title": title,
                            "content_text": text,
                            "url": f"https://example.com/posts/{id_}",
                        }
                        for id_, title, text in entries
                    ],
                }
            ),
        }
    )


async def ready(context: FakePluginContext, config: MonitorConfig) -> FeedPoller:
    await save_subscriptions(context, config.subscriptions)
    return FeedPoller(context, asyncio.Event())


async def due(context: FakePluginContext) -> None:
    raw = await context.storage.get(STATE_NAMESPACE, "updates")
    state = FeedState.model_validate(raw).model_copy(
        update={"next_poll_at": datetime.now(UTC) - timedelta(seconds=1)}
    )
    await context.storage.set(STATE_NAMESPACE, "updates", state.model_dump(mode="json"))


async def test_baseline_keyword_filter_and_agent_only_publication_survive_restart() -> None:
    context = FakePluginContext("subscription-monitor")
    config = MonitorConfig(
        subscriptions=(
            subscription(include_any=["reset"], exclude_any=["rumor"], judge_template="reset_time"),
        )
    )
    poller = await ready(context, config)
    context.http.request = AsyncMock(return_value=response(("old", "reset", "Yesterday")))
    await poller.poll("updates", config)
    assert context.notifications.published == []
    await due(context)
    context.http.request.return_value = response(
        ("hit", "Reset announcement", "Quota resets at 08:00 UTC."),
        ("ignored", "Reset rumor", "An unconfirmed report."),
        ("other", "News", "Unrelated update."),
        ("old", "reset", "Yesterday"),
        etag="v2",
    )
    await poller.poll("updates", config)
    assert context.http.request.call_args.kwargs["headers"]["If-None-Match"] == "v1"
    [request] = context.notifications.published
    assert request.ask_agent and request.text == "" and request.media_handles == ()
    # Main Agent receives summary and intent, not arbitrary plugin payload fields.
    assert "08:00 UTC" in request.summary and "https://example.com/posts/hit" in request.summary
    assert "重置时间" in request.agent_intent and "send_message" in request.agent_intent
    assert context.messages.sent == []
    assert context.llm.calls == [] and context.agent.calls == []
    _, state = await poller.load_state("updates")
    assert state.filtered == 2 and state.accepted == 1 and not state.pending
    await due(context)
    restarted = FeedPoller(context, asyncio.Event())
    context.http.request.return_value = PluginResult(data={"status_code": 304})
    await restarted.poll("updates", config)
    assert len(context.notifications.published) == 1


async def test_unknown_publish_result_preserves_request_across_config_and_feed_changes() -> None:
    context = FakePluginContext("subscription-monitor")
    row = subscription(initial_sync="replay_recent", judge_prompt="只通知明确的重置时间")
    config = MonitorConfig(subscriptions=(row,))
    poller = await ready(context, config)
    context.http.request = AsyncMock(return_value=response(("1", "Reset", "08:00 UTC")))
    saved_publish = context.notifications.publish
    attempts = []

    async def lost_response(request):
        attempts.append(request.model_dump(mode="json"))
        await saved_publish(request)  # Host accepted; the plugin did not receive the receipt.
        raise TimeoutError

    context.notifications.publish = lost_response
    with pytest.raises(TimeoutError):
        await poller.poll("updates", config)
    _, state = await poller.load_state("updates")
    assert len(state.pending) == 1
    await save_subscriptions(
        context, (row.model_copy(update={"judge_prompt": "Changed condition"}),)
    )
    restarted = FeedPoller(context, asyncio.Event())
    context.notifications.publish = saved_publish
    # No fetch or new model calculation is permitted to rewrite the pending request.
    context.http.request.side_effect = AssertionError("pending requests must be drained first")
    await restarted.poll("updates", config)
    assert context.notifications.published[-1].model_dump(mode="json") == attempts[0]
    _, state = await restarted.load_state("updates")
    assert not state.pending and state.accepted == 1


async def test_checkpoint_failure_before_and_after_host_admission_never_changes_request() -> None:
    context = FakePluginContext("subscription-monitor")
    config = MonitorConfig(subscriptions=(subscription(initial_sync="replay_recent"),))
    poller = await ready(context, config)
    context.http.request = AsyncMock(return_value=response(("1", "News", "Hello")))
    original = context.storage.compare_and_set
    context.storage.compare_and_set = AsyncMock(return_value=False)
    with pytest.raises(RuntimeError):
        await poller.poll("updates", config)
    assert context.notifications.published == []

    writes = 0

    async def fail_after_publish(namespace, key, expected, value):
        nonlocal writes
        writes += 1
        if writes == 3:
            raise OSError("simulated storage failure")
        return await original(namespace, key, expected, value)

    context.storage.compare_and_set = fail_after_publish
    with pytest.raises(OSError):
        await poller.poll("updates", config)
    [first] = context.notifications.published
    context.storage.compare_and_set = original
    await FeedPoller(context, asyncio.Event()).poll("updates", config)
    assert context.notifications.published[-1] == first


async def test_invalid_feed_preserves_cursor_and_pending_batches_drain_without_refetch() -> None:
    context = FakePluginContext("subscription-monitor")
    config = MonitorConfig(
        max_notifications_per_poll=1, subscriptions=(subscription(initial_sync="replay_recent"),)
    )
    poller = await ready(context, config)
    context.http.request = AsyncMock(
        return_value=response(("new", "New", "new"), ("old", "Old", "old"))
    )
    await poller.poll("updates", config)
    _, state = await poller.load_state("updates")
    assert len(state.pending) == 1
    context.http.request.side_effect = AssertionError("must drain durable pending batch")
    await poller.poll("updates", config)
    assert len(context.notifications.published) == 2
    await due(context)
    previous = await context.storage.get(STATE_NAMESPACE, "updates")
    context.http.request.side_effect = None
    context.http.request.return_value = PluginResult(
        data={"status_code": 200, "body": "<html>bad gateway</html>"}
    )
    with pytest.raises(FeedParseError):
        await poller.poll("updates", config)
    assert await context.storage.get(STATE_NAMESPACE, "updates") == previous


async def test_commands_manage_grants_conditions_and_pause_without_direct_notifications() -> None:
    plugin = SubscriptionMonitorPlugin()
    context = FakePluginContext("subscription-monitor")
    await plugin.start(context)
    result = await plugin.command(
        CommandArguments(
            text="add updates https://example.com/feed.json group:123456 仅通知重置时间"
        )
    )
    assert result.ok and ("group", "123456") in context.notifications.grants
    config = MonitorConfig(subscriptions=await context.config.get("subscriptions"))
    assert config.subscriptions[0].judge_prompt == "仅通知重置时间"
    paused = await plugin.command(CommandArguments(text="pause updates"))
    assert paused.ok
    poller = FeedPoller(context, asyncio.Event())
    context.http.request = AsyncMock(
        side_effect=AssertionError("paused subscription must not fetch")
    )
    await poller.poll("updates", config)
    assert (await plugin.command(CommandArguments(text="resume updates"))).ok
    changed_source = config.subscriptions[0].model_copy(update={"url": "https://example.net/other"})
    assert not (
        await plugin.command(CommandArguments(text="set " + changed_source.model_dump_json()))
    ).ok
    assert (await plugin.command(CommandArguments(text="status"))).ok
    assert (await plugin.command(CommandArguments(text="remove updates"))).ok
    assert await context.config.get("subscriptions") == []
    assert context.notifications.published == [] and context.messages.sent == []
    await plugin.stop()
    await plugin.stop()


def test_long_unicode_evidence_fits_host_limits_and_marks_excerpt_truncation() -> None:
    entry = FeedEntry(
        id="id", title="标题" * 250, content="内容" * 3000, url="https://example.com/post"
    )
    [request] = prepare_request(subscription(judge_prompt="条件" * 350), entry, datetime.now(UTC))
    assert len(request.summary) <= 1200 and len(request.agent_intent) <= 1000
    assert "[正文已截短]" in request.summary and entry.url in request.summary
    assert len(json.dumps(request.payload, ensure_ascii=False).encode()) <= 32768
    assert request.text == "" and request.ask_agent


async def test_plugin_uses_real_sdk_contract_and_has_no_direct_send_or_model_permission() -> None:
    import tomllib

    root = Path(__file__).parents[1]
    report = await run_plugin_contract_tests(root)
    assert report.passed, report.model_dump()
    manifest = tomllib.loads((root / "plugin.toml").read_text(encoding="utf-8"))
    assert {"notification.publish", "notification.agent"} <= set(manifest["permissions"])
    assert not {
        "llm.generate",
        "agent.run",
        "agent.session",
        "message.group.send",
        "message.private.send",
        "onebot.send",
    } & set(manifest["permissions"])


async def test_recent_replay_selects_newest_dated_item_from_oldest_first_feed() -> None:
    context = FakePluginContext("subscription-monitor")
    config = MonitorConfig(
        subscriptions=(subscription(initial_sync="replay_recent", replay_recent_limit=1),)
    )
    poller = await ready(context, config)
    feed = response(("old", "Old", "Old item"), ("new", "New", "New item"))
    data = json.loads(feed.data["body"])
    data["items"][0]["date_published"] = "2026-09-21T00:00:00Z"
    data["items"][1]["date_published"] = "2026-09-22T00:00:00Z"
    context.http.request = AsyncMock(
        return_value=PluginResult(data={"status_code": 200, "body": json.dumps(data)})
    )
    await poller.poll("updates", config)
    [request] = context.notifications.published
    assert request.payload["entry"]["id"] == "new"


async def test_observed_ids_outlive_hot_cache_and_partial_marker_writes() -> None:
    context = FakePluginContext("subscription-monitor")
    config = MonitorConfig(subscriptions=(subscription(initial_sync="replay_recent"),))
    poller = await ready(context, config)
    context.http.request = AsyncMock(return_value=response(("old", "Original", "Original text")))
    storage_set = context.storage.set
    context.storage.set = AsyncMock(side_effect=OSError("marker write interrupted"))
    with pytest.raises(OSError):
        await poller.poll("updates", config)
    assert not context.notifications.published
    _, state = await poller.load_state("updates")
    assert state.pending and state.unmarked
    context.storage.set = storage_set
    await poller.poll("updates", config)
    assert len(context.notifications.published) == 1

    _, state = await poller.load_state("updates")
    state = state.model_copy(update={"seen": (), "next_poll_at": None})
    await context.storage.set(STATE_NAMESPACE, "updates", state.model_dump(mode="json"))
    context.http.request.return_value = response(("old", "Changed title", "Changed text"))
    await FeedPoller(context, asyncio.Event()).poll("updates", config)
    assert len(context.notifications.published) == 1
