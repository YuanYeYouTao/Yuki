"""One original-call execution boundary for direct and composed tool calls."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from qq_ai_bot.capabilities.invocation import (
    Invocation,
    child_operation_id,
    counts_toward_business_limit,
)
from qq_ai_bot.domain.messages import ToolCall

if TYPE_CHECKING:
    from qq_ai_bot.runtime.work_control import WorkControl
    from qq_ai_bot.runtime.work_session import WorkSession

logger = logging.getLogger(__name__)
_TOOL_AUDITS: ContextVar[
    tuple[str, tuple[str, int, int], list[Callable[[], Awaitable[None]]]] | None
] = ContextVar("work_tool_post_effect_audits", default=None)


def defer_tool_audit(call_key: str, audit: Callable[[], Awaitable[None]]) -> bool:
    """Defer only this call's derived audit until its durable effect commits."""
    current = _TOOL_AUDITS.get()
    if current is None:
        return False
    if current[0] != call_key:
        raise ValueError("tool_audit_effect_key_mismatch")
    current[2].append(audit)
    return True


def tool_audit_source(call_key: str) -> tuple[str, int, int] | None:
    """Original Conversation/generation/privacy authority, frozen before dispatch."""
    current = _TOOL_AUDITS.get()
    if current is None:
        return None
    if current[0] != call_key:
        raise ValueError("tool_audit_effect_key_mismatch")
    return current[1]


@dataclass(frozen=True, slots=True)
class BatchPlan:
    """Response-local identity validation, independent of completion order."""

    calls: tuple[ToolCall, ...]
    conflicting_ids: frozenset[str]

    @classmethod
    def prepare(cls, calls: tuple[ToolCall, ...]) -> BatchPlan:
        seen: set[str] = set()
        conflicts: set[str] = set()
        for call in calls:
            if call.id in seen:
                conflicts.add(call.id)
            seen.add(call.id)
        return cls(calls, frozenset(conflicts))


class InvocationService:
    """The original effect and budget owner for one Host invocation.

    WorkSession keeps the protocol (call keys, request sequence, journal); this
    service owns the T1 prepare / T2 admit / domain / T3 record ordering.
    """

    async def invoke(
        self,
        invocation: Invocation,
        execute: Callable[[], Awaitable[str]],
        *,
        side_effecting: bool,
    ) -> str:
        from sqlalchemy import select

        from qq_ai_bot.capabilities.media import MediaResultText
        from qq_ai_bot.capabilities.results import ToolExecutionResult
        from qq_ai_bot.execution_trace.db_models import ExecutionTraceStateModel
        from qq_ai_bot.runtime.effect_outcomes import (
            ResultCapture,
            current_result_capture,
            effect_evidence,
            execution_finished,
            readonly_call_signature,
        )
        from qq_ai_bot.runtime.work_budget import WorkBudgetExceeded
        from qq_ai_bot.runtime.work_repository import WorkConflict

        runtime: Any = invocation.context.runtime
        control: WorkControl | None = getattr(runtime, "work_control", None)
        session: WorkSession | None = getattr(control, "session", None)
        if control is None or session is None:
            return await execute()
        call, identity = invocation.call, invocation.identity
        if identity.parent_operation_id is None:
            if session.call_key(call.id) != identity.operation_id:
                raise ValueError("invocation_journal_identity_conflict")
        elif (
            identity.child_ordinal is None
            or child_operation_id(identity.parent_operation_id, identity.child_ordinal)
            != identity.operation_id
        ):
            # A child is keyed by its parent and Host admission ordinal only.
            raise ValueError("invocation_journal_identity_conflict")
        # A composition child never bypasses the pending-input fence.
        allow_pending = (
            call.function.name == "send_message" and identity.parent_operation_id is None
        )
        journal = session.journal
        # The original Host operation, including a composition child's identity.
        key = identity.operation_id
        if control.current is not None:
            if identity.owner_execution_id != control.current["id"]:
                raise WorkConflict("invocation_owner_conflict")
            await control.repository.validate_invocation(key, invocation.durable_metadata())

        def refuse(error_code: str, detail: str = "") -> str:
            outcome = ToolExecutionResult(
                ok=False,
                provider_id="core",
                tool_name=call.function.name,
                data={"executed": False},
                mutation_committed=False,
                error_code=error_code,
                public_message=detail,
            )
            capture = current_result_capture.get()
            if capture is not None:
                capture.outcome = outcome
            return json.dumps(outcome.model_payload(), ensure_ascii=False)

        report = None
        report_target = None
        if call.function.name == "send_message":
            if control.current is not None:
                child_intent = (
                    identity.parent_operation_id is not None
                    and await control.repository.undispatched_intent(control.current["id"], key)
                )
                if not child_intent and await journal.effect_state(key) is not None:
                    if not await control.repository.valid(control.lease):
                        raise WorkConflict("work_activation_obsolete")
                    return await journal.effect_result(key)
            try:
                arguments = json.loads(call.function.arguments)
                if isinstance(arguments, dict):
                    report = await control.validate_work_report(arguments)
                    if report is not None:
                        report_target = await control.communication_target()
            except ValueError as exc:
                return refuse(str(exc))
        if control.current is None:
            return await execute()
        if not allow_pending and await control.pending():
            return refuse("new_input_before_execution")
        if not allow_pending:
            await control.validate()
        if (
            side_effecting
            and not allow_pending
            and await control.has_unresolved_effects(pending=False)
        ):
            return refuse("unresolved_prior_effect", "先查询原执行结果；结果未知时不能继续副作用。")
        if not await control.repository.prepare_effect(
            control.lease,
            control.current["id"],
            key,
            "tool",
            invocation=invocation.durable_metadata(),
            outcome={
                "tool": call.function.name,
                "side_effecting": side_effecting,
                "ok": False,
                "pending": False,
                "uncertain": False,
                "executed": False,
                **(
                    {
                        "readonly_call_signature": readonly_call_signature(
                            call.function.name, call.function.arguments
                        )
                    }
                    if not side_effecting
                    else {}
                ),
                **({"work_report": report, "report_target": report_target} if report else {}),
            },
        ):
            await control.repository.validate_invocation(key, invocation.durable_metadata())
            # A composition child's intent was published at T1 with its snapshot;
            # only that exact undispatched intent continues to T2. Anything else
            # (dispatched, settled, legacy) returns the original receipt.
            if identity.parent_operation_id is None or not (
                await control.repository.undispatched_intent(control.current["id"], key)
            ):
                return await journal.effect_result(key)
        charged = counts_toward_business_limit(call.function.name)
        try:
            if not await control.repository.admit_dispatch(
                control.lease, control.current["id"], key, charge=charged
            ):
                return await journal.effect_result(key)
            if charged:
                control.current["tool_calls"] += 1
                control.tools_started += 1
        except WorkBudgetExceeded:
            await journal.record_effect(
                key,
                "accepted",
                {
                    "result": json.dumps(
                        {"ok": False, "executed": False, "error": "work_total_budget_exhausted"}
                    )
                },
            )
            raise
        async with control.repository.database.sessions() as reader:
            privacy_generation = (
                await reader.scalar(
                    select(ExecutionTraceStateModel.privacy_generation).where(
                        ExecutionTraceStateModel.id == 1
                    )
                )
                or 0
            )
        # An erasure during invoke or accepted persistence cannot authorize this
        # old result under the deletion generation observed by its later audit.
        audit_source = (control.lease.conversation_id, control.lease.generation, privacy_generation)
        audits: list[Callable[[], Awaitable[None]]] = []
        audit_token = _TOOL_AUDITS.set((key, audit_source, audits))

        def receipt_evidence(outcome: ToolExecutionResult) -> dict[str, Any]:
            return effect_evidence(
                outcome,
                tool=call.function.name,
                side_effecting=side_effecting,
                arguments=call.function.arguments,
                report=report,
                report_target=report_target,
            )

        parent_capture = current_result_capture.get()
        capture = ResultCapture(control.current["id"], key)
        capture_token = current_result_capture.set(capture)
        try:
            result = await execute()
        except BaseException as exc:
            try:
                if capture.outcome is None:
                    await journal.record_effect(
                        key,
                        "unknown",
                        {
                            "error": "execution_interrupted",
                            "outcome": {
                                "tool": call.function.name,
                                "side_effecting": side_effecting,
                                "uncertain": True,
                                "delivered_message": False,
                                **(
                                    {"work_report": report, "report_target": report_target}
                                    if report
                                    else {}
                                ),
                            },
                        },
                    )
                else:
                    # The backend returned a typed outcome before presentation
                    # persistence failed. Body loss cannot erase execution facts.
                    evidence = receipt_evidence(capture.outcome)
                    fallback = json.dumps(
                        {
                            **evidence,
                            "result_unavailable": True,
                            "result_error": "tool_result_publication_failed",
                            "replay_forbidden": True,
                        },
                        ensure_ascii=False,
                    )
                    await journal.record_effect(
                        key,
                        "accepted",
                        {
                            "result": MediaResultText(fallback, capture.outcome.images),
                            "outcome": evidence,
                            "artifact_handle": capture.artifact_handle,
                        },
                        media_source=audit_source,
                    )
                    if isinstance(exc, OSError) and not capture.outcome.uncertain:
                        return MediaResultText(fallback, capture.outcome.images)
            except Exception as secondary:
                exc.add_note(f"effect receipt persistence deferred: {type(secondary).__name__}")
            raise
        finally:
            _TOOL_AUDITS.reset(audit_token)
            current_result_capture.reset(capture_token)
            if parent_capture is not None and capture.outcome is not None:
                parent_capture.outcome = capture.outcome
                parent_capture.artifact_handle = capture.artifact_handle
        if capture.outcome is None:
            raise TypeError("live tool execution did not publish a typed outcome")
        evidence = receipt_evidence(capture.outcome)
        await journal.record_effect(
            key,
            "accepted",
            {
                "result": result,
                "outcome": evidence,
                "artifact_handle": capture.artifact_handle,
            },
            media_source=audit_source,
        )
        control.observe_evidence(evidence)
        if (
            capture.outcome.provider_id == "core"
            and call.function.name
            in {"get_code_run", "terminal_read", "cancel_code_run", "terminal_control"}
            and isinstance(evidence.get("run_id"), str)
            and execution_finished(evidence)
        ):
            await control.repository.resolve_run_effects(
                control.lease,
                control.current["id"],
                evidence["run_id"],
                evidence,
            )
        # No audit runs after an uncertain effect commit. Cancellation after this
        # commit propagates without replacing its already-confirmed effect.
        for audit in audits:
            try:
                await audit()
            except Exception as exc:
                logger.warning(
                    "tool_evidence_record_failed category=%s coverage_incomplete=true",
                    type(exc).__name__,
                )
        return result
