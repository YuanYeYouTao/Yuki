"""Per-activation turn state and the three typed Pi core boundaries."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field, replace
from functools import partial
from typing import Any

from qq_ai_bot.agent_core import (
    RETRY,
    STOP,
    Continue,
    End,
    ToolBatchOutcome,
    ToolCallOutcome,
    TurnDecision,
    run_agent_loop,
)
from qq_ai_bot.agent_core.model_boundary import LoopSignal
from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.capabilities.coordinator import (
    CoordinatedToolResult,
)
from qq_ai_bot.domain.messages import (
    ChatImage,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ChatTool,
    ModelResponseStatus,
    NativeToolDefinition,
    NativeToolEvent,
    PromptRequestDiagnostics,
    ResponseCitation,
)
from qq_ai_bot.llm.base import (
    LLMEmptyResponseError,
    LLMError,
    LLMIncompleteResponseError,
    LLMTimeoutError,
    LLMUnavailableError,
)
from qq_ai_bot.model_runtime.capacity import estimate_request_tokens
from qq_ai_bot.model_runtime.dispatch_guard import model_dispatch_guard
from qq_ai_bot.model_runtime.models import ModelCapability, ModelExecutionPriority
from qq_ai_bot.model_runtime.structured import (
    tool_free_structured_output_mode,
)
from qq_ai_bot.prompting.serializer import serialized_messages_hash
from qq_ai_bot.runtime.execution_receipts import current_receipts
from qq_ai_bot.runtime.work_control import WorkControl, WorkInputsPreparing
from qq_ai_bot.runtime.work_repository import WorkCapacityError
from qq_ai_bot.services.agent_runner import (
    CODE_COMPOSITION_YIELDED,
    AgentRunner,
    AgentRunResult,
    AgentRuntime,
    AgentToolBackend,
    _RequestNotStarted,
)
from qq_ai_bot.services.context_boundary import ContextBoundary
from qq_ai_bot.services.evidence_observation import EVIDENCE_TOOLS, EvidenceObservation
from qq_ai_bot.services.turn_transcript import (
    DispatchOrigin,
    TranscriptRequest,
    TurnTranscript,
    validating_request,
)
from qq_ai_bot.services.work_reporting import (
    append_input_feedback,
    initialize_input_feedback,
    require_interactive_exit,
    stage_feedback_opportunity,
    start_feedback_updates,
)
from qq_ai_bot.web.models import WebMode

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TurnState:
    """Mutable state belonging to one activation and its original Work only."""

    transcript: TurnTranscript
    evidence_observation: EvidenceObservation
    fixed_definitions: tuple[ChatTool, ...] | None = None
    staged_evidence_results: int = 0
    calls_used: int = 0
    web_was_used: bool = False
    empty_retries: int = 0
    mention_recovery_used: bool = False
    answer_recovery_used: bool = False
    native_events: list[NativeToolEvent] = field(default_factory=list)
    citations: list[ResponseCitation] = field(default_factory=list)
    response_status: ModelResponseStatus = ModelResponseStatus.COMPLETED
    incomplete_recovery_used: bool = False
    continuation_tools: tuple[ChatTool, ...] = ()
    continuation_native_tools: tuple[NativeToolDefinition, ...] = ()
    previous_batch_fingerprint: tuple[tuple[str, str, str], ...] | None = None
    repeated_batch_count: int = 0
    no_progress_recovery: bool = False
    reusable_tool_results: dict[tuple[str, str], str] = field(default_factory=dict)
    input_feedback_watermark: int = 0
    stage_feedback_batch: str | None = None
    pending_stage_feedback: str | None = None
    provider_pause_replay: bool = False
    ordinary_compaction_tokens: int = 0
    ordinary_observations: list[dict[str, Any]] = field(default_factory=list)
    ordinary_evidence: list[dict[str, Any]] = field(default_factory=list)
    observed_event_ids: set[int] = field(default_factory=set)
    public_tail: list[ChatMessage] = field(default_factory=list)
    deferred_paid_compaction: bool = False
    control: WorkControl | None = None
    boundary: ContextBoundary | None = None
    paid_staging: bool = False
    exact_dispatch_replay: bool = False
    definitions: tuple[ChatTool, ...] = ()
    response_observation: dict[str, Any] = field(default_factory=dict)
    coordinated: CoordinatedToolResult = field(default_factory=lambda: CoordinatedToolResult((), 0))
    observations: list[dict[str, Any]] = field(default_factory=list)
    opportunity: tuple[str, str] | None = None
    segment_handoff_index: int | None = None
    caller_completion_checked: bool = False


@dataclass(frozen=True, slots=True)
class _PreparedRequest:
    sequence: TranscriptRequest
    request: ChatRequest
    priority: ModelExecutionPriority
    compacting: bool


@dataclass(slots=True)
class _OrdinarySummaryDispatch:
    """One tool-free summary admission; HTTP retries reuse its paid reservation."""

    runner: AgentRunner
    runtime: AgentRuntime
    request: ChatRequest
    request_index: int
    sequence: TranscriptRequest
    control: WorkControl | None
    priority: ModelExecutionPriority
    prepared: bool = field(default=False, init=False)

    async def complete(self) -> ChatResponse:
        with model_dispatch_guard(self.admit):
            return await self.runner._models.execute(
                self.runner._task,
                self.request,
                priority=self.priority,
                canonical_conversation_id=self.runtime.canonical_conversation_id,
            )

    async def admit(self) -> None:
        if self.prepared:
            return
        assert self.runtime.auxiliary_requests is not None
        if (
            self.request_index + self.runtime.auxiliary_requests[0] + 1
            >= self.runtime.max_model_requests
        ):
            raise WorkCapacityError("model_request_budget")
        if self.runtime.before_model_request is not None:
            with validating_request(self.sequence):
                await self.runtime.before_model_request()
        if self.control is not None:
            await self.control.reserve_request(auxiliary=True)
        self.runtime.auxiliary_requests[0] += 1
        self.prepared = True


@dataclass(slots=True)
class _PrimaryDispatch:
    """One admitted model request; retries share its original reservation/CAS."""

    runner: AgentRunner
    runtime: AgentRuntime
    execute: Callable[[], Awaitable[ChatResponse]]
    sequence: TranscriptRequest
    input_feedback_watermark: int
    stage_feedback_batch: str | None
    boundary: ContextBoundary | None
    observed_event_ids: set[int]
    tools: AgentToolBackend | None = None
    selected_media: tuple[ChatImage, ...] = ()
    has_native_effects: bool = False
    prepared: bool = field(default=False, init=False)
    selected_boundary: ContextBoundary | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        # Capacity compaction may replace a candidate with a summary. Only a
        # rendering present in this frozen primary request can be published.
        self.selected_boundary = (
            self.boundary
            if self.boundary is not None
            and all(
                message in (*self.sequence.messages, *self.sequence.items)
                for _, message in self.boundary.fragments
            )
            else None
        )

    async def complete(self) -> ChatResponse:
        with model_dispatch_guard(self.admit):
            return await self.execute()

    async def admit(self) -> None:
        if self.prepared:
            return
        self.runner._check_request_media_budget(self.selected_media, self.runtime, self.tools)
        await self.runner._validate_tool_media(
            self.tools, self.runtime, self.sequence, self.selected_media
        )
        # The executor invokes this only after real admission.
        # HTTP retries must not reserve this logical request again.
        if self.runtime.before_model_request is not None:
            try:
                with validating_request(self.sequence):
                    await self.runtime.before_model_request()
            except LLMError as exc:
                raise _RequestNotStarted(exc) from exc
        if self.runtime.work_control is not None:
            await self.runtime.work_control.reserve_request()
            candidate = None
            work_session = self.runtime.work_control.session
            prior_event_ids = None
            if self.selected_boundary is not None:
                if (
                    work_session is not None
                    and self.runtime.work_control.current is not None
                    and self.selected_boundary.prepare is not None
                ):
                    candidate = await self.selected_boundary.prepare()
                    candidate.stage()
                    prior_event_ids = list(work_session.event_ids)
                    work_session.event_ids.extend(
                        sorted(self.selected_boundary.event_ids.difference(work_session.event_ids))
                    )
                else:
                    await self.selected_boundary.commit()
            communication_updates: dict[str, Any] = {}
            communication = self.runtime.work_control.communication
            if self.input_feedback_watermark > communication.get("input_feedback_through_id", 0):
                communication_updates["input_feedback_through_id"] = self.input_feedback_watermark
            if self.stage_feedback_batch and self.stage_feedback_batch != communication.get(
                "stage_feedback_batch"
            ):
                communication_updates["stage_feedback_batch"] = self.stage_feedback_batch
            if work_session is not None:
                try:
                    await work_session.save(
                        "dispatched",
                        communication_updates=communication_updates,
                        publication=candidate.publication if candidate is not None else None,
                    )
                except BaseException:
                    if candidate is not None:
                        candidate.rollback()
                        assert prior_event_ids is not None
                        work_session.event_ids[:] = prior_event_ids
                    raise
                if candidate is not None:
                    candidate.finalize()
            elif communication_updates:
                await self.runtime.work_control.patch_communication(**communication_updates)
            if self.selected_boundary is not None:
                self.observed_event_ids.update(self.selected_boundary.event_ids)
                if work_session is not None:
                    work_session.public_event_ids.update(self.selected_boundary.event_ids)
        elif self.selected_boundary is not None:
            await self.selected_boundary.commit()
            self.observed_event_ids.update(self.selected_boundary.event_ids)
        if self.has_native_effects:
            # Mark the original private task before real native dispatch. SDK
            # backends without an ordinary private token have no protection hook.
            protect_native = getattr(self.tools, "protect_native_dispatch", None)
            if callable(protect_native):
                await protect_native(self.runtime)
        self.prepared = True


class TurnExecution:
    """Typed model, invocation and settlement boundaries for one core activation."""

    def __init__(
        self,
        runner: AgentRunner,
        initial_messages: tuple[ChatMessage, ...],
        runtime: AgentRuntime,
        tools: AgentToolBackend | None,
    ) -> None:
        self.runner = runner
        self.initial_messages = initial_messages
        self.runtime = runtime
        self.tools = tools
        self.state = TurnState(
            transcript=TurnTranscript(initial_messages),
            evidence_observation=EvidenceObservation(runtime.origin.value),
            observed_event_ids=set(runtime.visible_event_ids),
            control=runtime.work_control,
        )

    async def activate(self) -> AgentRunResult:
        if self.runtime.fixed_tools is not None:
            self.state.fixed_definitions = self.runtime.fixed_tools
        elif self.runner.main_contract is not None:
            self.state.fixed_definitions = await self.runner.main_contract.definitions()
            if not self.runtime.dynamic_context_prepared:
                raise LLMError("main_agent_composition_required")
            if self.tools is None:
                raise LLMError("main_agent_executor_required")
        await self.runner._prepare_tools(self.tools, self.runtime)
        if self.runtime.work_control is not None:
            from qq_ai_bot.runtime.work_session import WorkSession

            contract = self.runner.work_contract(
                self.runtime.runtime_config, self.initial_messages, self.state.fixed_definitions
            )
            self.runtime.work_control.session = WorkSession(self.runtime.work_control, contract)
            self.state.transcript = await self.runtime.work_control.session.restore(
                self.state.transcript,
                compaction_brief=self.runtime.compaction_brief,
                visible_event_ids=self.runtime.visible_event_ids,
            )
            pending_code = self.runtime.work_control.session.pending_compositions
            if pending_code:
                # The original owner continues the same composition before any
                # generic pairing or new model request (P02 restore split).
                if await self.runner._resume_compositions(
                    pending_code,
                    self.state.transcript,
                    self.tools,
                    self.runtime,
                    self.state.fixed_definitions,
                ):
                    return AgentRunResult(
                        text="",
                        tool_calls_used=0,
                        model_requests=0,
                        web_was_used=False,
                        suppress_delivery=True,
                        work_state="queued",
                    )
            self.state.repeated_batch_count = int(
                self.runtime.work_control.session.progress.get("repeats", 0)
            )
            self.state.provider_pause_replay = bool(
                self.runtime.work_control.session.progress.get("provider_pause_replay", False)
            )
            await initialize_input_feedback(self.runtime.work_control)
            self.state.observations = self.runtime.work_control.session.progress.get(
                "model_observations", []
            )
            if self.state.observations:
                self.state.opportunity = await stage_feedback_opportunity(
                    self.runtime.work_control, self.state.observations[-1]
                )
                if self.state.opportunity is not None:
                    self.state.stage_feedback_batch, self.state.pending_stage_feedback = (
                        self.state.opportunity
                    )
            if self.runtime.work_control.handoff_work_id is not None:
                await self.runtime.work_control.session.save("paired")
            if (
                self.runtime.work_control.session.recovered_delivery
                or self.runtime.work_control.handoff_work_id is not None
            ):
                return AgentRunResult(
                    text="",
                    tool_calls_used=0,
                    model_requests=0,
                    web_was_used=False,
                    suppress_delivery=True,
                )

        result = await run_agent_loop(
            max_requests=self.runtime.max_model_requests,
            model=self,
            invocation=self,
            settlement=self,
        )
        assert isinstance(result, AgentRunResult)
        return result

    async def revalidate_caller_completion(self) -> bool:
        control = self.runtime.work_control
        if (
            control is None
            or control.current is None
            or control.ending != "completed"
            or control.source.get("delivery_contract") != "return_to_caller"
        ):
            return False
        if await control.pending():
            control.ending = None
            if control.session is not None:
                control.session.progress.pop("caller_completion_pending_result", None)
            return False
        proposed = (
            control.session.progress.get("caller_completion_pending_result", {})
            if control.session is not None
            else {}
        )
        receipt = await control.execute(
            "task_control",
            {
                "action": "complete",
                "artifact_ids": proposed.get("artifact_ids", [])
                if isinstance(proposed, dict)
                else [],
            },
            "caller-result-revalidate",
        )
        if json.loads(receipt).get("ok") is not True:
            control.ending = None
            if control.session is not None:
                control.session.progress.pop("caller_completion_pending_result", None)
            return False
        return True

    async def caller_has_confirmed_delivery(self) -> bool:
        if not await self.revalidate_caller_completion():
            return False
        control = self.runtime.work_control
        assert control is not None
        # A fresh backend has no turn-local send count. Only an original
        # confirmed receipt permits an empty internal result.
        return any(
            fact.get("ok") is True
            and fact.get("delivered_message") is True
            and not fact.get("pending")
            and not fact.get("uncertain")
            for fact in await control.effect_evidence()
        )

    async def take_boundary_inputs(
        self, request_index: int, boundary: ContextBoundary | None
    ) -> AgentRunResult | None:

        control = self.runtime.work_control
        assert control is not None
        try:
            added = await control.take_inputs(
                f"{self.state.transcript.chain_id}:{request_index}",
                observed_event_ids=boundary.event_ids if boundary is not None else frozenset(),
            )
        except WorkInputsPreparing:
            control.ending = "waiting_external"
            return AgentRunResult(
                text="",
                suppress_delivery=True,
                work_state="waiting_external",
                tool_calls_used=self.state.calls_used,
                model_requests=request_index,
                web_was_used=self.state.web_was_used,
            )
        # Queued Work input may be prepared by a newer ingress catalog
        # while this activation is still pinned to the old provider.
        if ModelCapability.IMAGE_INPUT not in self.runner._models.capabilities(self.runner._task):
            added = tuple(
                replace(
                    message,
                    images=(),
                    content=(message.content or "")
                    + "\n[本次输入的图片或视频帧未读取：当前模型连接不支持图片输入。]",
                )
                if message.images
                else message
                for message in added
            )
        for message in added:
            self.state.transcript.append(message)
        if control.session is not None:
            proposed = control.session.progress.get("caller_completion_pending_result")
            if added:
                control.session.progress.pop("caller_completion_pending_result", None)
            elif (
                not self.state.caller_completion_checked
                and isinstance(proposed, dict)
                and proposed.get("action") == "complete"
                and control.source.get("delivery_contract") == "return_to_caller"
                and control.ending is None
            ):
                # The saved proposal is evidence, never completion authority.
                receipt = await control.execute(
                    "task_control",
                    {"action": "complete", "artifact_ids": proposed.get("artifact_ids", [])},
                    "caller-completion-revalidate",
                )
                if json.loads(receipt).get("ok") is True:
                    self.state.transcript.append(
                        ChatMessage(
                            role="system",
                            content=(
                                "原 Work 的完成条件已按当前来源与真实回执重新核验；"
                                "现在仅返回调用方需要的内部结果，不重复已完成的操作。"
                            ),
                        )
                    )
                else:
                    control.session.progress.pop("caller_completion_pending_result", None)
            self.state.caller_completion_checked = True
        self.state.input_feedback_watermark = await append_input_feedback(
            control,
            self.state.transcript,
            self.state.input_feedback_watermark,
            extra_feedback=self.state.pending_stage_feedback,
        )
        self.state.pending_stage_feedback = None
        return None

    async def begin(self, request_index: int) -> LoopSignal | None:

        if (
            self.runtime.auxiliary_requests
            and request_index + self.runtime.auxiliary_requests[0]
            >= self.runtime.max_model_requests
        ):
            return STOP
        self.state.control = self.runtime.work_control
        self.state.boundary = None
        if (
            request_index > 0
            and self.runtime.observation_boundary is not None
            and not self.state.provider_pause_replay
            and not (
                self.state.control is not None
                and self.state.control.current is not None
                and (
                    self.state.control.lease.work_id
                    or (
                        self.state.control.session
                        and self.state.control.session.uses_recovery_transcript
                    )
                )
            )
        ):
            known = self.state.observed_event_ids | (
                self.state.control.session.public_event_ids
                if self.state.control and self.state.control.session
                else set()
            )
            self.state.boundary = await self.runtime.observation_boundary(frozenset(known))
            if self.state.boundary is not None:
                for _, message in self.state.boundary.fragments:
                    self.state.transcript.append(message)
                    if message not in self.state.public_tail:
                        self.state.public_tail.append(message)
        if (
            self.state.control is not None
            and self.state.control.current is not None
            and self.state.control.requests_started >= self.runtime.max_model_requests
        ):
            return STOP
        if (
            self.state.control is not None
            and self.state.control.current is not None
            and self.runtime.max_tool_calls > 0
            and self.state.control.tools_started >= self.runtime.max_tool_calls
            and not self.state.provider_pause_replay
        ):
            # Keep the current working data for one final model dispatch. The
            # same fixed declarations and zero remaining business allowance
            # still apply; lifecycle/note calls never recharge that allowance.
            if self.state.segment_handoff_index is not None:
                return STOP
            self.state.segment_handoff_index = request_index
            self.state.transcript.append(
                ChatMessage(
                    "user",
                    json.dumps(
                        {
                            "kind": "work_segment_handoff",
                            "remaining_business_calls": 0,
                            "instruction": (
                                "This activation has exhausted its business tool allowance. "
                                "You have one model request before the working transcript retires. "
                                "If the goal is verified, call task_control(action='complete'). "
                                "Otherwise call task_control(action='update', context_note=...) "
                                "alone. Save cumulative findings, necessary intermediate values, "
                                "completed "
                                "steps and the next step, merging any previous context_note. Use "
                                "version=1, facts/unresolved/next_steps with text and valid refs. "
                                "No further business tool call can execute in this activation."
                            ),
                        },
                        ensure_ascii=False,
                    ),
                )
            )
        self.state.paid_staging = bool(
            self.state.control
            and self.state.control.session
            and self.state.control.session.progress.get("compaction_staging")
        )
        self.state.exact_dispatch_replay = bool(
            request_index == 0
            and self.state.control
            and self.state.control.session
            and self.state.control.session.recovered_phase == "dispatched"
        )
        return None

    async def steer(self, request_index: int) -> End | None:
        if (
            self.state.control is not None
            and not self.state.provider_pause_replay
            and not self.state.paid_staging
            and not self.state.exact_dispatch_replay
        ):
            waiting = await self.take_boundary_inputs(request_index, self.state.boundary)
            if waiting is not None:
                return End(waiting)
        return None

    async def request(self, request_index: int) -> ChatResponse | End | LoopSignal:

        if self.state.fixed_definitions is not None:
            if self.tools is not None:
                self.tools.refresh_catalog(self.runtime, web_was_used=self.state.web_was_used)
            self.state.definitions = self.state.fixed_definitions
        else:
            self.state.definitions = (
                self.tools.definitions(self.runtime, web_was_used=self.state.web_was_used)
                if self.tools is not None
                else ()
            )
        web_config = self.runtime.runtime_config.web
        try:
            web_mode = WebMode(web_config.mode)
        except ValueError:
            web_mode = WebMode.DISABLED
        self.state.definitions, native_definitions = self.runner.prepare_request_tools(
            self.state.definitions,
            runtime_config=self.runtime.runtime_config,
            allowed_capabilities=self.runtime.allowed_capabilities,
            web_was_used=self.state.web_was_used,
        )
        if self.tools is not None:
            # Discovery/execution policy cannot discard a submitted request prefix.
            self.tools.consume_provider_chain_restart()
        if self.state.transcript.continuation is not None:
            # Responses continuations are one cumulative request chain.
            # Keep previously declared tools paired with their function outputs.
            # The Main Agent manifest is fixed for the submitted chain.
            self.state.definitions = self.runner._merge_function_tools(
                self.state.continuation_tools, self.state.definitions
            )
            native_definitions = self.runner._merge_native_tools(
                self.state.continuation_native_tools, native_definitions
            )
        try:
            candidate = await self.prepare_request(request_index, native_definitions, web_mode)
            if isinstance(candidate, (End, LoopSignal)):
                return candidate
            execute = (
                partial(
                    self.runner._models.execute,
                    self.runner._task,
                    candidate.request,
                    priority=candidate.priority,
                    canonical_conversation_id=self.runtime.canonical_conversation_id,
                )
                if self.runtime.canonical_conversation_id is not None
                else partial(
                    self.runner._models.execute,
                    self.runner._task,
                    candidate.request,
                    priority=candidate.priority,
                )
            )

            dispatch = _PrimaryDispatch(
                runner=self.runner,
                selected_media=tuple(
                    image
                    for message in self.state.transcript.portable_entries()
                    if isinstance(message, ChatMessage)
                    for image in message.images
                ),
                runtime=self.runtime,
                execute=execute,
                sequence=candidate.sequence,
                input_feedback_watermark=self.state.input_feedback_watermark,
                stage_feedback_batch=self.state.stage_feedback_batch,
                boundary=self.state.boundary,
                observed_event_ids=self.state.observed_event_ids,
                tools=self.tools,
                has_native_effects=bool(candidate.request.native_tools),
            )

            response = await self.runner._concurrency.run_llm(
                self.runtime.conversation_key,
                dispatch.complete,
            )
            if self.runtime.work_control is not None:
                await self.runtime.work_control.confirm_inputs()
            receipts = current_receipts.get()
            if receipts is not None:
                await receipts.confirm()
            # A prepared request may be cancelled while waiting for the LLM
            # slot or rejected by the transport budget before dispatch.
            # Confirm conservatively only after a response was received.
            if self.tools is not None:
                try:
                    await self.tools.confirm_memory_prompt_exposure()
                except Exception as exc:
                    self.state.evidence_observation.emit(
                        "exposure_confirmation_failed", category=type(exc).__name__
                    )
            self.state.evidence_observation.emit(
                "response_received",
                request_index=request_index + 1,
                confirmed_prior_results=self.state.staged_evidence_results,
                native_completed=sum(
                    event.status.value == "completed" for event in response.native_tool_events
                ),
                native_failed=sum(
                    event.status.value == "failed" for event in response.native_tool_events
                ),
                source_count=len(response.citations),
            )
            self.state.staged_evidence_results = 0
        except _RequestNotStarted as exc:
            self.runner._record_failure_usage(
                self.tools, tool_calls=self.state.calls_used, model_requests=request_index
            )
            raise exc.cause from exc
        except (LLMTimeoutError, LLMUnavailableError):
            self.runner._record_failure_usage(
                self.tools, tool_calls=self.state.calls_used, model_requests=request_index + 1
            )
            raise
        except LLMEmptyResponseError:
            if (
                self.state.control is not None
                and self.state.control.session is not None
                and self.state.control.source.get("delivery_contract") == "return_to_caller"
                and "caller_completion_pending_result" in self.state.control.session.progress
                and not await self.revalidate_caller_completion()
            ):
                self.state.control.ending = None
                self.state.control.session.progress.pop("caller_completion_pending_result", None)
                await self.state.control.session.save("paired")
                return RETRY
            has_visible_effects = bool(self.tools is not None and self.tools.has_visible_effects())
            if not has_visible_effects:
                has_visible_effects = await self.caller_has_confirmed_delivery()
            if has_visible_effects and (
                self.state.control is None
                or self.state.control.current is None
                or self.state.control.ending == "completed"
            ):
                return End(
                    AgentRunResult(
                        text="",
                        tool_calls_used=self.state.calls_used,
                        model_requests=request_index + 1,
                        web_was_used=self.state.web_was_used,
                        native_tool_events=tuple(self.state.native_events),
                        citations=tuple(self.state.citations),
                        response_status=self.state.response_status,
                    )
                )
            if (
                self.state.empty_retries >= 2
                or request_index + 1 >= self.runtime.max_model_requests
            ):
                self.runner._record_failure_usage(
                    self.tools, tool_calls=self.state.calls_used, model_requests=request_index + 1
                )
                raise
            self.state.empty_retries += 1
            logger.warning(
                "agent_empty_response_retry retry=%d tool_calls_used=%d",
                self.state.empty_retries,
                self.state.calls_used,
            )
            self.state.transcript.append(
                ChatMessage(
                    role="system",
                    content=(
                        "上一次模型请求返回了空内容。请继续当前同一轮任务：如果已有工具"
                        "结果，先核对结果再给出简短、真实的最终答复；如果任务尚未完成，"
                        "继续调用必要工具。不得声称未成功的操作已经完成。"
                    ),
                )
            )
            return RETRY
        except LLMError:
            self.runner._record_failure_usage(
                self.tools, tool_calls=self.state.calls_used, model_requests=request_index + 1
            )
            raise
        return await self.observe_response(
            request_index, response, candidate.request.native_tools, compacting=candidate.compacting
        )

    async def prepare_request(
        self,
        request_index: int,
        native_definitions: tuple[NativeToolDefinition, ...],
        web_mode: WebMode,
    ) -> _PreparedRequest | End | LoopSignal:
        """Prepare capacity/compaction before the original request admission."""
        compacting = False
        diagnostics = self.runtime.prompt_diagnostics
        sequence = self.state.transcript.request()
        if self.state.control is not None and self.state.control.session is not None:
            sequence = replace(
                sequence,
                origin=(
                    DispatchOrigin.WORK_RECOVERY
                    if self.state.control.session.uses_recovery_transcript
                    else DispatchOrigin.COMPOSED_INITIAL
                ),
            )
            if self.state.control.session.uses_recovery_transcript:
                guard = self.state.control.session.source_guard
                diagnostics = PromptRequestDiagnostics(
                    conversation_prefix_hash=serialized_messages_hash(sequence.messages),
                    prompt_snapshot_fingerprint=hashlib.sha256(
                        json.dumps(
                            guard.snapshot()
                            if guard is not None
                            else {
                                "conversation_id": self.state.control.lease.conversation_id,
                                "source_revision": self.state.control.session.source_revision,
                            },
                            sort_keys=True,
                            default=str,
                        ).encode()
                    ).hexdigest(),
                    static_prompt_revision=hashlib.sha256(
                        "\n\n".join(
                            m.content or "" for m in sequence.messages if m.role == "system"
                        ).encode()
                    ).hexdigest(),
                )
        request = ChatRequest(
            messages=sequence.messages,
            request_chain_id=self.state.transcript.chain_id,
            continuation_items=sequence.items,
            model=self.runtime.runtime_config.llm.model or "fake",
            temperature=self.runtime.runtime_config.llm.temperature,
            max_output_tokens=self.runtime.runtime_config.llm.max_output_tokens,
            thinking_enabled=self.runtime.runtime_config.llm.thinking_enabled,
            tools=self.state.definitions,
            # Recovery and compaction keep the submitted declaration/settings.
            # Their local response fences, not provider tool_choice support,
            # prevent local function execution in those phases. Native
            # tools, where supported, execute at the provider boundary.
            tool_choice="auto" if self.state.definitions or native_definitions else None,
            native_tools=native_definitions,
            continuation=sequence.continuation,
            conversation_prefix_hash=(diagnostics.conversation_prefix_hash if diagnostics else ""),
            prompt_snapshot_fingerprint=(
                diagnostics.prompt_snapshot_fingerprint if diagnostics else ""
            ),
            static_prompt_revision=(diagnostics.static_prompt_revision if diagnostics else ""),
        )
        self.state.evidence_observation.request(
            request_index + 1,
            self.state.definitions,
            native_definitions,
            finalization=False,
            web_mode=web_mode.value,
        )
        priority = (
            ModelExecutionPriority.BACKGROUND
            if self.runtime.origin
            in {
                TurnOrigin.SCHEDULED_AUTOMATION,
                TurnOrigin.PLUGIN_BACKGROUND,
                TurnOrigin.AUTONOMOUS_GROUP,
                TurnOrigin.SELF_INITIATIVE,
                TurnOrigin.SYSTEM_TASK,
            }
            or bool(self.runtime.work_control and self.runtime.work_control.lease.work_id)
            else ModelExecutionPriority.FOREGROUND
        )
        capacity = self.runner._models.capacity(self.runner._task)
        context = self.runtime.runtime_config.context
        input_budget = capacity.input_budget(
            context.work_window_tokens
            if self.state.control and self.state.control.current
            else context.window_tokens,
            output_tokens=request.max_output_tokens,
        )
        predicted_tokens = estimate_request_tokens(self.runner._capacity_request(request))
        maintenance_budget = min(input_budget, context.compaction_window_tokens)
        compaction_threshold = maintenance_budget * context.work_compaction_trigger_ratio
        if self.state.control is not None and self.state.control.session is not None:
            compacted_tokens = self.state.control.session.progress.get(
                "compaction_request_tokens", 0
            )
            if isinstance(compacted_tokens, int) and compacted_tokens > 0:
                # A compacted request may legitimately sit above the soft
                # target. Wait for growth into its remaining headroom,
                # rather than summarizing the same checkpoint again.
                compaction_threshold = max(
                    compaction_threshold,
                    compacted_tokens
                    + max(
                        1,
                        maintenance_budget * (1 - context.work_compaction_trigger_ratio),
                        (maintenance_budget - compacted_tokens)
                        * context.work_compaction_trigger_ratio,
                    ),
                )
        if (
            self.state.control is not None
            and self.state.control.session is not None
            and self.state.control.current is not None
            and self.state.control.ending is None
            and not self.state.exact_dispatch_replay
            and not self.state.deferred_paid_compaction
            and (
                self.state.paid_staging
                or predicted_tokens >= compaction_threshold
                or predicted_tokens > input_budget
            )
        ):
            try:
                self.state.transcript = await self.runner._compact_work(
                    self.runtime,
                    priority,
                    input_budget,
                    request,
                    retained_public=tuple(self.state.public_tail),
                )
            except (WorkCapacityError, LLMError) as exc:
                candidate_failure = isinstance(exc, LLMError) or str(exc) in {
                    "work_compaction_source_capacity",
                    "work_compaction_no_capacity_improvement",
                    "work_compaction_incomplete",
                    "work_compaction_invalid_structure",
                    "work_compaction_invalid_reference",
                    "work_compaction_invalid_directive_source",
                    "work_compaction_invalid_correction",
                    "work_compaction_invalid_input_disposition",
                    "work_compaction_missing_directive",
                    "work_compaction_missing_input",
                }
                if predicted_tokens > input_budget or not candidate_failure:
                    raise
                # A soft maintenance target cannot prohibit a complete
                # request that still fits. The failed candidate preserves
                # the original transcript and paired receipts.
                self.state.control.session.progress["compaction_request_tokens"] = predicted_tokens
                self.state.deferred_paid_compaction = self.state.paid_staging
                logger.info("work_compaction_deferred category=%s", type(exc).__name__)
            else:
                # The paid source is now safely paired. New inputs can
                # enter this new request without invalidating its cursor.
                if self.state.control.requests_started >= self.runtime.max_model_requests:
                    return STOP
                if self.state.paid_staging and not self.state.provider_pause_replay:
                    waiting = await self.take_boundary_inputs(request_index, self.state.boundary)
                    if waiting is not None:
                        return End(waiting)
                # Keep this prepared public delta through the explicit
                # private-tail replacement; dispatch it once below.
                sequence = self.state.transcript.request()
                request = replace(
                    request,
                    messages=sequence.messages,
                    request_chain_id=self.state.transcript.chain_id,
                    continuation=sequence.continuation,
                    continuation_items=sequence.items,
                    continuation_messages=(),
                    function_outputs=(),
                )
                predicted_tokens = estimate_request_tokens(self.runner._capacity_request(request))
                self.state.continuation_tools = ()
                self.state.continuation_native_tools = ()
        ordinary_threshold = max(
            maintenance_budget * context.compaction_trigger_ratio,
            estimate_request_tokens(
                self.runner._capacity_request(
                    replace(
                        request,
                        messages=self.initial_messages,
                        continuation=None,
                        continuation_items=(),
                        continuation_messages=(),
                        function_outputs=(),
                    )
                )
            )
            + maintenance_budget * (1 - context.compaction_trigger_ratio),
            self.state.ordinary_compaction_tokens
            + maintenance_budget * (1 - context.compaction_trigger_ratio),
        )
        ordinary_maintenance = (
            (self.state.control is None or self.state.control.current is None)
            and not self.state.provider_pause_replay
            and len(self.state.transcript.portable_entries()) > len(self.initial_messages)
            and predicted_tokens >= ordinary_threshold
        )
        if predicted_tokens > input_budget or ordinary_maintenance:
            if (
                (self.state.control is None or self.state.control.current is None)
                and not self.state.provider_pause_replay
                and len(self.state.transcript.portable_entries()) > len(self.initial_messages)
            ):
                from qq_ai_bot.services.ordinary_compaction import compact_ordinary

                async def summarize(candidate: ChatRequest) -> ChatResponse:
                    dispatch = _OrdinarySummaryDispatch(
                        self.runner,
                        self.runtime,
                        candidate,
                        request_index,
                        sequence,
                        self.state.control,
                        priority,
                    )
                    return await self.runner._concurrency.run_llm(
                        self.runtime.conversation_key, dispatch.complete
                    )

                try:
                    compacted = await compact_ordinary(
                        self.initial_messages,
                        self.state.transcript,
                        main_request=request,
                        structured_mode=tool_free_structured_output_mode(
                            self.runner._models, self.runner._task
                        ),
                        summary_budget=capacity.input_budget(
                            context.window_tokens,
                            output_tokens=context.compaction_output_tokens,
                        ),
                        input_budget=input_budget,
                        output_tokens=context.compaction_output_tokens,
                        prepare=self.runner._capacity_request,
                        execute=summarize,
                        evidence=self.state.ordinary_evidence,
                        model_observations=self.state.ordinary_observations,
                        retained_public=tuple(self.state.public_tail),
                    )
                except (WorkCapacityError, LLMError):
                    if predicted_tokens > input_budget:
                        raise
                    logger.info("ordinary_compaction_deferred_with_available_capacity")
                    compacted = self.state.transcript
                if compacted is not self.state.transcript:
                    self.state.ordinary_observations.clear()
                self.state.transcript = compacted
                self.state.ordinary_compaction_tokens = estimate_request_tokens(
                    self.runner._capacity_request(
                        replace(
                            request,
                            messages=self.state.transcript.request().messages,
                            continuation=self.state.transcript.continuation,
                            continuation_items=self.state.transcript.request().items,
                        )
                    )
                )
                if self.state.control is not None and self.state.control.session is not None:
                    self.state.control.session.transcript = self.state.transcript
                sequence = self.state.transcript.request()
                request = replace(
                    request,
                    messages=sequence.messages,
                    request_chain_id=self.state.transcript.chain_id,
                    continuation=sequence.continuation,
                    continuation_items=sequence.items,
                    continuation_messages=(),
                    function_outputs=(),
                )
                self.state.continuation_tools = ()
                self.state.continuation_native_tools = ()
            else:
                raise WorkCapacityError("model_request_capacity")
        return _PreparedRequest(sequence, request, priority, compacting)

    async def observe_response(
        self,
        request_index: int,
        response: ChatResponse,
        native_definitions: tuple[NativeToolDefinition, ...],
        *,
        compacting: bool,
    ) -> ChatResponse:
        """Record the response and original private/provider continuation once."""
        self.state.native_events.extend(response.native_tool_events)
        self.state.citations.extend(response.citations)
        if (
            self.state.control is not None
            and self.state.control.session is not None
            and response.citations
        ):
            self.state.control.session.record_search_sources(
                [(item.url, item.title) for item in response.citations]
            )
        self.state.response_status = response.status
        if response.native_tool_events:
            self.state.web_was_used = True
            if self.tools is not None:
                self.tools.mark_native_web_used()
        if self.tools is not None:
            await self.tools.observe_response(response, self.runtime)
        if response.continuation is not None:
            self.state.transcript.accept(response.continuation)
        self.state.provider_pause_replay = response.incomplete_reason == "pause_turn"
        self.state.response_observation = {
            "sequence": request_index + 1,
            "content": response.content,
            "tool_calls": [asdict(call) for call in response.tool_calls],
            "citations": [asdict(item) for item in response.citations],
            "native_tool_events": [asdict(item) for item in response.native_tool_events],
            "status": response.status.value,
        }
        self.state.ordinary_observations.append(self.state.response_observation)
        if self.state.control is not None and self.state.control.session is not None:
            if self.state.provider_pause_replay:
                self.state.control.session.progress["provider_pause_replay"] = True
            else:
                self.state.control.session.progress.pop("provider_pause_replay", None)
            if self.state.control.lease.work_id:
                last_tokens = self.state.control.session.progress.get("context_tokens", 0)
                samples = self.state.control.session.progress.setdefault("cache_samples", [])
                samples.append(
                    {
                        "sequence": self.state.control.session.sequence,
                        "chain_id": self.state.transcript.chain_id,
                        "kind": "compaction"
                        if compacting
                        else ("resume" if request_index == 0 and last_tokens else "execution"),
                        "input": response.prompt_tokens,
                        "cached": response.cached_prompt_tokens,
                        "warm_candidate": bool(
                            response.prompt_tokens and last_tokens >= response.prompt_tokens * 0.95
                        ),
                    }
                )
                del samples[:-32]
            if response.prompt_tokens is not None:
                self.state.control.session.progress["context_tokens"] = response.prompt_tokens
            self.state.observations = self.state.control.session.progress.setdefault(
                "model_observations", []
            )
            self.state.response_observation["sequence"] = self.state.control.session.sequence
            self.state.observations.append(self.state.response_observation)
            self.state.continuation_tools = self.state.definitions
            self.state.continuation_native_tools = native_definitions
        return response

    async def settle_truncated(
        self,
        request_index: int,
        response: ChatResponse,
        outcomes: tuple[ToolCallOutcome, ...],
    ) -> TurnDecision:

        # Truncated calls never execute. Pair non-execution receipts before
        # recovery so either protocol retains a valid, append-only history.
        if response.continuation is None:
            self.state.transcript.append(
                ChatMessage(
                    role="assistant",
                    content=response.content or None,
                    tool_calls=response.tool_calls,
                    reasoning_content=response.reasoning_content,
                )
            )
        # Pi failToolCallsFromTruncatedMessage: the core produced these
        # non-execution receipts; nothing was dispatched.
        for outcome in outcomes:
            self.state.transcript.append_result(outcome.call.id, outcome.result)
        if self.state.control is not None and self.state.control.session is not None:
            await self.state.control.session.save("paired")
        if (
            self.state.incomplete_recovery_used
            or request_index + 1 >= self.runtime.max_model_requests
        ):
            raise LLMIncompleteResponseError(
                "provider response remained incomplete after bounded recovery"
            )
        self.state.incomplete_recovery_used = True
        if response.incomplete_reason == "pause_turn":
            if response.continuation is None:
                raise LLMIncompleteResponseError(
                    "paused provider response has no resumable checkpoint"
                )
            # Claude's paused server tool must be echoed unchanged.
            # A synthetic user/system message would change that replay.
        else:
            self.state.transcript.append(
                ChatMessage(
                    role="system",
                    content=(
                        "上一响应未完整结束。根据真实回执继续原任务，必要时查询或解释；"
                        "不要重复任何已经完成的原生搜索或本地工具调用。"
                    ),
                )
            )
        logger.warning(
            "agent_incomplete_response_recovery reason=%s",
            response.incomplete_reason or "unknown",
        )
        return Continue()

    async def settle_final(self, request_index: int, response: ChatResponse) -> TurnDecision:

        content = response.content
        assistant_recorded = False
        assistant_message = ChatMessage(
            role="assistant",
            content=response.content,
            reasoning_content=response.reasoning_content,
        )
        self.state.control = self.runtime.work_control
        if (
            self.state.deferred_paid_compaction
            and self.state.control is not None
            and self.state.control.session is not None
        ):
            if response.continuation is None:
                self.state.transcript.append(assistant_message)
                assistant_recorded = True
            await self.state.control.session.retire_paid_compaction()
            self.state.deferred_paid_compaction = False
        if self.state.control is not None and await self.state.control.pending():
            if response.continuation is None and not assistant_recorded:
                self.state.transcript.append(assistant_message)
            self.state.transcript.append(
                ChatMessage(
                    role="system",
                    content=("上一段回复尚未发送；有新的用户输入到达，请先处理新增内容再继续。"),
                )
            )
            return Continue()
        if (
            self.state.control is not None
            and self.state.control.session is not None
            and self.state.control.ending == "completed"
            and self.state.control.source.get("delivery_contract") == "return_to_caller"
            and "caller_completion_pending_result" in self.state.control.session.progress
            and not await self.revalidate_caller_completion()
        ):
            if response.continuation is None and not assistant_recorded:
                self.state.transcript.append(assistant_message)
            self.state.transcript.append(
                ChatMessage(
                    role="system",
                    content=(
                        "原完成条件已变化或原执行回执仍未决，不能宣告完成。"
                        "保留已有结果，按真实来源与回执接续，不重复已完成操作。"
                    ),
                )
            )
            await self.state.control.session.save("paired")
            return Continue()
        # The final body is internal; the main backend can reject an
        # unsent user-facing answer without implicitly delivering it.
        if "[提及" in content:
            if (
                not self.state.mention_recovery_used
                and request_index + 1 < self.runtime.max_model_requests
                and any(tool.name == "send_message" for tool in self.state.definitions)
            ):
                self.state.mention_recovery_used = True
                if response.continuation is None and not assistant_recorded:
                    self.state.transcript.append(assistant_message)
                self.state.transcript.append(
                    ChatMessage(
                        role="system",
                        content=(
                            "上一回复含 [提及…] 历史占位标记，已拦截且未发送；"
                            "该占位标记不是发送回执。若用户要求提醒成员，"
                            "先明确人物，再用 send_message.mentions 发送；"
                            "普通正文和 @名字都不能触发提醒。无法执行时如实说明。"
                        ),
                    )
                )
                return Continue()
            raise LLMError("model repeated an invalid mention placeholder")
        issue = (
            self.tools.response_feedback(content, self.runtime) if self.tools is not None else None
        )
        if (
            self.state.control is not None
            and self.state.control.reporting == "interactive"
            and self.state.control.ending is None
        ):
            if response.continuation is None and not assistant_recorded:
                self.state.transcript.append(assistant_message)
            if await require_interactive_exit(
                self.state.control, self.state.transcript, extra_feedback=issue
            ):
                return Continue()
        if issue:
            if (
                self.state.answer_recovery_used
                or request_index + 1 >= self.runtime.max_model_requests
            ):
                raise LLMError("model repeated an unsupported final response")
            self.state.answer_recovery_used = True
            if response.continuation is None and not assistant_recorded:
                self.state.transcript.append(assistant_message)
            self.state.transcript.append(ChatMessage(role="system", content=issue))
            return Continue()
        if self.tools is not None:
            content = self.tools.finalize(content, self.runtime)
        has_visible_effects = bool(self.tools is not None and self.tools.has_visible_effects())
        if not content.strip() and not has_visible_effects:
            has_visible_effects = await self.caller_has_confirmed_delivery()
        if not content.strip() and not has_visible_effects:
            if self.tools is not None and self.tools.allow_silent_final(self.runtime):
                logger.info("agent_silent_final origin=%s", self.runtime.origin.value)
            else:
                if (
                    self.state.empty_retries >= 2
                    or request_index + 1 >= self.runtime.max_model_requests
                ):
                    raise LLMEmptyResponseError("model returned no final answer")
                self.state.empty_retries += 1
                logger.warning(
                    "agent_empty_final_retry retry=%d tool_calls_used=%d",
                    self.state.empty_retries,
                    self.state.calls_used,
                )
                if response.continuation is None and not assistant_recorded:
                    self.state.transcript.append(assistant_message)
                self.state.transcript.append(
                    ChatMessage(
                        role="system",
                        content=(
                            "上一响应正文为空；回执仍保留，不能据此断言整个任务完成。"
                            "根据目标和真实结果选择继续执行、等待或回答；"
                            "不要重复已经成功的工具调用，也不要只描述发送模式。"
                        ),
                    )
                )
                return Continue()
        if (
            self.state.control is not None
            and self.state.control.current is not None
            and self.state.control.ending is None
        ):
            # Infer lifecycle completion from a real final answer, but use
            # the same receipt validation as explicit task_control.complete.
            await self.state.control.reconcile_completed_children()
            state = await self.state.control.background_state()
            await self.state.control.refresh_effects()
            if await self.state.control.has_unresolved_effects(pending=False):
                state = "suspended"
            elif await self.state.control.has_unresolved_effects(uncertain=False):
                state = state or "waiting_external"
            if state is not None:
                self.state.control.ending = state
            else:
                artifacts = list(
                    dict.fromkeys(
                        artifact
                        for effect in self.state.control.known_effects
                        if effect.get("ok") or effect.get("delivered_artifacts")
                        for artifact in effect.get("artifacts", [])
                    )
                )[-8:]
                receipt = await self.state.control.execute(
                    "task_control",
                    {"action": "complete", "artifact_ids": artifacts},
                    f"final-answer:{request_index}",
                )
                if not json.loads(receipt).get("ok"):
                    if response.continuation is None and not assistant_recorded:
                        self.state.transcript.append(assistant_message)
                    self.state.transcript.append(ChatMessage(role="system", content=receipt))
                    return Continue()
        if self.state.control is not None and self.state.control.session is not None:
            if response.continuation is None and not assistant_recorded:
                self.state.transcript.append(assistant_message)
            await self.state.control.session.save("paired")
        return End(
            AgentRunResult(
                text=content,
                tool_calls_used=self.state.calls_used,
                model_requests=request_index + 1,
                web_was_used=self.state.web_was_used,
                native_tool_events=tuple(self.state.native_events),
                citations=tuple(self.state.citations),
                response_status=self.state.response_status,
            )
        )

    async def stop_before_tools(self, request_index: int, response: ChatResponse) -> End | None:
        if self.state.no_progress_recovery:
            logger.warning(
                "agent_tool_no_progress_stopped tool_calls_used=%d model_requests=%d",
                self.state.calls_used,
                request_index + 1,
            )
            return End(
                AgentRunResult(
                    text=("检测到模型反复调用相同工具且结果没有变化，已停止本轮工具循环。"),
                    tool_calls_used=self.state.calls_used,
                    model_requests=request_index + 1,
                    web_was_used=self.state.web_was_used,
                    native_tool_events=tuple(self.state.native_events),
                    citations=tuple(self.state.citations),
                    response_status=self.state.response_status,
                )
            )
        return None

    async def execute_tools(self, request_index: int, response: ChatResponse) -> ToolBatchOutcome:

        responses_path = response.continuation is not None
        if not responses_path:
            self.state.transcript.append(
                ChatMessage(
                    role="assistant",
                    content=response.content or None,
                    tool_calls=response.tool_calls,
                    reasoning_content=response.reasoning_content,
                )
            )
        tooling = self.runtime.runtime_config.tooling
        self.state.coordinated = await self.runner._execute_tool_batch(
            response.tool_calls,
            self.tools,
            self.runtime,
            remaining_calls=max(
                0,
                self.runtime.max_tool_calls
                - max(
                    self.state.calls_used,
                    self.runtime.work_control.tools_started
                    if self.runtime.work_control is not None
                    and self.runtime.work_control.current is not None
                    else 0,
                ),
            ),
            max_parallel_calls=tooling.max_parallel_calls if tooling is not None else 1,
            reusable_results=self.state.reusable_tool_results,
            cacheable_names=frozenset(t.name for t in self.state.definitions if t.result_cacheable),
            declared_names=frozenset(t.name for t in self.state.definitions),
            chain_id=self.state.transcript.chain_id,
            request_sequence=request_index + 1,
        )
        self.state.coordinated = replace(
            self.state.coordinated,
            calls=self.runner._budget_tool_media(
                self.state.coordinated.calls, self.state.transcript, self.runtime, self.tools
            ),
        )
        batch, executed = self.state.coordinated.calls, self.state.coordinated.executed_count
        self.state.calls_used += executed
        return ToolBatchOutcome(
            tuple(ToolCallOutcome(c, r, e) for c, r, e in batch),
            executed_count=executed,
            reused_count=self.state.coordinated.reused_count,
        )

    async def finish_tool_turn(
        self, request_index: int, response: ChatResponse, _outcome: ToolBatchOutcome
    ) -> TurnDecision | LoopSignal:

        batch = self.state.coordinated.calls
        if any(result == CODE_COMPOSITION_YIELDED for _, result, _ in batch):
            return End(await self._code_yield(request_index + 1))
        for call, result, _was_executed in batch:
            try:
                outcome = json.loads(result)
            except json.JSONDecodeError:
                outcome = {}
            if (
                call.function.name == "web_search"
                and isinstance(outcome, dict)
                and outcome.get("ok") is True
                and self.runtime.work_control is not None
                and self.runtime.work_control.session is not None
            ):
                data = outcome.get("data")
                sources = data.get("sources") if isinstance(data, dict) else None
                if isinstance(sources, list):
                    self.runtime.work_control.session.record_search_sources(
                        [
                            (
                                source["url"],
                                source.get("title", ""),
                                source.get("snippet", ""),
                            )
                            for source in sources
                            if isinstance(source, dict)
                            and isinstance(source.get("url"), str)
                            and isinstance(source.get("title", ""), str)
                            and isinstance(source.get("snippet", ""), str)
                        ]
                    )
            if call.function.name in EVIDENCE_TOOLS:
                self.state.evidence_observation.emit(
                    "tool_result_staged",
                    request_index=request_index + 1,
                    tool=call.function.name,
                    reused=not _was_executed,
                    ok=isinstance(outcome, dict) and outcome.get("ok") is True,
                )
                self.state.staged_evidence_results += 1
            logger.info(
                "agent_tool_complete tool=%s ok=%s error=%s reused=%s",
                call.function.name,
                outcome.get("ok") if isinstance(outcome, dict) else None,
                (
                    outcome.get("error") or outcome.get("error_code")
                    if isinstance(outcome, dict)
                    else None
                ),
                not _was_executed,
            )
            self.state.transcript.append_result(call.id, result)
            if self.runtime.work_control is not None:
                self.runtime.work_control.observe_result(
                    call.function.name,
                    result,
                    _was_executed,
                    side_effecting=self.runner._is_side_effecting(self.tools, call, self.runtime),
                    arguments=call.function.arguments,
                )
        self.state.transcript.append_tool_media(
            tuple((call.id, result) for call, result, _ in batch)
        )
        from qq_ai_bot.capabilities.results import normalize_legacy_result
        from qq_ai_bot.runtime.effect_outcomes import execution_evidence

        public_results = []
        for call, result, was_executed in batch:
            public_result = {
                "call_id": call.id,
                "name": call.function.name,
                "arguments": call.function.arguments,
                "output": result,
                "executed": was_executed,
            }
            fact = execution_evidence(
                normalize_legacy_result(
                    result, provider_id="display", tool_name=call.function.name
                ),
                tool=call.function.name,
                side_effecting=self.runner._is_side_effecting(self.tools, call, self.runtime),
                arguments=call.function.arguments,
            )
            if (
                was_executed
                and fact["executed"]
                and (fact["side_effecting"] or fact["run_id"] or fact["artifacts"])
            ):
                # This is a turn-local model view of the existing execution
                # receipt, not another effect ledger or replay authority.
                self.state.ordinary_evidence.append({"call_id": call.id, **fact})
            public_results.append(public_result)
        self.state.response_observation["results"] = public_results
        if self.runtime.work_control is not None and self.runtime.work_control.session is not None:
            control = self.runtime.work_control
            assert control.session is not None
            if control.ending != "completed":
                control.session.progress.pop("caller_completion_pending_result", None)
            if (
                control.ending == "completed"
                and control.source.get("delivery_contract") == "return_to_caller"
            ):
                for call, result, _ in batch:
                    if (
                        call.function.name == "task_control"
                        and json.loads(result).get("ok") is True
                    ):
                        arguments = json.loads(call.function.arguments)
                        if arguments.get("action") == "complete":
                            control.session.progress["caller_completion_pending_result"] = {
                                "action": "complete",
                                "artifact_ids": arguments.get("artifact_ids", []),
                            }
            batch_hash = hashlib.sha256(
                json.dumps(
                    [
                        (call.function.name, self.runner._tool_call_signature(call)[1], result)
                        for call, result, _ in batch
                    ],
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            persisted_progress = self.runtime.work_control.session.progress
            self.state.observations = persisted_progress.get("model_observations", [])
            if self.state.observations:
                self.state.opportunity = await stage_feedback_opportunity(
                    self.runtime.work_control, self.state.observations[-1]
                )
                if self.state.opportunity is not None:
                    self.state.stage_feedback_batch, self.state.pending_stage_feedback = (
                        self.state.opportunity
                    )
            repeats = (
                int(persisted_progress.get("repeats", 0)) + 1
                if (
                    batch
                    and persisted_progress.get("fingerprint") == batch_hash
                    and not any(self.runner._tool_result_pending(result) for _, result, _ in batch)
                )
                else 0
            )
            persisted_progress.update(fingerprint=batch_hash, repeats=repeats)
            communication_updates = await start_feedback_updates(self.runtime.work_control, batch)
            if self.state.deferred_paid_compaction:
                await self.runtime.work_control.session.retire_paid_compaction(
                    communication_updates=communication_updates
                )
                self.state.deferred_paid_compaction = False
            else:
                await self.runtime.work_control.session.save(
                    "paired", communication_updates=communication_updates
                )
            if self.runtime.work_control.handoff_work_id is not None:
                return End(
                    AgentRunResult(
                        text="",
                        tool_calls_used=self.state.calls_used,
                        model_requests=request_index + 1,
                        web_was_used=self.state.web_was_used,
                        suppress_delivery=True,
                        work_state="suspended",
                    )
                )
            if (
                self.runtime.work_control.ending == "completed"
                and self.runtime.work_control.source.get("delivery_contract") != "return_to_caller"
                and not self.runtime.work_control.lease.work_id
                and not await self.runtime.work_control.pending()
            ):
                self.runtime.work_control.final_delivery = True
                await self.runtime.work_control.session.save("delivered")
                return End(
                    AgentRunResult(
                        text="",
                        tool_calls_used=self.state.calls_used,
                        model_requests=request_index + 1,
                        web_was_used=self.state.web_was_used,
                        suppress_delivery=True,
                        work_state="completed",
                    )
                )
            if (
                self.runtime.work_control.lease.work_id
                or self.runtime.work_control.source.get("delivery_contract") == "return_to_caller"
            ) and self.runtime.work_control.ending in {
                "waiting_user",
                "waiting_external",
            }:
                return End(
                    AgentRunResult(
                        text="",
                        tool_calls_used=self.state.calls_used,
                        model_requests=request_index + 1,
                        web_was_used=self.state.web_was_used,
                        suppress_delivery=True,
                        work_state=self.runtime.work_control.ending,
                    )
                )
        if self.runtime.work_control is not None and self.runtime.work_control.session is not None:
            # The journal's persisted count, computed above, survives restarts.
            self.state.repeated_batch_count = int(
                self.runtime.work_control.session.progress.get("repeats", 0)
            )
        else:
            fingerprint = tuple(
                (call.function.name, self.runner._tool_call_signature(call)[1], result)
                for call, result, _was_executed in batch
            )
            pending_work = any(self.runner._tool_result_pending(result) for _, result, _ in batch)
            if (
                fingerprint
                and fingerprint == self.state.previous_batch_fingerprint
                and not pending_work
            ):
                self.state.repeated_batch_count += 1
            else:
                self.state.repeated_batch_count = 0
            self.state.previous_batch_fingerprint = fingerprint
        if self.state.coordinated.reused_count == len(batch) and batch:
            logger.info(
                "agent_tool_batch_reused reused_calls=%d tool_calls_used=%d",
                self.state.coordinated.reused_count,
                self.state.calls_used,
            )
        if self.state.repeated_batch_count >= 2:
            if (
                self.runtime.work_control is not None
                and self.runtime.work_control.current is not None
            ):
                from qq_ai_bot.runtime.activation_outcome import WorkNoProgress

                raise WorkNoProgress("repeated_tool_results")
            self.state.no_progress_recovery = True
            logger.warning(
                "agent_tool_no_progress_detected repeated_batches=%d tool_calls_used=%d",
                self.state.repeated_batch_count,
                self.state.calls_used,
            )
            if self.state.transcript.continuation is None:
                self.state.transcript.append(
                    ChatMessage(
                        role="system",
                        content=(
                            "相同工具调用已经连续返回相同结果。停止调用工具，"
                            "只根据已有结果给出简短、真实的最终答复。"
                        ),
                    )
                )
        if self.tools is not None and self.tools.did_use_web():
            self.state.web_was_used = True
        # begin() bounds the segment handoff to one admitted model request.
        return Continue()

    async def _code_yield(self, model_requests: int) -> AgentRunResult:
        # The response with the pending code call is already journaled; no
        # result is paired, so the next segment resumes the same program.
        assert self.runtime.work_control is not None
        self.runtime.work_control.yield_segment = True
        self.runtime.work_control.ending = "queued"
        return AgentRunResult(
            text="",
            tool_calls_used=self.state.calls_used,
            model_requests=model_requests,
            web_was_used=self.state.web_was_used,
            suppress_delivery=True,
            work_state="queued",
        )

    async def exhausted(
        self,
    ) -> AgentRunResult:
        if self.runtime.work_control is not None and self.runtime.work_control.current is not None:
            self.runtime.work_control.yield_segment = True
            self.runtime.work_control.ending = "queued"
            if self.runtime.work_control.session is not None:
                await self.runtime.work_control.session.save("paired")
            return AgentRunResult(
                text="",
                tool_calls_used=self.state.calls_used,
                model_requests=self.runtime.work_control.requests_started,
                web_was_used=self.state.web_was_used,
                suppress_delivery=True,
                work_state="queued",
            )
        exhausted = (
            self.tools.exhausted(self.runtime)
            if self.tools is not None
            else "工具调用次数过多，Agent 已停止。"
        )
        return AgentRunResult(
            text=exhausted,
            tool_calls_used=self.state.calls_used,
            model_requests=self.runtime.max_model_requests,
            web_was_used=self.state.web_was_used,
            native_tool_events=tuple(self.state.native_events),
            citations=tuple(self.state.citations),
            response_status=self.state.response_status,
        )
