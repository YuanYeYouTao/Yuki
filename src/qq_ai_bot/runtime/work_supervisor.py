"""One fenced decision for every activation, independent of its entrypoint."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from qq_ai_bot.runtime.activation_outcome import (
    ActivationOutcome,
    ExitReason,
    SegmentBudgetReached,
    classify_failure,
)
from qq_ai_bot.runtime.work_budget import WorkBudgetExceeded
from qq_ai_bot.runtime.work_recovery_schema import recovery
from qq_ai_bot.runtime.work_repository import WorkCapacityError, WorkConflict, encode_json
from qq_ai_bot.runtime.work_schema_v1 import work

if TYPE_CHECKING:
    from qq_ai_bot.runtime.work_control import WorkControl

from qq_ai_bot.runtime.work_control import ACCEPTED_ENDINGS

logger = logging.getLogger(__name__)

ACCEPTED_REASONS = {
    "completed": ExitReason.COMPLETED,
    "failed": ExitReason.FAILED,
    "cancelled": ExitReason.CANCELLED,
    "waiting_user": ExitReason.INPUT,
    "waiting_external": ExitReason.EXTERNAL,
}


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
    assert control.current is not None
    observed = await control.repository.get(control.current["id"])
    if observed is not None and observed["state"] == "cancelled":
        return _cancelled(control, observed)
    failure = classify_failure(exc)
    reason = ExitReason.FAILED
    if isinstance(exc, WorkBudgetExceeded):
        reason = ExitReason.BUDGET
    elif isinstance(exc, SegmentBudgetReached):
        reason = ExitReason.SEGMENT
    elif isinstance(exc, WorkCapacityError):
        reason = ExitReason.CAPACITY
    elif isinstance(exc, WorkConflict):
        # A stale owner must never commit a new state over its replacement.
        if not await control.repository.valid(control.lease):
            observed = await control.repository.get(control.current["id"])
            if observed is not None and observed["state"] == "cancelled":
                return _cancelled(control, observed)
            raise exc
    await control.refresh_effects()
    assert control.current is not None
    identity = control.current["id"]
    async with control.repository.database.immediate_session() as session:
        current = (
            (await session.execute(select(work).where(work.c.id == identity))).mappings().first()
        )
        if current is not None and current["state"] == "cancelled":
            return _cancelled(control, dict(current))
        await control.repository._assert_lease(session, control.lease)
        deferred = control.deferred_failure
        if deferred is not None:
            from qq_ai_bot.runtime.work_schema_v1 import inputs

            failed = deferred.work
            if (
                failed is None
                or current is None
                or current["state"] != "running"
                or current["generation"] != failed["generation"]
                or current["revision"] != failed["revision"]
                or current["model_requests"] != failed["model_requests"]
                or current["tool_calls"] != failed["tool_calls"]
                or await session.scalar(
                    select(inputs.c.id)
                    .where(
                        inputs.c.work_id == identity,
                        inputs.c.state == "pending",
                        control.repository._business_input_clause(),
                    )
                    .limit(1)
                )
                is not None
            ):
                raise WorkConflict("deferred_failure_superseded")
        prior = (
            (await session.execute(select(recovery).where(recovery.c.work_id == identity)))
            .mappings()
            .first()
        )
        attempts = 1
        if prior and json.loads(prior["failure_json"]).get("code") == failure.code:
            attempts += int(prior["attempts"])
        not_before = 0.0
        if failure.retryable:
            reason = ExitReason.RETRY
            delay = (
                (0.25, 0.75, 1.5)[min(attempts, 3) - 1]
                if failure.code == "sqlite_busy"
                else (2, 10, 30)[min(attempts, 3) - 1]
            )
            supplied_delay = failure.diagnostics.get("retry_after_seconds", 0)
            if isinstance(supplied_delay, (int, float)):
                delay = max(delay, supplied_delay)
            not_before = time.time() + delay
        state = (
            "queued"
            if reason in {ExitReason.RETRY, ExitReason.SEGMENT}
            else "suspended"
            if reason in {ExitReason.BUDGET, ExitReason.CAPACITY}
            else "failed"
        )
        # Accepted lifecycle decisions survive auxiliary failures. The writer
        # settles input ownership and gathers the original owned execution.
        accepted = (
            json.loads(current["checkpoint_json"]).get("accepted_control")
            if current is not None
            else None
        )
        proposed = ACCEPTED_ENDINGS.get(str(accepted.get("action"))) if accepted else None
        if proposed is not None:
            state, reason, not_before = proposed, ACCEPTED_REASONS[proposed], 0
        values = dict(
            activation_id=control.lease.owner,
            exit_reason=reason.value,
            stage=failure.stage,
            failure_json=encode_json(asdict(failure)),
            attempts=attempts,
            not_before=not_before,
            updated=time.time(),
        )
        committed: dict[str, Any] | None
        try:
            committed = await control.repository.commit_state(
                session,
                control.lease,
                identity,
                state,
                revision=None,
                reason=failure.code if proposed is None else None,
                recovery_detail=values,
            )
        except WorkConflict:
            committed = None
        if committed is None:
            raise WorkConflict("work_recovery_obsolete")
        state = committed["state"]
    control.current = committed
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


def _cancelled(control: WorkControl, current: dict[str, Any]) -> ActivationOutcome:
    """A committed cancellation is an exit, never a second failure notice."""
    control.current = current
    control.ending = "cancelled"
    control.settled = True
    control.outcome = ActivationOutcome(
        ExitReason.CANCELLED, current["id"], **activation_details(control)
    )
    return control.outcome


async def settle(control: WorkControl, *, pending_inputs: bool) -> None:
    if control.settled or control.current is None:
        return
    await control.refresh_effects()
    proposed = control.accepted_ending()
    if control.handoff_work_id is not None:
        state = "queued" if pending_inputs else await control.background_state()
        if state is None:
            state = "waiting_external" if await control.has_owned_execution() else "suspended"
        reason = ExitReason.INPUT
    elif proposed is not None:
        state, reason = proposed, ACCEPTED_REASONS[proposed]
    elif control.yield_segment:
        state, reason = "queued", ExitReason.SEGMENT
    elif pending_inputs:
        pending = await control.pending()
        ready = bool(pending and pending[0]["ready"])
        state, reason = ("queued" if ready else "waiting_external"), ExitReason.INPUT
    elif control.ending in {"waiting_external", "waiting_user"}:
        # Host-owned waits (input preparation, a child's question) are not
        # business controls and carry no accepted decision.
        state = control.ending
        reason = ExitReason.EXTERNAL if state == "waiting_external" else ExitReason.INPUT
    else:
        state = await control.background_state()
        if state is None and await control.has_owned_execution():
            state = "waiting_external"
        if state is None:
            state, reason = "suspended", ExitReason.PAUSED
        else:
            reason = ExitReason.EXTERNAL
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
    elif control.current["state"] == "waiting_external" and state != "waiting_external":
        reason = ExitReason.EXTERNAL
    elif control.current["state"] == "suspended" and state != "suspended":
        reason = ExitReason.PAUSED
    control.ending = control.current["state"]
    control.outcome = ActivationOutcome(
        reason, control.current["id"], **activation_details(control)
    )
    control.settled = True
