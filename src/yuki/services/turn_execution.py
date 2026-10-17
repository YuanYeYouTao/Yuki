"""Per-activation turn state and the three typed Pi core boundaries."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field, replace
from functools import partial
from typing import Any

from yuki.agent_core import (
    STOP,
    Continue,
    End,
    ToolBatchOutcome,
    ToolCallOutcome,
    TurnDecision,
    run_agent_loop,
)
from yuki.agent_core.model_boundary import LoopSignal
from yuki.automation.models import TurnOrigin
from yuki.capabilities.coordinator import (
    CoordinatedToolResult,
)
from yuki.domain.messages import (
    ChatImage,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ChatTool,
    FunctionCallOutput,
    ModelResponseStatus,
    NativeToolDefinition,
    NativeToolEvent,
    PromptRequestDiagnostics,
    ProviderContinuation,
    ResponseCitation,
)
from yuki.llm.base import (
    LLMEmptyResponseError,
    LLMError,
    LLMIncompleteResponseError,
    LLMInvalidResponseError,
    LLMMalformedFunctionCallError,
    LLMNativeToolError,
    LLMTimeoutError,
    LLMUnavailableError,
)
from yuki.model_runtime.capacity import estimate_request_tokens
from yuki.model_runtime.dispatch_guard import model_dispatch_guard
from yuki.model_runtime.models import ModelCapability, ModelExecutionPriority
from yuki.prompting.serializer import serialized_messages_hash
from yuki.runtime.execution_receipts import current_receipts
from yuki.runtime.work_control import WorkControl, WorkInputsPreparing
from yuki.runtime.work_repository import WorkCapacityError
from yuki.services.agent_runner import (
    CODE_COMPOSITION_YIELDED,
    AgentRunner,
    AgentRunResult,
    AgentRuntime,
    AgentToolBackend,
    _RequestNotStarted,
)
from yuki.services.concurrency import RequestCancelledError
from yuki.services.context_boundary import ContextBoundary
from yuki.services.evidence_observation import EVIDENCE_TOOLS, EvidenceObservation
from yuki.services.turn_transcript import (
    DispatchOrigin,
    TranscriptRequest,
    TurnTranscript,
    validating_request,
)
from yuki.web.models import WebMode

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TurnState:
    """Mutable state belonging to one activation and its original Work only."""

    transcript: TurnTranscript
    evidence_observation: EvidenceObservation
    fixed_definitions: tuple[ChatTool, ...] = ()
    staged_evidence_results: int = 0
    calls_used: int = 0
    native_events: list[NativeToolEvent] = field(default_factory=list)
    citations: list[ResponseCitation] = field(default_factory=list)
    response_status: ModelResponseStatus = ModelResponseStatus.COMPLETED
    continuation_tools: tuple[ChatTool, ...] = ()
    continuation_native_tools: tuple[NativeToolDefinition, ...] = ()
    provider_pause_replay: bool = False
    observed_event_ids: set[int] = field(default_factory=set)
    public_tail: list[ChatMessage] = field(default_factory=list)
    control: WorkControl | None = None
    boundary: ContextBoundary | None = None
    definitions: tuple[ChatTool, ...] = ()
    response_observation: dict[str, Any] = field(default_factory=dict)
    coordinated: CoordinatedToolResult = field(default_factory=lambda: CoordinatedToolResult((), 0))
    segment_handoff_index: int | None = None


@dataclass(frozen=True, slots=True)
class _PreparedRequest:
    sequence: TranscriptRequest
    request: ChatRequest
    priority: ModelExecutionPriority


@dataclass(slots=True)
class _PrimaryDispatch:
    """One admitted model request; retries share its original reservation/CAS."""

    runner: AgentRunner
    runtime: AgentRuntime
    request_index: int
    execute: Callable[[], Awaitable[ChatResponse]]
    sequence: TranscriptRequest
    boundary: ContextBoundary | None
    observed_event_ids: set[int]
    tools: AgentToolBackend | None = None
    selected_media: tuple[ChatImage, ...] = ()
    has_native_effects: bool = False
    on_dispatch: Callable[[], Awaitable[None]] | None = None
    business_inputs: bool = True
    prepared: bool = field(default=False, init=False)
    selected_boundary: ContextBoundary | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        # Capacity compaction may replace a candidate with a summary. Only a
        # rendering present in this frozen primary request can be published.
        if self.boundary is None:
            return
        messages = (*self.sequence.messages, *self.sequence.items)
        if not all(message in messages for _, message in self.boundary.fragments):
            return
        inputs = self.boundary.ordinary_inputs
        if self.business_inputs:
            # An execution compact can cover one private input while retaining
            # another. Each admission still requires its own original material.
            inputs = tuple(
                candidate
                for candidate in inputs
                if candidate.message is None or candidate.message in messages
            )
            if self.boundary.ordinary_inputs and not inputs:
                return
        self.selected_boundary = replace(self.boundary, ordinary_inputs=inputs)

    async def complete(self) -> ChatResponse:
        with model_dispatch_guard(self.admit):
            return await self.execute()

    async def admit(self) -> None:
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
        if self.prepared:
            return
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
                    candidate = await self.selected_boundary.prepare(
                        self.business_inputs, self.selected_boundary.ordinary_inputs
                    )
                    candidate.stage()
                    prior_event_ids = list(work_session.event_ids)
                    work_session.event_ids.extend(
                        sorted(self.selected_boundary.event_ids.difference(work_session.event_ids))
                    )
                else:
                    await self.selected_boundary.commit(
                        self.business_inputs, self.selected_boundary.ordinary_inputs
                    )
            if work_session is not None:
                try:
                    await work_session.save(
                        "dispatched",
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
            if self.selected_boundary is not None:
                self.observed_event_ids.update(self.selected_boundary.event_ids)
                if work_session is not None:
                    work_session.public_event_ids.update(self.selected_boundary.event_ids)
        else:
            if self.selected_boundary is not None:
                await self.selected_boundary.commit(
                    self.business_inputs, self.selected_boundary.ordinary_inputs
                )
                self.observed_event_ids.update(self.selected_boundary.event_ids)
        if self.has_native_effects:
            # Mark the original private task before real native dispatch. SDK
            # backends without an ordinary private token have no protection hook.
            protect_native = getattr(self.tools, "protect_native_dispatch", None)
            if callable(protect_native):
                await protect_native(self.runtime)
        if self.on_dispatch is not None:
            await self.on_dispatch()
        self.runner._record_failure_usage(
            self.tools,
            tool_calls=0,
            model_requests=self.request_index + 1 + sum(self.runtime.auxiliary_requests or ()),
        )
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
        self._initial_layout_finalized = False
        self._initial_inputs: list[ChatMessage] = []
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
            manifest = self.runner.main_contract
            entry = await manifest.entry_for(self.runtime.canonical_conversation_id)
            self.state.fixed_definitions = await manifest.model_definitions(entry)
            self.runtime = replace(
                self.runtime,
                script_api=manifest.app_script_api if entry == "app" else manifest.script_api,
                manifest_revision=manifest.app_revision if entry == "app" else manifest.revision,
            )
            if not self.runtime.dynamic_context_prepared:
                raise LLMError("main_agent_composition_required")
            if self.tools is None:
                raise LLMError("main_agent_executor_required")
        await self.runner._prepare_tools(self.tools, self.runtime)
        if self.runtime.work_control is not None:
            from yuki.runtime.work_session import WorkSession

            contract = self.runner.work_contract(
                self.runtime.runtime_config,
                self.initial_messages,
                self.state.fixed_definitions,
                script_api=self.runtime.script_api,
                manifest_revision=self.runtime.manifest_revision,
            )
            self.runtime.work_control.session = WorkSession(self.runtime.work_control, contract)
            public_count = (
                len(self.runtime.public_compaction.messages)
                if self.runtime.public_compaction is not None
                else 0
            )
            self.state.transcript = await self.runtime.work_control.session.restore(
                self.state.transcript,
                visible_event_ids=self.runtime.visible_event_ids,
                public_compaction=self.runtime.public_compaction,
            )
            if self.runtime.public_compaction is not None:
                self.initial_messages = (
                    *self.runtime.public_compaction.messages,
                    *self.initial_messages[public_count:],
                )
            pending_response = self.runtime.work_control.session.pending
            if pending_response:
                # Original undispatched calls and open programs finish before
                # an ending can settle or another model request can be bought.
                if await self.runner._resume_tool_response(
                    pending_response,
                    self.state.transcript,
                    self.tools,
                    self.runtime,
                    self.state.fixed_definitions,
                ):
                    return AgentRunResult(
                        text="",
                        tool_calls_used=0,
                        model_requests=0,
                        suppress_delivery=True,
                        work_state="queued",
                    )
            if pending_response:
                session = self.runtime.work_control.session
                self.state.calls_used = self.runtime.work_control.tools_started
                # _resume_tool_response paired the original response and saved the
                # old signed chain. Continue this activation on current public
                # history, carrying only its unobserved portable round.
                if await session.rebase_business(TurnTranscript(self.initial_messages)):
                    assert session.transcript is not None
                    self.state.transcript = session.transcript
            self.state.provider_pause_replay = bool(
                self.runtime.work_control.session.progress.get("provider_pause_replay", False)
            )
            await self.runtime.work_control.retire_fulfilled_wait()
            if self.runtime.work_control.current is not None:
                pending = await self.runtime.work_control.pending()
                if (
                    pending
                    and pending[0]["ready"]
                    and await self.runtime.work_control.repository.has_pending_business_inputs(
                        self.runtime.work_control.lease, self.runtime.work_control.current["id"]
                    )
                ):
                    await self.runtime.work_control.retire_accepted()
            if self.runtime.work_control.accepted_ending() is not None:
                await self.runtime.work_control.validate()
                await self.runtime.work_control.session.save("paired")
                return self._accepted_result(0)
            if self.runtime.work_control.session.recovered_delivery:
                # Pause by original ID; never execute a legacy frozen plan.
                return AgentRunResult(
                    text="",
                    tool_calls_used=0,
                    model_requests=0,
                    suppress_delivery=True,
                    work_state="suspended",
                )
            if self.runtime.work_control.handoff_work_id is not None:
                await self.runtime.work_control.session.save("paired")
                return AgentRunResult(
                    text="",
                    tool_calls_used=0,
                    model_requests=0,
                    suppress_delivery=True,
                )

        recovered = await self.restore_compact_receipt()
        if recovered is not None:
            return recovered
        result = await run_agent_loop(
            max_requests=self.runtime.max_model_requests,
            model=self,
            invocation=self,
            settlement=self,
        )
        assert isinstance(result, AgentRunResult)
        return result

    async def restore_compact_receipt(self) -> AgentRunResult | None:
        """Reuse ordinary maintenance by its internal execution owner."""
        from yuki.conversation.frozen_fragments import FrozenFragments
        from yuki.runtime.work_journal import (
            JournalUnavailable,
            decode_transcript,
            encode_transcript,
        )
        from yuki.services.context_compaction import (
            CompactReceipt,
            compacted_transcript,
            current_public_compact,
            public_compact_publication,
        )

        context = self.runtime.public_compaction
        control = self.state.control
        if (
            context is None
            or not self.runtime.execution_id
            or (control is not None and control.current is not None)
        ):
            return None
        receipt = CompactReceipt(context, self.runtime.execution_id, self.runtime.actor_user_id)
        saved = await receipt.read()
        if saved is None:
            return None
        if saved["phase"] == "dispatched":
            raise JournalUnavailable("context_compaction_outcome_unknown")
        contract = self.runner.work_contract(
            self.runtime.runtime_config,
            self.initial_messages,
            self.state.fixed_definitions,
            script_api=self.runtime.script_api,
            manifest_revision=self.runtime.manifest_revision,
        )
        if saved["main_contract"] != contract:
            from yuki.runtime.work_repository import WorkConflict

            raise WorkConflict("context_compaction_contract_changed")
        public = saved.get("public")
        summary = saved.get("response", "")
        if saved["valid"] and public is not None:
            public, summary = await current_public_compact(context, public, summary)
        if saved["phase"] == "response":
            candidate = (
                compacted_transcript(saved)
                if saved["valid"]
                else decode_transcript(saved["original"])
            )
            publication = (
                await public_compact_publication(context.repository, public, summary)
                if saved["valid"] and public is not None
                else None
            )
            saved.update(phase="paired", candidate=encode_transcript(candidate))
            await receipt.write(saved, publication=publication)
        candidate = decode_transcript(saved["candidate"])
        if saved["valid"] or candidate.continuation is None:
            fresh = self.state.transcript.request().messages
            if saved["valid"] and public is not None:
                adopted = await context.adopt(
                    summary, public["coverage"], FrozenFragments.load(public["items"])
                )
                self.state.transcript = TurnTranscript(
                    (*adopted, *fresh[len(self.initial_messages) :])
                )
                self.initial_messages = adopted
                tail = decode_transcript(saved["retained"]).request().messages
            else:
                # The saved initial block is paid-request evidence. Current
                # history and activation material belong to this fresh run.
                old_count = len(decode_transcript(saved["prefix"]).request().messages)
                tail = candidate.request().messages[old_count:]
            await self._finalize_initial_layout()
            for message in tail:
                self.state.transcript.append(message)
        else:
            self.state.transcript = candidate
            self.initial_messages = decode_transcript(saved["prefix"]).request().messages
        if self.runtime.auxiliary_requests is not None:
            self.runtime.auxiliary_requests[0] = saved["model_requests"]
        if saved["valid"] and saved.get("public") is not None:
            if self.runtime.compact_focus is not None:
                return AgentRunResult(
                    text="已压缩当前会话的公共上下文。",
                    tool_calls_used=0,
                    model_requests=saved["model_requests"],
                    suppress_delivery=True,
                )
        return None

    async def adopt_public_compact(self, public: dict[str, Any], summary: str) -> None:
        from yuki.conversation.frozen_fragments import FrozenFragments

        context = self.runtime.public_compaction
        assert context is not None
        self.initial_messages = self.state.transcript.request().messages[
            : len(self.initial_messages)
            - public["public_count"]
            + public["static_count"]
            + 1
            + len(public["items"])
        ]
        await context.adopt(summary, public["coverage"], FrozenFragments.load(public["items"]))

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
            control.ending = "work_input_preparing"
            return AgentRunResult(
                text="",
                suppress_delivery=True,
                work_state="waiting_external",
                tool_calls_used=self.state.calls_used,
                model_requests=request_index,
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
        if not self._initial_layout_finalized:
            self._initial_inputs.extend(added)
        for message in added:
            self.state.transcript.append(message)
        return None

    async def begin(self, request_index: int) -> LoopSignal | None:

        if (
            self.runtime.max_model_requests is not None
            and self.runtime.auxiliary_requests
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
                for message in self.state.boundary.current_inputs:
                    self.state.transcript.append(message)
        if (
            self.state.control is not None
            and self.state.control.current is not None
            and self.runtime.max_model_requests is not None
            and self.state.control.requests_started >= self.runtime.max_model_requests
        ):
            return STOP
        if (
            self.state.control is not None
            and self.state.control.current is not None
            and self.runtime.max_tool_calls is not None
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
                                "Otherwise retain useful findings and the next step in an optional "
                                "task_control(action='update', context_note=...). "
                                "No further business tool call can execute in this activation."
                            ),
                        },
                        ensure_ascii=False,
                    ),
                )
            )
        return None

    async def steer(self, request_index: int) -> End | None:
        if self.state.control is not None and not self.state.provider_pause_replay:
            waiting = await self.take_boundary_inputs(request_index, self.state.boundary)
            if waiting is not None:
                return End(waiting)
        return None

    async def request(self, request_index: int) -> ChatResponse | End | LoopSignal:

        if self.tools is not None:
            self.tools.refresh_catalog(self.runtime)
        self.state.definitions = self.state.fixed_definitions
        web_config = self.runtime.runtime_config.web
        try:
            web_mode = WebMode(web_config.mode)
        except ValueError:
            web_mode = WebMode.DISABLED
        self.state.definitions, native_definitions = self.runner.prepare_request_tools(
            self.state.definitions,
            runtime_config=self.runtime.runtime_config,
            allowed_capabilities=self.runtime.allowed_capabilities,
        )
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
        prepared_request: _PreparedRequest | None = None
        try:
            candidate = await self.prepare_request(request_index, native_definitions, web_mode)
            if isinstance(candidate, (End, LoopSignal)):
                return candidate
            prepared_request = candidate
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
                request_index=request_index,
                execute=execute,
                sequence=candidate.sequence,
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
            self.runner._record_failure_usage(
                self.tools,
                tool_calls=self.state.calls_used,
                model_requests=request_index + 1 + (self.runtime.auxiliary_requests or [0])[0],
            )
        except _RequestNotStarted as exc:
            self.runner._record_failure_usage(
                self.tools, tool_calls=self.state.calls_used, model_requests=request_index
            )
            raise exc.cause from exc
        except (LLMTimeoutError, LLMUnavailableError, RequestCancelledError) as exc:
            self.runner._record_failure_usage(
                self.tools, tool_calls=self.state.calls_used, model_requests=request_index + 1
            )
            physical_count = exc.diagnostics.get("physical_request_count")
            if (
                prepared_request is not None
                and prepared_request.request.native_tools
                and (physical_count > 0 if type(physical_count) is int else dispatch.prepared)
            ):
                raise LLMNativeToolError(
                    "provider-native request transport outcome is unknown",
                    diagnostics=exc.diagnostics,
                ) from exc
            if (
                not isinstance(exc, RequestCancelledError)
                and self.state.control is not None
                and self.state.control.session is not None
            ):
                await self.state.control.session.save("paired")
            raise
        except (LLMEmptyResponseError, LLMMalformedFunctionCallError) as exc:
            malformed = isinstance(exc, LLMMalformedFunctionCallError)
            if (
                not malformed
                and prepared_request is not None
                and prepared_request.request.native_tools
            ):
                self.runner._record_failure_usage(
                    self.tools, tool_calls=self.state.calls_used, model_requests=request_index + 1
                )
                raise LLMNativeToolError(
                    "provider-native request returned no resumable output",
                    diagnostics=exc.diagnostics,
                ) from exc
            if malformed:
                # This typed provider failure confirms a response with no usable
                # calls or native effects; prior receipts stay authoritative.
                if self.state.control is not None:
                    await self.state.control.confirm_inputs()
                receipts = current_receipts.get()
                if receipts is not None:
                    await receipts.confirm()
            if self.state.control is not None and self.state.control.session is not None:
                await self.state.control.session.save("paired")
            self.runner._record_failure_usage(
                self.tools, tool_calls=self.state.calls_used, model_requests=request_index + 1
            )
            raise
        except LLMError as exc:
            self.runner._record_failure_usage(
                self.tools, tool_calls=self.state.calls_used, model_requests=request_index + 1
            )
            if (
                self.state.control is not None
                and self.state.control.session is not None
                and not isinstance(exc, LLMNativeToolError)
                and (
                    prepared_request is None
                    or not prepared_request.request.native_tools
                    or not isinstance(exc, (LLMInvalidResponseError, LLMIncompleteResponseError))
                )
            ):
                await self.state.control.session.save("paired")
            raise
        response = await self.observe_response(
            request_index, response, candidate.request.native_tools
        )
        await self.guard_native_response(request_index, response)
        return response

    async def guard_native_response(
        self,
        request_index: int,
        response: ChatResponse,
    ) -> None:
        """A paid native response grants no generic empty/truncation replay."""
        native_empty = (
            response.native_tool_events
            and not response.content.strip()
            and not response.tool_calls
            and not self.state.provider_pause_replay
        )
        if response.incomplete_reason == "duplicate_tool_call_id" or native_empty:
            # These are paid responses with retained protocol evidence, not
            # confirmed effect-free empty generations. Only a supported
            # pause may automatically continue a server tool. A generic
            # empty/truncation retry could repeat already-dispatched work.
            self.runner._record_failure_usage(
                self.tools, tool_calls=self.state.calls_used, model_requests=request_index + 1
            )
            failure = (
                LLMInvalidResponseError("provider returned duplicate local tool call IDs")
                if response.incomplete_reason == "duplicate_tool_call_id"
                else LLMNativeToolError("provider-native result has no final response")
            )
            if self.state.control is not None and self.state.control.session is not None:
                try:
                    await self.state.control.session.save("paired")
                except Exception as exc:
                    # A failure to publish the received server-tool state
                    # cannot grant a database retry that repeats its HTTP
                    # dispatch. The paid request budget is already durable.
                    failure.diagnostics["checkpoint_saved"] = False
                    raise failure from exc
            raise failure
        return None

    async def _finalize_initial_layout(self) -> None:
        """Freeze Host initialization only after restore/rebase and first steer."""
        if self._initial_layout_finalized:
            return
        self._initial_layout_finalized = True
        control = self.runtime.work_control
        session = control.session if control is not None else None
        if session is not None and session.uses_recovery_transcript:
            return
        transcript = self.state.transcript
        initial = self.initial_messages
        sequence = transcript.request()
        if not initial or sequence.continuation is not None:
            return
        if sequence.messages[: len(initial)] != initial:
            return
        extras = sequence.messages[len(initial) :]
        input_ids = {id(message) for message in self._initial_inputs}
        host = tuple(message for message in extras if id(message) not in input_ids)
        # These are exact Host-appended initialization blocks, not text-label
        # detection. Opaque/protocol entries can only belong to an old chain.
        if any(message.tool_calls or message.role in {"assistant", "tool"} for message in host):
            return
        host = tuple(
            replace(message, content="\n\n" + (message.content or "") + "\n\n") for message in host
        )
        hosts = (
            (
                ChatMessage(
                    role="user",
                    content="\n\n<runtime_context>\n"
                    + json.dumps(
                        await control.runtime_state(), ensure_ascii=False, separators=(",", ":")
                    )
                    + "\n</runtime_context>\n\n",
                ),
                *host,
            )
            if control is not None
            else host
        )
        current_inputs = tuple(self._initial_inputs)
        transcript.finalize_initial(initial, hosts, current_inputs)
        self.initial_messages = (*initial[:-1], *hosts, initial[-1], *current_inputs)

    async def prepare_request(
        self,
        request_index: int,
        native_definitions: tuple[NativeToolDefinition, ...],
        web_mode: WebMode,
    ) -> _PreparedRequest | End | LoopSignal:
        """Prepare capacity/compaction before the original request admission."""
        await self._finalize_initial_layout()
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
        control = self.state.control
        work_id = (
            control.current["id"]
            if control is not None and control.current is not None
            else control.lease.work_id
            if control is not None
            else None
        )
        generation = (
            control.lease.generation
            if control is not None
            else self.runtime.conversation_generation
        )
        conversation_id = (
            control.lease.conversation_id
            if control is not None
            else self.runtime.canonical_conversation_id
        )
        request = ChatRequest(
            messages=sequence.messages,
            provider_session_id=(
                f"yuki:{conversation_id}:g{generation}:"
                + (f"work:{work_id}" if work_id else "main")
                if conversation_id is not None and generation is not None
                else None
            ),
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
        input_budget = capacity.input_budget(output_tokens=request.max_output_tokens)
        predicted_tokens = estimate_request_tokens(self.runner._capacity_request(request))
        if self.runtime.compact_focus is not None:
            public = self.runtime.public_compaction
            if public is None or not public.fragments.items:
                return End(
                    AgentRunResult(
                        text="当前会话没有可压缩的公共历史。",
                        tool_calls_used=0,
                        model_requests=0,
                        suppress_delivery=True,
                    )
                )
            original = self.state.transcript
            await self.compact_current(request_index, request, priority, input_budget)
            return End(
                AgentRunResult(
                    text="已压缩当前会话的公共上下文。"
                    if self.state.transcript.chain_id != original.chain_id
                    else "本次压缩没有产生有效检查点。",
                    tool_calls_used=0,
                    model_requests=sum(self.runtime.auxiliary_requests or ()),
                    suppress_delivery=True,
                )
            )
        if (
            predicted_tokens >= context.compact_trigger_tokens
            and (input_budget is None or predicted_tokens <= input_budget)
            and not self.state.provider_pause_replay
        ):
            original = self.state.transcript
            request = await self.compact_current(
                request_index,
                request,
                priority,
                input_budget,
                trigger_tokens=context.compact_trigger_tokens,
            )
            predicted_tokens = estimate_request_tokens(self.runner._capacity_request(request))
            if self.state.transcript.chain_id != original.chain_id:
                sequence = self.state.transcript.request()
                self.state.continuation_tools = ()
                self.state.continuation_native_tools = ()
            if (
                self.runtime.max_model_requests is not None
                and request_index + sum(self.runtime.auxiliary_requests or ())
                >= self.runtime.max_model_requests
            ):
                return STOP
        while input_budget is not None and predicted_tokens > input_budget:
            if self.state.provider_pause_replay:
                raise WorkCapacityError("model_request_capacity")
            request = await self.compact_current(request_index, request, priority, input_budget)
            if (
                self.runtime.max_model_requests is not None
                and request_index + sum(self.runtime.auxiliary_requests or ())
                >= self.runtime.max_model_requests
            ):
                return STOP
            sequence = self.state.transcript.request()
            predicted_tokens = estimate_request_tokens(self.runner._capacity_request(request))
            self.state.continuation_tools = ()
            self.state.continuation_native_tools = ()
        return _PreparedRequest(sequence, request, priority)

    async def compact_current(
        self,
        request_index: int,
        request: ChatRequest,
        priority: ModelExecutionPriority,
        input_budget: int | None,
        *,
        trigger_tokens: int | None = None,
    ) -> ChatRequest:
        """Maintain the actual Main chain under its original dispatch and journal."""
        from yuki.conversation.observations import privacy_generation
        from yuki.runtime.work_journal import encode_transcript
        from yuki.services.context_compaction import (
            CompactReceipt,
            compacted_transcript,
            execution_compact_plan,
            public_compact_plan,
            public_compact_publication,
        )

        public_context = self.runtime.public_compaction
        from yuki.services.turn_transcript import recorded_response

        active_session = self.state.control.session if self.state.control is not None else None
        recorded = (
            active_session.progress.get("model_observations", [])
            if active_session is not None
            else [self.state.response_observation]
            if self.state.response_observation
            else []
        )
        public_eligible = (
            public_context is not None
            and bool(public_context.fragments.items)
            and request.continuation is None
            and request.messages[: len(public_context.messages)] == public_context.messages
        )
        initial_tokens = estimate_request_tokens(
            self.runner._capacity_request(
                replace(
                    request,
                    messages=self.initial_messages,
                    continuation=None,
                    continuation_items=(),
                )
            )
        )
        public_requested = self.runtime.compact_focus is not None
        public_limit = trigger_tokens if trigger_tokens is not None else input_budget
        try:
            if public_eligible and (
                public_requested or (public_limit is not None and initial_tokens >= public_limit)
            ):
                assert public_context is not None
                plan = public_compact_plan(
                    public_context,
                    self.state.transcript,
                    request,
                    initial=self.initial_messages,
                    input_budget=input_budget,
                    prepare=self.runner._capacity_request,
                    focus=self.runtime.compact_focus or "",
                )
            else:
                tail = self.state.transcript.entries()[len(self.initial_messages) :]
                if trigger_tokens is not None and not any(
                    isinstance(entry, (FunctionCallOutput, ProviderContinuation))
                    or (isinstance(entry, ChatMessage) and entry.role in {"assistant", "tool"})
                    for entry in tail
                ):
                    return request
                try:
                    plan = execution_compact_plan(
                        self.state.transcript,
                        self.initial_messages,
                        request,
                        input_budget=input_budget,
                        prepare=self.runner._capacity_request,
                        retained_public=tuple(self.state.public_tail),
                        responses=tuple(recorded_response(value) for value in recorded),
                        call_key=(
                            self.state.control.session.call_key
                            if self.state.control is not None
                            and self.state.control.session is not None
                            else None
                        ),
                    )
                except WorkCapacityError:
                    if not public_eligible:
                        raise
                    assert public_context is not None
                    plan = public_compact_plan(
                        public_context,
                        self.state.transcript,
                        request,
                        initial=self.initial_messages,
                        input_budget=input_budget,
                        prepare=self.runner._capacity_request,
                    )
        except WorkCapacityError:
            # A soft watermark is a maintenance policy, not an input-capacity
            # error. Only planning failures are optional; paid/unknown outcomes,
            # source changes and cancellation keep their original recovery path.
            if trigger_tokens is None:
                raise
            return request
        original = self.state.transcript
        control = self.state.control
        session = control.session if control is not None and control.current is not None else None
        receipt = (
            CompactReceipt(public_context, self.runtime.execution_id, self.runtime.actor_user_id)
            if session is None and public_context is not None and self.runtime.execution_id
            else None
        )
        if not public_requested:
            previous = (
                session.progress.get("context_compaction")
                if session is not None
                else await receipt.read()
                if receipt is not None
                else None
            )
            if (
                previous is not None
                and previous.get("phase") == "paired"
                and previous.get("valid") is False
            ):
                # Reuse the original paid failure for this exact candidate.
                # A cold activation changes chain/diagnostic metadata, not the
                # model input. New source content or settings are a new input.
                metadata = {
                    "request_chain_id",
                    "request_shape_hash",
                    "conversation_prefix_hash",
                    "prompt_snapshot_fingerprint",
                    "static_prompt_revision",
                }
                current_input = {
                    key: value for key, value in asdict(plan.request).items() if key not in metadata
                }
                previous_input = {
                    key: value for key, value in previous["request"].items() if key not in metadata
                }
                if json.dumps(current_input, sort_keys=True, default=str) == json.dumps(
                    previous_input, sort_keys=True, default=str
                ):
                    if (
                        input_budget is not None
                        and estimate_request_tokens(self.runner._capacity_request(request))
                        > input_budget
                    ):
                        raise WorkCapacityError("model_request_capacity")
                    return request
        if session is not None:
            await session.prepare_context_compaction(
                self.initial_messages,
                plan.retained,
                plan.public,
            )
            state = session.progress["context_compaction"]
            session.transcript = plan.transcript
        else:
            state = {
                "ordinal": sum(self.runtime.auxiliary_requests or ()) + 1,
                "prefix": encode_transcript(TurnTranscript(self.initial_messages)),
                "retained": encode_transcript(TurnTranscript(plan.retained)),
                "public": plan.public,
                "original": encode_transcript(original),
                "phase": "dispatched",
            }
            sequence = original.request()
            if sequence.layout_public_initial is not None:
                state["layout"] = {
                    "public": encode_transcript(TurnTranscript(sequence.layout_public_initial)),
                    "host": encode_transcript(TurnTranscript(sequence.layout_host_initial)),
                    "inputs": encode_transcript(TurnTranscript(sequence.layout_current_inputs)),
                }
            if public_context is not None:
                state["source_revision"] = public_context.version.prompt_source_revision
                async with public_context.repository.database.sessions() as reader:
                    state["privacy_generation"] = await privacy_generation(reader)
        state["request"] = asdict(plan.request)
        state["covered_entries"] = len(plan.transcript.entries()) - 1
        state["main_contract"] = self.runner.work_contract(
            self.runtime.runtime_config,
            self.initial_messages,
            self.state.fixed_definitions,
            script_api=self.runtime.script_api,
            manifest_revision=self.runtime.manifest_revision,
        )
        self.state.transcript = plan.transcript

        async def execute() -> ChatResponse:
            return await self.runner._models.execute(
                self.runner._task,
                plan.request,
                priority=priority,
                canonical_conversation_id=self.runtime.canonical_conversation_id,
            )

        async def persist_dispatch() -> None:
            if receipt is not None:
                await receipt.write(state)

        dispatch = _PrimaryDispatch(
            self.runner,
            self.runtime,
            request_index,
            execute,
            plan.transcript.request(),
            self.state.boundary,
            self.state.observed_event_ids,
            self.tools,
            has_native_effects=bool(plan.request.native_tools),
            on_dispatch=persist_dispatch if receipt is not None else None,
            business_inputs=False,
        )
        response: ChatResponse | None = None
        try:
            response = await self.runner._concurrency.run_llm(
                self.runtime.conversation_key,
                dispatch.complete,
            )
        except (LLMEmptyResponseError, LLMMalformedFunctionCallError) as exc:
            if plan.request.native_tools:
                raise
            # The adapter confirmed a response without usable local output.
            # Keep this paid failure, then resume the original input; no model
            # response, usage or provider continuation is invented here.
            state["error_category"] = type(exc).__name__
        except BaseException:
            if not dispatch.prepared:
                self.state.transcript = original
                if session is not None:
                    session.transcript = original
                    session.progress.pop("context_compaction", None)
            raise
        finally:
            if dispatch.prepared and self.runtime.auxiliary_requests is not None:
                self.runtime.auxiliary_requests[0] += 1
        valid = (
            response is not None
            and bool(response.content.strip())
            and not response.tool_calls
            and response.status is ModelResponseStatus.COMPLETED
        )
        if response is not None and response.continuation is not None:
            plan.transcript.accept(response.continuation)
        if response is not None:
            plan.transcript.append(
                ChatMessage("assistant", response.content, tool_calls=response.tool_calls),
            )
            state["model_response"] = asdict(response)
        state.update(
            response=response.content if response is not None else "",
            valid=valid,
            phase="response",
            model_requests=sum(self.runtime.auxiliary_requests or ()),
        )
        if session is not None:
            await session.save("response")
            self.state.transcript = await session.finish_context_compaction()
        else:
            if receipt is not None:
                await receipt.write(state)
            self.state.transcript = compacted_transcript(state) if valid else original
            if valid and plan.public is not None:
                assert public_context is not None
                publication = await public_compact_publication(
                    public_context.repository,
                    plan.public,
                    state["response"],
                )
            else:
                publication = None
            if receipt is not None:
                state.update(phase="paired", candidate=encode_transcript(self.state.transcript))
                await receipt.write(state, publication=publication)
            elif publication is not None:
                assert public_context is not None
                async with public_context.repository.database.immediate_session() as writer:
                    await publication(writer)
        if not valid:
            if (
                input_budget is not None
                and estimate_request_tokens(self.runner._capacity_request(request)) > input_budget
            ):
                raise WorkCapacityError("model_request_capacity")
            return request
        if plan.public is not None:
            await self.adopt_public_compact(plan.public, state["response"])
        sequence = self.state.transcript.request()
        candidate = replace(
            request,
            messages=sequence.messages,
            continuation=sequence.continuation,
            continuation_items=sequence.items,
            request_chain_id=self.state.transcript.chain_id,
        )
        if (
            not plan.retained
            and plan.public is None
            and input_budget is not None
            and estimate_request_tokens(self.runner._capacity_request(candidate)) > input_budget
        ):
            raise WorkCapacityError("model_request_capacity")
        return candidate

    async def observe_response(
        self,
        request_index: int,
        response: ChatResponse,
        native_definitions: tuple[NativeToolDefinition, ...],
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
        self.state.continuation_native_tools = native_definitions
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
        if self.state.control is not None and self.state.control.session is not None:
            if self.state.provider_pause_replay:
                self.state.control.session.progress["provider_pause_replay"] = True
            else:
                self.state.control.session.progress.pop("provider_pause_replay", None)
            if response.prompt_tokens is not None:
                self.state.control.session.progress["context_tokens"] = response.prompt_tokens
            observations = self.state.control.session.progress.setdefault("model_observations", [])
            self.state.response_observation["sequence"] = self.state.control.session.sequence
            observations.append(self.state.response_observation)
            self.state.continuation_tools = self.state.definitions
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
        if response.incomplete_reason != "pause_turn":
            if (
                response.tool_calls
                and not self.state.continuation_native_tools
                and not response.native_tool_events
            ):
                return Continue()
            raise LLMIncompleteResponseError("provider response was incomplete")
        if response.continuation is None:
            raise LLMIncompleteResponseError("paused provider response has no resumable checkpoint")
        # Resume the paid server tool using its original checkpoint. The main
        # loop still owns the current request budget and cancellation boundary.
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
            self.state.control is not None
            and await self.state.control.has_pending_business_inputs()
        ):
            if response.continuation is None and not assistant_recorded:
                self.state.transcript.append(assistant_message)
            self.state.transcript.append(
                ChatMessage(
                    role="system",
                    content="有新的输入到达，请先处理再继续。",
                )
            )
            return Continue()
        if self.tools is not None:
            content = self.tools.finalize(content, self.runtime)
        control = self.state.control
        if control is not None and control.current is not None and control.ending is None:
            # The sanitized internal final uses the original complete decision.
            await control.complete_final(content, f"final-answer:{request_index}")
        if self.state.control is not None and self.state.control.session is not None:
            if response.continuation is None and not assistant_recorded:
                self.state.transcript.append(assistant_message)
            await self.state.control.session.save("paired")
        return End(
            AgentRunResult(
                text=content,
                tool_calls_used=self.state.calls_used,
                model_requests=request_index + 1,
                native_tool_events=tuple(self.state.native_events),
                citations=tuple(self.state.citations),
                response_status=self.state.response_status,
            )
        )

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
            remaining_calls=None
            if self.runtime.max_tool_calls is None
            else max(
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
        self.runner._record_failure_usage(
            self.tools, tool_calls=self.state.calls_used, model_requests=0
        )
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
            fact = self.state.coordinated.evidence.get(call.id, {})
            if (
                call.function.name == "web_search"
                and fact.get("ok") is True
                and self.runtime.work_control is not None
                and self.runtime.work_control.session is not None
            ):
                # Source snippets remain presentation material. Only the accepted
                # typed fact decides whether this execution succeeded.
                try:
                    display = json.loads(result)
                except ValueError:
                    display = None
                data = display.get("data") if isinstance(display, dict) else None
                sources = data.get("sources") if isinstance(data, dict) else None
                if isinstance(sources, list):
                    self.runtime.work_control.session.record_search_sources(
                        [
                            (source["url"], source.get("title", ""), source.get("snippet", ""))
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
                    ok=fact.get("ok") is True,
                )
                self.state.staged_evidence_results += 1
            logger.info(
                "agent_tool_complete tool=%s ok=%s error=%s reused=%s",
                call.function.name,
                fact.get("ok"),
                fact.get("error_code"),
                not _was_executed,
            )
            self.state.transcript.append_result(call.id, result)
            # The paired send receipt already carries these exact ledger events.
            self.state.observed_event_ids.update(fact.get("observed_event_ids", ()))
        self.state.transcript.append_tool_media(
            tuple((call.id, result) for call, result, _ in batch)
        )
        public_results = []
        for call, result, was_executed in batch:
            public_result = {
                "call_id": call.id,
                "name": call.function.name,
                "arguments": call.function.arguments,
                "output": result,
                "executed": was_executed,
            }
            public_results.append(public_result)
        self.state.response_observation["results"] = public_results
        if self.runtime.work_control is not None and self.runtime.work_control.session is not None:
            control = self.runtime.work_control
            assert control.session is not None
            await self.runtime.work_control.session.save("paired")
            if self.runtime.work_control.handoff_work_id is not None:
                return End(
                    AgentRunResult(
                        text="",
                        tool_calls_used=self.state.calls_used,
                        model_requests=request_index + 1,
                        suppress_delivery=True,
                        work_state="suspended",
                    )
                )
            if self.runtime.work_control.accepted_ending() is not None:
                # An accepted complete/fail/wait/need_input, direct or nested in
                # Code, ends this activation once its call is paired. The writer
                # commits the actual state; no further model request is bought.
                return End(self._accepted_result(request_index + 1))
            if self.runtime.work_control.ending in {
                "waiting_user",
                "waiting_external",
            }:
                return End(
                    AgentRunResult(
                        text="",
                        tool_calls_used=self.state.calls_used,
                        model_requests=request_index + 1,
                        suppress_delivery=True,
                        work_state=self.runtime.work_control.ending,
                    )
                )
        if self.state.coordinated.reused_count == len(batch) and batch:
            logger.info(
                "agent_tool_batch_reused reused_calls=%d tool_calls_used=%d",
                self.state.coordinated.reused_count,
                self.state.calls_used,
            )
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
            suppress_delivery=True,
            work_state="queued",
        )

    def _accepted_result(self, model_requests: int) -> AgentRunResult:
        control = self.runtime.work_control
        assert control is not None
        accepted = control.accepted or {}
        result = accepted.get("result")
        return AgentRunResult(
            text=result if isinstance(result, str) else "",
            tool_calls_used=self.state.calls_used,
            model_requests=model_requests,
            # The internal result returns to its owner; it is never sent by itself.
            suppress_delivery=True,
            work_state=control.accepted_ending(),
        )

    async def exhausted(
        self,
    ) -> AgentRunResult:
        control = self.runtime.work_control
        if control is not None and control.current is not None and control.accepted_ending():
            return self._accepted_result(control.requests_started)
        if self.runtime.work_control is not None and self.runtime.work_control.current is not None:
            self.runtime.work_control.yield_segment = True
            self.runtime.work_control.ending = "queued"
            if self.runtime.work_control.session is not None:
                await self.runtime.work_control.session.save("paired")
            return AgentRunResult(
                text="",
                tool_calls_used=self.state.calls_used,
                model_requests=self.runtime.work_control.requests_started,
                suppress_delivery=True,
                work_state="queued",
            )
        from yuki.runtime.work_budget import WorkBudgetExceeded

        raise WorkBudgetExceeded("agent_segment_budget_exhausted")
