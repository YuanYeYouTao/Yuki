"""Bounded in-process activation ownership backed by persistent scope leases."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_repository import WorkConflict, WorkRepository

logger = logging.getLogger(__name__)


current_work_control: ContextVar[WorkControl | None] = ContextVar(
    "current_work_control", default=None
)


@asynccontextmanager
async def activate_work(
    repository: WorkRepository,
    conversation_id: str,
    generation: int,
    source_key: str,
    source: dict[str, Any],
    validate: Callable[[], Awaitable[None]],
    deliver: Callable[[str, str], Awaitable[dict[str, Any]]] | None = None,
    resolve_child: Callable[[str], Awaitable[dict[str, Any] | None]] | None = None,
    *,
    work_id: str | None = None,
) -> AsyncIterator[WorkControl]:
    lease = await repository.acquire(conversation_id, generation)
    if lease is None:
        raise WorkConflict("conversation_activation_busy")
    control = WorkControl(repository, lease, source_key, source, validate, deliver, resolve_child)
    token = current_work_control.set(control)

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(15)
            if not await repository.renew(lease):
                return
            await control.meter_active_time()

    pulse = asyncio.create_task(heartbeat(), name="work-lease-renew")
    interrupted = False
    try:
        # Authority is reconstructed by the caller, not copied out of a prior work.
        # A different actor cannot silently take over the original actor's goal.
        candidates = await repository.active(conversation_id, generation)
        candidates.sort(key=lambda candidate: candidate["source_key"] != source_key)
        for candidate in candidates:
            if work_id is not None and candidate["id"] != work_id:
                continue
            if work_id is None and json.loads(candidate["checkpoint_json"]).get("handoff_work_id"):
                # A later message cannot select the old owner ahead of the work
                # it just registered. Explicit execution wakeups retain its ID.
                continue
            previous = json.loads(candidate["source_json"])
            if all(
                previous.get(key) == source.get(key)
                for key in (
                    "actor_user_id",
                    "origin",
                    "plugin_id",
                    "delegation_id",
                    "execution_boundary",
                )
            ):
                control.current = candidate
                break
        if control.current is not None and control.current["state"] != "running":
            control.current = await repository.transition(
                lease,
                control.current["id"],
                control.current["revision"],
                "running",
            )
        yield control
    except BaseException as exc:
        interrupted = type(exc).__name__ in {
            "TurnSupersededError",
            "LLMTimeoutError",
            "LLMUnavailableError",
            "CancelledError",
        }
        raise
    finally:
        pulse.cancel()
        await asyncio.gather(pulse, return_exceptions=True)
        current_work_control.reset(token)
        try:
            if await repository.valid(lease) and control.current is not None:
                await control.meter_active_time()
                pending = bool(await control.pending())
                await control.settle(delivered=control.final_delivery, pending_inputs=pending)
                if control.current["state"] == "running":
                    await repository.transition(
                        lease,
                        control.current["id"],
                        control.current["revision"],
                        "queued" if pending or interrupted else "suspended",
                        reason="pending_input"
                        if pending
                        else "activation_ended_without_completion",
                    )
        except SQLAlchemyError as exc:
            # Accepted delivery and its durable journal must not become a second
            # user-facing failure. The next fenced activation reconciles state.
            logger.warning("work_cleanup_deferred stage=settle category=%s", type(exc).__name__)
        finally:
            try:
                await repository.release(lease)
            except SQLAlchemyError as exc:
                # Leases expire independently; never resend or reset work here.
                logger.warning(
                    "work_cleanup_deferred stage=release category=%s", type(exc).__name__
                )
