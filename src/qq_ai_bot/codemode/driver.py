"""Drive one outer `execute_code` call as the original Work's composition.

The outer call is a `code_composition` parent effect; every wrapper call is a
child Invocation with Host identity. Boundaries follow the design T0-T4:

* T0: snapshot dump, child arguments and code are prepared outside SQLite.
* T1: ``publish_code_boundary`` publishes the snapshot, parent checkpoint and
  the child intent (``dispatch_started=false``) in one writer transaction.
* T2/T3: business children go through the same InvocationService/WorkSession
  admission and receipt path as direct calls; lifecycle children through the
  Host control gate.
* T4: the VM is answered only with the saved original receipt.

The VM never holds authority: on any closing condition the Host stops the
script and settles the outer call; a script cannot catch that and continue.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from qq_ai_bot.capabilities.invocation import (
    Invocation,
    InvocationIdentity,
    TrustedInvocationContext,
    child_operation_id,
    counts_toward_business_limit,
)
from qq_ai_bot.capabilities.media import MediaResultText, result_images
from qq_ai_bot.capabilities.results import ToolExecutionResult
from qq_ai_bot.codemode.api_projection import ScriptApi, ToolReceiptView, receipt_view
from qq_ai_bot.codemode.contract import (
    ADMISSION_CLOSING_ERRORS,
    DUMP_FORMAT,
    NEW_INPUT_ERRORS,
    STOP_ADMISSION_CLOSED,
    STOP_BUDGET,
    STOP_HOST_CONTROL,
    STOP_MEMORY,
    STOP_NEW_INPUT,
    STOP_SNAPSHOT,
    STOP_UNKNOWN_EFFECT,
)
from qq_ai_bot.codemode.driver_types import EngineAnswer, EngineCall, EngineOutcome
from qq_ai_bot.codemode.engine_monty import CodeEngineUnavailable
from qq_ai_bot.codemode.limits import CodeModeLimits
from qq_ai_bot.codemode.snapshot_binding import load_boundary, load_output, persist_boundary
from qq_ai_bot.domain.messages import ToolCall, ToolFunction
from qq_ai_bot.execution_trace.recorder import record_trace, trace_span
from qq_ai_bot.runtime.effect_outcomes import (
    ResultCapture,
    captured_evidence,
    current_result_capture,
    execution_evidence,
)
from qq_ai_bot.runtime.protocol_store import CodeSnapshotBinding
from qq_ai_bot.runtime.work_control import WORK_CONTROL_NAMES
from qq_ai_bot.runtime.work_repository import WorkConflict

if TYPE_CHECKING:
    from qq_ai_bot.codemode.engine_monty import MontyRun, PinnedWorker
    from qq_ai_bot.runtime.work_control import WorkControl

logger = logging.getLogger(__name__)

MEMORY_WRITE = "memory_change"
SEND = "send_message"
# Lifecycle actions that end or yield the original Work. After any of them the
# remaining code is never executed (design §8 control table).
TERMINAL_CONTROL_ACTIONS = frozenset(
    {"wait", "need_input", "complete", "fail", "answer", "accept", "handoff", "resume"}
)


class CodeCompositionYield(Exception):
    """Resource yield: the outer call stays pending on the original journal."""


@dataclass(frozen=True, slots=True)
class ChildClass:
    kind: str  # read / write / send / memory_write / control / state
    parallel_safe: bool
    side_effecting: bool


@dataclass(slots=True)
class CodeHost:
    """Trusted Host services for one activation; never visible to the VM."""

    control: WorkControl
    api: ScriptApi | None
    worker: PinnedWorker | None
    limits: CodeModeLimits
    # Business child through InvocationService → WorkSession T2/T3 → backend.
    execute_business: Callable[[Invocation, bool], Awaitable[str]]
    # Lifecycle child through the original control checks (no business budget).
    execute_control: Callable[[ToolCall, str], Awaitable[tuple[str, bool]]]
    classify: Callable[[ToolCall], ChildClass]
    max_parallel: int
    tool_limit: int
    result_limit: int
    archive: Callable[[str], Awaitable[str | None]] | None = None
    engine_factory: Callable[[PinnedWorker, CodeModeLimits], Any] | None = None
    background: bool = False


@dataclass(slots=True)
class _Child:
    operation_id: str
    ordinal: int
    engine_call_id: int
    feed_index: int
    tool: str
    arguments: str
    klass: ChildClass
    state: str = "prepared"
    dispatched: bool = False
    receipt: str | None = None
    view: ToolReceiptView | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    stop: dict[str, Any] | None = None


@dataclass(slots=True)
class _Usage:
    business_admitted: int = 0
    control_calls: int = 0
    rejected_before_dispatch: int = 0
    reused_receipts: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "compositions": 1,
            "business_admitted": self.business_admitted,
            "control_calls": self.control_calls,
            "rejected_before_dispatch": self.rejected_before_dispatch,
            "reused_receipts": self.reused_receipts,
        }


class _Stop(Exception):
    def __init__(self, reason: str, detail: str = "", payload: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail
        self.payload = payload or {}


@dataclass(slots=True)
class _State:
    parent_key: str
    revision: int
    media_source: tuple[str, int, int] | None = None
    children: dict[tuple[int, int], _Child] = field(default_factory=dict)
    side_effect_done: bool = False


def _refusal(error: str, **extra: Any) -> str:
    return json.dumps({"ok": False, "executed": False, "error": error, **extra}, ensure_ascii=False)


class CodeModeDriver:
    """One outer code call. Shared runtime state never holds the script."""

    def __init__(self, host: CodeHost, outer: Invocation) -> None:
        self.host = host
        self.outer = outer
        self.control = host.control
        self.usage = _Usage()
        self._run: MontyRun | None = None
        self._stdout: list[str] = []
        self._stdout_truncated = False
        self._inflight = 0

    # -- entry points --------------------------------------------------------------

    async def run(self) -> str:
        return await self._traced_composition(resumed=False)

    async def resume(self) -> str:
        return await self._traced_composition(resumed=True)

    async def _traced_composition(self, *, resumed: bool) -> str:
        """Diagnostic hierarchy only; originals stay in Work/artifact storage."""
        identity = self.outer.identity
        async with trace_span(
            "code_composition",
            {
                "operation_id": identity.operation_id,
                "owner_execution_id": identity.owner_execution_id,
                "provider_call_id": identity.provider_call_id,
                "manifest_revision": self.host.api.manifest_revision if self.host.api else None,
                "engine_resource_policy": self.host.limits.engine(),
                "api_revision": self.host.api.digest() if self.host.api else None,
                "resumed": resumed,
            },
        ) as span:
            result = await self._resume() if resumed else await self._start()
            try:
                decoded = json.loads(result)
            except ValueError:
                decoded = None
            value = decoded if isinstance(decoded, dict) else {}
            span.result = {
                "operation_id": identity.operation_id,
                "receipt_sha256": hashlib.sha256(result.encode()).hexdigest(),
                "status": value.get("status"),
                "stop_reason": value.get("stop_reason"),
                "error": value.get("error"),
                "usage": value.get("usage"),
                "result_ref": value.get("result_ref"),
            }
            return result

    async def _start(self) -> str:
        control = self.control
        if control.current is None or control.session is None:
            return _refusal("accept_work_before_execution")
        key = self.outer.identity.operation_id
        repository = control.repository
        existing = await self._parent_row()
        if existing is not None:
            # Reentry of the same original outer call: never a second script.
            if existing["kind"] != "code_composition":
                raise WorkConflict("code_composition_identity_conflict")
            if existing["state"] in {"accepted", "failed"}:
                self.usage.reused_receipts += 1
                return await control.session.journal.effect_result(key)
            return await self._resume()
        try:
            arguments = json.loads(self.outer.call.function.arguments)
        except ValueError:
            return _refusal("invalid_json")
        code = arguments.get("code") if isinstance(arguments, dict) else None
        inputs = arguments.get("inputs", {}) if isinstance(arguments, dict) else None
        if (
            not isinstance(arguments, dict)
            or set(arguments) - {"code", "inputs"}
            or not isinstance(code, str)
            or not 1 <= len(code) <= 65536
            or not isinstance(inputs, dict)
        ):
            return _refusal("tool_input_validation_failed")
        if self.host.worker is None or self.host.api is None:
            return _refusal("code_engine_unavailable")
        store = control.session.journal.objects
        code_ref = await store.put({"code": code, "inputs": inputs})
        _, media_privacy = await self._live_source()
        prepared = await repository.prepare_effect(
            control.lease,
            control.current["id"],
            key,
            "code_composition",
            composition={
                "version": 1,
                "media_privacy_generation": media_privacy,
                "script_id": uuid4().hex,
                "code_ref": code_ref,
                "api_revision": self.host.api.digest() if self.host.api else None,
                "engine_digest": self.host.worker.execution_digest(),
                "dump_format": DUMP_FORMAT,
                "snapshot_revision": 0,
                "manifest_revision": self.host.api.manifest_revision if self.host.api else None,
                "engine_resource_policy": self.host.limits.engine(),
            },
            outcome={
                "tool": "execute_code",
                "side_effecting": False,
                "ok": False,
                "pending": False,
                "uncertain": False,
                "executed": False,
            },
        )
        if not prepared:
            return await control.session.journal.effect_result(key)
        state = _State(
            key, 0, (control.lease.conversation_id, control.lease.generation, media_privacy)
        )
        return await self._drive(state, start=(code, inputs))

    async def _resume(self) -> str:
        """Continue the same composition from its last trusted boundary."""
        control = self.control
        assert control.current is not None and control.session is not None
        key = self.outer.identity.operation_id
        row = await self._parent_row()
        if row is None or row["kind"] != "code_composition":
            raise WorkConflict("code_composition_missing")
        if row["state"] in {"accepted", "failed"}:
            return await control.session.journal.effect_result(key)
        composition = json.loads(row["receipt_json"]).get("composition", {})
        media_privacy = composition.get(
            "media_privacy_generation", composition.get("privacy_generation")
        )
        state = _State(
            key,
            int(composition.get("snapshot_revision", 0)),
            (control.lease.conversation_id, control.lease.generation, media_privacy)
            if isinstance(media_privacy, int)
            else None,
        )
        await self._load_children(state)
        if self.host.worker is None or self.host.api is None:
            for child in state.children.values():
                if child.dispatched and child.receipt is None:
                    child.receipt = await self._original_receipt(child)
                    child.view = receipt_view(
                        child.receipt,
                        evidence=child.evidence,
                        operation_id=child.operation_id,
                        executed=True,
                    )
            return await self._settle(state, stop=_Stop(STOP_SNAPSHOT, "code_engine_unavailable"))
        if not composition.get("snapshot_ref"):
            # No business boundary was ever published: nothing can be resumed and
            # nothing was dispatched. Settle instead of re-running the code.
            return await self._settle(state, stop=_Stop(STOP_SNAPSHOT, "no_trusted_boundary"))
        if (
            composition.get("engine_digest") != self.host.worker.execution_digest()
            or composition.get("api_revision") != self.host.api.digest()
            or composition.get("dump_format") != DUMP_FORMAT
        ):
            return await self._settle(state, stop=_Stop(STOP_SNAPSHOT, "code_api_changed"))
        if not self.host.limits.accepts_saved_engine_policy(
            composition.get("engine_resource_policy")
        ):
            return await self._settle(
                state, stop=_Stop(STOP_SNAPSHOT, "code_engine_resource_policy_changed")
            )
        binding = self._binding(composition)
        try:
            dump, _expected, counters = await load_boundary(
                control.session.journal.objects, binding, composition
            )
            stdout, truncated = await load_output(
                control.session.journal.objects,
                binding,
                composition,
                max_bytes=self.host.limits.max_output_bytes,
            )
            self._stdout, self._stdout_truncated = [stdout], truncated
        except ValueError as exc:
            return await self._settle(state, stop=_Stop(STOP_SNAPSHOT, str(exc)))
        try:
            # A committed stop is an original result, not an activation-local
            # flag. Restore it before answering even one VM future.
            for child in sorted(state.children.values(), key=lambda item: item.ordinal):
                if child.receipt is None:
                    continue
                if child.stop is not None:
                    # The stop ends this VM; only the Work checkpoint's current
                    # accepted control may still end the activation.
                    control.ending = control.accepted_ending(child.operation_id)
                    control.handoff_work_id = child.stop.get("handoff_work_id")
                    raise _Stop(child.stop["reason"], child.operation_id, child.stop["payload"])
                if self._terminal_control(child):
                    control.ending = control.accepted_ending(child.operation_id)
                    raise _Stop(
                        STOP_HOST_CONTROL, child.operation_id, {"control": _loads(child.receipt)}
                    )
                self._closing(child)
                if child.klass.kind == "memory_write" and child.view and child.view.executed:
                    raise _Stop(STOP_MEMORY, child.operation_id)
        except _Stop as stop:
            return await self._settle(state, stop=stop)
        return await self._drive(state, restore=(dump, composition["boundary_call"], counters))

    # -- main loop -----------------------------------------------------------------

    async def _drive(
        self,
        state: _State,
        *,
        start: tuple[str, dict[str, Any]] | None = None,
        restore: tuple[bytes, dict[str, Any], Any] | None = None,
    ) -> str:
        assert self.host.worker is not None and self.host.api is not None
        factory = self.host.engine_factory
        try:
            if factory is None:
                from qq_ai_bot.codemode.engine_monty import MontyEngine

                engine_context = MontyEngine(
                    self.host.worker, self.host.limits, background=self.host.background
                )
            else:
                engine_context = factory(self.host.worker, self.host.limits)
            async with engine_context as engine:
                run = engine.run(self.host.api.names)
                self._run = run
                try:
                    if start is not None:
                        outcome = await run.start(*start)
                        restored = False
                    else:
                        assert restore is not None
                        outcome = await run.restore(*restore)
                        restored = True
                    while outcome.status == "suspended":
                        self._take_output(outcome)
                        assert outcome.call is not None
                        outcome = await self._step(state, outcome.call, restored=restored)
                        restored = False
                    self._take_output(outcome)
                    return await self._settle(state, outcome=outcome)
                finally:
                    await run.terminate()
        except _Stop as stop:
            return await self._settle(state, stop=stop)
        except CodeEngineUnavailable as exc:
            return await self._settle(state, stop=_Stop(STOP_SNAPSHOT, str(exc)))

    async def _step(self, state: _State, call: EngineCall, *, restored: bool) -> EngineOutcome:
        run = self._run
        assert run is not None
        if call.kind == "function":
            assert call.engine_call_id is not None
            existing = state.children.get((call.feed_index, call.engine_call_id))
            if existing is None:
                child = await self._register(state, call)
                if child is None:
                    # Wrong call shape: a script error, not a business call.
                    return await run.answer(
                        call.engine_call_id,
                        EngineAnswer.error("wrapper takes exactly one dict argument", "ValueError"),
                    )
            elif not restored:
                raise WorkConflict("code_child_identity_conflict")
            return await run.answer(call.engine_call_id, EngineAnswer.future())
        # A future wait: persist the program position, then dispatch in Host order.
        if not restored:
            await self._publish(state, call, None)
        pending = [
            child
            for (feed, engine_id), child in state.children.items()
            if feed == call.feed_index and engine_id in call.pending_call_ids
        ]
        if {child.engine_call_id for child in pending} != set(call.pending_call_ids):
            raise _Stop(STOP_SNAPSHOT, "code_pending_mapping_missing")
        pending.sort(key=lambda child: child.ordinal)
        await self._dispatch_all(state, pending)
        answers = {
            child.engine_call_id: EngineAnswer.ok(await self._vm_value(child)) for child in pending
        }
        return await run.settle(answers)

    # -- T1 ------------------------------------------------------------------------

    async def _register(self, state: _State, call: EngineCall) -> _Child | None:
        assert self.host.api is not None
        tool = self.host.api.tool_for(call.function_name or "")
        if tool is None:
            raise _Stop(STOP_ADMISSION_CLOSED, "code_wrapper_unknown")
        if len(call.args) != 1 or call.kwargs or not isinstance(call.args[0], dict):
            return None
        arguments = json.dumps(call.args[0], ensure_ascii=False, separators=(",", ":"))
        ordinal = len(state.children)
        operation_id = child_operation_id(state.parent_key, ordinal)
        invocation = self._child_invocation(
            operation_id, ordinal, call.engine_call_id, call.feed_index, tool, arguments
        )
        klass = self.host.classify(invocation.call)
        child = _Child(
            operation_id,
            ordinal,
            int(call.engine_call_id or 0),
            call.feed_index,
            tool,
            arguments,
            klass,
        )
        await self._publish(state, call, child)
        state.children[(child.feed_index, child.engine_call_id)] = child
        return child

    async def _publish(self, state: _State, call: EngineCall, child: _Child | None) -> None:
        control = self.control
        assert control.current is not None and control.session is not None
        run = self._run
        assert run is not None
        store = control.session.journal.objects
        source_revision, privacy = await self._live_source()
        binding = CodeSnapshotBinding(
            work_id=control.current["id"],
            operation_id=state.parent_key,
            source_execution_id=control.source_key,
            conversation_id=control.lease.conversation_id,
            generation=control.lease.generation,
            source_revision=source_revision,
            privacy_generation=privacy,
            engine_digest=self.host.worker.execution_digest() if self.host.worker else "",
            api_revision=self.host.api.digest() if self.host.api else "",
            dump_format=DUMP_FORMAT,
        )
        try:
            dump = run.dump()
            record = await persist_boundary(
                store,
                binding,
                dump,
                call,
                run.counters,
                max_bytes=self.host.limits.max_snapshot_bytes,
                stdout="".join(self._stdout),
                stdout_truncated=self._stdout_truncated,
            )
        except ValueError as exc:
            # Capacity: stop before the next dispatch, keeping prior effects.
            raise _Stop(STOP_SNAPSHOT, str(exc)) from exc
        child_metadata = None
        if child is not None:
            invocation = self._child_invocation(
                child.operation_id,
                child.ordinal,
                child.engine_call_id,
                child.feed_index,
                child.tool,
                child.arguments,
            )
            arguments_ref = await store.put(child.arguments)
            child_metadata = {**invocation.durable_metadata(), "arguments_ref": arguments_ref}
        try:
            await control.repository.publish_code_boundary(
                control.lease,
                control.current["id"],
                state.parent_key,
                expected_revision=state.revision,
                composition={
                    **record.composition_fields(),
                    "source_revision": source_revision,
                    "privacy_generation": privacy,
                    "source_execution_id": control.source_key,
                },
                child=child_metadata,
                store=store,
                binding=binding,
                side_effecting=child.klass.side_effecting if child is not None else False,
            )
        except WorkConflict as exc:
            raise _Stop(STOP_ADMISSION_CLOSED, exc.code) from exc
        state.revision += 1

    # -- T2/T3 ---------------------------------------------------------------------

    async def _dispatch_all(self, state: _State, pending: list[_Child]) -> None:
        """Bounded read stretches; sends, writes, memory and control are barriers."""
        index = 0
        while index < len(pending):
            child = pending[index]
            if child.receipt is not None:
                index += 1
                continue
            if not child.klass.parallel_safe:
                await self._dispatch_one(state, child, peers=pending)
                index += 1
                continue
            end = index
            while end < len(pending) and pending[end].klass.parallel_safe:
                end += 1
            stretch = [item for item in pending[index:end] if item.receipt is None]
            semaphore = asyncio.Semaphore(max(1, self.host.max_parallel))
            # Host stops are collected, never raised inside the group: a closing
            # read must not turn its siblings' recorded receipts into a group error.
            stops: list[BaseException] = []

            async def bounded(
                item: _Child,
                semaphore: asyncio.Semaphore = semaphore,
                stops: list[BaseException] = stops,
            ) -> None:
                async with semaphore:
                    if stops:
                        return  # Admission closed; this sibling is never dispatched.
                    try:
                        await self._dispatch_one(state, item, peers=pending)
                    except (_Stop, CodeCompositionYield) as stopped:
                        stops.append(stopped)

            async with asyncio.TaskGroup() as group:
                for item in stretch:
                    group.create_task(bounded(item))
            index = end
            if stops:
                # Ordered by Host ordinal; a yield never hides a stop.
                raise next((item for item in stops if isinstance(item, _Stop)), stops[0])

    async def _dispatch_one(self, state: _State, child: _Child, *, peers: list[_Child]) -> None:
        async with trace_span(
            "code_child",
            {
                "operation_id": child.operation_id,
                "parent_effect_key": state.parent_key,
                "child_ordinal": child.ordinal,
                "tool": child.tool,
                "feed_index": child.feed_index,
                "engine_call_id": child.engine_call_id,
            },
        ) as span:
            try:
                await self._dispatch_child(state, child, peers=peers)
            finally:
                view = child.view
                span.result = {
                    "operation_id": child.operation_id,
                    "status": view.status if view else "not_dispatched",
                    "executed": view.executed if view else False,
                    "reused": view.reused if view else False,
                    "pending": view.pending if view else False,
                    "uncertain": view.uncertain if view else False,
                }
                # A closing Host stop exits the span through its error path;
                # retain the observed status without duplicating the result body.
                await record_trace("code_child_outcome", span.result)

    async def _dispatch_child(self, state: _State, child: _Child, *, peers: list[_Child]) -> None:
        control = self.control
        if child.state != "prepared":
            # Restored child: the original receipt only, never a second dispatch.
            child.receipt = await self._original_receipt(child)
            child.view = receipt_view(
                child.receipt,
                evidence=child.evidence,
                operation_id=child.operation_id,
                executed=True,
                reused=True,
            )
            self.usage.reused_receipts += 1
            self._closing(child)
            return
        if child.dispatched:
            # T2 committed before a crash: possibly sent. Never re-run.
            child.receipt = await self._original_receipt(child)
            child.view = receipt_view(
                child.receipt,
                evidence=child.evidence,
                operation_id=child.operation_id,
                executed=True,
            )
            raise _Stop(STOP_UNKNOWN_EFFECT, child.operation_id)
        call = ToolCall(f"c{child.ordinal}", ToolFunction(child.tool, child.arguments))
        if child.klass.kind == "control":
            if any(item is not child and item.receipt is None for item in peers):
                await self._not_dispatched(child, "work_control_requires_exclusive_gate")
                return
            if not self._query_control(child) and await control.repository.has_unresolved_effects(
                control.lease, control.current["id"] if control.current else ""
            ):
                await self._not_dispatched(child, "unresolved_prior_effect")
                return
            await self._dispatch_control(child, call)
            return
        # Segment allowance, checked synchronously with in-flight reservations so
        # concurrent reads cannot overshoot it. The root budget stays atomic at T2.
        charged = counts_toward_business_limit(child.tool)
        if charged and control.tools_started + self._inflight >= self.host.tool_limit:
            raise CodeCompositionYield(child.operation_id)
        self._inflight += int(charged)
        invocation = self._child_invocation(
            child.operation_id,
            child.ordinal,
            child.engine_call_id,
            child.feed_index,
            child.tool,
            child.arguments,
        )
        from qq_ai_bot.runtime.work_budget import WorkBudgetExceeded

        capture = ResultCapture(
            control.current["id"] if control.current else "", child.operation_id
        )
        token = current_result_capture.set(capture)
        try:
            child.receipt = await self.host.execute_business(invocation, child.klass.side_effecting)
            child.evidence = self._captured_evidence(child, capture)
        except WorkBudgetExceeded as exc:
            # T2 rolled back: nothing dispatched or charged. WorkSession stored the
            # original not-executed receipt; the script ends here.
            child.receipt = await self._original_receipt(child)
            child.state = "settled"
            child.view = receipt_view(
                child.receipt,
                evidence=child.evidence,
                operation_id=child.operation_id,
                executed=False,
            )
            self.usage.rejected_before_dispatch += 1
            raise _Stop(STOP_BUDGET, child.operation_id) from exc
        finally:
            current_result_capture.reset(token)
            self._inflight -= int(charged)
        # Admission is the durable T2 marker, never a counter delta that another
        # concurrent sibling could have moved.
        admitted = await self._admitted(child.operation_id)
        self.usage.business_admitted += int(admitted and charged)
        if not admitted:
            self.usage.rejected_before_dispatch += 1
        child.dispatched = admitted
        child.state = "settled"
        child.view = receipt_view(
            child.receipt,
            evidence=child.evidence,
            operation_id=child.operation_id,
            executed=admitted,
        )
        if child.klass.side_effecting and child.view.executed:
            state.side_effect_done = True
        self._closing(child)
        if child.klass.kind == "memory_write" and child.view.executed:
            raise _Stop(STOP_MEMORY, child.operation_id)

    async def _dispatch_control(self, child: _Child, call: ToolCall) -> None:
        control = self.control
        assert control.current is not None
        # The dispatch marker without a business charge: lifecycle controls use
        # the model budget, but a crash after this point is "possibly applied".
        if not await control.repository.admit_dispatch(
            control.lease, control.current["id"], child.operation_id, charge=False
        ):
            child.receipt = await self._original_receipt(child)
            raise _Stop(STOP_UNKNOWN_EFFECT, child.operation_id)
        child.dispatched = True
        ending_before, handoff_before = control.ending, control.handoff_work_id
        capture = ResultCapture(control.current["id"], child.operation_id)
        token = current_result_capture.set(capture)
        try:
            result, executed = await self.host.execute_control(call, child.operation_id)
            child.evidence = self._captured_evidence(child, capture)
        finally:
            current_result_capture.reset(token)
        terminal = (
            control.ending != ending_before
            or control.handoff_work_id != handoff_before
            or (self._terminal_control(child))
        )
        stop = (
            {
                "reason": STOP_HOST_CONTROL,
                "payload": {"control": _loads(result)},
                "ending": control.ending,
                "handoff_work_id": control.handoff_work_id,
            }
            if terminal
            else None
        )
        await control.repository.record_effect(
            child.operation_id,
            "accepted",
            {
                "result": result,
                **({"code_stop": stop} if stop is not None else {}),
                "outcome": child.evidence,
            },
        )
        child.receipt = result
        child.state = "settled"
        child.view = receipt_view(
            result, evidence=child.evidence, operation_id=child.operation_id, executed=executed
        )
        self.usage.control_calls += 1
        if terminal:
            raise _Stop(STOP_HOST_CONTROL, child.operation_id, {"control": _loads(result)})
        self._closing(child)

    async def _not_dispatched(self, child: _Child, error: str = "") -> None:
        payload = _refusal(error)
        child.evidence = {"ok": False, "executed": False, "error_code": error}
        # The intent is settled as never dispatched; no budget was admitted.
        await self.control.repository.record_effect(
            child.operation_id,
            "failed",
            {"result": payload, "error": "never_dispatched"},
        )
        child.receipt = payload
        child.state = "settled"
        child.view = receipt_view(
            payload, evidence=child.evidence, operation_id=child.operation_id, executed=False
        )
        self.usage.rejected_before_dispatch += 1

    def _closing(self, child: _Child) -> None:
        view = child.view
        if view is None:
            return
        code = (view.error or {}).get("code")
        if view.uncertain:
            raise _Stop(STOP_UNKNOWN_EFFECT, child.operation_id)
        if code in NEW_INPUT_ERRORS:
            raise _Stop(STOP_NEW_INPUT, child.operation_id)
        if code in ADMISSION_CLOSING_ERRORS:
            raise _Stop(STOP_ADMISSION_CLOSED, str(code))
        if code == STOP_BUDGET:
            raise _Stop(STOP_BUDGET, child.operation_id)

    def _query_control(self, child: _Child) -> bool:
        """Reads and waiting use WorkControl's ownership/condition checks.

        Pending owned execution is precisely why wait exists. It is not a new
        business dispatch; unknown effects still fence complete and mutations.
        """
        if child.tool != "task_control":
            return False
        try:
            return json.loads(child.arguments).get("action") in {
                "get",
                "list",
                "wait",
                "wait_status",
            }
        except (ValueError, AttributeError):
            return False

    async def _admitted(self, key: str) -> bool:
        from sqlalchemy import select

        from qq_ai_bot.runtime.work_schema_v1 import effects

        async with self.control.repository.database.sessions() as reader:
            raw = await reader.scalar(
                select(effects.c.receipt_json).where(effects.c.effect_key == key)
            )
        if raw is None:
            return False
        metadata = json.loads(raw).get("invocation", {})
        return bool(metadata.get("dispatch_started"))

    def _terminal_control(self, child: _Child) -> bool:
        if child.tool != "task_control":
            return False
        try:
            action = json.loads(child.arguments).get("action")
        except (ValueError, AttributeError):
            return False
        return action in TERMINAL_CONTROL_ACTIONS and child.evidence.get("ok") is True

    # -- settlement ----------------------------------------------------------------

    async def _settle(
        self, state: _State, *, outcome: EngineOutcome | None = None, stop: _Stop | None = None
    ) -> str:
        control = self.control
        # Registered but never dispatched (closed admission, unawaited wrapper):
        # settled as never dispatched; no budget, never resumable.
        for child in state.children.values():
            if child.receipt is None and not child.dispatched and child.state == "prepared":
                await self._not_dispatched(child, "code_composition_closed")
                self.usage.rejected_before_dispatch -= 1
        operations = [
            {
                "operation_id": child.operation_id,
                "tool": child.tool,
                "status": child.view.status if child.view else "not_executed",
            }
            for child in sorted(state.children.values(), key=lambda item: item.ordinal)
        ]
        executed = any(child.dispatched for child in state.children.values())
        body: dict[str, Any] = {
            "operations": operations,
            "usage": self.usage.as_dict(),
            "stdout": "".join(self._stdout),
            "stdout_truncated": self._stdout_truncated,
            "replay_forbidden": True,
        }
        if stop is not None:
            body.update(
                ok=stop.reason in {STOP_HOST_CONTROL, STOP_MEMORY},
                status="stopped" if stop.reason in {STOP_HOST_CONTROL, STOP_MEMORY} else "partial",
                stop_reason=stop.reason,
                executed=executed,
                detail=_STOP_DETAIL.get(stop.reason, ""),
                **stop.payload,
            )
            if stop.reason not in {STOP_HOST_CONTROL, STOP_MEMORY}:
                body["error"] = stop.reason
            if stop.reason == STOP_UNKNOWN_EFFECT:
                body["uncertain_operation_id"] = stop.detail
            if stop.reason == STOP_SNAPSHOT:
                body["snapshot_reason"] = stop.detail
        elif outcome is not None and outcome.status == "completed":
            body.update(ok=True, status="completed", executed=executed)
            body.update(await self._result_view(outcome.output))
        else:
            failure = outcome.failure if outcome is not None else None
            body.update(
                ok=False,
                status="partial" if executed else "failed",
                executed=executed,
                error=f"code_{failure.category}" if failure else "code_failed",
                detail=(failure.message if failure else "")[:2000],
            )
        result = await self._bounded_result(body, state.parent_key)
        images = tuple(
            dict.fromkeys(
                image
                for child in sorted(state.children.values(), key=lambda item: item.ordinal)
                for image in result_images(child.receipt)
            )
        )
        if images:
            if state.media_source is None:
                raise WorkConflict("code_media_source_missing")
            result = MediaResultText(result, images)
        assert control.session is not None
        evidence = execution_evidence(
            ToolExecutionResult(
                ok=body.get("ok") is True,
                data={"executed": executed},
                provider_id="core",
                tool_name="execute_code",
            ),
            tool="execute_code",
            side_effecting=state.side_effect_done,
        )
        evidence["stop_reason"] = body.get("stop_reason")
        await control.session.journal.record_effect(
            state.parent_key,
            "accepted",
            {"result": result, "outcome": evidence},
            media_source=state.media_source,
        )
        capture = current_result_capture.get()
        if capture is not None:
            capture.evidence = evidence
        return result

    async def _bounded_result(self, body: dict[str, Any], parent_key: str) -> str:
        """Budget the final JSON, retaining control facts and original leaf refs."""

        def encode() -> str:
            return json.dumps(body, ensure_ascii=False, default=str, separators=(",", ":"))

        result = encode()
        if len(result) <= self.host.result_limit:
            return result
        if self.host.archive is not None:
            reference = await self.host.archive(result)
            if reference is not None:
                body["summary_ref"] = reference
        operations = body["operations"]
        if operations:
            body.update(
                operations_count=len(operations),
                operations_truncated=True,
                operations_ref={
                    "parent_key_sha256": hashlib.sha256(parent_key.encode()).hexdigest(),
                    "source": "original composition children",
                },
            )
            body["operations"] = []
        if "result" in body and len(encode()) > self.host.result_limit:
            body["result_preview"] = json.dumps(body.pop("result"), ensure_ascii=False, default=str)
            body.update(complete=False, truncated=True)
        # Shrink text only when it actually exists and is removed. JSON escaping
        # is included in every measurement; an empty stdout is not truncated.
        for key in ("result_preview", "stdout", "detail"):
            text = body.get(key)
            if not isinstance(text, str) or len(encode()) <= self.host.result_limit:
                continue
            low, high = 0, len(text)
            while low < high:
                middle = (low + high + 1) // 2
                body[key] = text[:middle]
                if len(encode()) <= self.host.result_limit:
                    low = middle
                else:
                    high = middle - 1
            body[key] = text[:low]
            if key == "stdout" and low < len(text):
                body["stdout_truncated"] = True
        # IDs in the paired original remain intact; oversized display facts use
        # a digest that can be checked against that original, never a new ID.
        for key in ("uncertain_operation_id", "snapshot_reason", "control"):
            if len(encode()) <= self.host.result_limit:
                break
            if key in body:
                value = json.dumps(body.pop(key), ensure_ascii=False, default=str)
                body[key + "_ref_sha256"] = hashlib.sha256(value.encode()).hexdigest()
        # Fill the remaining budget with a prefix of the operation summaries.
        if operations:
            for operation in operations:
                body["operations"].append(operation)
                if len(encode()) > self.host.result_limit:
                    body["operations"].pop()
                    break
        result = encode()
        return result

    async def _result_view(self, output: Any) -> dict[str, Any]:
        """Full program result once; the model gets a bounded view plus a reference."""
        encoded = json.dumps(output, ensure_ascii=False)
        budget = max(256, self.host.result_limit // 2)
        if len(encoded) <= budget:
            return {"result": output, "complete": True, "result_ref": None}
        reference = await self.host.archive(encoded) if self.host.archive is not None else None
        return {
            "result_preview": encoded[:budget],
            "complete": False,
            "truncated": True,
            "original_characters": len(encoded),
            "result_ref": reference,
        }

    # -- helpers -------------------------------------------------------------------

    async def _vm_value(self, child: _Child) -> Any:
        assert child.view is not None
        view = child.view
        if child.tool == "workspace_read" and view.ok and not view.complete and view.result_ref:
            # Rehydrate only the original accepted page, never today's workspace.
            # The reference must be bound to this child receipt, not supplied by code.
            from sqlalchemy import select

            from qq_ai_bot.runtime.work_schema_v1 import effects
            from qq_ai_bot.tool_results.access import access_from_source

            control = self.control
            assert control.current is not None
            database = control.repository.database
            async with database.sessions() as reader:
                receipt = await reader.scalar(
                    select(effects.c.receipt_json).where(
                        effects.c.effect_key == child.operation_id,
                        effects.c.work_id == control.current["id"],
                        effects.c.state == "accepted",
                    )
                )
            saved = _loads(receipt) if isinstance(receipt, str) else {}
            store = database.work_result_store
            if store is not None and saved.get("artifact_handle") == view.result_ref:
                page = await store.read(
                    view.result_ref,
                    operation="get",
                    max_characters=self.host.limits.max_result_bytes,
                    access=control.context_access
                    or access_from_source(
                        control.lease.conversation_id, control.lease.generation, control.source
                    ),
                )
                if (
                    isinstance(page, dict)
                    and page.get("tool_name") == "workspace_read"
                    and page.get("provider_id") == "core"
                    and isinstance(page.get("value"), dict)
                    and isinstance(page["value"].get("text"), str)
                    and page.get("total_items", len(page["value"])) == len(page["value"])
                ):
                    view = replace(view, data=page["value"], complete=True)
        value = view.as_json()
        limit = self.host.limits.max_result_bytes
        if len(json.dumps(value, ensure_ascii=False).encode()) > limit:
            value = {**value, "data": None, "complete": False}
        return value

    def _child_invocation(
        self,
        operation_id: str,
        ordinal: int,
        engine_call_id: int | None,
        feed_index: int,
        tool: str,
        arguments: str,
    ) -> Invocation:
        outer = self.outer.identity
        call = ToolCall(f"c{ordinal}", ToolFunction(tool, arguments))
        return Invocation(
            InvocationIdentity(
                operation_id=operation_id,
                owner_execution_id=outer.owner_execution_id,
                chain_id=outer.chain_id,
                request_sequence=outer.request_sequence,
                provider_call_id=call.id,
                parent_operation_id=outer.operation_id,
                child_ordinal=ordinal,
                engine_call_id=str(engine_call_id),
                feed_index=feed_index,
            ),
            call,
            TrustedInvocationContext(
                self.outer.context.runtime, self.outer.context.manifest_revision
            ),
        )

    def _binding(self, composition: dict[str, Any]) -> CodeSnapshotBinding:
        control = self.control
        assert control.current is not None
        return CodeSnapshotBinding(
            work_id=control.current["id"],
            operation_id=self.outer.identity.operation_id,
            source_execution_id=str(composition.get("source_execution_id", control.source_key)),
            conversation_id=control.lease.conversation_id,
            generation=control.lease.generation,
            source_revision=int(composition.get("source_revision", -1)),
            privacy_generation=int(composition.get("privacy_generation", -1)),
            engine_digest=str(composition.get("engine_digest", "")),
            api_revision=str(composition.get("api_revision", "")),
            dump_format=str(composition.get("dump_format", "")),
        )

    async def _load_children(self, state: _State) -> None:
        control = self.control
        assert control.current is not None and control.session is not None
        rows = await control.repository.composition_children(
            control.current["id"], state.parent_key
        )
        store = control.session.journal.objects
        for row in rows:
            metadata = row["invocation"]
            reference = metadata.get("arguments_ref")
            if not isinstance(reference, str):
                raise _Stop(STOP_SNAPSHOT, "code_child_arguments_missing")
            arguments = await store.get(reference)
            tool = str(metadata["tool_id"])
            call = ToolCall(f"c{metadata['child_ordinal']}", ToolFunction(tool, arguments))
            child = _Child(
                row["effect_key"],
                int(metadata["child_ordinal"]),
                int(metadata["engine_call_id"]),
                int(metadata["feed_index"]),
                tool,
                arguments,
                self.host.classify(call),
                state=row["state"],
                dispatched=bool(metadata.get("dispatch_started")),
            )
            if row["state"] != "prepared":
                # Settled in an earlier segment: its original receipt, for the summary.
                child.receipt = await self._original_receipt(child)
                child.view = receipt_view(
                    child.receipt,
                    evidence=child.evidence,
                    operation_id=child.operation_id,
                    executed=bool(
                        row.get("outcome", {}).get("executed", metadata.get("dispatch_started"))
                    ),
                )
                child.stop = row.get("code_stop")
            state.children[(child.feed_index, child.engine_call_id)] = child
            if child.klass.side_effecting and child.view and child.view.executed:
                state.side_effect_done = True

    async def _original_receipt(self, child: _Child) -> str:
        assert self.control.session is not None
        capture = ResultCapture(
            self.control.current["id"] if self.control.current else "", child.operation_id
        )
        token = current_result_capture.set(capture)
        try:
            result = await self.control.session.journal.effect_result(child.operation_id)
            child.evidence = self._captured_evidence(child, capture)
            return result
        finally:
            current_result_capture.reset(token)

    @staticmethod
    def _captured_evidence(child: _Child, capture: ResultCapture) -> dict[str, Any]:
        evidence = captured_evidence(
            capture,
            tool=child.tool,
            side_effecting=child.klass.side_effecting,
            arguments=child.arguments,
        )
        if evidence is None:
            raise TypeError("Code child execution did not publish typed evidence")
        return evidence

    async def _parent_row(self) -> dict[str, Any] | None:
        from sqlalchemy import select

        from qq_ai_bot.runtime.work_schema_v1 import effects

        assert self.control.current is not None
        async with self.control.repository.database.sessions() as reader:
            row = (
                (
                    await reader.execute(
                        select(effects).where(
                            effects.c.effect_key == self.outer.identity.operation_id,
                            effects.c.work_id == self.control.current["id"],
                        )
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row is not None else None

    async def _live_source(self) -> tuple[int, int]:
        from sqlalchemy import select

        from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
        from qq_ai_bot.execution_trace.db_models import ExecutionTraceStateModel

        async with self.control.repository.database.sessions() as reader:
            source = await reader.get(
                CanonicalConversationModel, self.control.lease.conversation_id
            )
            privacy = (
                await reader.scalar(
                    select(ExecutionTraceStateModel.privacy_generation).where(
                        ExecutionTraceStateModel.id == 1
                    )
                )
                or 0
            )
        if source is None or source.generation != self.control.lease.generation:
            raise _Stop(STOP_ADMISSION_CLOSED, "work_source_generation_changed")
        return int(source.prompt_source_revision), int(privacy)

    def _take_output(self, outcome: EngineOutcome) -> None:
        if outcome.stdout:
            self._stdout.append(outcome.stdout)
        self._stdout_truncated = self._stdout_truncated or outcome.stdout_truncated
        encoded = "".join(self._stdout).encode()
        if len(encoded) > self.host.limits.max_output_bytes:
            self._stdout = [encoded[-self.host.limits.max_output_bytes :].decode(errors="ignore")]
            self._stdout_truncated = True


_STOP_DETAIL = {
    STOP_ADMISSION_CLOSED: "宿主已关闭本脚本的后续调用；已发生的子调用回执保留，需由模型重新规划。",
    STOP_UNKNOWN_EFFECT: "有子调用结果未知；脚本已停止，先按原 operation_id 核对，禁止重发。",
    STOP_HOST_CONTROL: "生命周期控制已执行，脚本余下代码不再运行。",
    STOP_MEMORY: "记忆写入已保存；先观察回执再决定后续发送，脚本余下代码不再运行。",
    STOP_NEW_INPUT: "新输入到达；脚本已停止，按新要求继续。",
    STOP_BUDGET: "工作总预算已用尽；脚本已停止。",
    STOP_SNAPSHOT: "可信恢复点不可用；脚本以部分结果结束，不从头重跑。",
}


def _loads(value: str) -> Any:
    try:
        return json.loads(value)
    except ValueError:
        return value


def is_control_tool(name: str) -> bool:
    return name in WORK_CONTROL_NAMES
