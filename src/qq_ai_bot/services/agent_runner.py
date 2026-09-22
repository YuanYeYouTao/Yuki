"""Reusable bounded Chat Completions tool loop for user and scheduled turns."""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING, Any, Protocol, cast

from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.automation.authority import DelegatedAuthority
from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.capabilities.coordinator import (
    MISSING_TOOL_RESULT,
    CoordinatedToolResult,
    ToolInvocationCoordinator,
)
from qq_ai_bot.domain.messages import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ChatTool,
    ModelResponseStatus,
    NativeToolDefinition,
    NativeToolEvent,
    PromptRequestDiagnostics,
    ResponseCitation,
    ToolCall,
)
from qq_ai_bot.llm.base import (
    LLMEmptyResponseError,
    LLMError,
    LLMIncompleteResponseError,
    LLMTimeoutError,
    LLMUnavailableError,
)
from qq_ai_bot.model_runtime.executor import ModelCompleter, ModelExecutor, require_model_executor
from qq_ai_bot.model_runtime.models import ModelTask
from qq_ai_bot.runtime.activation_outcome import ActivationOutcome
from qq_ai_bot.runtime.execution_receipts import ExecutionReceipts, current_receipts
from qq_ai_bot.runtime.work_control import WORK_CONTROL_NAMES, WorkControl, WorkInputsPreparing
from qq_ai_bot.runtime.work_repository import WorkCapacityError
from qq_ai_bot.services.concurrency import ConcurrencyManager
from qq_ai_bot.services.evidence_observation import EVIDENCE_TOOLS, EvidenceObservation
from qq_ai_bot.services.native_tool_binder import NativeToolBinder
from qq_ai_bot.services.turn_transcript import TranscriptRequest, TurnTranscript, validating_request
from qq_ai_bot.time.models import TimeContext
from qq_ai_bot.web.models import WebMode

if TYPE_CHECKING:
    from qq_ai_bot.services.main_agent_contract import MainAgentContract

logger = logging.getLogger(__name__)


class _RequestNotStarted(Exception):
    def __init__(self, cause: LLMError) -> None:
        self.cause = cause
        super().__init__(str(cause))


@dataclass(frozen=True, slots=True)
class AgentRuntime:
    origin: TurnOrigin
    actor_user_id: str
    actor_is_superuser: bool
    delegated_authority: DelegatedAuthority | None
    conversation_key: str
    current_group_id: str | None
    bot_user_id: str
    gateway: object | None
    runtime_config: RuntimeConfigSnapshot
    current_time: TimeContext
    allowed_capabilities: frozenset[str]
    max_tool_calls: int
    max_model_requests: int
    prompt_diagnostics: PromptRequestDiagnostics | None = None
    before_model_request: Callable[[], Awaitable[None]] | None = None
    canonical_conversation_id: str | None = None
    dynamic_context_prepared: bool = False
    work_control: WorkControl | None = None
    execution_id: str | None = None
    fixed_tools: tuple[ChatTool, ...] | None = None
    context_token_limit: int | None = None
    invocation_source: dict[str, Any] | None = None
    invocation_goal: str | None = None


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    text: str
    tool_calls_used: int
    model_requests: int
    web_was_used: bool
    native_tool_events: tuple[NativeToolEvent, ...] = ()
    citations: tuple[ResponseCitation, ...] = ()
    response_status: ModelResponseStatus = ModelResponseStatus.COMPLETED
    suppress_delivery: bool = False
    work_state: str | None = None
    outcome: ActivationOutcome | None = None
    work_id: str | None = None


class AgentToolBackend(Protocol):
    def definitions(self, runtime: AgentRuntime, *, web_was_used: bool) -> tuple[ChatTool, ...]: ...

    def begin_batch(self, calls: tuple[ToolCall, ...], runtime: AgentRuntime) -> None: ...

    async def execute(self, name: str, arguments_json: str, runtime: AgentRuntime) -> str: ...

    def parallel_safe(self, name: str, runtime: AgentRuntime) -> bool: ...

    def is_side_effecting(
        self,
        name: str,
        arguments_json: str,
        runtime: AgentRuntime,
    ) -> bool: ...

    def finalize(self, content: str, runtime: AgentRuntime) -> str: ...

    def exhausted(self, runtime: AgentRuntime) -> str: ...


class AgentRunner:
    """Execute a provider-neutral bounded tool loop without fabricating inbound events."""

    def __init__(
        self,
        model_executor: ModelExecutor | ModelCompleter,
        concurrency: ConcurrencyManager,
        *,
        task: ModelTask = ModelTask.CHAT_AGENT,
    ) -> None:
        if callable(getattr(model_executor, "execute", None)):
            self._models = cast(ModelExecutor, model_executor)
        else:
            self._models = require_model_executor(
                None,
                provider=cast(ModelCompleter, model_executor),
            )
        self._concurrency = concurrency
        self._task = task
        self._tool_coordinator = ToolInvocationCoordinator()
        self._native_tools = NativeToolBinder()
        self.main_contract: MainAgentContract | None = None

    async def run(
        self,
        initial_messages: tuple[ChatMessage, ...],
        runtime: AgentRuntime,
        tools: AgentToolBackend | None,
    ) -> AgentRunResult:
        from qq_ai_bot.runtime.work_budget import WorkBudgetExceeded

        receipts = ExecutionReceipts()
        token = current_receipts.set(receipts)
        control = runtime.work_control
        if control is not None:
            control.segment_model_limit = runtime.max_model_requests
        try:
            try:
                result = await self._run(initial_messages, runtime, tools)
                if control is not None and control.current is not None:
                    result = replace(
                        result,
                        model_requests=control.requests_started,
                        work_id=control.current["id"],
                    )
                return result
            except ExceptionGroup as exc:
                budget_errors, other_errors = exc.split(WorkBudgetExceeded)
                if budget_errors is not None and other_errors is None:
                    raise WorkBudgetExceeded("work_total_budget_exhausted") from exc
                raise
        except (WorkBudgetExceeded, WorkCapacityError) as exc:
            if control is None or control.current is None:
                raise
            outcome = await control.recover_failure(exc)
            return AgentRunResult(
                text="",
                tool_calls_used=control.tools_started,
                model_requests=control.requests_started,
                web_was_used=False,
                suppress_delivery=True,
                work_state=control.ending,
                outcome=outcome,
            )
        except Exception as exc:
            if control is None or control.current is None:
                raise
            outcome = await control.recover_failure(exc)
            return AgentRunResult(
                text="",
                tool_calls_used=control.tools_started,
                model_requests=control.requests_started,
                web_was_used=False,
                suppress_delivery=True,
                work_state=control.ending,
                outcome=outcome,
            )
        finally:
            current_receipts.reset(token)

    async def _run(
        self,
        initial_messages: tuple[ChatMessage, ...],
        runtime: AgentRuntime,
        tools: AgentToolBackend | None,
    ) -> AgentRunResult:
        fixed_definitions = None
        if runtime.fixed_tools is not None:
            fixed_definitions = runtime.fixed_tools
        elif self.main_contract is not None:
            fixed_definitions = await self.main_contract.definitions()
            if not runtime.dynamic_context_prepared:
                raise LLMError("main_agent_composition_required")
            if tools is None:
                raise LLMError("main_agent_executor_required")
        transcript = TurnTranscript(initial_messages)
        evidence_observation = EvidenceObservation(runtime.origin.value)
        staged_evidence_results = 0
        calls_used = 0
        web_was_used = False
        empty_retries = 0
        mention_recovery_used = False
        answer_recovery_used = False
        native_events: list[NativeToolEvent] = []
        citations: list[ResponseCitation] = []
        response_status = ModelResponseStatus.COMPLETED
        incomplete_recovery_used = False
        continuation_tools: tuple[ChatTool, ...] = ()
        continuation_native_tools: tuple[NativeToolDefinition, ...] = ()
        previous_batch_fingerprint: tuple[tuple[str, str, str], ...] | None = None
        repeated_batch_count = 0
        no_progress_recovery = False
        reusable_tool_results: dict[tuple[str, str], str] = {}
        await self._prepare_tools(tools, runtime)
        if runtime.work_control is not None:
            from dataclasses import asdict

            from qq_ai_bot.runtime.work_session import WorkSession

            profile_revision = getattr(self._models, "profile_revision", None)
            contract = hashlib.sha256(
                json.dumps(
                    [
                        repr(fixed_definitions),
                        asdict(runtime.runtime_config.llm),
                        asdict(runtime.runtime_config.web),
                        profile_revision(self._task) if callable(profile_revision) else "legacy",
                        [(m.role, m.content) for m in initial_messages if m.role == "system"],
                    ],
                    sort_keys=True,
                    default=str,
                ).encode()
            ).hexdigest()
            runtime.work_control.session = WorkSession(runtime.work_control, contract)
            transcript = await runtime.work_control.session.restore(transcript)
            repeated_batch_count = int(runtime.work_control.session.progress.get("repeats", 0))
            if runtime.work_control.handoff_work_id is not None:
                await runtime.work_control.session.save("paired")
            if (
                runtime.work_control.session.recovered_delivery
                or runtime.work_control.handoff_work_id is not None
            ):
                return AgentRunResult(
                    text="",
                    tool_calls_used=0,
                    model_requests=0,
                    web_was_used=False,
                    suppress_delivery=True,
                )
        for request_index in range(runtime.max_model_requests):
            control = runtime.work_control
            if (
                control is not None
                and control.current is not None
                and control.requests_started >= runtime.max_model_requests
            ):
                break
            if control is not None:
                try:
                    added = await control.take_inputs(f"{transcript.chain_id}:{request_index}")
                except WorkInputsPreparing:
                    control.ending = "waiting_external"
                    return AgentRunResult(
                        text="",
                        suppress_delivery=True,
                        work_state="waiting_external",
                        tool_calls_used=calls_used,
                        model_requests=request_index,
                        web_was_used=web_was_used,
                    )
                for message in added:
                    transcript.append(message)
            definitions = (
                tools.definitions(runtime, web_was_used=web_was_used) if tools is not None else ()
            )
            if fixed_definitions is not None:
                definitions = fixed_definitions
            web_config = getattr(runtime.runtime_config, "web", None)
            try:
                web_mode = WebMode(getattr(web_config, "mode", WebMode.DISABLED.value))
            except ValueError:
                web_mode = WebMode.DISABLED
            web_search_selected = any(tool.name == "web_search" for tool in definitions)
            native_definitions = (
                self._native_tools.bind(
                    protocol=self._models.protocol(self._task),
                    capabilities=self._models.capabilities(self._task),
                    allowed_capabilities=runtime.allowed_capabilities,
                    web_mode=web_mode,
                    web_was_used=web_was_used,
                )
                if web_search_selected
                else ()
            )
            if web_mode is WebMode.NATIVE and fixed_definitions is None:
                # Native-only deliberately excludes external search. Mixed mode
                # keeps the pinned Tavily function alongside the native tool;
                # availability must not depend on a preceding native failure.
                definitions = tuple(
                    item for item in definitions if item.name not in {"web_search", "read_webpage"}
                )
            restart_chain = getattr(tools, "consume_provider_chain_restart", None)
            if callable(restart_chain):
                # Discovery/execution policy cannot discard a submitted request prefix.
                restart_chain()
            if transcript.continuation is not None:
                # Responses continuations are one cumulative request chain.
                # Tools may be added after request_tools, but removing a tool
                # previously declared in the chain makes some providers reject
                # the next function-output request with HTTP 400.
                definitions = self._merge_function_tools(continuation_tools, definitions)
                native_definitions = self._merge_native_tools(
                    continuation_native_tools, native_definitions
                )
            if (
                no_progress_recovery
                and transcript.continuation is None
                and fixed_definitions is None
            ):
                definitions = ()
                native_definitions = ()
            compacting = False
            if (
                control is not None
                and control.session is not None
                and control.current is not None
                and control.ending is None
            ):
                compacting = await control.session.needs_compaction() or bool(
                    control.session.progress.get("context_tokens", 0)
                    >= (runtime.context_token_limit or 131072) * 0.85
                )
                if compacting and not control.session.progress.get("compacting"):
                    control.session.progress["compacting"] = True
                    transcript.append(
                        ChatMessage(
                            role="user",
                            content=(
                                "[显式容量压缩] 本次只输出供原工作继续执行的摘要，不调用工具。"
                                "完整保留目标、追加要求、关键证据、文件/artifact 引用、验证结论、"
                                "待回答问题、未完成操作及原 run_id。区分已完成、失败和结果不确定，"
                                "不能把计划当成事实。控制在 16000 字以内。"
                            ),
                        )
                    )
            try:
                diagnostics = runtime.prompt_diagnostics
                sequence = transcript.request()
                request = ChatRequest(
                    messages=sequence.messages,
                    request_chain_id=transcript.chain_id,
                    continuation_items=sequence.items,
                    model=runtime.runtime_config.llm.model or "fake",
                    temperature=runtime.runtime_config.llm.temperature,
                    max_output_tokens=runtime.runtime_config.llm.max_output_tokens,
                    thinking_enabled=runtime.runtime_config.llm.thinking_enabled,
                    tools=definitions,
                    tool_choice=(
                        "none"
                        if (compacting or no_progress_recovery)
                        and (definitions or native_definitions)
                        else ("auto" if definitions or native_definitions else None)
                    ),
                    native_tools=native_definitions,
                    continuation=sequence.continuation,
                    conversation_prefix_hash=(
                        diagnostics.conversation_prefix_hash if diagnostics else ""
                    ),
                    prompt_snapshot_fingerprint=(
                        diagnostics.prompt_snapshot_fingerprint if diagnostics else ""
                    ),
                    static_prompt_revision=(
                        diagnostics.static_prompt_revision if diagnostics else ""
                    ),
                )
                evidence_observation.request(
                    request_index + 1,
                    definitions,
                    native_definitions,
                    finalization=False,
                    web_mode=web_mode.value,
                )
                execute = (
                    partial(
                        self._models.execute,
                        self._task,
                        request,
                        canonical_conversation_id=runtime.canonical_conversation_id,
                    )
                    if runtime.canonical_conversation_id is not None
                    else partial(self._models.execute, self._task, request)
                )

                async def dispatch(
                    execute: Callable[[], Awaitable[ChatResponse]] = execute,
                    sequence: TranscriptRequest = sequence,
                    request_count: int = request_index + 1,
                    prior_tools: int = calls_used,
                ) -> ChatResponse:
                    # Admission can wait behind other conversations. Validate
                    # only after acquiring the slot, immediately before execution.
                    if runtime.before_model_request is not None:
                        try:
                            with validating_request(sequence):
                                await runtime.before_model_request()
                        except LLMError as exc:
                            raise _RequestNotStarted(exc) from exc
                    if runtime.work_control is not None:
                        await runtime.work_control.reserve_request()
                        if runtime.work_control.session is not None:
                            await runtime.work_control.session.save("dispatched")
                    return await execute()

                response = await self._concurrency.run_llm(
                    runtime.conversation_key,
                    dispatch,
                    background=(
                        runtime.origin
                        in {
                            TurnOrigin.SCHEDULED_AUTOMATION,
                            TurnOrigin.PLUGIN_BACKGROUND,
                            TurnOrigin.PLUGIN_SESSION,
                        }
                        or bool(runtime.work_control and runtime.work_control.lease.work_id)
                    ),
                )
                if runtime.work_control is not None:
                    await runtime.work_control.confirm_inputs()
                receipts = current_receipts.get()
                if receipts is not None:
                    await receipts.confirm()
                # A prepared request may be cancelled while waiting for the LLM
                # slot or rejected by the transport budget before dispatch.
                # Confirm conservatively only after a response was received.
                if tools is not None:
                    confirm_exposure = getattr(tools, "confirm_memory_prompt_exposure", None)
                    if callable(confirm_exposure):
                        try:
                            await confirm_exposure()
                        except Exception as exc:
                            evidence_observation.emit(
                                "exposure_confirmation_failed", category=type(exc).__name__
                            )
                evidence_observation.emit(
                    "response_received",
                    request_index=request_index + 1,
                    confirmed_prior_results=staged_evidence_results,
                    native_completed=sum(
                        event.status.value == "completed" for event in response.native_tool_events
                    ),
                    native_failed=sum(
                        event.status.value == "failed" for event in response.native_tool_events
                    ),
                    source_count=len(response.citations),
                )
                staged_evidence_results = 0
            except _RequestNotStarted as exc:
                self._record_failure_usage(
                    tools, tool_calls=calls_used, model_requests=request_index
                )
                raise exc.cause from exc
            except (LLMTimeoutError, LLMUnavailableError):
                self._record_failure_usage(
                    tools, tool_calls=calls_used, model_requests=request_index + 1
                )
                raise
            except LLMEmptyResponseError:
                has_visible_effects = bool(
                    tools is not None
                    and callable(getattr(tools, "has_visible_effects", None))
                    and tools.has_visible_effects()  # type: ignore[attr-defined]
                )
                if has_visible_effects and (control is None or control.ending == "completed"):
                    return AgentRunResult(
                        text="",
                        tool_calls_used=calls_used,
                        model_requests=request_index + 1,
                        web_was_used=web_was_used,
                        native_tool_events=tuple(native_events),
                        citations=tuple(citations),
                        response_status=response_status,
                    )
                if empty_retries >= 2 or request_index + 1 >= runtime.max_model_requests:
                    self._record_failure_usage(
                        tools, tool_calls=calls_used, model_requests=request_index + 1
                    )
                    raise
                empty_retries += 1
                logger.warning(
                    "agent_empty_response_retry retry=%d tool_calls_used=%d",
                    empty_retries,
                    calls_used,
                )
                transcript.append(
                    ChatMessage(
                        role="system",
                        content=(
                            "上一次模型请求返回了空内容。请继续当前同一轮任务：如果已有工具"
                            "结果，先核对结果再给出简短、真实的最终答复；如果任务尚未完成，"
                            "继续调用必要工具。不得声称未成功的操作已经完成。"
                        ),
                    )
                )
                continue
            except LLMError:
                self._record_failure_usage(
                    tools, tool_calls=calls_used, model_requests=request_index + 1
                )
                raise
            native_events.extend(response.native_tool_events)
            citations.extend(response.citations)
            response_status = response.status
            if response.native_tool_events:
                web_was_used = True
                mark_native_web = getattr(tools, "mark_native_web_used", None)
                if callable(mark_native_web):
                    mark_native_web()
            if response.continuation is not None:
                transcript.accept(response.continuation)
            if control is not None and control.session is not None:
                if control.lease.work_id:
                    last_tokens = control.session.progress.get("context_tokens", 0)
                    samples = control.session.progress.setdefault("cache_samples", [])
                    samples.append(
                        {
                            "sequence": control.session.sequence,
                            "chain_id": transcript.chain_id,
                            "kind": "compaction"
                            if compacting
                            else ("resume" if request_index == 0 and last_tokens else "execution"),
                            "input": response.prompt_tokens,
                            "cached": response.cached_prompt_tokens,
                            "warm_candidate": bool(
                                response.prompt_tokens
                                and last_tokens >= response.prompt_tokens * 0.95
                            ),
                        }
                    )
                if response.prompt_tokens is not None:
                    control.session.progress["context_tokens"] = response.prompt_tokens
                if compacting:
                    if response.tool_calls or response.status != ModelResponseStatus.COMPLETED:
                        raise ValueError("worker_compaction_incomplete")
                    transcript = await control.session.compact(response.content)
                    control.session.progress["context_tokens"] = 0
                    continue
                continuation_tools = definitions
                continuation_native_tools = native_definitions
            if response.status is ModelResponseStatus.INCOMPLETE:
                # Truncated calls never execute. Pair non-execution receipts before
                # recovery so either protocol retains a valid, append-only history.
                if response.continuation is None:
                    transcript.append(
                        ChatMessage(
                            role="assistant",
                            content=response.content or None,
                            tool_calls=response.tool_calls,
                            reasoning_content=response.reasoning_content,
                        )
                    )
                for call in response.tool_calls:
                    transcript.append_result(
                        call.id,
                        json.dumps(
                            {
                                "ok": False,
                                "error": "provider_response_incomplete",
                                "executed": False,
                                "mutation_committed": False,
                            }
                        ),
                    )
                if control is not None and control.session is not None:
                    await control.session.save("paired")
                if incomplete_recovery_used or request_index + 1 >= runtime.max_model_requests:
                    raise LLMIncompleteResponseError(
                        "provider response remained incomplete after bounded recovery"
                    )
                incomplete_recovery_used = True
                transcript.append(
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
                continue
            if not response.tool_calls:
                content = response.content
                assistant_message = ChatMessage(
                    role="assistant",
                    content=response.content,
                    reasoning_content=response.reasoning_content,
                )
                control = runtime.work_control
                if control is not None and await control.pending():
                    if response.continuation is None:
                        transcript.append(assistant_message)
                    transcript.append(
                        ChatMessage(
                            role="system",
                            content=(
                                "上一段回复尚未发送；有新的用户输入到达，请先处理新增内容再继续。"
                            ),
                        )
                    )
                    continue
                # The final body is internal; the main backend can reject an
                # unsent user-facing answer without implicitly delivering it.
                if "[提及" in content:
                    if (
                        not mention_recovery_used
                        and request_index + 1 < runtime.max_model_requests
                        and any(tool.name == "send_message" for tool in definitions)
                    ):
                        mention_recovery_used = True
                        if response.continuation is None:
                            transcript.append(assistant_message)
                        transcript.append(
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
                        continue
                    raise LLMError("model repeated an invalid mention placeholder")
                feedback = getattr(tools, "response_feedback", None)
                issue = feedback(content, runtime) if callable(feedback) else None
                if issue:
                    if answer_recovery_used or request_index + 1 >= runtime.max_model_requests:
                        raise LLMError("model repeated an unsupported final response")
                    answer_recovery_used = True
                    if response.continuation is None and not response.tool_calls:
                        transcript.append(assistant_message)
                    transcript.append(ChatMessage(role="system", content=issue))
                    continue
                if tools is not None:
                    content = tools.finalize(content, runtime)
                has_visible_effects = bool(
                    tools is not None
                    and callable(getattr(tools, "has_visible_effects", None))
                    and tools.has_visible_effects()  # type: ignore[attr-defined]
                )
                if not content.strip() and not has_visible_effects:
                    allow_silence = getattr(tools, "allow_silent_final", None)
                    if callable(allow_silence) and allow_silence(runtime):
                        logger.info("agent_silent_final origin=%s", runtime.origin.value)
                    else:
                        if empty_retries >= 2 or request_index + 1 >= runtime.max_model_requests:
                            raise LLMEmptyResponseError("model returned no final answer")
                        empty_retries += 1
                        logger.warning(
                            "agent_empty_final_retry retry=%d tool_calls_used=%d",
                            empty_retries,
                            calls_used,
                        )
                        if response.continuation is None:
                            transcript.append(assistant_message)
                        transcript.append(
                            ChatMessage(
                                role="system",
                                content=(
                                    "上一响应正文为空；回执仍保留，不能据此断言整个任务完成。"
                                    "根据目标和真实结果选择继续执行、等待或回答；"
                                    "不要重复已经成功的工具调用，也不要只描述发送模式。"
                                ),
                            )
                        )
                        continue
                if control is not None and control.current is not None and control.ending is None:
                    # Infer lifecycle completion from a real final answer, but use
                    # the same receipt validation as explicit task_control.complete.
                    await control.reconcile_completed_children()
                    state = await control.background_state()
                    if any(effect.get("uncertain") for effect in control.known_effects):
                        state = "suspended"
                    elif any(effect.get("pending") for effect in control.known_effects):
                        state = state or "waiting_external"
                    if state is not None:
                        control.ending = state
                    else:
                        artifacts = list(
                            dict.fromkeys(
                                artifact
                                for effect in control.known_effects
                                if effect.get("ok") or effect.get("delivered_artifacts")
                                for artifact in effect.get("artifacts", [])
                            )
                        )[-8:]
                        receipt = await control.execute(
                            "task_control",
                            {"action": "complete", "artifact_ids": artifacts},
                            f"final-answer:{request_index}",
                        )
                        if not json.loads(receipt).get("ok"):
                            if response.continuation is None:
                                transcript.append(assistant_message)
                            transcript.append(ChatMessage(role="system", content=receipt))
                            continue
                if control is not None and control.session is not None:
                    if response.continuation is None:
                        transcript.append(assistant_message)
                    await control.session.save("paired")
                return AgentRunResult(
                    text=content,
                    tool_calls_used=calls_used,
                    model_requests=request_index + 1,
                    web_was_used=web_was_used,
                    native_tool_events=tuple(native_events),
                    citations=tuple(citations),
                    response_status=response_status,
                )
            if no_progress_recovery:
                logger.warning(
                    "agent_tool_no_progress_stopped tool_calls_used=%d model_requests=%d",
                    calls_used,
                    request_index + 1,
                )
                return AgentRunResult(
                    text=("检测到模型反复调用相同工具且结果没有变化，已停止本轮工具循环。"),
                    tool_calls_used=calls_used,
                    model_requests=request_index + 1,
                    web_was_used=web_was_used,
                    native_tool_events=tuple(native_events),
                    citations=tuple(citations),
                    response_status=response_status,
                )
            responses_path = response.continuation is not None
            if not responses_path:
                transcript.append(
                    ChatMessage(
                        role="assistant",
                        content=response.content or None,
                        tool_calls=response.tool_calls,
                        reasoning_content=response.reasoning_content,
                    )
                )
            if runtime.work_control is not None and runtime.work_control.session is not None:
                await runtime.work_control.session.save("response", response.tool_calls)
            tooling = getattr(runtime.runtime_config, "tooling", None)
            coordinated = await self._execute_tool_batch(
                response.tool_calls,
                tools,
                runtime,
                remaining_calls=max(
                    0,
                    runtime.max_tool_calls
                    - max(
                        calls_used,
                        runtime.work_control.tools_started
                        if runtime.work_control is not None
                        and runtime.work_control.current is not None
                        else 0,
                    ),
                ),
                max_parallel_calls=tooling.max_parallel_calls if tooling is not None else 1,
                reusable_results=reusable_tool_results,
                cacheable_names=frozenset(t.name for t in definitions if t.result_cacheable),
                declared_names=frozenset(t.name for t in definitions),
            )
            batch, executed = coordinated.calls, coordinated.executed_count
            calls_used += executed
            for call, result, _was_executed in batch:
                try:
                    outcome = json.loads(result)
                except json.JSONDecodeError:
                    outcome = {}
                if call.function.name in EVIDENCE_TOOLS:
                    evidence_observation.emit(
                        "tool_result_staged",
                        request_index=request_index + 1,
                        tool=call.function.name,
                        reused=not _was_executed,
                        ok=isinstance(outcome, dict) and outcome.get("ok") is True,
                    )
                    staged_evidence_results += 1
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
                transcript.append_result(call.id, result)
                if runtime.work_control is not None:
                    runtime.work_control.observe_result(
                        call.function.name,
                        result,
                        _was_executed,
                        side_effecting=self._is_side_effecting(tools, call, runtime),
                        arguments=call.function.arguments,
                    )
            if runtime.work_control is not None and runtime.work_control.session is not None:
                batch_hash = hashlib.sha256(
                    json.dumps(
                        [
                            (call.function.name, self._tool_call_signature(call)[1], result)
                            for call, result, _ in batch
                        ],
                        sort_keys=True,
                    ).encode()
                ).hexdigest()
                persisted_progress = runtime.work_control.session.progress
                repeats = (
                    int(persisted_progress.get("repeats", 0)) + 1
                    if (
                        batch
                        and persisted_progress.get("fingerprint") == batch_hash
                        and not any(self._tool_result_pending(result) for _, result, _ in batch)
                    )
                    else 0
                )
                persisted_progress.update(fingerprint=batch_hash, repeats=repeats)
                await runtime.work_control.session.save("paired")
                if runtime.work_control.handoff_work_id is not None:
                    return AgentRunResult(
                        text="",
                        tool_calls_used=calls_used,
                        model_requests=request_index + 1,
                        web_was_used=web_was_used,
                        suppress_delivery=True,
                        work_state="suspended",
                    )
                if (
                    runtime.work_control.ending == "completed"
                    and runtime.work_control.source.get("delivery_contract") != "return_to_caller"
                    and not runtime.work_control.lease.work_id
                    and not await runtime.work_control.pending()
                ):
                    runtime.work_control.final_delivery = True
                    await runtime.work_control.session.save("delivered")
                    return AgentRunResult(
                        text="",
                        tool_calls_used=calls_used,
                        model_requests=request_index + 1,
                        web_was_used=web_was_used,
                        suppress_delivery=True,
                        work_state="completed",
                    )
                if (
                    runtime.work_control.lease.work_id
                    or runtime.work_control.source.get("delivery_contract") == "return_to_caller"
                ) and runtime.work_control.ending in {
                    "waiting_user",
                    "waiting_external",
                }:
                    return AgentRunResult(
                        text="",
                        tool_calls_used=calls_used,
                        model_requests=request_index + 1,
                        web_was_used=web_was_used,
                        suppress_delivery=True,
                        work_state=runtime.work_control.ending,
                    )
            fingerprint = tuple(
                (call.function.name, self._tool_call_signature(call)[1], result)
                for call, result, _was_executed in batch
            )
            pending_work = any(self._tool_result_pending(result) for _, result, _ in batch)
            if fingerprint and fingerprint == previous_batch_fingerprint and not pending_work:
                repeated_batch_count += 1
            else:
                repeated_batch_count = 0
            previous_batch_fingerprint = fingerprint
            if runtime.work_control is not None and runtime.work_control.session is not None:
                repeated_batch_count = int(runtime.work_control.session.progress.get("repeats", 0))
            if coordinated.reused_count == len(batch) and batch:
                logger.info(
                    "agent_tool_batch_reused reused_calls=%d tool_calls_used=%d",
                    coordinated.reused_count,
                    calls_used,
                )
            if repeated_batch_count >= 2:
                if runtime.work_control is not None and runtime.work_control.current is not None:
                    from qq_ai_bot.runtime.activation_outcome import WorkNoProgress

                    raise WorkNoProgress("repeated_tool_results")
                no_progress_recovery = True
                logger.warning(
                    "agent_tool_no_progress_detected repeated_batches=%d tool_calls_used=%d",
                    repeated_batch_count,
                    calls_used,
                )
                if transcript.continuation is None:
                    transcript.append(
                        ChatMessage(
                            role="system",
                            content=(
                                "相同工具调用已经连续返回相同结果。停止调用工具，"
                                "只根据已有结果给出简短、真实的最终答复。"
                            ),
                        )
                    )
            if tools is not None:
                effect_probe = getattr(tools, "did_use_web", None)
                if callable(effect_probe) and effect_probe():
                    web_was_used = True
            if (
                runtime.work_control is not None
                and runtime.work_control.tools_started >= runtime.max_tool_calls
            ):
                break
        if runtime.work_control is not None and runtime.work_control.current is not None:
            runtime.work_control.yield_segment = True
            runtime.work_control.ending = "queued"
            if runtime.work_control.session is not None:
                await runtime.work_control.session.save("paired")
            return AgentRunResult(
                text="",
                tool_calls_used=calls_used,
                model_requests=runtime.work_control.requests_started,
                web_was_used=web_was_used,
                suppress_delivery=True,
                work_state="queued",
            )
        exhausted = (
            tools.exhausted(runtime) if tools is not None else "工具调用次数过多，Agent 已停止。"
        )
        return AgentRunResult(
            text=exhausted,
            tool_calls_used=calls_used,
            model_requests=runtime.max_model_requests,
            web_was_used=web_was_used,
            native_tool_events=tuple(native_events),
            citations=tuple(citations),
            response_status=response_status,
        )

    async def _execute_tool_batch(
        self,
        calls: tuple[ToolCall, ...],
        tools: AgentToolBackend | None,
        runtime: AgentRuntime,
        *,
        remaining_calls: int,
        max_parallel_calls: int,
        reusable_results: dict[tuple[str, str], str],
        cacheable_names: frozenset[str],
        declared_names: frozenset[str],
    ) -> CoordinatedToolResult:
        """Execute each semantic call once and fan its result out to duplicate IDs."""

        control = runtime.work_control
        control_calls = [call for call in calls if call.function.name in WORK_CONTROL_NAMES]
        if control_calls:
            if len(calls) != 1:
                result = json.dumps({"ok": False, "error": "work_control_requires_single_call"})
                return CoordinatedToolResult(
                    calls=tuple((call, result, False) for call in calls),
                    executed_count=0,
                    reused_count=0,
                )
            call = calls[0]
            control = runtime.work_control
            allowed = getattr(tools, "work_control_allowed", None)
            if (
                call.function.name not in declared_names
                or control is None
                or (callable(allowed) and not allowed(call.function.name))
            ):
                result = json.dumps({"ok": False, "error": "work_control_unavailable"})
                executed = False
            else:
                try:
                    arguments = json.loads(call.function.arguments)
                    if not isinstance(arguments, dict):
                        raise ValueError("arguments must be an object")
                except (ValueError, TypeError):
                    result = json.dumps({"ok": False, "error": "invalid_work_arguments"})
                    executed = False
                else:
                    result = await control.execute(
                        call.function.name,
                        arguments,
                        control.session.call_key(call.id)
                        if control.session
                        else f"{control.lease.owner}:{call.id}",
                    )
                    executed = True
            return CoordinatedToolResult(
                calls=((call, result, executed),),
                # Lifecycle controls use the model and message budgets, not
                # the caller's delegated business-tool execution allowance.
                executed_count=0,
                reused_count=0,
            )

        work_admission_blocked = {
            call.id
            for call in calls
            if control is not None
            and control.current is None
            and call.function.name != "send_message"
            and self._is_side_effecting(tools, call, runtime)
        }

        signatures = {call.id: self._tool_call_signature(call) for call in calls}
        first_call_by_signature: dict[tuple[str, str], ToolCall] = {}
        reused_by_id: dict[str, str] = {}
        rejected_by_id: dict[str, str] = {}
        aliases: dict[str, str] = {}
        unique_calls: list[ToolCall] = []
        for call in calls:
            if call.id in work_admission_blocked:
                rejected_by_id[call.id] = json.dumps(
                    {"ok": False, "error": "accept_work_before_execution"}
                )
                continue
            if call.function.name not in declared_names:
                rejected_by_id[call.id] = json.dumps(
                    {
                        "ok": False,
                        "error": "tool_not_declared",
                        "detail": "Tool is not part of this request's declared manifest.",
                    }
                )
                continue
            signature = signatures[call.id]
            side_effecting = self._is_side_effecting(tools, call, runtime)
            cached = (
                reusable_results.get(signature)
                if not side_effecting and call.function.name in cacheable_names
                else None
            )
            if cached is not None:
                reused_by_id[call.id] = cached
                continue
            representative = None if side_effecting else first_call_by_signature.get(signature)
            if representative is not None:
                aliases[call.id] = representative.id
                continue
            if not side_effecting:
                first_call_by_signature[signature] = call
            unique_calls.append(call)

        if tools is not None:
            write_calls = [call for call in unique_calls if call.function.name == "memory_change"]
            if write_calls:
                conflicting = [
                    call
                    for call in unique_calls
                    if call.function.name != "memory_change"
                    and self._is_side_effecting(tools, call, runtime)
                ]
                non_delivery_conflicts = [
                    call for call in conflicting if call.function.name != "send_message"
                ]
                if non_delivery_conflicts:
                    violation = json.dumps(
                        {
                            "ok": False,
                            "error": "memory_mutation_exclusive_violation",
                            "detail": "记忆写入批次不能夹带其他副作用工具。",
                        },
                        ensure_ascii=False,
                    )
                    return CoordinatedToolResult(
                        calls=tuple((call, violation, False) for call in calls),
                        executed_count=0,
                        reused_count=0,
                    )
                for call in conflicting:
                    rejected_by_id[call.id] = json.dumps(
                        {
                            "ok": False,
                            "error": "delivery_requires_observed_result",
                            "executed": False,
                            "detail": "先观察记忆写入的真实回执，再决定要发送的内容。",
                        },
                        ensure_ascii=False,
                    )
                unique_calls = [
                    call for call in unique_calls if call.function.name != "send_message"
                ]
            tools.begin_batch(tuple(unique_calls), runtime)
        coordinated = await self._tool_coordinator.execute_batch(
            tuple(unique_calls),
            tools,
            runtime,
            remaining_calls=remaining_calls,
            max_parallel_calls=max_parallel_calls,
        )
        unique_results = {call.id: result for call, result, _executed in coordinated.calls}
        unique_executed = {call.id: executed for call, _result, executed in coordinated.calls}

        ordered: list[tuple[ToolCall, str, bool]] = []
        for call in calls:
            if call.id in rejected_by_id:
                ordered.append((call, rejected_by_id[call.id], False))
                continue
            if call.id in reused_by_id:
                ordered.append((call, reused_by_id[call.id], False))
                continue
            representative_id = aliases.get(call.id, call.id)
            payload = unique_results.get(representative_id)
            if payload is None:
                logger.error("tool_result_missing call_id=%s", call.id)
                ordered.append((call, MISSING_TOOL_RESULT, False))
                continue
            ordered.append(
                (
                    call,
                    payload,
                    unique_executed.get(representative_id, False)
                    if representative_id == call.id
                    else False,
                )
            )

        for call, result, executed in coordinated.calls:
            if not executed or not self._tool_result_reusable(result):
                continue
            signature = signatures[call.id]
            if self._successful_side_effect(tools, call, result, runtime):
                reusable_results.clear()
            elif (
                not self._is_side_effecting(tools, call, runtime)
                and call.function.name in cacheable_names
            ):
                reusable_results[signature] = result

        return CoordinatedToolResult(
            calls=tuple(ordered),
            executed_count=coordinated.executed_count,
            reused_count=len(reused_by_id) + len(aliases),
        )

    @staticmethod
    def _tool_call_signature(call: ToolCall) -> tuple[str, str]:
        try:
            arguments = json.loads(call.function.arguments)
        except json.JSONDecodeError:
            normalized = call.function.arguments.strip()
        else:
            normalized = json.dumps(
                arguments,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        return call.function.name, normalized

    @staticmethod
    def _tool_result_pending(result: str) -> bool:
        try:
            payload = json.loads(result)
        except json.JSONDecodeError:
            return False
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            return False
        data = payload.get("data")
        return isinstance(data, dict) and data.get("pending") is True

    @classmethod
    def _tool_result_reusable(cls, result: str) -> bool:
        try:
            payload = json.loads(result)
        except json.JSONDecodeError:
            return False
        return bool(
            isinstance(payload, dict)
            and payload.get("ok") is True
            and payload.get("retryable") is not True
            and not cls._tool_result_pending(result)
        )

    @staticmethod
    def _successful_side_effect(
        tools: AgentToolBackend | None,
        call: ToolCall,
        result: str,
        runtime: AgentRuntime,
    ) -> bool:
        if tools is None:
            return False
        try:
            payload = json.loads(result)
        except json.JSONDecodeError:
            payload = None
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            return False
        committed = payload.get("mutation_committed")
        if committed is not None:
            return committed is True
        probe = getattr(tools, "is_side_effecting", None)
        return bool(callable(probe) and probe(call.function.name, call.function.arguments, runtime))

    @staticmethod
    def _is_side_effecting(
        tools: AgentToolBackend | None,
        call: ToolCall,
        runtime: AgentRuntime,
    ) -> bool:
        if tools is None:
            return False
        probe = getattr(tools, "is_side_effecting", None)
        return bool(callable(probe) and probe(call.function.name, call.function.arguments, runtime))

    @staticmethod
    async def _prepare_tools(tools: AgentToolBackend | None, runtime: AgentRuntime) -> None:
        if tools is None:
            return
        prepare = getattr(tools, "prepare", None)
        if not callable(prepare):
            return
        result = prepare(runtime)
        if inspect.isawaitable(result):
            await result

    @staticmethod
    def _record_failure_usage(
        tools: AgentToolBackend | None,
        *,
        tool_calls: int,
        model_requests: int,
    ) -> None:
        recorder = getattr(tools, "record_failure_usage", None)
        if callable(recorder):
            recorder(tool_calls=tool_calls, model_requests=model_requests)

    @staticmethod
    def _merge_function_tools(
        previous: tuple[ChatTool, ...],
        current: tuple[ChatTool, ...],
    ) -> tuple[ChatTool, ...]:
        merged = {item.name: item for item in previous}
        for item in current:
            existing = merged.get(item.name)
            if existing is None or existing.parameters == item.parameters:
                merged[item.name] = item
            # Responses declared schemas are append-only; never silently replace.
        return tuple(sorted(merged.values(), key=lambda item: item.name))

    @staticmethod
    def _merge_native_tools(
        previous: tuple[NativeToolDefinition, ...],
        current: tuple[NativeToolDefinition, ...],
    ) -> tuple[NativeToolDefinition, ...]:
        merged = {item.type: item for item in previous}
        merged.update({item.type: item for item in current})
        return tuple(sorted(merged.values(), key=lambda item: item.type))
