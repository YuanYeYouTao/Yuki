"""Keep durable worker loops alive after transient database failures."""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from sqlalchemy.exc import SQLAlchemyError

from yuki.persistence.database import write_failure_phase


async def recover_database_loop(
    run: Callable[[], Awaitable[None]], *, stop: asyncio.Event, logger: logging.Logger
) -> None:
    while not stop.is_set():
        try:
            await run()
            return
        except Exception as exc:
            if not isinstance(exc, SQLAlchemyError) and write_failure_phase(exc) is None:
                raise
            logger.error(
                "worker_database_iteration_failed exception_category=%s", type(exc).__name__
            )
            try:
                await asyncio.wait_for(stop.wait(), timeout=1.0)
            except TimeoutError:
                pass
