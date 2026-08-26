from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from github_monitor.commands import GitHubCommandArguments, GitHubCommands
from github_monitor.config import (
    GitHubMonitorConfig,
    NotificationTargetConfig,
    RepositorySubscription,
    load_config,
)
from github_monitor.events import normalize_event, raw_event_fingerprint
from github_monitor.models import (
    ActivationState,
    DeliveryUnit,
    GitHubAPIResponse,
    PreparedMedia,
    QueuedSourceEvent,
    QueueState,
    RateLimitState,
    TargetDelivery,
    TargetPolicySnapshot,
    build_prepared_notification,
    delivery_target_key,
)
from github_monitor.polling import GitHubPoller, GitHubQueueGap
from github_monitor.state import (
    LEGACY_NAMESPACE,
    QUEUE_NAMESPACE,
    QueueStateConflict,
    load_queue_state,
)

from yuki_plugin_sdk.models import (
    JsonValue,
    MediaArtifactHandle,
    NotificationPublishReceipt,
    NotificationTarget,
    PublishNotificationRequest,
)
from yuki_plugin_sdk.testing import FakeConfigFacade, FakePluginContext, FakeStorage

REPOSITORY = "owner/repo"
TARGET = NotificationTargetConfig(
    target_type="group",
    target_id="2001",
    ask_agent=False,
    send_text=True,
    send_card=False,
)


def _raw(
    event_id: int,
    *,
    action: str = "started",
    actor: str = "alice",
    event_type: str = "WatchEvent",
    ref: str = "",
    ref_type: str = "branch",
) -> dict[str, Any]:
    payload: dict[str, object] = {"action": action}
    if event_type in {"CreateEvent", "DeleteEvent"}:
        payload.update({"ref": ref or f"feature/{event_id}", "ref_type": ref_type})
    return {
        "id": str(event_id),
        "type": event_type,
        "actor": {"login": actor, "type": "User"},
        "created_at": (datetime(2026, 8, 27, tzinfo=UTC) + timedelta(seconds=event_id))
        .isoformat()
        .replace("+00:00", "Z"),
        "payload": payload,
    }


def _subscription(*, enabled: bool = True, targets=(TARGET,)) -> RepositorySubscription:
    return RepositorySubscription(
        repository=REPOSITORY,
        enabled=enabled,
        targets=targets,
    )


def _config(subscription: RepositorySubscription, *, maximum: int = 50) -> GitHubMonitorConfig:
    return GitHubMonitorConfig(
        repositories=(subscription,),
        max_events_per_poll=maximum,
        events_per_repository=100,
    )


async def _poll(
    poller: GitHubPoller,
    context: FakePluginContext,
    subscription: RepositorySubscription,
    config: GitHubMonitorConfig,
) -> None:
    for key, value in config.model_dump(mode="json").items():
        await context.config.set(key, value)
    await poller.poll_repository(subscription, config)


class PageClient:
    def __init__(self, pages: dict[int, list[dict[str, Any]]]) -> None:
        self.pages = pages
        self.calls: list[int] = []
        self.requests: list[dict[str, object]] = []

    async def repository_events(self, *_args: object, **kwargs: object) -> GitHubAPIResponse:
        page = int(kwargs.get("page", 1))
        self.calls.append(page)
        self.requests.append(dict(kwargs))
        headers = {"etag": '"events"'}
        if page < max(self.pages):
            headers["link"] = '<https://api.github.test/events?page=2>; rel="next"'
        return GitHubAPIResponse(
            status_code=200,
            headers=headers,
            body=self.pages.get(page, []),
            rate_limit=RateLimitState(remaining=4_000),
        )

    async def compare(self, *_args: object, **_kwargs: object) -> GitHubAPIResponse:
        return GitHubAPIResponse(status_code=200, body={})


class ConditionalPageClient(PageClient):
    async def repository_events(self, *_args: object, **kwargs: object) -> GitHubAPIResponse:
        if kwargs.get("etag") or kwargs.get("last_modified"):
            self.requests.append(dict(kwargs))
            return GitHubAPIResponse(status_code=304, headers={"etag": '"events"'})
        return await super().repository_events(*_args, **kwargs)


class NotModifiedClient(PageClient):
    async def repository_events(self, *_args: object, **kwargs: object) -> GitHubAPIResponse:
        self.requests.append(dict(kwargs))
        return GitHubAPIResponse(status_code=304, headers={})


class BlockingConfig(FakeConfigFacade):
    def __init__(self) -> None:
        super().__init__()
        self.first_repository_write = asyncio.Event()
        self.release_first_write = asyncio.Event()
        self._block_once = True

    async def set(
        self,
        key: str,
        value: JsonValue,
        *,
        scope_type: str = "global",
        scope_id: str = "",
    ) -> None:
        if key == "repositories" and self._block_once:
            self._block_once = False
            self.first_repository_write.set()
            await self.release_first_write.wait()
        await super().set(key, value, scope_type=scope_type, scope_id=scope_id)


class FaultStorage(FakeStorage):
    def __init__(self) -> None:
        super().__init__()
        self.cas_calls = 0
        self.fail_on: int | None = None

    def arm(self, call: int | None) -> None:
        self.cas_calls = 0
        self.fail_on = call

    async def compare_and_set(
        self,
        namespace: str,
        key: str,
        expected: Any,
        value: Any,
    ) -> bool:
        self.cas_calls += 1
        if self.fail_on == self.cas_calls:
            self.fail_on = None
            return False
        return await super().compare_and_set(namespace, key, expected, value)


class FailSecondTargetNotifications:
    def __init__(self) -> None:
        self.published: list[PublishNotificationRequest] = []
        self.failed = False

    async def publish(self, request: PublishNotificationRequest) -> NotificationPublishReceipt:
        self.published.append(request)
        if len(self.published) == 2 and not self.failed:
            self.failed = True
            raise RuntimeError("second target interrupted")
        return NotificationPublishReceipt(
            notification_id=f"receipt-{len(self.published)}",
            source_event_id=len(self.published),
            event_created=True,
            delivery_enqueued=True,
            agent_turn_enqueued=request.ask_agent,
            deduplicated=False,
        )


class FutureMedia:
    def __init__(self) -> None:
        self.calls = 0

    async def create_artifact(
        self,
        *,
        data: bytes,
        content_type: str,
        filename: str,
        ttl_seconds: int,
    ) -> MediaArtifactHandle:
        del content_type, ttl_seconds
        self.calls += 1
        return MediaArtifactHandle(
            handle_id=f"card-{self.calls}",
            content_type="image/png",
            filename=filename,
            byte_size=len(data),
            sha256="a" * 64,
            expires_at=datetime.now(UTC) + timedelta(days=7),
        )


def _seed_state(storage: FakeStorage, state: QueueState) -> None:
    storage._values[(QUEUE_NAMESPACE, REPOSITORY)] = state.model_dump(mode="json")


def _cursor_state(event_id: int) -> QueueState:
    raw = _raw(event_id)
    fingerprint = raw_event_fingerprint(raw)
    return QueueState(
        accepted_cursor=str(event_id),
        accepted_fingerprint=fingerprint,
        committed_cursor=str(event_id),
        committed_fingerprint=fingerprint,
        committed_created_at=datetime.fromisoformat(str(raw["created_at"]).replace("Z", "+00:00")),
    )


def _target_snapshot(target: NotificationTargetConfig = TARGET) -> TargetPolicySnapshot:
    return TargetPolicySnapshot(
        target_type=target.target_type,
        target_id=target.target_id,
        send_text=target.send_text,
        send_card=target.send_card,
        ask_agent=target.ask_agent,
    )


def _queued(raw: dict[str, Any], target: NotificationTargetConfig = TARGET) -> QueuedSourceEvent:
    event = normalize_event(REPOSITORY, raw)
    assert event is not None
    return QueuedSourceEvent(
        github_event_id=str(raw["id"]),
        source_fingerprint=raw_event_fingerprint(raw),
        source_created_at=event.created_at,
        normalized=event,
        target_snapshot=(_target_snapshot(target),),
    )


@pytest.mark.asyncio
async def test_oldest_first_backlog_advances_without_truncating_newest() -> None:
    context = FakePluginContext(plugin_id="github-monitor")
    storage = FaultStorage()
    context.storage = storage  # type: ignore[assignment]
    _seed_state(storage, _cursor_state(98))
    subscription = _subscription()
    config = _config(subscription, maximum=2)
    client = ConditionalPageClient({1: [_raw(value) for value in range(105, 97, -1)]})
    poller = GitHubPoller(context, asyncio.Event())
    poller._client = client  # type: ignore[assignment]

    await _poll(poller, context, subscription, config)
    first = (await load_queue_state(context, REPOSITORY)).state
    assert [request.payload["source_event_ids"] for request in context.notifications.published] == [
        ["99", "100"]
    ]
    assert first.accepted_cursor == "105"
    assert first.committed_cursor == "100"
    assert [item.github_event_id for item in first.pending] == ["101", "102", "103", "104", "105"]
    assert first.backlog_pending is True

    await _poll(poller, context, subscription, config)
    assert [request.payload["source_event_ids"] for request in context.notifications.published][
        -1:
    ] == [["101", "102"]]
    assert len(client.requests) == 1
    second = (await load_queue_state(context, REPOSITORY)).state
    assert second.accepted_cursor == "105"
    assert second.committed_cursor == "102"
    assert [item.github_event_id for item in second.pending] == ["103", "104", "105"]


@pytest.mark.asyncio
async def test_existing_queue_consumes_one_poll_budget_before_fetch() -> None:
    context = FakePluginContext(plugin_id="github-monitor")
    storage = FaultStorage()
    context.storage = storage  # type: ignore[assignment]
    raw = _raw(99)
    event = normalize_event(REPOSITORY, raw)
    assert event is not None
    source = QueuedSourceEvent(
        github_event_id="99",
        source_fingerprint=raw_event_fingerprint(raw),
        source_created_at=event.created_at,
        normalized=event,
        target_snapshot=(_target_snapshot(),),
    )
    state = QueueState(
        accepted_cursor="99",
        accepted_fingerprint=source.source_fingerprint,
        committed_cursor="98",
        committed_fingerprint=raw_event_fingerprint(_raw(98)),
        committed_created_at=datetime.fromisoformat(
            str(_raw(98)["created_at"]).replace("Z", "+00:00")
        ),
        pending=(source,),
    )
    _seed_state(storage, state)
    subscription = _subscription()
    client = PageClient({1: [_raw(102), _raw(101), _raw(100), _raw(99)]})
    poller = GitHubPoller(context, asyncio.Event())
    poller._client = client  # type: ignore[assignment]

    await _poll(poller, context, subscription, _config(subscription, maximum=2))

    assert len(context.notifications.published) == 1
    assert client.calls == []
    assert (await load_queue_state(context, REPOSITORY)).state.committed_cursor == "99"


@pytest.mark.asyncio
async def test_not_modified_without_validators_preserves_cached_headers() -> None:
    context = FakePluginContext(plugin_id="github-monitor")
    storage = FaultStorage()
    context.storage = storage  # type: ignore[assignment]
    seeded = _cursor_state(98).model_copy(
        update={"etag": '"old-etag"', "last_modified": "old-date"}
    )
    _seed_state(storage, seeded)
    subscription = _subscription()
    client = NotModifiedClient({1: []})
    poller = GitHubPoller(context, asyncio.Event())
    poller._client = client  # type: ignore[assignment]

    await _poll(poller, context, subscription, _config(subscription))

    state = (await load_queue_state(context, REPOSITORY)).state
    assert state.etag == '"old-etag"'
    assert state.last_modified == "old-date"


@pytest.mark.asyncio
async def test_gap_and_boundary_payload_conflict_fail_closed() -> None:
    subscription = _subscription()
    config = _config(subscription)

    missing = FakePluginContext(plugin_id="github-monitor")
    missing_storage = FaultStorage()
    missing.storage = missing_storage  # type: ignore[assignment]
    _seed_state(missing_storage, _cursor_state(98))
    missing_poller = GitHubPoller(missing, asyncio.Event())
    missing_poller._client = PageClient({1: [_raw(100), _raw(99)]})  # type: ignore[assignment]
    await _poll(missing_poller, missing, subscription, config)
    missing_state = (await load_queue_state(missing, REPOSITORY)).state
    assert missing_state.gap_reason == "github_cursor_missing_from_overlap"
    assert missing_state.accepted_cursor == missing_state.committed_cursor == "98"
    assert missing.notifications.published == []

    changed = FakePluginContext(plugin_id="github-monitor")
    changed_storage = FaultStorage()
    changed.storage = changed_storage  # type: ignore[assignment]
    _seed_state(changed_storage, _cursor_state(98))
    changed_poller = GitHubPoller(changed, asyncio.Event())
    changed_poller._client = PageClient(  # type: ignore[assignment]
        {1: [_raw(99), _raw(98, action="stopped")]}
    )
    await _poll(changed_poller, changed, subscription, config)
    changed_state = (await load_queue_state(changed, REPOSITORY)).state
    assert changed_state.gap_reason == "github_cursor_payload_conflict"
    assert changed.notifications.published == []


@pytest.mark.asyncio
async def test_malformed_supported_event_persists_normalization_gap() -> None:
    context = FakePluginContext(plugin_id="github-monitor")
    storage = FaultStorage()
    context.storage = storage  # type: ignore[assignment]
    _seed_state(storage, _cursor_state(98))
    malformed = {**_raw(99), "created_at": "not-a-timestamp"}
    subscription = _subscription()
    poller = GitHubPoller(context, asyncio.Event())
    poller._client = PageClient({1: [malformed, _raw(98)]})  # type: ignore[assignment]

    await _poll(poller, context, subscription, _config(subscription))

    state = (await load_queue_state(context, REPOSITORY)).state
    assert state.gap_reason == "github_event_normalization_failed"
    assert state.accepted_cursor == state.committed_cursor == "98"
    assert context.notifications.published == []


@pytest.mark.asyncio
async def test_legacy_identity_gap_is_imported_to_authoritative_queue() -> None:
    context = FakePluginContext(plugin_id="github-monitor")
    storage = FaultStorage()
    context.storage = storage  # type: ignore[assignment]
    await storage.set(
        LEGACY_NAMESPACE,
        REPOSITORY,
        {"last_event_id": "legacy-business-key", "baseline_notified": True},
    )
    subscription = _subscription()
    client = PageClient({1: [_raw(99)]})
    poller = GitHubPoller(context, asyncio.Event())
    poller._client = client  # type: ignore[assignment]

    await _poll(poller, context, subscription, _config(subscription))

    snapshot = await load_queue_state(context, REPOSITORY)
    assert snapshot.raw is not None
    assert snapshot.state.gap_reason == "legacy_cursor_not_numeric"
    assert snapshot.state.activation is not None and snapshot.state.activation.complete
    assert client.calls == []


@pytest.mark.asyncio
async def test_overlap_conflict_and_nonnumeric_id_persist_gap() -> None:
    subscription = _subscription()
    config = _config(subscription)
    for pages, reason in (
        (
            {1: [_raw(100)], 2: [_raw(100, action="stopped"), _raw(98)]},
            "github_event_payload_conflict",
        ),
        (
            {1: [{**_raw(99), "id": "not-numeric"}, _raw(98)]},
            "github_event_id_not_numeric",
        ),
    ):
        context = FakePluginContext(plugin_id="github-monitor")
        storage = FaultStorage()
        context.storage = storage  # type: ignore[assignment]
        _seed_state(storage, _cursor_state(98))
        poller = GitHubPoller(context, asyncio.Event())
        poller._client = PageClient(pages)  # type: ignore[assignment]
        await _poll(poller, context, subscription, config)
        state = (await load_queue_state(context, REPOSITORY)).state
        assert state.gap_reason == reason
        assert context.notifications.published == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fail_on", "first_publish_count", "final_publish_count"),
    ((2, 0, 1), (3, 0, 1), (5, 1, 2), (6, 1, 1)),
    ids=("after_accept", "after_seal", "after_host_before_completion", "after_completion"),
)
async def test_wal_crash_points_resume_without_lost_work(
    fail_on: int,
    first_publish_count: int,
    final_publish_count: int,
) -> None:
    context = FakePluginContext(plugin_id="github-monitor")
    storage = FaultStorage()
    context.storage = storage  # type: ignore[assignment]
    _seed_state(storage, _cursor_state(98))
    storage.arm(fail_on)
    subscription = _subscription()
    config = _config(subscription)
    poller = GitHubPoller(context, asyncio.Event())
    poller._client = PageClient({1: [_raw(99), _raw(98)]})  # type: ignore[assignment]

    with pytest.raises(QueueStateConflict):
        await _poll(poller, context, subscription, config)
    assert len(context.notifications.published) == first_publish_count
    storage.arm(None)
    await _poll(poller, context, subscription, config)
    assert len(context.notifications.published) == final_publish_count
    state = (await load_queue_state(context, REPOSITORY)).state
    assert state.committed_cursor == "99"
    assert state.pending == () and state.inflight is None
    if final_publish_count == 2:
        assert context.notifications.published[0] == context.notifications.published[1]


@pytest.mark.asyncio
async def test_first_target_completion_survives_second_target_failure() -> None:
    second = TARGET.model_copy(update={"target_id": "2002"})
    subscription = _subscription(targets=(TARGET, second))
    config = _config(subscription)
    context = FakePluginContext(plugin_id="github-monitor")
    storage = FaultStorage()
    context.storage = storage  # type: ignore[assignment]
    notifications = FailSecondTargetNotifications()
    context.notifications = notifications  # type: ignore[assignment]
    _seed_state(storage, _cursor_state(98))
    poller = GitHubPoller(context, asyncio.Event())
    poller._client = PageClient({1: [_raw(100), _raw(99), _raw(98)]})  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="second target"):
        await _poll(poller, context, subscription, config)
    state = (await load_queue_state(context, REPOSITORY)).state
    assert state.inflight is not None
    assert len(state.inflight.members) == 2
    assert [item.status for item in state.inflight.deliveries] == ["completed", "attempting"]
    await _poll(poller, context, subscription, config)
    targets = [request.target.target_id for request in notifications.published]
    assert targets == ["2001", "2002", "2002"]
    assert all(request.event_type == "github_event_batch" for request in notifications.published)
    assert notifications.published[1] == notifications.published[2]


@pytest.mark.asyncio
async def test_unknown_host_result_reuses_frozen_media_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    card_target = TARGET.model_copy(update={"send_card": True})
    subscription = _subscription(targets=(card_target,))
    context = FakePluginContext(plugin_id="github-monitor")
    storage = FaultStorage()
    context.storage = storage  # type: ignore[assignment]
    media = FutureMedia()
    context.media = media  # type: ignore[assignment]
    _seed_state(storage, _cursor_state(98))
    storage.arm(5)
    poller = GitHubPoller(context, asyncio.Event())
    poller._client = PageClient({1: [_raw(99), _raw(98)]})  # type: ignore[assignment]
    monkeypatch.setattr(
        "github_monitor.polling.render_event_card",
        lambda _event: (b"png", "event.png"),
    )
    with pytest.raises(QueueStateConflict):
        await _poll(poller, context, subscription, _config(subscription))
    assert media.calls == 1
    assert len(context.notifications.published) == 1
    storage.arm(None)
    await _poll(poller, context, subscription, _config(subscription))
    assert media.calls == 1
    assert len(context.notifications.published) == 2
    assert context.notifications.published[0] == context.notifications.published[1]
    assert context.notifications.published[0].media_handles == ("card-1",)


@pytest.mark.asyncio
async def test_expired_first_target_gap_blocks_later_target_publish() -> None:
    first = TARGET.model_copy(update={"send_card": True})
    second = TARGET.model_copy(update={"target_id": "2002"})
    subscription = _subscription(targets=(first, second))
    context = FakePluginContext(plugin_id="github-monitor")
    storage = FaultStorage()
    context.storage = storage  # type: ignore[assignment]
    raw = _raw(99)
    event = normalize_event(REPOSITORY, raw)
    assert event is not None
    source = QueuedSourceEvent(
        github_event_id="99",
        source_fingerprint=raw_event_fingerprint(raw),
        source_created_at=event.created_at,
        normalized=event,
        target_snapshot=(_target_snapshot(first), _target_snapshot(second)),
    )
    expired = build_prepared_notification(
        event_key="github:owner/repo:event:99",
        event_type="WatchEvent",
        target_type="group",
        target_id="2001",
        occurred_at=event.created_at,
        summary=event.summary,
        payload={"repository": REPOSITORY},
        media=(
            PreparedMedia(
                index=0,
                handle_id="expired-card",
                sha256="a" * 64,
                expires_at=datetime.now(UTC) - timedelta(seconds=1),
            ),
        ),
    )
    live = build_prepared_notification(
        event_key="github:owner/repo:event:99",
        event_type="WatchEvent",
        target_type="group",
        target_id="2002",
        occurred_at=event.created_at,
        summary=event.summary,
        payload={"repository": REPOSITORY},
        text=event.summary,
    )
    deliveries = (
        TargetDelivery(
            target_key=delivery_target_key("group", "2001"),
            target_type="group",
            target_id="2001",
            send_text=True,
            send_card=True,
            ask_agent=False,
            status="attempting",
            prepared=expired,
        ),
        TargetDelivery(
            target_key=delivery_target_key("group", "2002"),
            target_type="group",
            target_id="2002",
            send_text=True,
            send_card=False,
            ask_agent=False,
            status="attempting",
            prepared=live,
        ),
    )
    unit = DeliveryUnit(
        unit_id="github-unit-v1:expired-first",
        members=(source,),
        deliveries=deliveries,
        sealed_at=datetime.now(UTC),
    )
    state = QueueState(
        accepted_cursor="99",
        accepted_fingerprint=source.source_fingerprint,
        committed_cursor="98",
        committed_fingerprint=raw_event_fingerprint(_raw(98)),
        committed_created_at=datetime.fromisoformat(
            str(_raw(98)["created_at"]).replace("Z", "+00:00")
        ),
        inflight=unit,
        activation=ActivationState(
            activation_id="activation-complete",
            occurred_at=event.created_at,
            deliveries=(),
        ),
    )
    _seed_state(storage, state)
    poller = GitHubPoller(context, asyncio.Event())

    await _poll(poller, context, subscription, _config(subscription))

    after = (await load_queue_state(context, REPOSITORY)).state
    assert after.gap_reason == "github_prepared_media_expired"
    assert context.notifications.published == []
    poller._client = PageClient({1: [_raw(99)]})  # type: ignore[assignment]
    await poller.rebaseline(subscription, _config(subscription), "baseline")
    recovered = (await load_queue_state(context, REPOSITORY)).state
    assert recovered.gap_reason == ""
    assert recovered.pending == () and recovered.inflight is None
    assert [item.target.target_id for item in context.notifications.published] == ["2002"]


@pytest.mark.asyncio
async def test_accept_freezes_targets_and_prepared_delivery_honors_disable() -> None:
    context = FakePluginContext(plugin_id="github-monitor")
    storage = FaultStorage()
    context.storage = storage  # type: ignore[assignment]
    _seed_state(storage, _cursor_state(98))
    subscription = _subscription()
    config = _config(subscription)
    storage.arm(2)
    poller = GitHubPoller(context, asyncio.Event())
    poller._client = PageClient({1: [_raw(99), _raw(98)]})  # type: ignore[assignment]
    with pytest.raises(QueueStateConflict):
        await _poll(poller, context, subscription, config)
    accepted = (await load_queue_state(context, REPOSITORY)).state
    assert accepted.pending[0].target_snapshot == (_target_snapshot(),)

    raw = _raw(99)
    event = normalize_event(REPOSITORY, raw)
    assert event is not None
    source = QueuedSourceEvent(
        github_event_id="99",
        source_fingerprint=raw_event_fingerprint(raw),
        source_created_at=event.created_at,
        normalized=event,
        target_snapshot=(_target_snapshot(),),
    )
    prepared = build_prepared_notification(
        event_key="github:owner/repo:event:99",
        event_type="WatchEvent",
        target_type="group",
        target_id="2001",
        occurred_at=event.created_at,
        summary=event.summary,
        payload={"repository": REPOSITORY},
        text=event.summary,
    )
    delivery = TargetDelivery(
        target_key=delivery_target_key("group", "2001"),
        target_type="group",
        target_id="2001",
        send_text=True,
        send_card=False,
        ask_agent=False,
        status="prepared",
        prepared=prepared,
    )
    unit = DeliveryUnit(
        unit_id="github-unit-v1:prepared",
        members=(source,),
        deliveries=(delivery,),
        sealed_at=datetime.now(UTC),
    )
    prepared_state = QueueState(
        accepted_cursor="99",
        accepted_fingerprint=source.source_fingerprint,
        committed_cursor="98",
        committed_fingerprint=raw_event_fingerprint(_raw(98)),
        committed_created_at=datetime.fromisoformat(
            str(_raw(98)["created_at"]).replace("Z", "+00:00")
        ),
        inflight=unit,
    )
    _seed_state(storage, prepared_state)
    disabled_target = TARGET.model_copy(update={"send_text": False})
    disabled = _subscription(targets=(disabled_target,))
    poller._client = PageClient({1: [_raw(99)]})  # type: ignore[assignment]
    await _poll(poller, context, disabled, _config(disabled))
    after = (await load_queue_state(context, REPOSITORY)).state
    assert after.inflight is None
    assert after.committed_cursor == "99"
    assert context.notifications.published == []
    legacy = await storage.get(LEGACY_NAMESPACE, REPOSITORY)
    assert isinstance(legacy, dict)
    assert legacy["last_event_id"] == "99"
    assert (
        datetime.fromisoformat(str(legacy["last_event_created_at"]).replace("Z", "+00:00"))
        == event.created_at
    )


@pytest.mark.asyncio
async def test_remove_nonlast_attempting_target_finishes_before_revoke() -> None:
    context = FakePluginContext(plugin_id="github-monitor")
    storage = FaultStorage()
    context.storage = storage  # type: ignore[assignment]
    first = TARGET
    second = TARGET.model_copy(update={"target_id": "2002"})
    subscription = _subscription(targets=(first, second))
    raw = _raw(99)
    event = normalize_event(REPOSITORY, raw)
    assert event is not None
    source = QueuedSourceEvent(
        github_event_id="99",
        source_fingerprint=raw_event_fingerprint(raw),
        source_created_at=event.created_at,
        normalized=event,
        target_snapshot=(_target_snapshot(first), _target_snapshot(second)),
    )
    prepared = build_prepared_notification(
        event_key="github:owner/repo:event:99",
        event_type="WatchEvent",
        target_type="group",
        target_id="2001",
        occurred_at=event.created_at,
        summary=event.summary,
        payload={"repository": REPOSITORY},
        text=event.summary,
    )
    deliveries = (
        TargetDelivery(
            target_key=delivery_target_key("group", "2001"),
            target_type="group",
            target_id="2001",
            send_text=True,
            send_card=False,
            ask_agent=False,
            status="attempting",
            prepared=prepared,
        ),
        TargetDelivery(
            target_key=delivery_target_key("group", "2002"),
            target_type="group",
            target_id="2002",
            send_text=True,
            send_card=False,
            ask_agent=False,
        ),
    )
    unit = DeliveryUnit(
        unit_id="github-unit-v1:remove-nonlast",
        members=(source,),
        deliveries=deliveries,
        sealed_at=datetime.now(UTC),
    )
    state = QueueState(
        accepted_cursor="99",
        accepted_fingerprint=source.source_fingerprint,
        committed_cursor="98",
        committed_fingerprint=raw_event_fingerprint(_raw(98)),
        committed_created_at=datetime.fromisoformat(
            str(_raw(98)["created_at"]).replace("Z", "+00:00")
        ),
        inflight=unit,
    )
    _seed_state(storage, state)
    config = _config(subscription)
    for key, value in config.model_dump(mode="json").items():
        await context.config.set(key, value)
    await context.notifications.grant_target(
        NotificationTarget(target_type="group", target_id="2001"),
        bot_user_id="test-bot",
    )
    poller = GitHubPoller(context, asyncio.Event())

    class Holder:
        pass

    holder = Holder()
    holder.context = context
    holder.poller = poller
    result = await GitHubCommands(holder, asyncio.Event()).handle(
        GitHubCommandArguments(text="remove owner/repo group:2001")
    )

    after = (await load_queue_state(context, REPOSITORY)).state
    assert result.ok is True
    assert [item.target.target_id for item in context.notifications.published] == ["2001"]
    assert after.inflight is not None
    assert [item.status for item in after.inflight.deliveries] == ["completed", "pending"]
    assert ("group", "2001") not in context.notifications.grants


@pytest.mark.asyncio
async def test_rebaseline_mode_survives_crash_between_reset_and_fetch() -> None:
    context = FakePluginContext(plugin_id="github-monitor")
    storage = FaultStorage()
    context.storage = storage  # type: ignore[assignment]
    _seed_state(storage, _cursor_state(98))
    subscription = _subscription()
    config = _config(subscription)
    for key, value in config.model_dump(mode="json").items():
        await context.config.set(key, value)
    poller = GitHubPoller(context, asyncio.Event())
    poller._client = PageClient({1: [_raw(100), _raw(99), _raw(98)]})  # type: ignore[assignment]
    storage.arm(2)
    with pytest.raises(QueueStateConflict):
        await poller.rebaseline(subscription, config, "replay_recent")
    interrupted = (await load_queue_state(context, REPOSITORY)).state
    assert interrupted.rebaseline_mode == "replay_recent"
    storage.arm(None)
    await poller.poll_repository(subscription, config)
    source_ids = [
        item.payload.get("source_event_ids", []) for item in context.notifications.published
    ]
    assert any("98" in item for item in source_ids)
    assert (await load_queue_state(context, REPOSITORY)).state.rebaseline_mode == ""


@pytest.mark.asyncio
async def test_remove_last_target_drains_queue_before_repository_disappears() -> None:
    context = FakePluginContext(plugin_id="github-monitor")
    storage = FaultStorage()
    context.storage = storage  # type: ignore[assignment]
    raw = _raw(99)
    event = normalize_event(REPOSITORY, raw)
    assert event is not None
    source = QueuedSourceEvent(
        github_event_id="99",
        source_fingerprint=raw_event_fingerprint(raw),
        source_created_at=event.created_at,
        normalized=event,
        target_snapshot=(_target_snapshot(),),
    )
    state = QueueState(
        accepted_cursor="99",
        accepted_fingerprint=source.source_fingerprint,
        committed_cursor="98",
        committed_fingerprint=raw_event_fingerprint(_raw(98)),
        committed_created_at=datetime.fromisoformat(
            str(_raw(98)["created_at"]).replace("Z", "+00:00")
        ),
        pending=(source,),
    )
    _seed_state(storage, state)
    subscription = _subscription()
    config = _config(subscription)
    for key, value in config.model_dump(mode="json").items():
        await context.config.set(key, value)
    poller = GitHubPoller(context, asyncio.Event())

    class Holder:
        pass

    holder = Holder()
    holder.context = context
    holder.poller = poller
    commands = GitHubCommands(holder, asyncio.Event())
    result = await commands.handle(GitHubCommandArguments(text="remove owner/repo group:2001"))
    final_config = await load_config(context)
    after = (await load_queue_state(context, REPOSITORY)).state
    assert result.ok is True
    assert final_config.repositories == ()
    assert after.pending == () and after.inflight is None
    assert after.committed_cursor == "99"
    assert context.notifications.published == []
    legacy = await storage.get(LEGACY_NAMESPACE, REPOSITORY)
    assert isinstance(legacy, dict)
    assert legacy["last_event_id"] == "99"
    assert (
        datetime.fromisoformat(str(legacy["last_event_created_at"]).replace("Z", "+00:00"))
        == event.created_at
    )


@pytest.mark.asyncio
async def test_pause_blocks_both_ingest_and_existing_drain() -> None:
    context = FakePluginContext(plugin_id="github-monitor")
    storage = FaultStorage()
    context.storage = storage  # type: ignore[assignment]
    source_raw = _raw(99)
    event = normalize_event(REPOSITORY, source_raw)
    assert event is not None
    source = QueuedSourceEvent(
        github_event_id="99",
        source_fingerprint=raw_event_fingerprint(source_raw),
        normalized=event,
    )
    state = QueueState(
        accepted_cursor="99",
        accepted_fingerprint=source.source_fingerprint,
        committed_cursor="98",
        committed_fingerprint=raw_event_fingerprint(_raw(98)),
        pending=(source,),
    )
    _seed_state(storage, state)
    subscription = _subscription(enabled=False)
    client = PageClient({1: [_raw(100), _raw(99)]})
    poller = GitHubPoller(context, asyncio.Event())
    poller._client = client  # type: ignore[assignment]
    await _poll(poller, context, subscription, _config(subscription))
    after = (await load_queue_state(context, REPOSITORY)).state
    assert after == state
    assert client.calls == []
    assert context.notifications.published == []


@pytest.mark.asyncio
async def test_repository_guard_reloads_config_after_waiting_for_pause() -> None:
    context = FakePluginContext(plugin_id="github-monitor")
    storage = FaultStorage()
    context.storage = storage  # type: ignore[assignment]
    _seed_state(storage, _cursor_state(98))
    subscription = _subscription()
    client = PageClient({1: [_raw(98)]})
    poller = GitHubPoller(context, asyncio.Event())
    poller._client = client  # type: ignore[assignment]
    guard = poller.repository_guard(REPOSITORY)
    await guard.acquire()
    task = asyncio.create_task(_poll(poller, context, subscription, _config(subscription)))
    await asyncio.sleep(0)
    assert client.calls == []
    paused = _config(_subscription(enabled=False))
    for key, value in paused.model_dump(mode="json").items():
        await context.config.set(key, value)
    guard.release()
    await task
    assert client.calls == []


@pytest.mark.asyncio
async def test_cross_repository_config_commands_do_not_lose_updates() -> None:
    context = FakePluginContext(plugin_id="github-monitor")
    config_facade = BlockingConfig()
    context.config = config_facade  # type: ignore[assignment]
    poller = GitHubPoller(context, asyncio.Event())

    class Holder:
        pass

    holder = Holder()
    holder.context = context
    holder.poller = poller
    commands = GitHubCommands(holder, asyncio.Event())
    first = asyncio.create_task(
        commands.handle(GitHubCommandArguments(text="add owner/one group:2001"))
    )
    await config_facade.first_repository_write.wait()
    second = asyncio.create_task(
        commands.handle(GitHubCommandArguments(text="add owner/two group:2002"))
    )
    await asyncio.sleep(0)
    config_facade.release_first_write.set()
    first_result, second_result = await asyncio.gather(first, second)

    configured = await load_config(context)
    assert first_result.ok is True and second_result.ok is True
    assert {item.repository for item in configured.repositories} == {
        "owner/one",
        "owner/two",
    }


@pytest.mark.asyncio
async def test_rebaseline_rejects_nonempty_queue_without_deleting_state() -> None:
    context = FakePluginContext(plugin_id="github-monitor")
    storage = FaultStorage()
    context.storage = storage  # type: ignore[assignment]
    raw = _raw(99)
    event = normalize_event(REPOSITORY, raw)
    assert event is not None
    source = QueuedSourceEvent(
        github_event_id="99",
        source_fingerprint=raw_event_fingerprint(raw),
        normalized=event,
    )
    state = QueueState(
        accepted_cursor="99",
        accepted_fingerprint=source.source_fingerprint,
        committed_cursor="98",
        committed_fingerprint=raw_event_fingerprint(_raw(98)),
        pending=(source,),
    )
    _seed_state(storage, state)
    subscription = _subscription()
    config = _config(subscription)
    for key, value in config.model_dump(mode="json").items():
        await context.config.set(key, value)
    poller = GitHubPoller(context, asyncio.Event())
    with pytest.raises(GitHubQueueGap, match="not_drained"):
        await poller.rebaseline(subscription, config, "baseline")
    assert (await load_queue_state(context, REPOSITORY)).state == state
    assert (QUEUE_NAMESPACE, REPOSITORY) in storage._values


@pytest.mark.asyncio
async def test_c4_drains_prepared_multi_member_unit() -> None:
    context = FakePluginContext(plugin_id="github-monitor")
    storage = FaultStorage()
    context.storage = storage  # type: ignore[assignment]
    sources = []
    for event_id in (99, 100):
        raw = _raw(event_id)
        event = normalize_event(REPOSITORY, raw)
        assert event is not None
        sources.append(
            QueuedSourceEvent(
                github_event_id=str(event_id),
                source_fingerprint=raw_event_fingerprint(raw),
                normalized=event,
            )
        )
    prepared = build_prepared_notification(
        event_key="github:owner/repo:batch:v1:test",
        event_type="github_event_batch",
        target_type="group",
        target_id="2001",
        occurred_at=datetime(2026, 8, 27, tzinfo=UTC),
        summary="two safe events",
        payload={"source_event_ids": ["99", "100"]},
        text="two safe events",
    )
    delivery = TargetDelivery(
        target_key=delivery_target_key("group", "2001"),
        target_type="group",
        target_id="2001",
        send_text=True,
        send_card=False,
        ask_agent=False,
        status="attempting",
        prepared=prepared,
    )
    unit = DeliveryUnit(
        unit_id="github-batch-v1:test",
        members=tuple(sources),
        deliveries=(delivery,),
        sealed_at=datetime(2026, 8, 27, tzinfo=UTC),
    )
    state = QueueState(
        accepted_cursor="100",
        accepted_fingerprint=sources[-1].source_fingerprint,
        committed_cursor="98",
        committed_fingerprint=raw_event_fingerprint(_raw(98)),
        inflight=unit,
    )
    _seed_state(storage, state)
    subscription = _subscription()
    poller = GitHubPoller(context, asyncio.Event())
    poller._client = PageClient({1: [_raw(100)]})  # type: ignore[assignment]
    await _poll(poller, context, subscription, _config(subscription))
    after = (await load_queue_state(context, REPOSITORY)).state
    assert after.committed_cursor == "100"
    assert after.inflight is None
    assert [item.event_key for item in context.notifications.published] == [
        "github:owner/repo:batch:v1:test"
    ]


@pytest.mark.asyncio
async def test_adjacent_delete_events_publish_one_body_free_batch() -> None:
    context = FakePluginContext(plugin_id="github-monitor")
    _seed_state(context.storage, _cursor_state(98))
    target = TARGET.model_copy(update={"ask_agent": True})
    subscription = _subscription(targets=(target,))
    config = _config(subscription)
    client = PageClient(
        {
            1: [
                _raw(100, event_type="DeleteEvent", action="deleted", ref="feature/b"),
                _raw(99, event_type="DeleteEvent", action="deleted", ref="feature/a"),
                _raw(98),
            ]
        }
    )
    poller = GitHubPoller(context, asyncio.Event())
    poller._client = client  # type: ignore[assignment]

    await _poll(poller, context, subscription, config)

    assert len(context.notifications.published) == 1
    request = context.notifications.published[0]
    assert request.event_type == "github_event_batch"
    assert request.event_key.startswith("github:owner/repo:batch:v1:")
    assert request.payload["source_event_ids"] == ["99", "100"]
    assert request.media_handles == ()
    assert request.ask_agent is True
    assert "feature/a" in request.text and "feature/b" in request.text
    assert "excerpt" not in str(request.payload)
    state = (await load_queue_state(context, REPOSITORY)).state
    assert state.committed_cursor == "100"
    assert not state.pending and state.inflight is None


@pytest.mark.asyncio
async def test_coalesce_false_drains_unsealed_events_as_singletons() -> None:
    context = FakePluginContext(plugin_id="github-monitor")
    _seed_state(context.storage, _cursor_state(98))
    subscription = _subscription()
    config = _config(subscription).model_copy(update={"coalesce": False})
    client = PageClient(
        {
            1: [
                _raw(100, event_type="DeleteEvent", action="deleted"),
                _raw(99, event_type="DeleteEvent", action="deleted"),
                _raw(98),
            ]
        }
    )
    poller = GitHubPoller(context, asyncio.Event())
    poller._client = client  # type: ignore[assignment]

    await _poll(poller, context, subscription, config)

    assert [item.event_type for item in context.notifications.published] == [
        "DeleteEvent",
        "DeleteEvent",
    ]
    assert [item.payload["branch"] for item in context.notifications.published] == [
        "feature/99",
        "feature/100",
    ]


@pytest.mark.asyncio
async def test_batch_budget_counts_source_events_not_delivery_units() -> None:
    context = FakePluginContext(plugin_id="github-monitor")
    _seed_state(context.storage, _cursor_state(98))
    subscription = _subscription()
    config = _config(subscription, maximum=2)
    client = PageClient(
        {
            1: [
                _raw(101, event_type="DeleteEvent", action="deleted"),
                _raw(100, event_type="DeleteEvent", action="deleted"),
                _raw(99, event_type="DeleteEvent", action="deleted"),
                _raw(98),
            ]
        }
    )
    poller = GitHubPoller(context, asyncio.Event())
    poller._client = client  # type: ignore[assignment]

    await _poll(poller, context, subscription, config)

    assert len(context.notifications.published) == 1
    assert context.notifications.published[0].payload["source_event_ids"] == ["99", "100"]
    state = (await load_queue_state(context, REPOSITORY)).state
    assert state.committed_cursor == "100"
    assert [item.github_event_id for item in state.pending] == ["101"]


@pytest.mark.asyncio
async def test_prepared_batch_target_can_be_skipped_before_first_attempt() -> None:
    context = FakePluginContext(plugin_id="github-monitor")
    members = (
        _queued(_raw(99, event_type="DeleteEvent", action="deleted")),
        _queued(_raw(100, event_type="DeleteEvent", action="deleted")),
    )
    poller = GitHubPoller(context, asyncio.Event())
    unit = DeliveryUnit(
        unit_id=poller._unit_id(REPOSITORY, members),
        members=members,
        deliveries=poller._aggregate_deliveries(members, ""),
        sealed_at=datetime.now(UTC),
    )
    boundary = _cursor_state(98)
    _seed_state(
        context.storage,
        boundary.model_copy(
            update={
                "accepted_cursor": "100",
                "accepted_fingerprint": members[-1].source_fingerprint,
                "inflight": unit,
            }
        ),
    )
    disabled_target = TARGET.model_copy(update={"send_text": False})
    subscription = _subscription(targets=(disabled_target,))
    snapshot = await load_queue_state(context, REPOSITORY)

    result = await poller._drain_queue(
        subscription,
        snapshot,
        max_events=50,
        max_batch_members=50,
        coalesce=True,
    )

    assert context.notifications.published == []
    assert result.state.committed_cursor == "100"
    assert result.state.inflight is None


@pytest.mark.asyncio
async def test_coalesce_false_does_not_change_an_already_sealed_batch() -> None:
    context = FakePluginContext(plugin_id="github-monitor")
    members = (
        _queued(_raw(99, event_type="DeleteEvent", action="deleted")),
        _queued(_raw(100, event_type="DeleteEvent", action="deleted")),
    )
    poller = GitHubPoller(context, asyncio.Event())
    deliveries = poller._aggregate_deliveries(members, "")
    frozen_hash = deliveries[0].prepared.request_hash if deliveries[0].prepared else ""
    unit = DeliveryUnit(
        unit_id=poller._unit_id(REPOSITORY, members),
        members=members,
        deliveries=deliveries,
        sealed_at=datetime.now(UTC),
    )
    boundary = _cursor_state(98)
    _seed_state(
        context.storage,
        boundary.model_copy(
            update={
                "accepted_cursor": "100",
                "accepted_fingerprint": members[-1].source_fingerprint,
                "inflight": unit,
            }
        ),
    )
    snapshot = await load_queue_state(context, REPOSITORY)

    result = await poller._drain_queue(
        _subscription(),
        snapshot,
        max_events=50,
        max_batch_members=50,
        coalesce=False,
    )

    assert len(context.notifications.published) == 1
    assert context.notifications.published[0].event_type == "github_event_batch"
    assert deliveries[0].prepared is not None
    assert deliveries[0].prepared.request_hash == frozen_hash
    assert result.state.committed_cursor == "100"


@pytest.mark.asyncio
async def test_unbounded_drain_still_caps_each_sealed_batch() -> None:
    context = FakePluginContext(plugin_id="github-monitor")
    members = tuple(
        _queued(_raw(event_id, event_type="DeleteEvent", action="deleted"))
        for event_id in range(99, 104)
    )
    boundary = _cursor_state(98)
    _seed_state(
        context.storage,
        boundary.model_copy(
            update={
                "accepted_cursor": "103",
                "accepted_fingerprint": members[-1].source_fingerprint,
                "pending": members,
            }
        ),
    )
    poller = GitHubPoller(context, asyncio.Event())
    snapshot = await load_queue_state(context, REPOSITORY)

    result = await poller._drain_queue(
        _subscription(),
        snapshot,
        max_events=None,
        max_batch_members=2,
        coalesce=True,
    )

    assert len(context.notifications.published) == 3
    assert context.notifications.published[0].payload["source_event_ids"] == ["99", "100"]
    assert context.notifications.published[1].payload["source_event_ids"] == ["101", "102"]
    assert context.notifications.published[2].event_key == "github:owner/repo:event:103"
    assert result.state.committed_cursor == "103"
    assert not result.state.pending and result.state.inflight is None
