"""One fenced decision for every activation, independent of its entrypoint."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, replace
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from qq_ai_bot.runtime.activation_outcome import (
    ActivationOutcome,
    ExitReason,
    SegmentBudgetReached,
    WorkNoProgress,
    classify_failure,
    failure_status_text,
)
from qq_ai_bot.runtime.work_budget import WorkBudgetExceeded
from qq_ai_bot.runtime.work_recovery_schema import deliveries, recovery
from qq_ai_bot.runtime.work_repository import WorkCapacityError, WorkConflict, bounded_json
from qq_ai_bot.runtime.work_schema_v1 import effects, inputs, journal, work
from qq_ai_bot.runtime.work_wait_schema import waits

if TYPE_CHECKING:
    from qq_ai_bot.runtime.work_control import WorkControl

from qq_ai_bot.runtime.work_control import ACCEPTED_ENDINGS

logger = logging.getLogger(__name__)

ACCEPTED_REASONS = {
    "completed": ExitReason.COMPLETED,
    "failed": ExitReason.PAUSED,
    "waiting_user": ExitReason.INPUT,
    "waiting_external": ExitReason.EXTERNAL,
}


def _capacity_pause_text(code: str) -> str:
    if code == "work_compaction_no_capacity_improvement":
        detail = "上下文压缩未释放足够空间"
    elif code == "work_compaction_source_capacity":
        detail = "用于压缩的资料超过单次模型输入窗口"
    elif code in {"model_request_capacity", "work_task_input_source_capacity"}:
        detail = "本轮上下文超过可用模型输入窗口"
    elif code == "work_protocol_storage_capacity":
        detail = "工作资料存储空间不足"
    elif code in {
        "work_record_too_large",
        "work_checkpoint_capacity",
        "work_protocol_object_capacity",
    }:
        detail = "工作记录超出存储容量限制"
    else:
        detail = "上下文整理未能完成"
    return f"{detail}，已暂停并保留已有结果。"


async def _has_recorded_effects(control: WorkControl) -> bool:
    """A changed source cannot automatically replay work with an effect receipt."""
    assert control.current is not None
    if control.current["sent_messages"]:
        return True
    async with control.repository.database.sessions() as session:
        return bool(
            await session.scalar(
                select(effects.c.effect_key)
                .where(effects.c.work_id == control.current["id"])
                .limit(1)
            )
            or await session.scalar(
                select(deliveries.c.id)
                .where(deliveries.c.work_id == control.current["id"])
                .limit(1)
            )
        )


async def _never_started(session: Any, current: dict[str, Any]) -> bool:
    """Persistent facts prove that no execution of this Work ever began.

    ``model_requests`` is reserved after request admission and before the
    dispatched journal, so any count excludes this policy even when no
    journal was saved. Inconsistent rows (journal/effect without a count) are
    treated as started; nothing is inferred from activation counters.
    """
    from qq_ai_bot.runtime.subagent_schema import children

    if (
        current["model_requests"]
        or current["tool_calls"]
        or current["sent_messages"]
        or json.loads(current["checkpoint_json"]).get("accepted_control") is not None
    ):
        return False
    identity = current["id"]
    for column in (
        journal.c.work_id,
        effects.c.work_id,
        deliveries.c.work_id,
        waits.c.work_id,
        inputs.c.work_id,
        children.c.root_id,
    ):
        if await session.scalar(select(column).where(column == identity).limit(1)) is not None:
            return False
    return True


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
            observed = await control.repository.get(control.current["id"])
            if observed is not None and observed["state"] == "cancelled":
                return _cancelled(control, observed)
            raise exc
        if failure.code == "work_journal_source_changed" and await _has_recorded_effects(control):
            failure = replace(
                failure,
                retryable=False,
                diagnostics={**failure.diagnostics, "effect_receipt_recorded": True},
            )
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
                    .where(inputs.c.work_id == identity, inputs.c.state == "pending")
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
        # A lifecycle decision accepted before the failure is still the decision.
        # The shared writer rechecks inputs, effects and children; an exception
        # never mints a new success candidate.
        accepted = (
            json.loads(current["checkpoint_json"]).get("accepted_control")
            if current is not None
            else None
        )
        proposed = ACCEPTED_ENDINGS.get(str(accepted.get("action"))) if accepted else None
        if proposed is not None:
            state, reason, not_before = proposed, ACCEPTED_REASONS[proposed], 0
        elif (
            # Approved SELF startup policy: the original SELF source's first
            # scene/Presence boundary was definitely not sent and its bounded
            # retries are exhausted. Fail by original ID (releases admission
            # capacity, keeps the fact); any execution evidence keeps the
            # retained pause instead.
            state == "suspended"
            and control.startup_boundary
            and control.source.get("origin") == "self_initiative"
            and failure.certainty == "not_sent"
            and current is not None
            and current["state"] in {"queued", "running"}
            and await _never_started(session, dict(current))
        ):
            state = "failed"
            failure = replace(failure, diagnostics={**failure.diagnostics, "startup_failed": True})
        values = dict(
            # Re-observing a suspended episode is not a new pause. Its original
            # delivery key and receipts remain authoritative across activations.
            activation_id=prior["activation_id"]
            if current is not None and current["state"] == "suspended" and prior
            else control.lease.owner,
            exit_reason=reason.value,
            stage=failure.stage,
            failure_json=bounded_json(asdict(failure)),
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
        if (
            state == "suspended"
            and failure.code != "work_activation_interrupted"
            and not control.lease.work_id
            and control.source.get("delivery_contract") != "return_to_caller"
        ):
            descriptions = {
                ExitReason.BUDGET: "这项工作的总执行额度已用完，已暂停并保留结果。",
                ExitReason.CAPACITY: _capacity_pause_text(failure.code),
                ExitReason.NO_PROGRESS: failure_status_text(failure),
            }
            if failure.diagnostics.get("category") == "work_conflict":
                if failure.code == "work_journal_source_changed":
                    text = (
                        "会话资料在处理期间变化，已执行的操作和回执已保留；"
                        "后续处理暂停。请先核对任务状态，避免重复执行。"
                        if failure.diagnostics.get("effect_receipt_recorded")
                        else "会话资料在处理期间变化，这项工作已暂停并保留已有结果。"
                    )
                else:
                    text = "工作状态发生冲突，已暂停并保留已有结果；请先核对任务状态。"
            else:
                text = descriptions.get(reason, "这项工作遇到执行错误，已暂停并保留已有结果。")
            await session.execute(
                insert(deliveries)
                .values(
                    id=f"notice:{identity}:{values['activation_id']}",
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
    rejected = None
    if control.handoff_work_id is not None:
        state = "queued" if pending_inputs else await control.background_state()
        if state is None:
            state = (
                "waiting_external"
                if await control.has_unresolved_effects(uncertain=False)
                else "suspended"
            )
        reason = ExitReason.INPUT
    elif proposed is not None:
        state, reason = proposed, ACCEPTED_REASONS[proposed]
        if proposed in {"completed", "failed"}:
            background = await control.background_state()
            if background:
                state, reason = background, ExitReason.EXTERNAL
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
        if state is None and await control.has_unresolved_effects(uncertain=False):
            state = "waiting_external"
        if state is None:
            state, reason = "suspended", ExitReason.PAUSED
            rejected = control.completion_rejected
        else:
            reason = ExitReason.EXTERNAL
    control.current = await control.repository.transition(
        control.lease,
        control.current["id"],
        control.current["revision"],
        state,
        reason=rejected or reason.value,
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
