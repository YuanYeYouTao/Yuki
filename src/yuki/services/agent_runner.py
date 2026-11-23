"""Reusable bounded Chat Completions tool loop for user and scheduled turns."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Set
from contextlib import AbstractContextManager, nullcontext
from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING, Any, Protocol

from yuki.admin.models import RuntimeConfigSnapshot
from yuki.capabilities.coordinator import (
    MISSING_TOOL_RESULT,
    CoordinatedToolBackend,
    CoordinatedToolResult,
    ToolInvocationCoordinator,
)
from yuki.capabilities.invocation import Invocation, direct_invocations
from yuki.capabilities.media import result_images
from yuki.capabilities.results import ToolExecutionResult
from yuki.domain.messages import (
    ChatImage,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ChatTool,
    ModelResponseStatus,
    NativeToolDefinition,
    NativeToolEvent,
    ResponseCitation,
    ToolCall,
    ToolFunction,
)
from yuki.execution_trace.recorder import trace_span
from yuki.llm.base import (
    LLMError,
)
from yuki.llm.request_media import request_media
from yuki.model_runtime.executor import ModelExecutor
from yuki.model_runtime.models import ModelCapability, ModelTask
from yuki.runtime.activation_outcome import ActivationOutcome, record_model_requests
from yuki.runtime.effect_outcomes import (
    ResultCapture,
    captured_evidence,
    current_result_capture,
)
from yuki.runtime.execution_receipts import ExecutionReceipts, current_receipts
from yuki.runtime.origin import TurnOrigin
from yuki.runtime.work_control import READ_ONLY_CONTROLS, WORK_CONTROL_NAMES, WorkControl
from yuki.runtime.work_repository import WorkCapacityError
from yuki.services.concurrency import ConcurrencyManager
from yuki.services.context_boundary import ContextBoundaryReader
from yuki.services.invocation_service import BatchPlan, InvocationService
from yuki.services.native_tool_binder import NativeToolBinder
from yuki.services.turn_transcript import (
    TurnTranscript,
)
from yuki.web.models import WebMode
from yuki.web.route_context import web_model_task

if TYPE_CHECKING:
    from yuki.services.context_compaction import PublicCompactContext
    from yuki.services.main_agent_contract import MainAgentContract

logger = logging.getLogger(__name__)


def _unexecuted_tool_result(name: str, error: str, detail: str = "") -> str:
    """Publish a pre-dispatch refusal before formatting its public result."""
    outcome = ToolExecutionResult(
        ok=False,
        error_code=error,
        public_message=detail
        or (
            f"没有名为 {name} 的工具；只调用当前声明的工具。"
            if error == "tool_not_declared"
            else "调用没有执行。"
        ),
        data={"executed": False},
        mutation_committed=False,
        provider_id="runtime",
        tool_name=name,
    )
    capture = current_result_capture.get()
    if capture is not None:
        capture.outcome = outcome
    return json.dumps(outcome.model_payload(), ensure_ascii=False)


def _undeclared(name: str) -> str:
    """A name outside this request's manifest; never resolved through history."""
    return json.dumps(
        {
            "ok": False,
            "error": "tool_not_declared",
            "message": f"没有名为 {name} 的工具；只调用当前声明的工具。",
        },
        ensure_ascii=False,
    )


class _RequestNotStarted(Exception):
    def __init__(self, cause: LLMError) -> None:
        self.cause = cause
        super().__init__(str(cause))


@dataclass(frozen=True, slots=True)
class AgentRuntime:
    origin: TurnOrigin
    actor_user_id: str
    actor_is_superuser: bool
    conversation_key: str
    current_group_id: str | None
    bot_user_id: str
    gateway: object | None
    runtime_config: RuntimeConfigSnapshot
    allowed_capabilities: frozenset[str]
    before_model_request: Callable[[], Awaitable[None]] | None = None
    canonical_conversation_id: str | None = None
    presence_id: str | None = None
    conversation_generation: int | None = None
    dynamic_context_prepared: bool = False
    work_control: WorkControl | None = None
    execution_id: str | None = None
    source_event_id: int | None = None
    fixed_tools: tuple[ChatTool, ...] | None = None
    manifest_revision: str = ""
    invocation_source: dict[str, Any] | None = None
    invocation_goal: str | None = None
    visible_event_ids: Set[int] = frozenset()
    auxiliary_requests: list[int] | None = None
    preparation_model_requests: int = 0
    observation_boundary: ContextBoundaryReader | None = None
    public_compaction: PublicCompactContext | None = None
    compact_focus: str | None = None
    activation_context: Callable[[], dict[str, Any]] | None = None


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    text: str
    tool_calls_used: int
    model_requests: int
    messages_sent: int = 0
    native_tool_events: tuple[NativeToolEvent, ...] = ()
    citations: tuple[ResponseCitation, ...] = ()
    response_status: ModelResponseStatus = ModelResponseStatus.COMPLETED
    suppress_delivery: bool = False
    work_state: str | None = None
    outcome: ActivationOutcome | None = None
    work_id: str | None = None


class AgentToolBackend(CoordinatedToolBackend, Protocol):
    async def execute_call(self, invocation: Invocation) -> str: ...

    async def render_result(self, outcome: ToolExecutionResult) -> str:
        """Model text of one tool outcome under this execution's result budget."""
        ...

    def parallel_safe(self, name: str, runtime: AgentRuntime) -> bool: ...

    def is_side_effecting(
        self,
        name: str,
        arguments_json: str,
        runtime: AgentRuntime,
    ) -> bool: ...

    def finalize(self, content: str, runtime: AgentRuntime) -> str: ...

    async def prepare(self, runtime: AgentRuntime) -> None:
        """Prepare authorized local exposure before the first request."""

    def record_failure_usage(self, *, tool_calls: int, model_requests: int) -> None:
        pass

    def refresh_catalog(self, runtime: AgentRuntime) -> None:
        pass

    def pin_web_provider(self) -> AbstractContextManager[None]:
        return nullcontext()

    def work_control_allowed(self, name: str) -> bool:
        return False

    def work_query_allowed(self, name: str) -> bool:
        return False

    async def observe_response(self, response: ChatResponse, runtime: AgentRuntime) -> None:
        pass

    async def participation_context(self, runtime: AgentRuntime) -> dict[str, Any] | None:
        """Read participation facts for this activation's original discussion."""
        return None


class AgentRunner:
    """Execute a provider-neutral bounded tool loop without fabricating inbound events."""

    def __init__(
        self,
        model_executor: ModelExecutor,
        concurrency: ConcurrencyManager,
        *,
        task: ModelTask = ModelTask.CHAT_AGENT,
        main_contract: MainAgentContract | None = None,
    ) -> None:
        self._models = model_executor
        self._concurrency = concurrency
        self._task = task
        self._tool_coordinator = ToolInvocationCoordinator()
        self._native_tools = NativeToolBinder()
        self.main_contract = main_contract

    def work_contract(
        self,
        runtime: RuntimeConfigSnapshot,
        system_messages: tuple[ChatMessage, ...],
        definitions: tuple[ChatTool, ...] | None,
        *,
        manifest_revision: str = "",
    ) -> str:
        if self.main_contract is not None:
            system_messages = self.main_contract.invariant_messages()
        return hashlib.sha256(
            json.dumps(
                [
                    "context-layout:2",
                    repr(definitions),
                    manifest_revision
                    or (self.main_contract.revision if self.main_contract is not None else ""),
                    asdict(runtime.web),
                    self._models.profile_revision(self._task),
                    [(m.role, m.content) for m in system_messages if m.role == "system"],
                ],
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest()

    def _capacity_request(self, request: ChatRequest) -> ChatRequest:
        return self._models.capacity_request(self._task, request)

    def prepare_request_tools(
        self,
        definitions: tuple[ChatTool, ...],
        *,
        runtime_config: RuntimeConfigSnapshot,
        allowed_capabilities: frozenset[str],
    ) -> tuple[tuple[ChatTool, ...], tuple[NativeToolDefinition, ...]]:
        """Use the dispatch tool shape for both preparation and the actual request."""
        web_config = runtime_config.web
        try:
            web_mode = WebMode(web_config.mode)
        except ValueError:
            web_mode = WebMode.DISABLED
        protocol = self._models.protocol(self._task)
        capabilities = self._models.capabilities(self._task)
        search_mode = self._models.search_mode(self._task)
        native = self._native_tools.bind(
            protocol=protocol,
            capabilities=capabilities,
            allowed_capabilities=allowed_capabilities,
            web_mode=web_mode,
            search_mode=search_mode,
        )
        excluded = self._native_tools.excluded_function_names(
            protocol=protocol,
            capabilities=capabilities,
            allowed_capabilities=allowed_capabilities,
            web_mode=web_mode,
            search_mode=search_mode,
        )
        return tuple(tool for tool in definitions if tool.name not in excluded), native

    async def run(
        self,
        initial_messages: tuple[ChatMessage, ...],
        runtime: AgentRuntime,
        tools: AgentToolBackend | None,
    ) -> AgentRunResult:
        with (
            web_model_task(self._task),
            self._models.pin(),
            tools.pin_web_provider() if tools is not None else nullcontext(),
        ):
            async with trace_span(
                "turn",
                {
                    "message_count": len(initial_messages),
                    # Provider attempts link their fixed tool list through this revision.
                    "manifest_revision": runtime.manifest_revision,
                },
                recorder=self._models.traces,
                conversation_id=runtime.canonical_conversation_id,
                execution_id=runtime.execution_id,
                source_event_id=runtime.source_event_id,
                origin=runtime.origin.value,
            ) as span:
                result = await self._run_with_receipts(initial_messages, runtime, tools)
                span.result = {
                    "tool_calls_used": result.tool_calls_used,
                    "model_requests": result.model_requests,
                    "messages_sent": result.messages_sent,
                    "response_status": result.response_status.value,
                    "suppress_delivery": result.suppress_delivery,
                    "work_state": result.work_state,
                    "work_id": result.work_id,
                    "outcome": asdict(result.outcome) if result.outcome is not None else None,
                    "native_tool_event_count": len(result.native_tool_events),
                    "citation_count": len(result.citations),
                }
                return result

    async def _run_with_receipts(
        self,
        initial_messages: tuple[ChatMessage, ...],
        runtime: AgentRuntime,
        tools: AgentToolBackend | None,
    ) -> AgentRunResult:
        receipts = ExecutionReceipts()
        runtime = replace(runtime, auxiliary_requests=[runtime.preparation_model_requests])
        self._record_failure_usage(
            tools, tool_calls=0, model_requests=runtime.preparation_model_requests
        )
        token = current_receipts.set(receipts)
        control = runtime.work_control
        if control is not None:
            for _ in range(runtime.preparation_model_requests):
                await control.reserve_request(auxiliary=True)
        try:
            from yuki.services.turn_execution import TurnExecution

            result = await TurnExecution(self, initial_messages, runtime, tools).activate()
            if control is not None:
                result = replace(
                    result,
                    model_requests=control.requests_started,
                    work_id=control.current["id"] if control.current is not None else None,
                )
            elif runtime.auxiliary_requests:
                result = replace(
                    result, model_requests=result.model_requests + runtime.auxiliary_requests[0]
                )
            return result
        except BaseException as exc:
            if control is None or control.current is None or not isinstance(exc, Exception):
                if control is not None:
                    # The unowned caller settles this exit with the requests it paid.
                    record_model_requests(exc, control.requests_started)
                raise
            outcome = await control.recover_failure(exc)
            return AgentRunResult(
                text="",
                tool_calls_used=control.tools_started,
                model_requests=control.requests_started,
                suppress_delivery=True,
                work_state=control.ending,
                outcome=outcome,
            )
        finally:
            current_receipts.reset(token)

    @staticmethod
    async def _validate_tool_media(
        tools: AgentToolBackend | None,
        runtime: AgentRuntime,
        selected_media: tuple[ChatImage, ...],
    ) -> None:
        """Recheck the sources of the images this request actually carries."""
        images = tuple(
            image for image in selected_media if image.source in {"history", "workspace", "tool"}
        )
        # Older pixels may already live in an opaque continuation. The portable
        # dependency set is validated by the backend separately below.
        validator = getattr(tools, "validate_images", None)
        if images:
            if not callable(validator):
                raise LLMError("tool_media_source_validator_unavailable")
            await validator(images, runtime)

    @staticmethod
    def _media_limits(runtime: AgentRuntime, tools: AgentToolBackend | None) -> tuple[int, int]:
        return runtime.runtime_config.vision.max_frames_per_turn, getattr(
            tools, "media_max_bytes", 16_777_216
        )

    @classmethod
    def _check_request_media_budget(
        cls,
        carried: tuple[str, ...],
        runtime: AgentRuntime,
        tools: AgentToolBackend | None,
    ) -> None:
        """Images left after replacing old observations; only other images can exceed."""
        frames, limit = cls._media_limits(runtime, tools)
        if len(carried) > frames or sum(len(url) for url in carried) > limit:
            raise WorkCapacityError("media_request_budget_exceeded")

    def _budget_tool_media(
        self,
        batch: tuple[tuple[ToolCall, str, bool], ...],
        transcript: TurnTranscript,
        runtime: AgentRuntime,
        tools: AgentToolBackend | None,
        evidence: Mapping[str, dict[str, Any]],
    ) -> tuple[tuple[ToolCall, str, bool], ...]:
        """Bound each new result's pixels; whether a refused result committed is typed evidence.

        Older observations are replaced at request assembly, so a new result
        only has to fit beside the images that are never replaced.
        """
        sequence = transcript.request()
        fixed = tuple(
            image.data_url
            for image in request_media(
                ChatRequest(
                    messages=sequence.messages,
                    continuation=sequence.continuation,
                    continuation_items=sequence.items,
                )
            )
            if not image.observation
        )
        frames, limit = self._media_limits(runtime, tools)
        size = sum(len(url) for url in fixed)
        output = []
        for call, result, executed in batch:
            images = result_images(result)
            if images:
                error = None
                if ModelCapability.IMAGE_INPUT not in self._models.capabilities(self._task):
                    error = "image_capability_unavailable"
                elif (
                    len(fixed) + len(images) > frames
                    or size + sum(len(image.data_url) for image in images) > limit
                ):
                    error = "media_request_budget_exceeded"
                if error:
                    outcome = json.loads(result)
                    if (
                        isinstance(outcome, dict)
                        and evidence.get(call.id, {}).get("mutation_committed") is True
                    ):
                        result = json.dumps({**outcome, "media_read": False, "media_error": error})
                    else:
                        result = json.dumps({"ok": False, "error": error, "read": False})
            output.append((call, result, executed))
        return tuple(output)

    async def _resume_tool_response(
        self,
        pending: list[Any],
        transcript: TurnTranscript,
        tools: AgentToolBackend | None,
        runtime: AgentRuntime,
        definitions: tuple[ChatTool, ...] | None,
    ) -> None:
        """Finish the original response before another model request is bought."""
        control = runtime.work_control
        assert control is not None and control.session is not None
        session = control.session
        declared = frozenset(tool.name for tool in definitions or ())
        tooling = runtime.runtime_config.tooling
        calls = tuple(
            ToolCall(item["id"], ToolFunction(item["name"], item["arguments"])) for item in pending
        )
        paired = session.paired_call_ids()
        results = []
        facts: dict[str, dict[str, Any]] = {}

        async def recorded(call: ToolCall, key: str) -> str:
            # The original receipt's typed evidence travels with its stored text.
            capture = ResultCapture("", key)
            token = current_result_capture.set(capture)
            try:
                return await session.journal.effect_result(key, original_tool=call.function.name)
            finally:
                current_result_capture.reset(token)
                if capture.evidence is not None:
                    facts[call.id] = capture.evidence

        for item in pending:
            call = ToolCall(item["id"], ToolFunction(item["name"], item["arguments"]))
            key = await session._pending_result_key(item, session.call_key(call.id))
            if call.id in paired:
                result = await recorded(call, key)
            elif "readonly_result_key" not in item and (
                await session.journal.effect_state(key) is None
                or (
                    control.current is not None
                    and await control.repository.undispatched_intent(control.current["id"], key)
                )
            ):
                coordinated = await self._execute_tool_batch(
                    (call,),
                    tools,
                    runtime,
                    max_parallel_calls=tooling.max_parallel_calls if tooling is not None else 1,
                    declared_names=declared,
                    batch_calls=calls,
                )
                result = coordinated.calls[0][1]
                fact = coordinated.evidence.get(call.id)
                if fact is not None:
                    facts[call.id] = fact
                    control.observe_evidence(fact)
            else:
                result = await recorded(call, key)
            results.append((call, result, True))

        results = list(self._budget_tool_media(tuple(results), transcript, runtime, tools, facts))
        for call, result, _executed in results:
            if call.id not in paired:
                transcript.append_result(call.id, result)
        transcript.append_tool_media(tuple((call.id, result) for call, result, _ in results))

        await session.save("paired")

    async def _execute_control_call(
        self,
        call: ToolCall,
        tools: AgentToolBackend | None,
        runtime: AgentRuntime,
        declared_names: frozenset[str],
        key: str,
        invocation: Invocation,
    ) -> tuple[str, bool]:
        """One lifecycle control; shared by direct and recovered calls.

        Arguments are checked against the frozen contract schema inside the
        original effect boundary, so a call that already has a receipt returns
        it without being validated again.
        """
        control = runtime.work_control
        name = call.function.name
        if (
            name not in declared_names
            or control is None
            or tools is None
            or not tools.work_control_allowed(name)
        ):
            return _unexecuted_tool_result(name, "work_control_unavailable"), False
        if name in READ_ONLY_CONTROLS and not tools.work_query_allowed(name):
            return _unexecuted_tool_result(name, "work_query_not_authorized"), False
        assert self.main_contract is not None
        schemas = self.main_contract.schema_view

        async def execute() -> str:
            validation = schemas.validate(name, call.function.arguments)
            if not validation.ok:
                return _unexecuted_tool_result(
                    name, str(validation.error_category), validation.detail
                )
            arguments = json.loads(call.function.arguments)
            # Lifecycle results are tool results: the same budget and artifact
            # preview bound what the model reads and what the receipt replays.
            return await tools.render_result(
                await control.tool_outcome(name, arguments, key, invocation=invocation)
            )

        return await InvocationService().invoke(invocation, execute, side_effecting=True), True

    async def _execute_tool_batch(
        self,
        calls: tuple[ToolCall, ...],
        tools: AgentToolBackend | None,
        runtime: AgentRuntime,
        *,
        max_parallel_calls: int,
        declared_names: frozenset[str],
        chain_id: str = "",
        request_sequence: int = 0,
        batch_calls: tuple[ToolCall, ...] | None = None,
    ) -> CoordinatedToolResult:
        """Dispatch each new call under its own identity and current authorization."""

        control = runtime.work_control
        session = getattr(control, "session", None)
        if session is not None and batch_calls is None:
            session.pending_readonly_keys = {}

        async def save_response() -> None:
            if session is not None:
                await session.save("response", batch_calls if batch_calls is not None else calls)

        if batch_calls is None and BatchPlan.prepare(calls).conflicting_ids:
            await save_response()
            result = json.dumps(
                {"ok": False, "executed": False, "error": "duplicate_provider_call_id"}
            )
            return CoordinatedToolResult(tuple((call, result, False) for call in calls), 0)

        if control is not None and control.accepted_ending() in {"failed", "cancelled"}:
            await save_response()
            return CoordinatedToolResult(
                tuple(
                    (
                        call,
                        _unexecuted_tool_result(call.function.name, "work_control_closed"),
                        False,
                    )
                    for call in calls
                ),
                0,
            )

        if len(calls) > 1 and any(call.function.name in WORK_CONTROL_NAMES for call in calls):
            # Controls are ordered barriers. Keep every original
            # call pending until the complete response is paired. A normal
            # lifecycle decision does not revoke the remaining original calls.
            mixed_calls: list[tuple[ToolCall, str, bool]] = []
            mixed_evidence: dict[str, dict[str, Any]] = {}
            mixed_outcomes: dict[str, ToolExecutionResult] = {}
            mixed_executed = mixed_reused = index = 0
            while index < len(calls):
                end = index + 1
                if calls[index].function.name not in WORK_CONTROL_NAMES:
                    while end < len(calls) and calls[end].function.name not in WORK_CONTROL_NAMES:
                        end += 1
                part_result = await self._execute_tool_batch(
                    calls[index:end],
                    tools,
                    runtime,
                    max_parallel_calls=max_parallel_calls,
                    declared_names=declared_names,
                    chain_id=chain_id,
                    request_sequence=request_sequence,
                    batch_calls=batch_calls if batch_calls is not None else calls,
                )
                mixed_calls.extend(part_result.calls)
                mixed_evidence.update(part_result.evidence)
                mixed_outcomes.update(part_result.outcomes)
                mixed_executed += part_result.executed_count
                mixed_reused += part_result.reused_count
                index = end
            return CoordinatedToolResult(
                tuple(mixed_calls), mixed_executed, mixed_reused, mixed_evidence, mixed_outcomes
            )

        def readonly_control(call: ToolCall) -> bool:
            return call.function.name in READ_ONLY_CONTROLS

        control_calls = [
            call
            for call in calls
            if call.function.name in WORK_CONTROL_NAMES and not readonly_control(call)
        ]
        if control_calls:
            await save_response()
            call = calls[0]
            invocation = direct_invocations(
                (call,),
                runtime,
                chain_id=chain_id,
                request_sequence=request_sequence,
                manifest_revision=runtime.manifest_revision
                or (self.main_contract.revision if self.main_contract is not None else ""),
            )[0]
            capture = ResultCapture("", call.id)
            token = current_result_capture.set(capture)
            try:
                result, executed = await self._execute_control_call(
                    call,
                    tools,
                    runtime,
                    declared_names,
                    runtime.work_control.session.call_key(call.id)
                    if runtime.work_control is not None and runtime.work_control.session
                    else f"{runtime.work_control.lease.owner}:{call.id}"
                    if runtime.work_control is not None
                    else call.id,
                    invocation,
                )
            finally:
                current_result_capture.reset(token)
            fact = captured_evidence(
                capture,
                tool=call.function.name,
                side_effecting=True,
                arguments=call.function.arguments,
            )
            return CoordinatedToolResult(
                calls=((call, result, executed),),
                # Lifecycle controls use the model and message budgets, not
                # the caller's delegated business-tool execution allowance.
                executed_count=0,
                reused_count=0,
                evidence={call.id: fact} if fact is not None else {},
            )

        metadata_results: dict[str, str] = {}
        for call in calls:
            if readonly_control(call):
                metadata_results[call.id], _ = await self._execute_control_call(
                    call,
                    tools,
                    runtime,
                    declared_names,
                    session.call_key(call.id) if session is not None else call.id,
                    direct_invocations(
                        (call,),
                        runtime,
                        chain_id=chain_id,
                        request_sequence=request_sequence,
                        manifest_revision=runtime.manifest_revision
                        or (self.main_contract.revision if self.main_contract is not None else ""),
                    )[0],
                )

        if calls and len(metadata_results) == len(calls):
            await save_response()
            return CoordinatedToolResult(
                tuple((call, metadata_results[call.id], False) for call in calls), 0
            )

        rejected_by_id: dict[str, str] = dict(metadata_results)
        unique_calls: list[ToolCall] = []
        for call in calls:
            if call.id in metadata_results:
                continue
            if call.function.name not in declared_names:
                rejected_by_id[call.id] = _undeclared(call.function.name)
                continue
            unique_calls.append(call)

        if session is not None:
            await session.save("response", batch_calls if batch_calls is not None else calls)
        coordinated = await self._tool_coordinator.execute_batch(
            tuple(unique_calls),
            tools,
            runtime,
            remaining_calls=await control.remaining_tools() if control is not None else None,
            max_parallel_calls=max_parallel_calls,
            chain_id=chain_id,
            request_sequence=request_sequence,
            manifest_revision=runtime.manifest_revision
            or (self.main_contract.revision if self.main_contract else ""),
        )
        unique_results = {call.id: result for call, result, _executed in coordinated.calls}
        unique_executed = {call.id: executed for call, _result, executed in coordinated.calls}

        ordered: list[tuple[ToolCall, str, bool]] = []
        for call in calls:
            if call.id in rejected_by_id:
                ordered.append((call, rejected_by_id[call.id], False))
                continue
            payload = unique_results.get(call.id)
            if payload is None:
                logger.error("tool_result_missing call_id=%s", call.id)
                ordered.append((call, MISSING_TOOL_RESULT, False))
                continue
            ordered.append(
                (
                    call,
                    payload,
                    unique_executed.get(call.id, False),
                )
            )

        return CoordinatedToolResult(
            calls=tuple(ordered),
            executed_count=coordinated.executed_count,
            reused_count=coordinated.reused_count,
            evidence=coordinated.evidence,
            outcomes=coordinated.outcomes,
        )

    @staticmethod
    async def _prepare_tools(tools: AgentToolBackend | None, runtime: AgentRuntime) -> None:
        if tools is None:
            return
        await tools.prepare(runtime)

    @staticmethod
    def _record_failure_usage(
        tools: AgentToolBackend | None,
        *,
        tool_calls: int,
        model_requests: int,
    ) -> None:
        if tools is not None:
            tools.record_failure_usage(tool_calls=tool_calls, model_requests=model_requests)

    @staticmethod
    def _merge_function_tools(
        previous: tuple[ChatTool, ...],
        current: tuple[ChatTool, ...],
    ) -> tuple[ChatTool, ...]:
        merged = {item.name: item for item in previous}
        for item in current:
            # Preserve the submitted declaration and its exact position.
            merged.setdefault(item.name, item)
        return tuple(merged.values())

    @staticmethod
    def _merge_native_tools(
        previous: tuple[NativeToolDefinition, ...],
        current: tuple[NativeToolDefinition, ...],
    ) -> tuple[NativeToolDefinition, ...]:
        merged = {item.type: item for item in previous}
        for item in current:
            merged.setdefault(item.type, item)
        return tuple(merged.values())
