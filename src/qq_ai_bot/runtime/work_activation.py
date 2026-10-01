"""Bounded in-process activation ownership backed by persistent scope leases."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import ExitStack, asynccontextmanager
from contextvars import ContextVar
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from qq_ai_bot.runtime.activation_bindings import ActiveWorkBindings
from qq_ai_bot.runtime.lease_heartbeat import supervise_lease
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
    resolve_child: Callable[[str], Awaitable[dict[str, Any] | None]] | None = None,
    *,
    work_id: str | None = None,
    bindings: ActiveWorkBindings | None = None,
    scope_key: str | None = None,
) -> AsyncIterator[WorkControl]:
    with ExitStack() as admission:
        if bindings is not None:
            admission.enter_context(bindings.executions.track())
        lease = await repository.acquire(conversation_id, generation)
        if lease is None:
            raise WorkConflict("conversation_activation_busy")
        control = WorkControl(repository, lease, source_key, source, validate, resolve_child)
        async with bind_work_activation(control, bindings=bindings, scope_key=scope_key):
            # Authority is reconstructed by the caller, not copied out of a prior work.
            # A different actor cannot silently take over the original actor's goal.
            candidates = await repository.active(conversation_id, generation)
            candidates.sort(key=lambda candidate: candidate["source_key"] != source_key)
            for candidate in candidates:
                if work_id is not None and candidate["id"] != work_id:
                    continue
                if work_id is None and candidate["source_key"] != source_key:
                    continue
                if work_id is None and json.loads(candidate["checkpoint_json"]).get(
                    "handoff_work_id"
                ):
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
                        "principal_kind",
                        "initiative_run_id",
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


@asynccontextmanager
async def bind_work_activation(
    control: WorkControl,
    *,
    finish: Callable[[WorkControl], Awaitable[None]] | None = None,
    release: Callable[[], Awaitable[None]] | None = None,
    bindings: ActiveWorkBindings | None = None,
    scope_key: str | None = None,
) -> AsyncIterator[WorkControl]:
    """Own one already-acquired root or child lease until activation exit.

    The caller reconstructs authority and selects the current Work. The default
    finish preserves root settlement; child callers provide their own settlement
    and child-result finalization. A finish callback also sees settled recovery,
    but never an invalid lease or deferred recovery. It must not replay effects.
    """
    repository, lease = control.repository, control.lease
    with ExitStack() as local_bindings:
        try:
            if bindings is not None:
                local_bindings.enter_context(
                    bindings.bind(scope_key or lease.conversation_id, control)
                )
        except BaseException as exc:
            # Admission may close while the caller awaits acquire. No ContextVar
            # or heartbeat owns this lease yet, but it still needs releasing.
            try:
                if release is None:
                    await repository.release(lease)
                else:
                    await release()
            except BaseException as cleanup:
                exc.add_note(f"activation admission release failed: {type(cleanup).__name__}")
            raise
        token = current_work_control.set(control)
        try:
            async with supervise_lease(
                lambda: repository.renew(lease),
                lambda: repository.lease_expiry(lease),
                meter=control.meter_active_time,
            ):
                yield control
        except BaseException as exc:
            await _recover_activation(control, exc)
            raise
        finally:
            current_work_control.reset(token)
            try:
                if (
                    await repository.valid(lease)
                    and control.current is not None
                    and not control.recovery_deferred
                ):
                    if not control.settled:
                        await control.meter_active_time()
                    if finish is not None:
                        await finish(control)
                    elif not control.settled:
                        pending = bool(await control.pending())
                        await control.settle(
                            delivered=control.final_delivery, pending_inputs=pending
                        )
            except (SQLAlchemyError, WorkConflict) as exc:
                # Confirmed effects stay confirmed even if derived cleanup fails.
                logger.warning("work_cleanup_deferred stage=settle category=%s", type(exc).__name__)
            finally:
                try:
                    if release is None:
                        await repository.release(lease)
                    else:
                        await release()
                except SQLAlchemyError as exc:
                    # Leases expire independently; never resend or reset work here.
                    logger.warning(
                        "work_cleanup_deferred stage=release category=%s", type(exc).__name__
                    )


async def _recover_activation(control: WorkControl, exc: BaseException) -> None:
    if control.current is not None and not control.settled and not control.recovery_deferred:
        try:
            await control.recover_failure(exc)
        except BaseException as cleanup:
            exc.add_note(f"work recovery deferred: {type(cleanup).__name__}")
    if control.current is not None and isinstance(exc, Exception):
        from qq_ai_bot.runtime.activation_outcome import (
            WorkActivationHandled,
            WorkRecoveryDeferred,
        )

        if control.recovery_deferred:
            raise WorkRecoveryDeferred("owned_activation_recovery_deferred") from exc
        if control.settled:
            raise WorkActivationHandled("owned_activation_recovered") from exc
