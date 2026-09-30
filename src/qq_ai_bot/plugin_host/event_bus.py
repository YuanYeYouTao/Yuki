"""Timeout-bounded, failure-isolated notification Hook execution."""

from __future__ import annotations

import asyncio
import logging
import time
from contextvars import Context
from dataclasses import dataclass

from yuki_plugin_sdk.errors import RegistrationError
from yuki_plugin_sdk.events import (
    EventEnvelope,
    EventName,
    HookExecution,
    NotificationHandler,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _Subscription:
    plugin_id: str
    hook_id: str
    event: EventName
    handler: NotificationHandler
    priority: int
    timeout_seconds: float | None


@dataclass(frozen=True, slots=True)
class _QueuedNotification:
    encoded_event: bytes
    subscriptions: tuple[_Subscription, ...]


class PluginEventBus:
    def __init__(
        self,
        *,
        default_timeout_seconds: float = 3.0,
        queue_capacity: int = 256,
        queue_max_bytes: int = 1024 * 1024,
    ) -> None:
        if default_timeout_seconds <= 0:
            raise ValueError("default hook timeout must be positive")
        if queue_capacity <= 0 or queue_max_bytes <= 0:
            raise ValueError("notification queue limits must be positive")
        self._default_timeout = default_timeout_seconds
        self._subscriptions: dict[tuple[str, str], _Subscription] = {}
        self._queue: asyncio.Queue[_QueuedNotification] = asyncio.Queue(maxsize=queue_capacity)
        self._queue_capacity = queue_capacity
        self._queue_max_bytes = queue_max_bytes
        self._pending_count = 0
        self._pending_bytes = 0
        self._accepting = False
        self._consumer: asyncio.Task[None] | None = None
        self._active_hooks: dict[str, set[asyncio.Task[object]]] = {}
        self._dropped = 0
        self._cancelled_hooks = 0
        self._stale_hooks = 0

    async def start(self) -> None:
        """Start the single consumer through the application's lifecycle."""

        if self._consumer is not None and self._consumer.done():
            try:
                self._consumer.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning(
                    "plugin_notification_consumer_failed error_category=%s", type(exc).__name__
                )
            self._consumer = None
        if self._consumer is None:
            self._accepting = True
            self._consumer = asyncio.create_task(
                self._consume_notifications(),
                name="plugin-lifecycle-notifications",
                context=Context(),
            )

    async def close(self) -> None:
        """Stop delivery and discard ephemeral notifications after producers stop."""

        self._accepting = False
        consumer = self._consumer
        if consumer is not None:
            consumer.cancel()
            done, _pending = await asyncio.wait({consumer}, timeout=2.0)
            if done:
                await asyncio.gather(consumer, return_exceptions=True)
                self._consumer = None
            else:
                logger.warning("plugin_notification_shutdown_timeout")
        while not self._queue.empty():
            queued = self._queue.get_nowait()
            self._drop("shutdown")
            self._release(queued)
            self._queue.task_done()

    async def health(self) -> dict[str, int | bool]:
        """Report queue metadata without event payloads or identities."""

        return {
            "running": self._accepting and self._consumer is not None and not self._consumer.done(),
            "pending": self._pending_count,
            "pending_bytes": self._pending_bytes,
            "capacity": self._queue_capacity,
            "max_bytes": self._queue_max_bytes,
            "dropped": self._dropped,
            "cancelled_hooks": self._cancelled_hooks,
            "stale_hooks": self._stale_hooks,
        }

    def enqueue_notification(self, event: EventEnvelope) -> bool:
        """Freeze and admit a metadata notification without waiting for handlers.

        Bounds include the event currently being delivered. Only the core's
        metadata publishers use this path; explicit SDK publishing still awaits.
        """

        subscriptions = self._matching_subscriptions(event.name)
        if not subscriptions:
            return True
        if not self._accepting or self._consumer is None or self._consumer.done():
            self._drop("not_running")
            return False
        if self._pending_count >= self._queue_capacity:
            self._drop("capacity")
            return False
        encoded_event = event.model_dump_json().encode("utf-8")
        if self._pending_bytes + len(encoded_event) > self._queue_max_bytes:
            self._drop("bytes")
            return False
        self._queue.put_nowait(_QueuedNotification(encoded_event, subscriptions))
        self._pending_count += 1
        self._pending_bytes += len(encoded_event)
        return True

    async def _consume_notifications(self) -> None:
        while True:
            queued = await self._queue.get()
            try:
                if not any(self._is_current(item) for item in queued.subscriptions):
                    self._stale_hooks += len(queued.subscriptions)
                    self._drop("unsubscribed")
                    continue
                event = EventEnvelope.model_validate_json(queued.encoded_event)
                await self._dispatch(event, queued.subscriptions)
            except asyncio.CancelledError:
                self._drop("shutdown")
                raise
            except Exception as exc:
                self._drop("consumer_error")
                logger.warning("plugin_notification_failed error_category=%s", type(exc).__name__)
            finally:
                self._release(queued)
                self._queue.task_done()

    def _release(self, queued: _QueuedNotification) -> None:
        self._pending_count -= 1
        self._pending_bytes -= len(queued.encoded_event)

    def _drop(self, reason: str) -> None:
        self._dropped += 1
        if self._dropped <= 3 or self._dropped & (self._dropped - 1) == 0:
            logger.warning(
                "plugin_notification_dropped reason=%s dropped=%d", reason, self._dropped
            )

    def _is_current(self, subscription: _Subscription) -> bool:
        return (
            self._subscriptions.get((subscription.plugin_id, subscription.hook_id)) is subscription
        )

    def _matching_subscriptions(self, name: EventName) -> tuple[_Subscription, ...]:
        return tuple(
            sorted(
                (item for item in self._subscriptions.values() if item.event is name),
                key=lambda item: (-item.priority, item.plugin_id, item.hook_id),
            )
        )

    def configure_default_timeout(self, timeout_seconds: float) -> None:
        """Apply the HOT default to hooks that did not declare their own timeout."""

        if timeout_seconds <= 0:
            raise ValueError("default hook timeout must be positive")
        self._default_timeout = timeout_seconds

    def subscribe(
        self,
        *,
        plugin_id: str,
        hook_id: str,
        event: EventName,
        handler: NotificationHandler,
        priority: int = 0,
        timeout_seconds: float | None = None,
    ) -> None:
        key = (plugin_id, hook_id)
        if key in self._subscriptions:
            raise RegistrationError(f"duplicate event hook: {plugin_id}:{hook_id}")
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("hook timeout must be positive")
        self._subscriptions[key] = _Subscription(
            plugin_id=plugin_id,
            hook_id=hook_id,
            event=event,
            handler=handler,
            priority=priority,
            timeout_seconds=timeout_seconds,
        )

    def unsubscribe_plugin(self, plugin_id: str) -> int:
        keys = [key for key in self._subscriptions if key[0] == plugin_id]
        for key in keys:
            del self._subscriptions[key]
        for task in tuple(self._active_hooks.get(plugin_id, ())):
            task.cancel()
        return len(keys)

    async def publish(self, event: EventEnvelope) -> tuple[HookExecution, ...]:
        """Await explicit SDK publication and return each handler's outcome."""

        return await self._dispatch(event, self._matching_subscriptions(event.name))

    async def _dispatch(
        self, event: EventEnvelope, subscriptions: tuple[_Subscription, ...]
    ) -> tuple[HookExecution, ...]:
        if not subscriptions:
            return ()
        executions = await asyncio.gather(
            *(self._execute(item, event.model_copy(deep=True)) for item in subscriptions)
        )
        return tuple(executions)

    async def _execute(self, subscription: _Subscription, event: EventEnvelope) -> HookExecution:
        started = time.perf_counter()
        error_category: str | None = None
        timeout_seconds = subscription.timeout_seconds or self._default_timeout
        task = asyncio.current_task()
        assert task is not None
        active = self._active_hooks.setdefault(subscription.plugin_id, set())
        active.add(task)
        try:
            if not self._is_current(subscription):
                self._stale_hooks += 1
                error_category = "hook_unsubscribed"
            else:
                async with asyncio.timeout(timeout_seconds):
                    await subscription.handler(event)
        except asyncio.CancelledError:
            self._cancelled_hooks += 1
            # A handler's own cancellation is a failed observation. Cancelling
            # the enclosing gather still propagates to its publisher/consumer.
            error_category = (
                "hook_cancelled" if self._is_current(subscription) else "hook_unsubscribed"
            )
        except TimeoutError:
            error_category = "hook_timeout"
        except Exception as exc:
            error_category = type(exc).__name__
        finally:
            active.discard(task)
            if not active:
                self._active_hooks.pop(subscription.plugin_id, None)
        if error_category is None and not self._is_current(subscription):
            self._stale_hooks += 1
            error_category = "hook_unsubscribed"
        duration = time.perf_counter() - started
        if error_category is not None:
            logger.warning(
                "plugin_hook_failed plugin_id=%s hook_id=%s event=%s error_category=%s",
                subscription.plugin_id,
                subscription.hook_id,
                event.name.value,
                error_category,
            )
        elif duration >= timeout_seconds * 0.8:
            logger.warning(
                "plugin_hook_slow plugin_id=%s hook_id=%s event=%s duration_seconds=%.4f",
                subscription.plugin_id,
                subscription.hook_id,
                event.name.value,
                duration,
            )
        return HookExecution(
            plugin_id=subscription.plugin_id,
            hook_id=subscription.hook_id,
            success=error_category is None,
            duration_seconds=duration,
            error_category=error_category,
        )
