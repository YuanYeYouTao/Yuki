from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from github_monitor.config import (
    GitHubMonitorConfig,
    NotificationTargetConfig,
    RepositorySubscription,
)
from github_monitor.models import GitHubAPIResponse, NormalizedGitHubEvent
from github_monitor.polling import GitHubPoller
from github_monitor.state import load_queue_state, load_repository_state

from yuki_plugin_sdk.testing import FakePluginContext


class StubClient:
    async def repository_events(self, *_args: object, **_kwargs: object) -> GitHubAPIResponse:
        return GitHubAPIResponse(
            status_code=200,
            headers={"etag": '"baseline"'},
            body=[
                {
                    "id": "100",
                    "type": "WatchEvent",
                    "actor": {"login": "alice", "type": "User"},
                    "created_at": "2026-08-05T10:30:00Z",
                    "payload": {"action": "started"},
                }
            ],
        )


@pytest.mark.asyncio
async def test_first_baseline_marks_cursor_and_only_publishes_enabled_notice() -> None:
    context = FakePluginContext(plugin_id="github-monitor")
    config = GitHubMonitorConfig(
        repositories=(
            RepositorySubscription(
                repository="owner/repo",
                targets=(
                    NotificationTargetConfig(
                        target_type="group",
                        target_id="2001",
                    ),
                ),
            ),
        )
    )
    for key, value in config.model_dump(mode="json").items():
        await context.config.set(key, value)
    poller = GitHubPoller(context, asyncio.Event())
    poller._client = StubClient()  # type: ignore[assignment]
    await poller.poll_repository(config.repositories[0], config)
    state = await load_repository_state(context, "owner/repo")
    assert state.last_event_id == "100"
    assert state.etag == '"baseline"'
    assert len(context.notifications.published) == 1
    assert context.notifications.published[0].event_type == "monitor_enabled"
    queue = (await load_queue_state(context, "owner/repo")).state
    assert queue.activation is not None and queue.activation.complete
    activation_id = queue.activation.activation_id
    activation_at = queue.activation.occurred_at
    assert activation_id in context.notifications.published[0].event_key
    assert queue.accepted_fingerprint == queue.committed_fingerprint

    await poller.poll_repository(config.repositories[0], config)
    repeated = (await load_queue_state(context, "owner/repo")).state
    assert repeated.activation is not None
    assert repeated.activation.activation_id == activation_id
    assert repeated.activation.occurred_at == activation_at
    assert len(context.notifications.published) == 1


@pytest.mark.asyncio
async def test_release_publication_attaches_card_to_enabled_target() -> None:
    context = FakePluginContext(plugin_id="github-monitor")
    subscription = RepositorySubscription(
        repository="owner/repo",
        targets=(NotificationTargetConfig(target_type="group", target_id="2001"),),
    )
    event = NormalizedGitHubEvent(
        github_event_id="142",
        repository="owner/repo",
        event_type="ReleaseEvent",
        actor="alice",
        created_at=datetime(2026, 8, 5, 13, 53, tzinfo=UTC),
        action="published",
        title="Yuki 3.4.2",
        summary="owner/repo 发布了 v3.4.2",
        event_key="github:owner/repo:event:142",
        payload={"tag": "v3.4.2", "target": "main", "assets_count": 0},
    )

    await GitHubPoller(context, asyncio.Event()).publish_event(subscription, event)

    assert len(context.notifications.published) == 1
    assert context.notifications.published[0].event_type == "ReleaseEvent"
    assert len(context.notifications.published[0].media_handles) == 1
