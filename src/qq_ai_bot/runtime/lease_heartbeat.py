"""Supervise renewal in the existing activation task without changing its identity."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from sqlalchemy.exc import OperationalError

from qq_ai_bot.runtime.work_repository import WorkConflict

logger = logging.getLogger(__name__)


async def lease_heartbeat(
    renew: Callable[[], Awaitable[bool]],
    expiry: Callable[[], Awaitable[float | None]],
    *,
    seconds: float,
    interval: float,
    clock: Callable[[], float] = time.time,
) -> None:
    """Retry only identified SQLITE_BUSY while the last confirmed lease is valid."""
    stage = "expiry"
    try:
        deadline = await expiry()
        if deadline is None:
            raise WorkConflict("work_heartbeat_lease_obsolete")
        while True:
            await asyncio.sleep(min(interval, max(0, deadline - clock())))
            stage = "renew"
            delay = 0.05
            while True:
                started = clock()
                remaining = deadline - started
                if remaining <= 0:
                    raise WorkConflict("work_heartbeat_lease_expired")
                try:
                    async with asyncio.timeout(remaining):
                        valid = await renew()
                except OperationalError as exc:
                    code = getattr(exc.orig, "sqlite_errorcode", None)
                    if not isinstance(code, int) or code & 255 != sqlite3.SQLITE_BUSY:
                        raise WorkConflict("work_heartbeat_renew_failed") from exc
                    remaining = deadline - clock()
                    if remaining <= 0:
                        raise WorkConflict("work_heartbeat_lease_expired") from exc
                    await asyncio.sleep(min(delay, remaining))
                    delay = min(delay * 2, 0.5)
                    continue
                except TimeoutError as exc:
                    raise WorkConflict("work_heartbeat_lease_expired") from exc
                if not valid:
                    raise WorkConflict("work_heartbeat_lease_obsolete")
                # Renewal is executed after this sample, so this lower bound
                # never invents extra lease time while SQLite waits for a writer.
                deadline = started + seconds
                break
    except Exception as exc:
        original = exc.__cause__ or exc
        logger.warning(
            "lease_heartbeat_failed stage=%s category=%s sqlite_code=%s",
            stage,
            type(original).__name__,
            getattr(getattr(original, "orig", None), "sqlite_errorcode", None),
        )
        raise


@asynccontextmanager
async def supervise_lease(
    renew: Callable[[], Awaitable[bool]],
    expiry: Callable[[], Awaitable[float | None]],
    *,
    seconds: float = 60,
    interval: float = 15,
    clock: Callable[[], float] = time.time,
) -> AsyncIterator[None]:
    """Interrupt the parent on pulse failure and expose it to existing recovery."""
    owner = asyncio.current_task()
    assert owner is not None
    pulse = asyncio.create_task(
        lease_heartbeat(renew, expiry, seconds=seconds, interval=interval, clock=clock),
        name="lease-heartbeat",
    )
    interrupted = False
    active = True

    def failed(task: asyncio.Task[None]) -> None:
        nonlocal interrupted
        if active and not task.cancelled() and task.exception() is not None:
            interrupted = owner.cancel("lease_heartbeat_failed")

    pulse.add_done_callback(failed)
    try:
        yield
    except asyncio.CancelledError:
        active = False
        if interrupted:
            owner.uncancel()
            interrupted = False
            if owner.cancelling() == 0:
                await pulse
        raise
    else:
        active = False
        if pulse.done() and not pulse.cancelled():
            await pulse
    finally:
        active = False
        pulse.remove_done_callback(failed)
        pulse.cancel()
        await asyncio.gather(pulse, return_exceptions=True)
        if interrupted:
            owner.uncancel()
