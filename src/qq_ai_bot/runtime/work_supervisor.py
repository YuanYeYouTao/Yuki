"""One fenced decision for every activation, independent of its entrypoint."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert

from qq_ai_bot.runtime.activation_outcome import (
    ActivationOutcome,
    DeliveryDeferred,
    ExitReason,
    SegmentBudgetReached,
    WorkNoProgress,
    classify_failure,
)
from qq_ai_bot.runtime.work_budget import WorkBudgetExceeded
from qq_ai_bot.runtime.work_recovery_schema import deliveries, recovery
from qq_ai_bot.runtime.work_repository import WorkCapacityError, WorkConflict, bounded_json
from qq_ai_bot.runtime.work_schema_v1 import work

if TYPE_CHECKING:
    from qq_ai_bot.runtime.work_control import WorkControl

logger = logging.getLogger(__name__)


def activation_details(control: WorkControl) -> dict[str, Any]:
    session = control.session
    return {
        "activation_id": control.lease.owner,
        "checkpoint_id": session.call_key("checkpoint") if session and session.transcript else None,
        "pending_execution_ids": tuple(
            str(e["run_id"]) for e in control.known_effects if e.get("pending") and e.get("run_id")
        ),
        "model_requests": control.requests_started,
        "tool_calls": control.tools_started,
    }


async def recover_failure(control: WorkControl, exc: BaseException) -> ActivationOutcome:
    failure = classify_failure(exc)
    reason = ExitReason.PAUSED
    if isinstance(exc, WorkBudgetExceeded):
        reason = ExitReason.BUDGET
    elif isinstance(exc, SegmentBudgetReached):
        reason = ExitReason.SEGMENT
    elif isinstance(exc, WorkNoProgress):
        reason = ExitReason.NO_PROGRESS
    elif isinstance(exc, WorkCapacityError):
        reason = ExitReason.CAPACITY
    elif isinstance(exc, WorkConflict):
        # A stale owner must never commit a new state over its replacement.
        if not await control.repository.valid(control.lease):
            raise exc
    assert control.current is not None
    verified = (
        control.ending == "completed"
        and (control.final_delivery or control.current["output_kind"] != "answer")
        and not any(e.get("pending") or e.get("uncertain") for e in control.known_effects)
        and not await control.pending()
        and await control.background_state() is None
    )
    identity = control.current["id"]
    async with control.repository.database.immediate_session() as session:
        await control.repository._assert_lease(session, control.lease)
        prior = (
            (await session.execute(select(recovery).where(recovery.c.work_id == identity)))
            .mappings()
            .first()
        )
        attempts = 1
        if prior and json.loads(prior["failure_json"]).get("code") == failure.code:
            attempts += int(prior["attempts"])
        not_before = 0.0
        if failure.retryable and attempts <= 3:
            reason = ExitReason.RETRY
            delay = (
                (0.25, 0.75, 1.5)[attempts - 1]
                if failure.code == "sqlite_busy"
                else (2, 10, 30)[attempts - 1]
            )
            supplied_delay = failure.diagnostics.get("retry_after_seconds", 0)
            if isinstance(supplied_delay, (int, float)):
                delay = max(delay, supplied_delay)
            not_before = time.time() + delay
        state = "queued" if reason in {ExitReason.RETRY, ExitReason.SEGMENT} else "suspended"
        if isinstance(exc, DeliveryDeferred):
            state, reason = "queued", ExitReason.RETRY
            not_before = max(time.time() + 1, exc.not_before)
        if verified:
            state, reason, not_before = "completed", ExitReason.COMPLETED, 0
        values = dict(
            work_id=identity,
            activation_id=control.lease.owner,
            exit_reason=reason.value,
            stage=failure.stage,
            failure_json=bounded_json(asdict(failure)),
            attempts=attempts,
            not_before=not_before,
            updated=time.time(),
        )
        await session.execute(
            insert(recovery)
            .values(**values)
            .on_conflict_do_update(index_elements=[recovery.c.work_id], set_=values)
        )
        current = (
            (
                await session.execute(
                    update(work)
                    .where(
                        work.c.id == identity,
                        work.c.state.not_in(("completed", "failed", "cancelled")),
                    )
                    .values(
                        state=state,
                        reason=failure.code,
                        revision=work.c.revision + 1,
                        updated=time.time(),
                    )
                    .returning(work)
                )
            )
            .mappings()
            .first()
        )
        if current is None:
            raise WorkConflict("work_recovery_obsolete")
        if state == "suspended" and not control.lease.work_id:
            descriptions = {
                ExitReason.BUDGET: "这项工作的总执行额度已用完，已暂停并保留结果。",
                ExitReason.CAPACITY: "工作记录容量不足，已暂停并保留已有结果。",
                ExitReason.NO_PROGRESS: "连续执行没有取得进展，已暂停并保留已有结果。",
            }
            text = descriptions.get(reason, "这项工作遇到执行错误，已暂停并保留已有结果。")
            await session.execute(
                insert(deliveries)
                .values(
                    id=f"notice:{identity}:{control.lease.owner}",
                    work_id=identity,
                    kind="notice",
                    target_key=control.lease.conversation_id,
                    state="planned",
                    payload_json=bounded_json({"text": text}),
                    created=time.time(),
                    updated=time.time(),
                )
                .on_conflict_do_nothing()
            )
    control.current = dict(current)
    control.ending = state
    control.settled = True
    outcome = ActivationOutcome(reason, identity, failure, **activation_details(control))
    control.outcome = outcome
    logger.warning(
        "work_activation_exit work_id=%s reason=%s failure=%s attempt=%s",
        identity,
        reason.value,
        failure.code,
        attempts,
    )
    return outcome


async def settle(control: WorkControl, *, delivered: bool, pending_inputs: bool) -> None:
    if control.settled or control.current is None:
        return
    if control.handoff_work_id is not None:
        state = "queued" if pending_inputs else await control.background_state()
        if state is None:
            state = (
                "waiting_external"
                if any(e.get("pending") for e in control.known_effects)
                else "suspended"
            )
        reason = ExitReason.INPUT
    elif control.yield_segment:
        state, reason = "queued", ExitReason.SEGMENT
    elif pending_inputs:
        ready = any(item.get("ready") for item in await control.pending())
        state, reason = ("queued" if ready else "waiting_external"), ExitReason.INPUT
    elif control.ending in {"waiting_external", "waiting_user"}:
        state = control.ending
        reason = ExitReason.EXTERNAL if state == "waiting_external" else ExitReason.INPUT
    elif control.ending in {"failed", "suspended"}:
        background = await control.background_state()
        state, reason = background or control.ending, ExitReason.PAUSED
    elif control.ending == "completed":
        background = await control.background_state()
        if background:
            state, reason = background, ExitReason.EXTERNAL
        else:
            # Artifact completion was already checked against transport receipts.
            artifact_done = control.current["output_kind"] != "answer"
            state = "completed" if delivered or artifact_done else "suspended"
            reason = ExitReason.COMPLETED if state == "completed" else ExitReason.PAUSED
    else:
        state, reason = "suspended", ExitReason.PAUSED
    control.current = await control.repository.transition(
        control.lease,
        control.current["id"],
        control.current["revision"],
        state,
        reason=reason.value,
        exit_reason=reason.value,
    )
    if control.current["state"] == "queued" and state != "queued":
        reason = ExitReason.INPUT
    control.outcome = ActivationOutcome(
        reason, control.current["id"], **activation_details(control)
    )
    control.settled = True
