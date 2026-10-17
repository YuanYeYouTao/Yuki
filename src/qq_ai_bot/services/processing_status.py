"""Best-effort platform typing for the actual lifetime of overlapping Main turns."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import Context
from dataclasses import dataclass
from functools import partial

from qq_ai_bot.gateway.activity import PlatformActivity
from qq_ai_bot.gateway.registry import GatewayConnectionRegistry
from qq_ai_bot.gateway.social import TransportTarget
from qq_ai_bot.identity.routing import PresenceRouter

logger = logging.getLogger(__name__)


@dataclass
class _ActiveStatus:
    users: int = 0
    task: asyncio.Task[None] | None = None
    previous: asyncio.Task[None] | None = None


class ProcessingStatusService:
    def __init__(self, router: PresenceRouter, registry: GatewayConnectionRegistry) -> None:
        self._router = router
        self._registry = registry
        self._active: dict[tuple[str, str | None], _ActiveStatus] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    @asynccontextmanager
    async def during(
        self,
        *,
        conversation_id: str | None,
        presence_id: str | None,
        source_event_id: int | None,
        group: bool,
    ) -> AsyncIterator[None]:
        if conversation_id is None:
            yield
            return
        key = conversation_id, presence_id
        active = self._active.get(key)
        if active is None or active.users == 0:
            previous = active.task if active is not None else None
            active = _ActiveStatus(previous=previous)
            active.task = asyncio.create_task(
                self._run(conversation_id, presence_id, source_event_id, group, active),
                name="processing-status",
                context=Context(),
            )
            self._active[key] = active
            self._tasks.add(active.task)
            active.task.add_done_callback(partial(self._finished, key, active), context=Context())
        active.users += 1
        try:
            yield
        finally:
            active.users -= 1
            if active.users == 0:
                task = active.task
                assert task is not None
                if active.previous is None or active.previous.done():
                    task.cancel()
                if task.done():
                    self._finished(key, active, task)

    def _finished(
        self,
        key: tuple[str, str | None],
        active: _ActiveStatus,
        task: asyncio.Task[None],
    ) -> None:
        self._tasks.discard(task)
        if active.users == 0 and self._active.get(key) is active:
            del self._active[key]

    async def close(self) -> None:
        tasks = tuple(self._tasks)
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.difference_update(tasks)
        self._active.clear()

    async def _run(
        self,
        conversation_id: str,
        presence_id: str | None,
        source_event_id: int | None,
        group: bool,
        active: _ActiveStatus,
    ) -> None:
        activity: PlatformActivity | None = None
        phase = "resolve"
        try:
            if active.previous is not None:
                # A final QQ stop must finish before the next session starts typing.
                await asyncio.shield(asyncio.gather(active.previous, return_exceptions=True))
            if active.users == 0:
                return
            async with asyncio.timeout(4.0):
                route = await self._router.resolve_send_for_conversation(
                    conversation_id, presence_id=presence_id, source_event_id=source_event_id
                )
            activity = self._registry.activity(
                route.connection,
                TransportTarget(
                    "space" if group else "person", route.external_target_id, route.thread_id
                ),
            )
            if activity is None:
                return
            phase = "start"
            while True:
                async with asyncio.timeout(activity.refresh_interval_seconds):
                    await activity.start()
                phase = "refresh"
                await asyncio.sleep(activity.refresh_interval_seconds)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning(
                "processing_status_failed phase=%s exception_category=%s", phase, type(exc).__name__
            )
        finally:
            if activity is not None:
                try:
                    async with asyncio.timeout(activity.refresh_interval_seconds):
                        await activity.stop()
                except Exception as exc:
                    logger.warning(
                        "processing_status_failed phase=stop exception_category=%s",
                        type(exc).__name__,
                    )
