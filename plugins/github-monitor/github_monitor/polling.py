"""CAS/WAL GitHub event ingestion and byte-stable Host publication."""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from yuki_plugin_sdk.context import PluginContext
from yuki_plugin_sdk.models import NotificationTarget, PublishNotificationRequest

from .client import GitHubClient
from .config import (
    GitHubMonitorConfig,
    NotificationTargetConfig,
    RepositorySubscription,
    load_config,
)
from .errors import GitHubAPIError
from .events import (
    GitHubEventIdentityError,
    deduplicate_raw_events,
    event_allowed,
    event_key_for_boundary,
    normalize_event,
    raw_event_fingerprint,
)
from .formatter import apply_compare, external_payload, notification_text
from .models import (
    ActivationState,
    DeliveryUnit,
    GitHubAPIResponse,
    NormalizedGitHubEvent,
    PreparedMedia,
    PreparedNotification,
    QueuedSourceEvent,
    QueueState,
    TargetDelivery,
    TargetPolicySnapshot,
    build_prepared_notification,
    delivery_target_key,
)
from .renderer import render_event_card
from .state import (
    QueueSnapshot,
    QueueStateConflict,
    compare_and_set_queue_state,
    load_queue_state,
    mirror_legacy_state,
)

AGENT_INTENT = "根据当前主会话关系和仓库事件，自然说一句真实反应；不要复述完整卡片。"
MAX_EVENT_PAGES = 10
MEDIA_TTL_SECONDS = 7 * 24 * 60 * 60


class GitHubQueueGap(RuntimeError):
    """Continuity cannot advance without an explicit operator decision."""


class CoordinatorRegistry:
    """Process-local serialization; persisted CAS covers other processes."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._config_lock = asyncio.Lock()

    def lock(self, repository: str) -> asyncio.Lock:
        return self._locks.setdefault(repository.casefold(), asyncio.Lock())

    @asynccontextmanager
    async def configuration_lock(self, repository: str) -> AsyncIterator[None]:
        async with self._config_lock:
            async with self.lock(repository):
                yield


_SHARED_COORDINATORS = CoordinatorRegistry()


class GitHubPoller:
    def __init__(
        self,
        context: PluginContext,
        stop: asyncio.Event,
        coordinators: CoordinatorRegistry | None = None,
    ) -> None:
        self._context = context
        self._stop = stop
        self._client = GitHubClient(context)
        self._coordinators = coordinators or _SHARED_COORDINATORS

    def repository_guard(self, repository: str) -> asyncio.Lock:
        return self._coordinators.lock(repository)

    def configuration_guard(self, repository: str) -> AbstractAsyncContextManager[None]:
        return self._coordinators.configuration_lock(repository)

    async def run(self) -> None:
        while not self._stop.is_set():
            config = await load_config(self._context)
            started = datetime.now(UTC)
            for subscription in config.repositories:
                if self._stop.is_set():
                    break
                if not subscription.enabled:
                    continue
                try:
                    await self.poll_repository(subscription, config)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._context.logger.warning(
                        "github_poll_failed repository=%s error_category=%s",
                        subscription.repository,
                        type(exc).__name__,
                    )
            elapsed = (datetime.now(UTC) - started).total_seconds()
            delay = max(1.0, config.poll_interval_seconds - elapsed)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except TimeoutError:
                pass

    async def poll_repository(
        self,
        subscription: RepositorySubscription,
        config: GitHubMonitorConfig,
    ) -> None:
        async with self.repository_guard(subscription.repository):
            current_config = await load_config(self._context)
            current_subscription = self._find_subscription(current_config, subscription.repository)
            if current_subscription is None:
                return
            await self._poll_repository_locked(current_subscription, current_config)

    async def rebaseline(
        self,
        subscription: RepositorySubscription,
        config: GitHubMonitorConfig,
        mode: str,
    ) -> None:
        if mode not in {"baseline", "replay_recent"}:
            raise ValueError("invalid rebaseline mode")
        async with self.repository_guard(subscription.repository):
            config = await load_config(self._context)
            current_subscription = next(
                (
                    item
                    for item in config.repositories
                    if item.repository.casefold() == subscription.repository.casefold()
                ),
                None,
            )
            if current_subscription is None:
                raise GitHubQueueGap("github_repository_not_configured")
            subscription = current_subscription
            snapshot = await load_queue_state(self._context, subscription.repository)
            if snapshot.state.gap_reason and snapshot.state.inflight is not None:
                snapshot = await self._resolve_expired_media_gap_for_rebaseline(
                    subscription,
                    snapshot,
                )
            if snapshot.state.pending or snapshot.state.inflight is not None:
                raise GitHubQueueGap("github_queue_not_drained")
            if snapshot.state.activation is not None and not snapshot.state.activation.complete:
                raise GitHubQueueGap("github_activation_not_drained")
            reset = snapshot.state.model_copy(
                update={
                    "accepted_cursor": "",
                    "accepted_fingerprint": "",
                    "committed_cursor": "",
                    "committed_fingerprint": "",
                    "committed_created_at": None,
                    "etag": "",
                    "last_modified": "",
                    "gap_reason": "",
                    "gap_at": None,
                    "backlog_pending": False,
                    "rebaseline_mode": mode,
                }
            )
            await compare_and_set_queue_state(
                self._context,
                subscription.repository,
                snapshot,
                reset,
                allow_rebaseline=True,
            )
            effective = config.model_copy(update={"initial_sync_mode": mode})
            await self._poll_repository_locked(subscription, effective)

    async def _resolve_expired_media_gap_for_rebaseline(
        self,
        subscription: RepositorySubscription,
        snapshot: QueueSnapshot,
    ) -> QueueSnapshot:
        if snapshot.state.gap_reason != "github_prepared_media_expired":
            raise GitHubQueueGap("github_queue_not_drained")
        unit = snapshot.state.inflight
        if unit is None:
            return snapshot
        now = datetime.now(UTC)
        changed = False
        deliveries: list[TargetDelivery] = []
        for delivery in unit.deliveries:
            prepared = delivery.prepared
            expired = bool(
                delivery.status == "attempting"
                and prepared is not None
                and any(
                    item.expires_at is None or item.expires_at <= now for item in prepared.media
                )
            )
            if expired:
                delivery = delivery.model_copy(
                    update={
                        "status": "skipped",
                        "skipped_reason": "prepared_media_expired",
                    }
                )
                changed = True
            deliveries.append(delivery)
        if not changed:
            raise GitHubQueueGap("github_expired_media_gap_not_resolvable")
        recovered = snapshot.state.model_copy(
            update={
                "inflight": unit.model_copy(update={"deliveries": tuple(deliveries)}),
                "gap_reason": "",
                "gap_at": None,
            }
        )
        snapshot = await compare_and_set_queue_state(
            self._context,
            subscription.repository,
            snapshot,
            recovered,
            allow_rebaseline=True,
        )
        return await self._drain_queue(
            subscription,
            snapshot,
            max_units=1,
        )

    async def retire_target_locked(
        self,
        subscription: RepositorySubscription,
        target: NotificationTargetConfig,
        *,
        last_target: bool,
    ) -> None:
        """Resolve a removed target before its Host grant can be revoked.

        The caller must hold ``repository_guard(subscription.repository)``.
        """

        snapshot = await load_queue_state(self._context, subscription.repository)
        target_key = delivery_target_key(target.target_type, target.target_id)
        activation_delivery = (
            self._find_optional_delivery(snapshot.state.activation.deliveries, target_key)
            if snapshot.state.activation is not None
            else None
        )
        inflight_delivery = (
            self._find_optional_delivery(snapshot.state.inflight.deliveries, target_key)
            if snapshot.state.inflight is not None
            else None
        )
        target_has_work = any(
            item is not None and item.status not in {"completed", "skipped"}
            for item in (activation_delivery, inflight_delivery)
        )
        repository_has_work = bool(
            snapshot.state.pending
            or snapshot.state.inflight is not None
            or (snapshot.state.activation is not None and not snapshot.state.activation.complete)
        )
        if not target_has_work and not (last_target and repository_has_work):
            if snapshot.state.rebaseline_mode:
                raise GitHubQueueGap("github_rebaseline_in_progress")
            if last_target:
                await self._mirror_if_drained(subscription.repository, snapshot.state)
            return
        now = datetime.now(UTC)
        if not subscription.enabled or (
            snapshot.state.paused_until is not None and snapshot.state.paused_until > now
        ):
            raise GitHubQueueGap("github_last_target_removal_requires_resumed_queue")
        if snapshot.state.gap_reason:
            raise GitHubQueueGap("github_target_removal_requires_gap_resolution")
        remaining_targets = tuple(
            item
            for item in subscription.targets
            if (item.target_type, item.target_id) != (target.target_type, target.target_id)
        )
        without_target = subscription.model_copy(update={"targets": remaining_targets})
        snapshot = await self._retire_activation_target(
            without_target,
            snapshot,
            target_key,
        )
        if snapshot.state.inflight is not None:
            delivery = self._find_optional_delivery(
                snapshot.state.inflight.deliveries,
                target_key,
            )
            if delivery is not None and delivery.status not in {"completed", "skipped"}:
                snapshot = await self._drain_unit_target(
                    without_target,
                    snapshot,
                    target_key,
                )
                unresolved = (
                    self._find_optional_delivery(
                        snapshot.state.inflight.deliveries,
                        target_key,
                    )
                    if snapshot.state.inflight is not None
                    else None
                )
                if unresolved is not None and unresolved.status not in {"completed", "skipped"}:
                    raise GitHubQueueGap("github_target_removal_not_drained")
        if last_target:
            snapshot = await self._drain_activation(without_target, snapshot)
            snapshot = await self._drain_queue(without_target, snapshot)
            if (
                snapshot.state.pending
                or snapshot.state.inflight is not None
                or (
                    snapshot.state.activation is not None and not snapshot.state.activation.complete
                )
            ):
                raise GitHubQueueGap("github_last_target_removal_not_drained")
            await self._mirror_if_drained(subscription.repository, snapshot.state)

    async def _poll_repository_locked(
        self,
        subscription: RepositorySubscription,
        config: GitHubMonitorConfig,
    ) -> None:
        if not subscription.enabled:
            return
        repository = subscription.repository
        snapshot = await load_queue_state(self._context, repository)
        if snapshot.raw is None and snapshot.state.legacy_imported:
            snapshot = await compare_and_set_queue_state(
                self._context,
                repository,
                snapshot,
                snapshot.state,
            )
        now = datetime.now(UTC)
        if snapshot.state.paused_until is not None and snapshot.state.paused_until > now:
            return
        if snapshot.state.gap_reason:
            return
        had_queued_work = bool(snapshot.state.pending or snapshot.state.inflight is not None)
        snapshot = await self._drain_activation(subscription, snapshot)
        snapshot = await self._drain_queue(
            subscription,
            snapshot,
            max_units=config.max_events_per_poll,
        )
        if had_queued_work:
            await self._mirror_if_drained(repository, snapshot.state)
            return
        if snapshot.state.pending or snapshot.state.inflight is not None:
            return
        self._context.logger.info("github_poll_started repository=%s", repository)
        try:
            response, raw_events, cursor_found = await self._fetch_pages(
                repository,
                snapshot.state,
                config,
            )
        except GitHubAPIError as exc:
            await self._record_failure(repository, snapshot, exc)
            return
        except GitHubEventIdentityError as exc:
            await self._record_gap(repository, snapshot, str(exc))
            return
        now = datetime.now(UTC)
        if response.status_code == 304:
            if snapshot.state.backlog_pending:
                await self._record_gap(
                    repository,
                    snapshot,
                    "github_backlog_returned_not_modified",
                )
                return
            snapshot = await self._write_success_metadata(repository, snapshot, response, now)
            await self._mirror_if_drained(repository, snapshot.state)
            return
        if snapshot.state.accepted_cursor and not cursor_found:
            await self._record_gap(repository, snapshot, "github_cursor_missing_from_overlap")
            return
        try:
            ordered = deduplicate_raw_events(raw_events)
        except GitHubEventIdentityError as exc:
            await self._record_gap(repository, snapshot, str(exc))
            return
        if snapshot.state.accepted_cursor:
            boundary = next(
                (raw for raw in ordered if str(raw["id"]) == snapshot.state.accepted_cursor),
                None,
            )
            if boundary is None:
                await self._record_gap(repository, snapshot, "github_cursor_missing_from_overlap")
                return
            boundary_fingerprint = raw_event_fingerprint(boundary)
            if (
                snapshot.state.accepted_fingerprint
                and snapshot.state.accepted_fingerprint != boundary_fingerprint
            ):
                await self._record_gap(repository, snapshot, "github_cursor_payload_conflict")
                return
            if not snapshot.state.accepted_fingerprint:
                updates: dict[str, object] = {"accepted_fingerprint": boundary_fingerprint}
                if (
                    snapshot.state.committed_cursor == snapshot.state.accepted_cursor
                    and not snapshot.state.committed_fingerprint
                ):
                    updates["committed_fingerprint"] = boundary_fingerprint
                adopted = snapshot.state.model_copy(update=updates)
                snapshot = await compare_and_set_queue_state(
                    self._context, repository, snapshot, adopted
                )
        if not snapshot.state.accepted_cursor:
            try:
                snapshot = await self._initialize(
                    subscription,
                    config,
                    snapshot,
                    ordered,
                    response,
                    now,
                )
            except GitHubEventIdentityError as exc:
                await self._record_gap(repository, snapshot, str(exc))
                return
        else:
            candidates = [
                raw for raw in ordered if int(str(raw["id"])) > int(snapshot.state.accepted_cursor)
            ]
            if candidates:
                try:
                    queued = tuple(
                        [*snapshot.state.pending]
                        + [await self._queue_source(subscription, raw) for raw in candidates]
                    )
                except GitHubEventIdentityError as exc:
                    await self._record_gap(repository, snapshot, str(exc))
                    return
                state = snapshot.state.model_copy(
                    update={
                        "accepted_cursor": str(candidates[-1]["id"]),
                        "accepted_fingerprint": raw_event_fingerprint(candidates[-1]),
                        "pending": queued,
                        "backlog_pending": len(candidates) > config.max_events_per_poll,
                        **self._response_metadata(response, now, snapshot.state),
                    }
                )
                snapshot = await compare_and_set_queue_state(
                    self._context, repository, snapshot, state
                )
            else:
                snapshot = await self._write_success_metadata(repository, snapshot, response, now)
        snapshot = await self._drain_activation(subscription, snapshot)
        snapshot = await self._drain_queue(
            subscription,
            snapshot,
            max_units=config.max_events_per_poll,
        )
        snapshot = await self._mark_success(repository, snapshot, response, now)
        await self._mirror_if_drained(repository, snapshot.state)
        self._context.logger.info(
            "github_poll_completed repository=%s accepted=%s committed=%s backlog=%s",
            repository,
            snapshot.state.accepted_cursor,
            snapshot.state.committed_cursor,
            snapshot.state.backlog_pending,
        )

    async def _fetch_pages(
        self,
        repository: str,
        state: QueueState,
        config: GitHubMonitorConfig,
    ) -> tuple[GitHubAPIResponse, list[dict[str, object]], bool]:
        conditional = not state.backlog_pending
        response = await self._client.repository_events(
            repository,
            per_page=config.events_per_repository,
            etag=state.etag if conditional else "",
            last_modified=state.last_modified if conditional else "",
            page=1,
        )
        if response.status_code == 304:
            return response, [], True
        raw_events = self._raw_event_page(response.body)
        cursor_found = not state.accepted_cursor or any(
            isinstance(item, dict) and str(item.get("id", "")) == state.accepted_cursor
            for item in raw_events
        )
        page = 1
        link = response.headers.get("link", "")
        while (
            state.accepted_cursor
            and not cursor_found
            and 'rel="next"' in link
            and page < MAX_EVENT_PAGES
        ):
            page += 1
            extra = await self._client.repository_events(
                repository,
                per_page=config.events_per_repository,
                page=page,
            )
            rows = self._raw_event_page(extra.body)
            raw_events.extend(rows)
            cursor_found = any(
                isinstance(item, dict) and str(item.get("id", "")) == state.accepted_cursor
                for item in rows
            )
            link = extra.headers.get("link", "")
        return response, raw_events, cursor_found

    async def _initialize(
        self,
        subscription: RepositorySubscription,
        config: GitHubMonitorConfig,
        snapshot: QueueSnapshot,
        ordered: tuple[dict[str, object], ...],
        response: GitHubAPIResponse,
        now: datetime,
    ) -> QueueSnapshot:
        state = snapshot.state
        activation = state.activation or ActivationState(
            activation_id=uuid4().hex,
            occurred_at=now,
            deliveries=tuple(self._target_delivery(target) for target in subscription.targets),
        )
        updates: dict[str, object] = {
            "activation": activation,
            **self._response_metadata(response, now, state),
        }
        mode = state.rebaseline_mode or config.initial_sync_mode
        updates["rebaseline_mode"] = ""
        if ordered:
            if mode == "baseline":
                newest_id = str(ordered[-1]["id"])
                updates.update(
                    {
                        "accepted_cursor": newest_id,
                        "accepted_fingerprint": raw_event_fingerprint(ordered[-1]),
                        "committed_cursor": newest_id,
                        "committed_fingerprint": raw_event_fingerprint(ordered[-1]),
                        "committed_created_at": self._raw_created_at(ordered[-1]),
                        "pending": (),
                    }
                )
            else:
                replay = ordered[-config.replay_recent_limit :]
                first_index = len(ordered) - len(replay)
                committed = str(ordered[first_index - 1]["id"]) if first_index else ""
                committed_fingerprint = (
                    raw_event_fingerprint(ordered[first_index - 1]) if first_index else ""
                )
                updates.update(
                    {
                        "accepted_cursor": str(replay[-1]["id"]),
                        "accepted_fingerprint": raw_event_fingerprint(replay[-1]),
                        "committed_cursor": committed,
                        "committed_fingerprint": committed_fingerprint,
                        "committed_created_at": (
                            self._raw_created_at(ordered[first_index - 1]) if first_index else None
                        ),
                        "pending": tuple(
                            [await self._queue_source(subscription, raw) for raw in replay]
                        ),
                    }
                )
        initialized = state.model_copy(update=updates)
        return await compare_and_set_queue_state(
            self._context,
            subscription.repository,
            snapshot,
            initialized,
            allow_rebaseline=True,
        )

    async def _queue_source(
        self,
        subscription: RepositorySubscription,
        raw: dict[str, object],
    ) -> QueuedSourceEvent:
        event_id = str(raw["id"])
        fingerprint = raw_event_fingerprint(raw)
        source_created_at = self._raw_created_at(raw)
        try:
            event = normalize_event(subscription.repository, raw)
        except (TypeError, ValueError) as exc:
            raise GitHubEventIdentityError("github_event_normalization_failed") from exc
        if event is None:
            return QueuedSourceEvent(
                github_event_id=event_id,
                source_fingerprint=fingerprint,
                source_created_at=source_created_at,
                skip_reason="unsupported_event",
            )
        if not event_allowed(event, subscription):
            return QueuedSourceEvent(
                github_event_id=event_id,
                source_fingerprint=fingerprint,
                source_created_at=source_created_at,
                skip_reason="filtered_event",
            )
        event = await self._enrich_push(event)
        return QueuedSourceEvent(
            github_event_id=event_id,
            source_fingerprint=fingerprint,
            source_created_at=source_created_at,
            normalized=event,
            target_snapshot=tuple(self._target_snapshot(target) for target in subscription.targets),
        )

    async def _drain_queue(
        self,
        subscription: RepositorySubscription,
        snapshot: QueueSnapshot,
        *,
        max_units: int | None = None,
    ) -> QueueSnapshot:
        completed_units = 0
        while not self._stop.is_set():
            if max_units is not None and completed_units >= max_units:
                return snapshot
            if snapshot.state.gap_reason:
                return snapshot
            if snapshot.state.inflight is None:
                if not snapshot.state.pending:
                    return snapshot
                source = snapshot.state.pending[0]
                deliveries = tuple(
                    self._target_delivery(target) for target in source.target_snapshot
                )
                unit = DeliveryUnit(
                    unit_id=self._unit_id(subscription.repository, (source,)),
                    members=(source,),
                    deliveries=deliveries,
                    sealed_at=datetime.now(UTC),
                )
                state = snapshot.state.model_copy(
                    update={"pending": snapshot.state.pending[1:], "inflight": unit}
                )
                snapshot = await compare_and_set_queue_state(
                    self._context, subscription.repository, snapshot, state
                )
            current_unit = snapshot.state.inflight
            if current_unit is None:
                continue
            for delivery in current_unit.deliveries:
                if delivery.status in {"completed", "skipped"}:
                    continue
                snapshot = await self._drain_unit_target(
                    subscription, snapshot, delivery.target_key
                )
                if snapshot.state.gap_reason:
                    return snapshot
                current_unit = snapshot.state.inflight
                if current_unit is None:
                    break
            current_unit = snapshot.state.inflight
            if current_unit is None:
                continue
            if any(item.status not in {"completed", "skipped"} for item in current_unit.deliveries):
                return snapshot
            committed = current_unit.members[-1].github_event_id
            committed_fingerprint = current_unit.members[-1].source_fingerprint
            state = snapshot.state.model_copy(
                update={
                    "inflight": None,
                    "committed_cursor": committed,
                    "committed_fingerprint": committed_fingerprint,
                    "committed_created_at": current_unit.members[-1].source_created_at,
                }
            )
            snapshot = await compare_and_set_queue_state(
                self._context, subscription.repository, snapshot, state
            )
            completed_units += 1
        return snapshot

    async def _drain_unit_target(
        self,
        subscription: RepositorySubscription,
        snapshot: QueueSnapshot,
        target_key: str,
    ) -> QueueSnapshot:
        if snapshot.state.gap_reason:
            return snapshot
        unit = snapshot.state.inflight
        if unit is None:
            return snapshot
        delivery = self._find_delivery(unit.deliveries, target_key)
        if delivery.status in {"pending", "prepared"}:
            current_target = self._find_target(
                subscription, delivery.target_type, delivery.target_id
            )
            if self._target_was_disabled(delivery, current_target):
                skipped = delivery.model_copy(
                    update={"status": "skipped", "skipped_reason": "target_disabled"}
                )
                return await self._replace_unit_delivery(subscription.repository, snapshot, skipped)
        if delivery.status == "pending":
            if len(unit.members) != 1:
                raise GitHubQueueGap("github_multi_member_request_not_prepared")
            source = unit.members[0]
            if source.normalized is None:
                skipped = delivery.model_copy(
                    update={"status": "skipped", "skipped_reason": source.skip_reason}
                )
                return await self._replace_unit_delivery(subscription.repository, snapshot, skipped)
            prepared = await self._prepare_event_request(
                source.normalized,
                self._delivery_policy(delivery),
                snapshot.state.legacy_boundary,
            )
            delivery = delivery.model_copy(
                update={
                    "status": "prepared",
                    "prepared": prepared,
                }
            )
            snapshot = await self._replace_unit_delivery(
                subscription.repository, snapshot, delivery
            )
        unit = snapshot.state.inflight
        if unit is None:
            return snapshot
        delivery = self._find_delivery(unit.deliveries, target_key)
        if delivery.status == "prepared":
            delivery = delivery.model_copy(update={"status": "attempting"})
            snapshot = await self._replace_unit_delivery(
                subscription.repository, snapshot, delivery
            )
        unit = snapshot.state.inflight
        if unit is None:
            return snapshot
        delivery = self._find_delivery(unit.deliveries, target_key)
        if delivery.status != "attempting" or delivery.prepared is None:
            return snapshot
        try:
            self._require_prepared_media_live(delivery.prepared)
        except GitHubQueueGap as exc:
            return await self._record_gap(
                subscription.repository,
                snapshot,
                str(exc),
            )
        receipt = await self._context.notifications.publish(
            self._publish_request(delivery.prepared)
        )
        completed = delivery.model_copy(
            update={
                "status": "completed",
                "notification_id": receipt.notification_id,
                "source_event_id": receipt.source_event_id,
                "completed_at": datetime.now(UTC),
            }
        )
        return await self._replace_unit_delivery(subscription.repository, snapshot, completed)

    async def _drain_activation(
        self,
        subscription: RepositorySubscription,
        snapshot: QueueSnapshot,
    ) -> QueueSnapshot:
        activation = snapshot.state.activation
        if activation is None or activation.complete or not subscription.enabled:
            return snapshot
        for delivery in activation.deliveries:
            if delivery.status in {"completed", "skipped"}:
                continue
            current = self._find_delivery(snapshot.state.activation.deliveries, delivery.target_key)  # type: ignore[union-attr]
            if current.status in {"pending", "prepared"}:
                target = self._find_target(subscription, current.target_type, current.target_id)
                if self._target_was_disabled(current, target):
                    current = current.model_copy(
                        update={"status": "skipped", "skipped_reason": "target_disabled"}
                    )
                elif current.status == "pending":
                    current = current.model_copy(
                        update={
                            "status": "prepared",
                            "prepared": build_prepared_notification(
                                event_key=self._activation_event_key(
                                    subscription.repository,
                                    activation.activation_id,
                                ),
                                event_type="monitor_enabled",
                                target_type=current.target_type,
                                target_id=current.target_id,
                                occurred_at=activation.occurred_at,
                                summary=f"已启用 {subscription.repository} 的 GitHub 监控",
                                payload={
                                    "repository": subscription.repository,
                                    "activation_id": activation.activation_id,
                                },
                                text=f"GitHub 监控已启用：{subscription.repository}",
                            ),
                        }
                    )
                snapshot = await self._replace_activation_delivery(
                    subscription.repository, snapshot, current
                )
            activation = snapshot.state.activation
            if activation is None:
                return snapshot
            current = self._find_delivery(activation.deliveries, delivery.target_key)
            if current.status == "prepared":
                current = current.model_copy(update={"status": "attempting"})
                snapshot = await self._replace_activation_delivery(
                    subscription.repository, snapshot, current
                )
            activation = snapshot.state.activation
            if activation is None:
                return snapshot
            current = self._find_delivery(activation.deliveries, delivery.target_key)
            if current.status == "attempting" and current.prepared is not None:
                receipt = await self._context.notifications.publish(
                    self._publish_request(current.prepared)
                )
                current = current.model_copy(
                    update={
                        "status": "completed",
                        "notification_id": receipt.notification_id,
                        "source_event_id": receipt.source_event_id,
                        "completed_at": datetime.now(UTC),
                    }
                )
                snapshot = await self._replace_activation_delivery(
                    subscription.repository, snapshot, current
                )
        return snapshot

    async def _retire_activation_target(
        self,
        subscription: RepositorySubscription,
        snapshot: QueueSnapshot,
        target_key: str,
    ) -> QueueSnapshot:
        activation = snapshot.state.activation
        if activation is None:
            return snapshot
        current = self._find_optional_delivery(activation.deliveries, target_key)
        if current is None or current.status in {"completed", "skipped"}:
            return snapshot
        if current.status in {"pending", "prepared"}:
            skipped = current.model_copy(
                update={"status": "skipped", "skipped_reason": "target_disabled"}
            )
            return await self._replace_activation_delivery(
                subscription.repository,
                snapshot,
                skipped,
            )
        if current.prepared is None:
            raise GitHubQueueGap("github_activation_request_not_prepared")
        self._require_prepared_media_live(current.prepared)
        receipt = await self._context.notifications.publish(self._publish_request(current.prepared))
        completed = current.model_copy(
            update={
                "status": "completed",
                "notification_id": receipt.notification_id,
                "source_event_id": receipt.source_event_id,
                "completed_at": datetime.now(UTC),
            }
        )
        return await self._replace_activation_delivery(
            subscription.repository,
            snapshot,
            completed,
        )

    async def _replace_unit_delivery(
        self,
        repository: str,
        snapshot: QueueSnapshot,
        delivery: TargetDelivery,
    ) -> QueueSnapshot:
        unit = snapshot.state.inflight
        if unit is None:
            raise QueueStateConflict("github_inflight_disappeared")
        deliveries = tuple(
            delivery if item.target_key == delivery.target_key else item for item in unit.deliveries
        )
        state = snapshot.state.model_copy(
            update={"inflight": unit.model_copy(update={"deliveries": deliveries})}
        )
        return await compare_and_set_queue_state(self._context, repository, snapshot, state)

    async def _replace_activation_delivery(
        self,
        repository: str,
        snapshot: QueueSnapshot,
        delivery: TargetDelivery,
    ) -> QueueSnapshot:
        activation = snapshot.state.activation
        if activation is None:
            raise QueueStateConflict("github_activation_disappeared")
        deliveries = tuple(
            delivery if item.target_key == delivery.target_key else item
            for item in activation.deliveries
        )
        state = snapshot.state.model_copy(
            update={"activation": activation.model_copy(update={"deliveries": deliveries})}
        )
        return await compare_and_set_queue_state(self._context, repository, snapshot, state)

    async def _prepare_event_request(
        self,
        event: NormalizedGitHubEvent,
        target: NotificationTargetConfig,
        legacy_boundary: str | None,
    ) -> PreparedNotification:
        event_key = (
            event.event_key
            if legacy_boundary is None
            else event_key_for_boundary(event, legacy_boundary)
        )
        keyed = event.model_copy(update={"event_key": event_key})
        media: tuple[PreparedMedia, ...] = ()
        if target.send_card:
            try:
                rendered = await asyncio.to_thread(render_event_card, keyed)
                if rendered is not None:
                    png, filename = rendered
                    handle = await self._context.media.create_artifact(
                        data=png,
                        content_type="image/png",
                        filename=filename,
                        ttl_seconds=MEDIA_TTL_SECONDS,
                    )
                    media = (
                        PreparedMedia(
                            index=0,
                            handle_id=handle.handle_id,
                            sha256=handle.sha256,
                            expires_at=handle.expires_at,
                        ),
                    )
            except Exception as exc:
                self._context.logger.warning(
                    "github_card_render_failed repository=%s error_category=%s",
                    keyed.repository,
                    type(exc).__name__,
                )
        return build_prepared_notification(
            event_key=keyed.event_key,
            event_type=keyed.event_type,
            target_type=target.target_type,
            target_id=target.target_id,
            occurred_at=keyed.created_at,
            summary=keyed.summary,
            payload=external_payload(keyed),
            text=notification_text(keyed) if target.send_text else "",
            media=media,
            ask_agent=target.ask_agent,
            agent_intent=AGENT_INTENT if target.ask_agent else "",
        )

    async def publish_event(
        self,
        subscription: RepositorySubscription,
        event: NormalizedGitHubEvent,
    ) -> None:
        """Direct operator test event; real monitoring always uses the WAL."""

        for target in subscription.targets:
            prepared = await self._prepare_event_request(event, target, None)
            await self._context.notifications.publish(self._publish_request(prepared))

    async def _enrich_push(self, event: NormalizedGitHubEvent) -> NormalizedGitHubEvent:
        if (
            event.event_type != "PushEvent"
            or event.push_deleted
            or not event.push_before
            or not event.push_head
            or set(event.push_before) == {"0"}
        ):
            return event
        try:
            response = await self._client.compare(
                event.repository,
                event.push_before,
                event.push_head,
            )
            return apply_compare(event, response.body)
        except GitHubAPIError as exc:
            self._context.logger.info(
                "github_compare_failed repository=%s error_category=%s",
                event.repository,
                exc.category,
            )
            return event

    async def _record_failure(
        self,
        repository: str,
        snapshot: QueueSnapshot,
        error: GitHubAPIError,
    ) -> QueueSnapshot:
        now = datetime.now(UTC)
        failures = snapshot.state.consecutive_failures + 1
        delay = min(3600, 30 * (2 ** min(failures - 1, 6)))
        if error.category == "token_invalid":
            delay = 3600
        elif error.category == "rate_limited":
            pause = error.reset_at or now + timedelta(seconds=error.retry_after_seconds or 60)
            delay = max(1, int((pause - now).total_seconds()))
        state = snapshot.state.model_copy(
            update={
                "last_poll_at": now,
                "consecutive_failures": failures,
                "paused_until": now + timedelta(seconds=delay + random.randint(1, 30)),
                "rate_limit_remaining": error.remaining,
                "rate_limit_reset_at": error.reset_at,
            }
        )
        return await compare_and_set_queue_state(self._context, repository, snapshot, state)

    async def _record_gap(
        self,
        repository: str,
        snapshot: QueueSnapshot,
        reason: str,
    ) -> QueueSnapshot:
        state = snapshot.state.model_copy(
            update={"gap_reason": reason[:128], "gap_at": datetime.now(UTC)}
        )
        return await compare_and_set_queue_state(self._context, repository, snapshot, state)

    async def _write_success_metadata(
        self,
        repository: str,
        snapshot: QueueSnapshot,
        response: GitHubAPIResponse,
        now: datetime,
    ) -> QueueSnapshot:
        state = snapshot.state.model_copy(
            update={
                **self._success_metadata(response, now, snapshot.state),
                "backlog_pending": bool(
                    snapshot.state.pending or snapshot.state.inflight is not None
                ),
            }
        )
        return await compare_and_set_queue_state(self._context, repository, snapshot, state)

    async def _mark_success(
        self,
        repository: str,
        snapshot: QueueSnapshot,
        response: GitHubAPIResponse,
        now: datetime,
    ) -> QueueSnapshot:
        state = snapshot.state.model_copy(
            update={
                **self._success_metadata(response, now, snapshot.state),
                "backlog_pending": bool(
                    snapshot.state.pending or snapshot.state.inflight is not None
                ),
            }
        )
        return await compare_and_set_queue_state(self._context, repository, snapshot, state)

    async def _mirror_if_drained(self, repository: str, state: QueueState) -> None:
        if not state.pending and state.inflight is None:
            await mirror_legacy_state(self._context, repository, state)

    @staticmethod
    def _response_metadata(
        response: GitHubAPIResponse,
        now: datetime,
        state: QueueState,
    ) -> dict[str, object]:
        return {
            "etag": response.headers.get("etag", state.etag),
            "last_modified": response.headers.get("last-modified", state.last_modified),
            "last_poll_at": now,
            "rate_limit_remaining": response.rate_limit.remaining,
            "rate_limit_reset_at": response.rate_limit.reset_at,
            "last_request_id": response.rate_limit.request_id,
        }

    @classmethod
    def _success_metadata(
        cls,
        response: GitHubAPIResponse,
        now: datetime,
        state: QueueState,
    ) -> dict[str, object]:
        metadata = {
            **cls._response_metadata(response, now, state),
            "last_success_at": now,
            "consecutive_failures": 0,
            "paused_until": None,
        }
        remaining = response.rate_limit.remaining
        if remaining is not None and remaining <= 100:
            pause = response.rate_limit.reset_at or now + timedelta(
                seconds=300 if remaining else 900
            )
            metadata["paused_until"] = pause + timedelta(seconds=random.randint(1, 30))
        return metadata

    @staticmethod
    def _target_delivery(
        target: NotificationTargetConfig | TargetPolicySnapshot,
    ) -> TargetDelivery:
        return TargetDelivery(
            target_key=delivery_target_key(target.target_type, target.target_id),
            target_type=target.target_type,
            target_id=target.target_id,
            send_text=target.send_text,
            send_card=target.send_card,
            ask_agent=target.ask_agent,
        )

    @staticmethod
    def _target_snapshot(target: NotificationTargetConfig) -> TargetPolicySnapshot:
        return TargetPolicySnapshot(
            target_type=target.target_type,
            target_id=target.target_id,
            send_text=target.send_text,
            send_card=target.send_card,
            ask_agent=target.ask_agent,
        )

    @staticmethod
    def _delivery_policy(delivery: TargetDelivery) -> NotificationTargetConfig:
        return NotificationTargetConfig(
            target_type=delivery.target_type,
            target_id=delivery.target_id,
            send_text=delivery.send_text,
            send_card=delivery.send_card,
            ask_agent=delivery.ask_agent,
        )

    @staticmethod
    def _target_was_disabled(
        delivery: TargetDelivery,
        current: NotificationTargetConfig | None,
    ) -> bool:
        return current is None or any(
            (
                delivery.send_text and not current.send_text,
                delivery.send_card and not current.send_card,
                delivery.ask_agent and not current.ask_agent,
            )
        )

    @staticmethod
    def _find_subscription(
        config: GitHubMonitorConfig,
        repository: str,
    ) -> RepositorySubscription | None:
        return next(
            (
                item
                for item in config.repositories
                if item.repository.casefold() == repository.casefold()
            ),
            None,
        )

    @staticmethod
    def _raw_created_at(raw: dict[str, object]) -> datetime | None:
        value = raw.get("created_at")
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.astimezone(UTC)

    @staticmethod
    def _raw_event_page(value: object) -> list[dict[str, object]]:
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise GitHubEventIdentityError("github_event_page_invalid")
        return [dict(item) for item in value]

    @staticmethod
    def _find_target(
        subscription: RepositorySubscription,
        target_type: str,
        target_id: str,
    ) -> NotificationTargetConfig | None:
        return next(
            (
                item
                for item in subscription.targets
                if item.target_type == target_type and item.target_id == target_id
            ),
            None,
        )

    @staticmethod
    def _find_delivery(deliveries: tuple[TargetDelivery, ...], target_key: str) -> TargetDelivery:
        return next(item for item in deliveries if item.target_key == target_key)

    @staticmethod
    def _find_optional_delivery(
        deliveries: tuple[TargetDelivery, ...], target_key: str
    ) -> TargetDelivery | None:
        return next((item for item in deliveries if item.target_key == target_key), None)

    @staticmethod
    def _publish_request(prepared: PreparedNotification) -> PublishNotificationRequest:
        payload = json.loads(prepared.payload_json)
        if not isinstance(payload, dict):
            raise GitHubQueueGap("github_prepared_payload_invalid")
        return PublishNotificationRequest(
            event_key=prepared.event_key,
            event_type=prepared.event_type,
            external_source=prepared.external_source,
            target=NotificationTarget(
                target_type=prepared.target_type,
                target_id=prepared.target_id,
            ),
            occurred_at=prepared.occurred_at,
            summary=prepared.summary,
            payload=payload,
            text=prepared.text,
            media_handles=tuple(item.handle_id for item in prepared.media),
            ask_agent=prepared.ask_agent,
            agent_intent=prepared.agent_intent,
        )

    @staticmethod
    def _require_prepared_media_live(prepared: PreparedNotification) -> None:
        now = datetime.now(UTC)
        if any(item.expires_at is None or item.expires_at <= now for item in prepared.media):
            raise GitHubQueueGap("github_prepared_media_expired")

    @staticmethod
    def _unit_id(repository: str, members: tuple[QueuedSourceEvent, ...]) -> str:
        ids = ",".join(item.github_event_id for item in members)
        digest = hashlib.sha256(f"{repository.casefold()}:{ids}".encode()).hexdigest()[:24]
        return f"github-unit-v1:{digest}"

    @staticmethod
    def _activation_event_key(repository: str, activation_id: str) -> str:
        raw = f"github:{repository}:monitor-enabled:{activation_id}"
        if len(raw) <= 255:
            return raw
        digest = hashlib.sha256(raw.encode()).hexdigest()
        return f"github:monitor-enabled:{digest}"
