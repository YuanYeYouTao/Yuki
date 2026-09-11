"""Reusable bounded Chat Completions tool loop for user and scheduled turns."""

from __future__ import annotations

import inspect
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Protocol, cast

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
from qq_ai_bot.services.concurrency import ConcurrencyManager
from qq_ai_bot.services.evidence_observation import EVIDENCE_TOOLS, EvidenceObservation
from qq_ai_bot.services.native_tool_binder import NativeToolBinder
from qq_ai_bot.services.turn_transcript import TurnTranscript
from qq_ai_bot.time.models import TimeContext
from qq_ai_bot.web.models import WebMode

if TYPE_CHECKING:
    from qq_ai_bot.services.main_agent_contract import MainAgentContract

logger = logging.getLogger(__name__)


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

    def post_commit_recovery_text(self) -> str | None: ...


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
        fixed_definitions = None
        if self.main_contract is not None:
            fixed_definitions = await self.main_contract.definitions()
            initial_messages = await self.main_contract.state.inject(initial_messages)
            if tools is None:
                from qq_ai_bot.services.main_agent_contract import ShortStateOnlyBackend

                tools = ShortStateOnlyBackend(self.main_contract.state)
        transcript = TurnTranscript(initial_messages)
        evidence_observation = EvidenceObservation(runtime.origin.value)
        staged_evidence_results = 0
        calls_used = 0
        web_was_used = False
        empty_retries = 0
        mention_recovery_used = False
        native_events: list[NativeToolEvent] = []
        citations: list[ResponseCitation] = []
        response_status = ModelResponseStatus.COMPLETED
        incomplete_recovery_used = False
        continuation_tools: tuple[ChatTool, ...] = ()
        continuation_native_tools: tuple[NativeToolDefinition, ...] = ()
        previous_batch_fingerprint: tuple[tuple[str, str, str], ...] | None = None
        repeated_batch_count = 0
        no_progress_recovery = False
        force_finalization = False
        reusable_tool_results: dict[tuple[str, str], str] = {}
        finalization_prompt_added = False
        await self._prepare_tools(tools, runtime)
        for request_index in range(runtime.max_model_requests):
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
            finalization_only = force_finalization or (
                request_index + 1 >= runtime.max_model_requests
            )
            if finalization_only:
                # Chat Completions can omit tools entirely. Responses continuations
                # must retain every previously declared schema, so keep those
                # definitions but force tool_choice=none below.
                if transcript.continuation is None and fixed_definitions is None:
                    definitions = ()
                    native_definitions = ()
                if not finalization_prompt_added:
                    transcript.append(
                        ChatMessage(
                            role="system",
                            content=(
                                "这是本轮预留的最终回复请求。不得继续调用工具；请只根据"
                                "当前对话和已经取得的工具结果，给出简短、真实的最终答复。"
                                "不得声称未成功的操作已经完成。"
                            ),
                        )
                    )
                    finalization_prompt_added = True
            if (
                incomplete_recovery_used
                and transcript.continuation is None
                and fixed_definitions is None
            ):
                definitions = ()
                native_definitions = ()
            if (
                no_progress_recovery
                and transcript.continuation is None
                and fixed_definitions is None
            ):
                definitions = ()
                native_definitions = ()
            try:
                if runtime.before_model_request is not None:
                    await runtime.before_model_request()
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
                        if (finalization_only or incomplete_recovery_used or no_progress_recovery)
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
                    finalization=finalization_only,
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
                response = await self._concurrency.run_llm(
                    runtime.conversation_key,
                    execute,
                )
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
            except (LLMTimeoutError, LLMUnavailableError) as exc:
                recovered = self._recover_committed_mutation(
                    tools,
                    calls_used=calls_used,
                    model_requests=request_index + 1,
                    web_was_used=web_was_used,
                    native_events=native_events,
                    citations=citations,
                    response_status=response_status,
                    exception_category=type(exc).__name__,
                )
                if recovered is not None:
                    return recovered
                self._record_failure_usage(
                    tools, tool_calls=calls_used, model_requests=request_index + 1
                )
                raise
            except LLMEmptyResponseError as exc:
                recovered = self._recover_committed_mutation(
                    tools,
                    calls_used=calls_used,
                    model_requests=request_index + 1,
                    web_was_used=web_was_used,
                    native_events=native_events,
                    citations=citations,
                    response_status=response_status,
                    exception_category=type(exc).__name__,
                )
                if recovered is not None:
                    return recovered
                has_visible_effects = bool(
                    tools is not None
                    and callable(getattr(tools, "has_visible_effects", None))
                    and tools.has_visible_effects()  # type: ignore[attr-defined]
                )
                if has_visible_effects:
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
            except LLMError as exc:
                recovered = self._recover_committed_mutation(
                    tools,
                    calls_used=calls_used,
                    model_requests=request_index + 1,
                    web_was_used=web_was_used,
                    native_events=native_events,
                    citations=citations,
                    response_status=response_status,
                    exception_category=type(exc).__name__,
                )
                if recovered is not None:
                    return recovered
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
                continuation_tools = definitions
                continuation_native_tools = native_definitions
            if response.status is ModelResponseStatus.INCOMPLETE:
                if incomplete_recovery_used or request_index + 1 >= runtime.max_model_requests:
                    recovered = self._recover_committed_mutation(
                        tools,
                        calls_used=calls_used,
                        model_requests=request_index + 1,
                        web_was_used=web_was_used,
                        native_events=native_events,
                        citations=citations,
                        response_status=response_status,
                        exception_category=LLMIncompleteResponseError.__name__,
                    )
                    if recovered is not None:
                        return recovered
                    raise LLMIncompleteResponseError(
                        "provider response remained incomplete after bounded recovery"
                    )
                incomplete_recovery_used = True
                transcript.append(
                    ChatMessage(
                        role="system",
                        content=(
                            "上一响应未完整结束。只根据本轮已有结果给出简短最终答复；"
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
                if "[提及" in content:
                    if (
                        not mention_recovery_used
                        and calls_used == 0
                        and not finalization_only
                        and request_index + 2 < runtime.max_model_requests
                        and any(tool.name == "send_group_message" for tool in definitions)
                    ):
                        mention_recovery_used = True
                        transcript.append(
                            ChatMessage(
                                role="system",
                                content=(
                                    "上一回复含 [提及…] 历史占位标记，已拦截且未发送；"
                                    "本轮尚未调用工具，不能声称已 @。若用户要求提醒成员，"
                                    "先明确人物，再用 send_group_message.mentions 发送；"
                                    "普通正文和 @名字都不能触发提醒。无法执行时如实说明。"
                                ),
                            )
                        )
                        continue
                    content = "这段回复没有形成有效的 @ 提醒。"
                if tools is not None:
                    content = tools.finalize(content, runtime)
                has_visible_effects = bool(
                    tools is not None
                    and callable(getattr(tools, "has_visible_effects", None))
                    and tools.has_visible_effects()  # type: ignore[attr-defined]
                )
                if not content.strip() and not has_visible_effects:
                    recovered = self._recover_committed_mutation(
                        tools,
                        calls_used=calls_used,
                        model_requests=request_index + 1,
                        web_was_used=web_was_used,
                        native_events=native_events,
                        citations=citations,
                        response_status=response_status,
                        exception_category=LLMEmptyResponseError.__name__,
                    )
                    if recovered is not None:
                        return recovered
                    if empty_retries >= 2 or request_index + 1 >= runtime.max_model_requests:
                        raise LLMEmptyResponseError("model returned no final answer")
                    empty_retries += 1
                    logger.warning(
                        "agent_empty_final_retry retry=%d tool_calls_used=%d",
                        empty_retries,
                        calls_used,
                    )
                    transcript.append(
                        ChatMessage(
                            role="system",
                            content=(
                                "你已经完成了本轮所需的工具调用，但最终回复正文为空。"
                                "请根据已有工具结果生成实际要发送给用户的简短正文；"
                                "不要重复已经成功的工具调用，也不要只描述发送模式。"
                            ),
                        )
                    )
                    continue
                return AgentRunResult(
                    text=content,
                    tool_calls_used=calls_used,
                    model_requests=request_index + 1,
                    web_was_used=web_was_used,
                    native_tool_events=tuple(native_events),
                    citations=tuple(citations),
                    response_status=response_status,
                )
            if finalization_only:
                logger.warning(
                    "agent_finalization_tool_call_rejected tool_calls=%d model_requests=%d",
                    len(response.tool_calls),
                    request_index + 1,
                )
                recovered = self._recover_committed_mutation(
                    tools,
                    calls_used=calls_used,
                    model_requests=request_index + 1,
                    web_was_used=web_was_used,
                    native_events=native_events,
                    citations=citations,
                    response_status=response_status,
                    exception_category="tool_call_during_finalization",
                )
                if recovered is not None:
                    return recovered
                exhausted = (
                    tools.exhausted(runtime)
                    if tools is not None
                    else "工具调用次数过多，Agent 已停止。"
                )
                return AgentRunResult(
                    text=exhausted,
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
            tooling = getattr(runtime.runtime_config, "tooling", None)
            coordinated = await self._execute_tool_batch(
                response.tool_calls,
                tools,
                runtime,
                remaining_calls=max(0, runtime.max_tool_calls - calls_used),
                max_parallel_calls=tooling.max_parallel_calls if tooling is not None else 1,
                reusable_results=reusable_tool_results,
                cacheable_names=frozenset(t.name for t in definitions if t.result_cacheable),
            )
            batch, executed = coordinated.calls, coordinated.executed_count
            calls_used += executed
            finalizing_commit_in_batch = False
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
                if (
                    isinstance(outcome, dict)
                    and outcome.get("ok") is True
                    and outcome.get("mutation_committed") is True
                    and outcome.get("finalize_after_commit") is True
                ):
                    finalizing_commit_in_batch = True
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
            if tools is not None:
                declined = getattr(tools, "declined_reply", None)
                if callable(declined) and declined():
                    return AgentRunResult(
                        text="",
                        tool_calls_used=calls_used,
                        model_requests=request_index + 1,
                        web_was_used=web_was_used,
                        native_tool_events=tuple(native_events),
                        citations=tuple(citations),
                        response_status=response_status,
                        suppress_delivery=True,
                    )
                terminal_reply = getattr(tools, "terminal_memory_reply", None)
                if callable(terminal_reply):
                    terminal_text = terminal_reply()
                    if isinstance(terminal_text, str) and terminal_text.strip():
                        return AgentRunResult(
                            text=terminal_text,
                            tool_calls_used=calls_used,
                            model_requests=request_index + 1,
                            web_was_used=web_was_used,
                            native_tool_events=tuple(native_events),
                            citations=tuple(citations),
                            response_status=response_status,
                        )
            if finalizing_commit_in_batch:
                force_finalization = True
                reusable_tool_results.clear()
                logger.info(
                    "agent_terminal_mutation_force_finalization "
                    "tool_calls_used=%d model_requests=%d",
                    calls_used,
                    request_index + 1,
                )
            fingerprint = tuple(
                (call.function.name, self._tool_call_signature(call)[1], result)
                for call, result, _was_executed in batch
            )
            if fingerprint and fingerprint == previous_batch_fingerprint:
                repeated_batch_count += 1
            else:
                repeated_batch_count = 0
            previous_batch_fingerprint = fingerprint
            if coordinated.reused_count == len(batch) and batch:
                logger.info(
                    "agent_tool_batch_reused reused_calls=%d tool_calls_used=%d",
                    coordinated.reused_count,
                    calls_used,
                )
            if repeated_batch_count >= 2:
                no_progress_recovery = True
                force_finalization = True
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
            if calls_used >= runtime.max_tool_calls or (
                request_index + 2 >= runtime.max_model_requests
            ):
                force_finalization = True
                logger.info(
                    "agent_final_reply_budget_reserved tool_calls_used=%d model_requests=%d",
                    calls_used,
                    request_index + 1,
                )
            if tools is not None:
                effect_probe = getattr(tools, "did_use_web", None)
                if callable(effect_probe) and effect_probe():
                    web_was_used = True
        recovered = self._recover_committed_mutation(
            tools,
            calls_used=calls_used,
            model_requests=runtime.max_model_requests,
            web_was_used=web_was_used,
            native_events=native_events,
            citations=citations,
            response_status=response_status,
            exception_category="model_request_budget_exhausted",
        )
        if recovered is not None:
            return recovered
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
    ) -> CoordinatedToolResult:
        """Execute each semantic call once and fan its result out to duplicate IDs."""

        signatures = {call.id: self._tool_call_signature(call) for call in calls}
        first_call_by_signature: dict[tuple[str, str], ToolCall] = {}
        reused_by_id: dict[str, str] = {}
        aliases: dict[str, str] = {}
        unique_calls: list[ToolCall] = []
        for call in calls:
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
                if conflicting:
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
            decline_calls = [call for call in unique_calls if call.function.name == "decline_reply"]
            if decline_calls:
                prior_effects = getattr(tools, "has_prior_reply_effects", None)
                mixed = len(unique_calls) != 1 or len(calls) != 1
                already_used = bool(callable(prior_effects) and prior_effects())
                if mixed or already_used:
                    violation = json.dumps(
                        {
                            "ok": False,
                            "error": "decline_reply_batch_rejected",
                            "detail": "decline_reply 必须是尚未产生效果时的单独调用。",
                        },
                        ensure_ascii=False,
                    )
                    return CoordinatedToolResult(
                        calls=tuple((call, violation, False) for call in calls),
                        executed_count=0,
                        reused_count=0,
                    )
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
    def _tool_result_reusable(result: str) -> bool:
        try:
            payload = json.loads(result)
        except json.JSONDecodeError:
            return False
        return bool(
            isinstance(payload, dict)
            and payload.get("ok") is True
            and payload.get("retryable") is not True
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

    @staticmethod
    def _recover_committed_mutation(
        tools: AgentToolBackend | None,
        *,
        calls_used: int,
        model_requests: int,
        web_was_used: bool,
        native_events: list[NativeToolEvent],
        citations: list[ResponseCitation],
        response_status: ModelResponseStatus,
        exception_category: str,
    ) -> AgentRunResult | None:
        recovery = getattr(tools, "post_commit_recovery_text", None)
        if not callable(recovery):
            return None
        text = recovery()
        if not isinstance(text, str) or not text.strip():
            return None
        logger.warning(
            "agent_post_commit_finalization_recovered exception_category=%s "
            "tool_calls_used=%d model_requests=%d",
            exception_category,
            calls_used,
            model_requests,
        )
        return AgentRunResult(
            text=text.strip(),
            tool_calls_used=calls_used,
            model_requests=model_requests,
            web_was_used=web_was_used,
            native_tool_events=tuple(native_events),
            citations=tuple(citations),
            response_status=response_status,
        )
