"""Execute model requests through explicit tasks and profiles."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import weakref
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any, Protocol

from qq_ai_bot.domain.messages import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    NativeToolType,
    minimum_reasoning_effort,
)
from qq_ai_bot.execution_trace.phases import (
    ModelPhases,
    current_model_phases,
    model_detail,
    switch_model_phase,
)
from qq_ai_bot.execution_trace.recorder import TraceRecorder, record_trace, trace_span
from qq_ai_bot.llm.base import LLMError, LLMUnsupportedFeatureError
from qq_ai_bot.model_runtime.capacity import ModelCapacity, estimate_request_tokens
from qq_ai_bot.model_runtime.dispatch_guard import check_model_dispatch
from qq_ai_bot.model_runtime.models import (
    ModelCapability,
    ModelExecutionPriority,
    ModelProfile,
    ModelProtocol,
    ModelSearchMode,
    ModelTask,
    StructuredOutputMode,
)
from qq_ai_bot.model_runtime.pool import ModelClientPool
from qq_ai_bot.model_runtime.profiles import ModelProfileCatalog
from qq_ai_bot.model_runtime.repository import ModelInvocationRepository
from qq_ai_bot.model_runtime.request_accounting import (
    ProviderAttemptCounter,
    current_provider_attempts,
)
from qq_ai_bot.model_runtime.routes import ModelRouter

logger = logging.getLogger(__name__)


def _profile_request(request: ChatRequest, profile: ModelProfile) -> ChatRequest:
    """Apply the same provider fields to capacity planning and actual dispatch."""
    return replace(
        request,
        model=profile.model,
        temperature=profile.default_temperature
        if request.temperature is None
        else request.temperature,
        max_output_tokens=(
            profile.default_max_output_tokens
            if request.max_output_tokens is None
            else request.max_output_tokens
        ),
        thinking_enabled=True,
        reasoning_effort=minimum_reasoning_effort(
            request.reasoning_effort, profile.reasoning_effort
        ),
    )


@dataclass(slots=True, weakref_slot=True)
class _PinnedModelRuntime:
    router: ModelRouter
    pool: ModelClientPool


def _json_hash(value: object) -> str:
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ProviderCacheShapeDiagnostics:
    """Pre-provider projection hashes; final HTTP diagnostics are authoritative."""

    provider_shape_hash: str
    instructions_hash: str
    tools_hash: str
    input_prefix_hash: str
    coverage: str = "normalized messages and tools; excludes native continuation body"


def _diagnostic_message(message: object) -> dict[str, object]:
    role = str(getattr(message, "role", ""))
    content = getattr(message, "content", None)
    tool_calls = getattr(message, "tool_calls", ())
    return {
        "role": role,
        "content_hash": _json_hash(content),
        "image_hashes": [_json_hash(image.data_url) for image in getattr(message, "images", ())],
        "tool_calls": [
            {
                "id_hash": _json_hash(getattr(call, "id", "")),
                "type": str(getattr(call, "type", "")),
                "name": str(getattr(getattr(call, "function", None), "name", "")),
                "arguments_hash": _json_hash(
                    getattr(getattr(call, "function", None), "arguments", "")
                ),
            }
            for call in tool_calls
        ],
        "tool_call_id_hash": _json_hash(getattr(message, "tool_call_id", None)),
        "reasoning_hash": _json_hash(getattr(message, "reasoning_content", None)),
    }


def provider_cache_shape_diagnostics(
    request: ChatRequest,
    *,
    provider: str,
    model: str,
    profile_id: str,
    protocol: str,
) -> ProviderCacheShapeDiagnostics:
    """Hash the application projection, excluding the current user tail.

    Native continuation body is outside this projection. WireRequestObserver on
    each adapter's final body is authoritative for that shape and first difference.
    """

    boundary = 0
    while boundary < len(request.messages) and request.messages[boundary].role in {
        "system",
        "developer",
    }:
        boundary += 1
    instructions = [_diagnostic_message(message) for message in request.messages[:boundary]]
    inputs = request.messages[boundary:]
    current_tail_index = next(
        (index for index in range(len(inputs) - 1, -1, -1) if inputs[index].role == "user"),
        None,
    )
    prefix_inputs = [
        _diagnostic_message(message)
        for message in (inputs if current_tail_index is None else inputs[:current_tail_index])
    ]
    tools = [
        {
            "name": tool.name,
            "description_hash": _json_hash(tool.description),
            "parameters_hash": _json_hash(tool.parameters),
        }
        for tool in request.tools
    ]
    native_tools = [tool.type.value for tool in request.native_tools]
    instructions_hash = _json_hash(instructions)
    tools_hash = _json_hash({"function": tools, "native": native_tools})
    input_prefix_hash = _json_hash(prefix_inputs)
    provider_shape_hash = _json_hash(
        {
            "provider": provider,
            "model": model,
            "profile_id": profile_id,
            "protocol": protocol,
            "instructions_hash": instructions_hash,
            "tools_hash": tools_hash,
            "input_prefix_hash": input_prefix_hash,
            "temperature": request.temperature,
            "max_output_tokens": request.max_output_tokens,
            "thinking_enabled": request.thinking_enabled,
            "reasoning_effort": (
                request.reasoning_effort.value if request.reasoning_effort is not None else None
            ),
            "tool_choice": request.tool_choice,
            "response_format": request.response_format,
            "structured_output": request.structured_output,
        }
    )
    return ProviderCacheShapeDiagnostics(
        provider_shape_hash=provider_shape_hash,
        instructions_hash=instructions_hash,
        tools_hash=tools_hash,
        input_prefix_hash=input_prefix_hash,
    )


def request_shape_hash(
    request: ChatRequest,
    *,
    provider: str,
    model: str,
    profile_id: str,
    protocol: str,
) -> str:
    """Hash the actual cache-relevant request shape without message content."""

    payload = {
        "provider": provider,
        "model": model,
        "profile_id": profile_id,
        "protocol": protocol,
        "static_prompt_revision": request.static_prompt_revision,
        "tools": [
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            }
            for tool in request.tools
        ],
        "native_tools": [tool.type.value for tool in request.native_tools],
        "response_format": request.response_format,
        "structured_output": request.structured_output,
    }
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class BackgroundModelPreempted(RuntimeError):
    """A best-effort provider call yielded to newly arrived foreground work."""


class ModelCompleter(Protocol):
    """Provider completion contract consumed by the physical dispatch owner."""

    async def complete(self, request: ChatRequest) -> ChatResponse: ...


class ModelExecutor(Protocol):
    """Business-facing task executor contract."""

    async def execute(
        self,
        task: ModelTask,
        request: ChatRequest,
        *,
        priority: ModelExecutionPriority = ModelExecutionPriority.FOREGROUND,
        canonical_conversation_id: str | None = None,
    ) -> ChatResponse: ...

    def model_name(self, task: ModelTask) -> str: ...

    def structured_output_mode(self, task: ModelTask) -> StructuredOutputMode: ...

    def protocol(self, task: ModelTask) -> ModelProtocol: ...

    def capabilities(self, task: ModelTask) -> frozenset[ModelCapability]: ...

    def capacity(self, task: ModelTask) -> ModelCapacity: ...

    def capacity_request(self, task: ModelTask, request: ChatRequest) -> ChatRequest: ...

    def profile_revision(self, task: ModelTask) -> str: ...

    def search_mode(self, task: ModelTask) -> ModelSearchMode | None: ...

    def pin(self) -> AbstractContextManager[None]: ...

    @property
    def traces(self) -> TraceRecorder | None: ...


class TaskModelExecutor:
    """The only main-model entry point used by business services."""

    def __init__(
        self,
        *,
        router: ModelRouter,
        pool: ModelClientPool,
        invocations: ModelInvocationRepository | None = None,
        traces: TraceRecorder | None = None,
        max_concurrency: int | None = None,
        compaction_timeout_seconds: float = 600.0,
        self_reflection_timeout_seconds: float = 180.0,
    ) -> None:
        if max_concurrency is not None and max_concurrency <= 0:
            raise ValueError("max_concurrency must be positive when configured")
        self._router = router
        self._pool = pool
        self._active_runtime = (router, pool)
        self._pinned_runtime: ContextVar[_PinnedModelRuntime | None] = ContextVar(
            "pinned_model_runtime", default=None
        )
        self._retired_pools: dict[int, ModelClientPool] = {}
        self._pool_lease_counts: dict[int, int] = {}
        self._pool_close_tasks: set[asyncio.Task[None]] = set()
        self._closing = False
        self._compaction_timeout_seconds = compaction_timeout_seconds
        self._self_reflection_timeout_seconds = self_reflection_timeout_seconds
        self._invocations = invocations
        self.traces = traces
        self._invocation_record_failures = 0
        self._max_concurrency = max_concurrency
        self._provider_active = 0
        self._nonforeground_active = 0
        self._provider_foreground_waiting = 0
        self._priority_condition = asyncio.Condition()
        self._ordinary_active = 0
        self._foreground_waiting = 0
        self._exclusive_active = 0
        self._exclusive_waiting = 0
        self._exclusive_slot = asyncio.Lock()
        self._background_slot = asyncio.Lock()
        self._background_provider_task: asyncio.Task[ChatResponse] | None = None
        self._maintenance_slot = asyncio.Lock()
        self._maintenance_provider_task: asyncio.Task[ChatResponse] | None = None
        self._prompt_shapes: OrderedDict[str, tuple[str, str]] = OrderedDict()
        self._prefix_shape_match_total = 0
        self._prefix_shape_split_total = 0

    @property
    def router(self) -> ModelRouter:
        return self._router

    def _runtime(self) -> tuple[ModelRouter, ModelClientPool]:
        pinned = self._pinned_runtime.get()
        return (pinned.router, pinned.pool) if pinned is not None else self._active_runtime

    @contextmanager
    def pin(self) -> Iterator[None]:
        """Keep one Agent activation on one provider catalog across its requests."""
        if self._pinned_runtime.get() is not None:
            yield
            return
        router, pool = self._active_runtime
        lease = _PinnedModelRuntime(router, pool)
        pool_id = id(pool)
        self._pool_lease_counts[pool_id] = self._pool_lease_counts.get(pool_id, 0) + 1
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        weakref.finalize(lease, self._release_pool_lease, weakref.ref(self), pool_id, loop)
        token = self._pinned_runtime.set(lease)
        try:
            yield
        finally:
            self._pinned_runtime.reset(token)
            # A child Task may still hold the inherited ContextVar value. Its
            # strong reference keeps the lease (and old pool) alive until safe.
            del lease

    @staticmethod
    def _release_pool_lease(
        owner_ref: weakref.ReferenceType[TaskModelExecutor],
        pool_id: int,
        loop: asyncio.AbstractEventLoop | None,
    ) -> None:
        owner = owner_ref()
        if owner is None:
            return
        remaining = owner._pool_lease_counts[pool_id] - 1
        if remaining:
            owner._pool_lease_counts[pool_id] = remaining
        else:
            del owner._pool_lease_counts[pool_id]
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(owner._retire_idle_pool, pool_id)
            except RuntimeError:
                pass

    def _retire_idle_pool(self, pool_id: int) -> None:
        if (
            self._closing
            or self._pool_lease_counts.get(pool_id)
            or pool_id == id(self._active_runtime[1])
        ):
            return
        pool = self._retired_pools.get(pool_id)
        if pool is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(pool.close(), name="retired-model-pool-close")
        del self._retired_pools[pool_id]
        self._pool_close_tasks.add(task)

        def finished(done: asyncio.Task[None]) -> None:
            self._pool_close_tasks.discard(done)
            try:
                done.result()
            except Exception:
                logger.exception("retired model client pool close failed")

        task.add_done_callback(finished)

    def apply_catalog(self, catalog: ModelProfileCatalog, pool: ModelClientPool) -> None:
        """Switch new requests atomically; pinned work retains its old clients."""
        router = ModelRouter(catalog)
        old_pool = self._active_runtime[1]
        if old_pool is not pool:
            self._retired_pools[id(old_pool)] = old_pool
        self._active_runtime = (router, pool)
        self._router, self._pool = router, pool
        if old_pool is not pool:
            self._retire_idle_pool(id(old_pool))

    async def execute(
        self,
        task: ModelTask,
        request: ChatRequest,
        *,
        priority: ModelExecutionPriority = ModelExecutionPriority.FOREGROUND,
        canonical_conversation_id: str | None = None,
    ) -> ChatResponse:
        phases = ModelPhases()
        phase_token = current_model_phases.set(phases)
        outcome = "terminated"
        try:
            with self.pin():
                async with trace_span(
                    "model",
                    {"task": task.value, "request": request},
                    recorder=self.traces,
                    conversation_id=canonical_conversation_id,
                ) as span:
                    try:
                        response = await self._execute(
                            task,
                            request,
                            priority=priority,
                            canonical_conversation_id=canonical_conversation_id,
                        )
                    except asyncio.CancelledError:
                        outcome = "cancelled"
                        raise
                    except Exception:
                        outcome = "error"
                        raise
                    else:
                        outcome = "success"
                        span.result = response
                        return response
                    finally:
                        # The same scope/operation contains all outcomes. This
                        # enqueues only numbers and does not wait for its consumer.
                        if outcome != "terminated":
                            await record_trace("model_phases", phases.snapshot(outcome))
        finally:
            current_model_phases.reset(phase_token)

    async def _execute(
        self,
        task: ModelTask,
        request: ChatRequest,
        *,
        priority: ModelExecutionPriority,
        canonical_conversation_id: str | None,
    ) -> ChatResponse:
        required: set[ModelCapability] = {ModelCapability.REASONING}
        if request.tools and not request.structured_output:
            required.add(ModelCapability.TOOLS)
        if request.structured_output or request.response_format is not None:
            required.add(ModelCapability.STRUCTURED_OUTPUT)
        if request.native_tools:
            if ModelCapability.NATIVE_WEB_SEARCH not in self.capabilities(task):
                raise LLMUnsupportedFeatureError(
                    "native web search is unavailable in the effective model contract"
                )
            required.add(ModelCapability.NATIVE_WEB_SEARCH)
        if any(
            message.images
            for message in (
                *request.messages,
                *(item for item in request.continuation_items if isinstance(item, ChatMessage)),
            )
        ):
            required.add(ModelCapability.IMAGE_INPUT)
        router, pool = self._runtime()
        _route, profile = router.route(task, required_capabilities=frozenset(required))
        await record_trace(
            "model_route",
            {
                "task": task.value,
                "profile_id": profile.id,
                "provider": profile.provider,
                "protocol": profile.protocol.value,
                "model": profile.model,
            },
        )
        for continuation in (
            *((request.continuation,) if request.continuation is not None else ()),
            *(
                message.response_item
                for message in request.messages
                if message.response_item is not None
            ),
        ):
            if (
                continuation.profile_id != profile.id
                or continuation.provider != profile.provider.casefold()
                or continuation.protocol != profile.protocol.value
            ):
                raise ValueError("continuation cannot be routed to a different model profile")
        if (
            profile.max_output_tokens_limit is not None
            and (request.max_output_tokens or profile.default_max_output_tokens)
            > profile.max_output_tokens_limit
        ):
            raise LLMUnsupportedFeatureError("request exceeds configured provider output limit")
        if profile.max_input_tokens is not None or profile.context_window_tokens is not None:
            capacity = ModelCapacity(
                input_tokens=profile.max_input_tokens,
                context_tokens=profile.context_window_tokens,
                output_tokens=profile.default_max_output_tokens,
            )
            budget = capacity.input_budget(
                profile.max_input_tokens or profile.context_window_tokens or 1,
                output_tokens=request.max_output_tokens,
            )
            with model_detail("token_estimation"):
                estimated = estimate_request_tokens(_profile_request(request, profile))
            if estimated > budget:
                raise LLMUnsupportedFeatureError(
                    "request exceeds configured provider input capacity"
                )
        provider = (
            pool.get(profile, timeout_seconds=self._compaction_timeout_seconds)
            if task is ModelTask.CONVERSATION_COMPACTION
            else pool.get(profile, timeout_seconds=self._self_reflection_timeout_seconds)
            if task is ModelTask.MEMORY_SELF_REFLECTION
            else pool.get(profile)
        )
        with model_detail("cache_shape_preparation"):
            normalized = replace(
                _profile_request(request, profile),
                request_shape_hash=request_shape_hash(
                    request,
                    provider=profile.provider,
                    model=profile.model,
                    profile_id=profile.id,
                    protocol=profile.protocol.value,
                ),
            )
            provider_cache_shape = provider_cache_shape_diagnostics(
                normalized,
                provider=profile.provider,
                model=profile.model,
                profile_id=profile.id,
                protocol=profile.protocol.value,
            )
        if normalized.conversation_prefix_hash:
            self._observe_prompt_shape(
                normalized,
                provider_shape_hash=provider_cache_shape.provider_shape_hash,
            )
            logger.info(
                "prompt_request_diagnostics stage=normalized_projection task=%s "
                "conversation_prefix_hash=%s "
                "request_shape_hash=%s provider_cache_shape_hash=%s "
                "provider_instructions_hash=%s provider_tools_hash=%s "
                "provider_input_prefix_hash=%s prompt_snapshot_fingerprint=%s coverage=%s",
                task.value,
                normalized.conversation_prefix_hash,
                normalized.request_shape_hash,
                provider_cache_shape.provider_shape_hash,
                provider_cache_shape.instructions_hash,
                provider_cache_shape.tools_hash,
                provider_cache_shape.input_prefix_hash,
                normalized.prompt_snapshot_fingerprint,
                provider_cache_shape.coverage,
            )
        if profile.protocol is ModelProtocol.RESPONSES:
            logger.info(
                "responses_request_routed task=%s profile_id=%s provider=%s protocol=%s "
                "model=%s native_tool_types=%s function_tool_count=%d web_scope_approved=%s",
                task.value,
                profile.id,
                profile.provider,
                profile.protocol.value,
                profile.model,
                ",".join(tool.type.value for tool in normalized.native_tools) or "none",
                len(normalized.tools),
                bool(normalized.native_tools),
            )

        started = time.perf_counter()
        switch_model_phase("slot_wait")
        native_search_requested = any(
            tool.type is NativeToolType.WEB_SEARCH for tool in normalized.native_tools
        )
        attempts = ProviderAttemptCounter()
        attempt_token = current_provider_attempts.set(attempts)
        try:
            response = await self._execute_provider(
                provider,
                normalized,
                priority=priority,
            )
        except Exception as exc:
            if isinstance(exc, LLMError):
                # The actual transport counter survives the executor boundary;
                # zero remains a predispatch failure, not an unknown paid effect.
                usage = attempts.usage_totals()
                prior_usage = exc.diagnostics.get("usage")
                # Preserve the adapter's diagnostic field shape; aggregation
                # adds reported values, not previously absent optional NULLs.
                reported = {
                    name: value
                    for name, value in usage.items()
                    if value is not None or (isinstance(prior_usage, dict) and name in prior_usage)
                }
                exc.diagnostics = {
                    **exc.diagnostics,
                    **({"usage": reported} if attempts.requests else {}),
                    "physical_request_count": attempts.requests,
                    "unknown_usage_request_count": attempts.unknown_usage_requests,
                }
            if self._invocations is not None:
                diagnostic_usage = (
                    exc.diagnostics.get("usage") if isinstance(exc, LLMError) else None
                )

                def reported_tokens(name: str) -> int | None:
                    value = (
                        diagnostic_usage.get(name) if isinstance(diagnostic_usage, dict) else None
                    )
                    return value if type(value) is int and value >= 0 else None

                await self._record_invocation(
                    task=task,
                    profile_id=profile.id,
                    provider=profile.provider,
                    model=profile.model,
                    original_failure=exc,
                    success=False,
                    prompt_tokens=reported_tokens("prompt_tokens"),
                    completion_tokens=reported_tokens("completion_tokens"),
                    total_tokens=reported_tokens("total_tokens"),
                    cached_prompt_tokens=reported_tokens("cached_prompt_tokens"),
                    cache_creation_input_tokens=reported_tokens("cache_creation_input_tokens"),
                    cache_creation_5m_input_tokens=reported_tokens(
                        "cache_creation_5m_input_tokens"
                    ),
                    cache_creation_1h_input_tokens=reported_tokens(
                        "cache_creation_1h_input_tokens"
                    ),
                    latency_seconds=time.perf_counter() - started,
                    error_category=type(exc).__name__,
                    physical_request_count=attempts.requests,
                    unknown_usage_request_count=attempts.unknown_usage_requests,
                    native_search_requested=native_search_requested,
                    canonical_conversation_id=canonical_conversation_id,
                )
            raise
        finally:
            current_provider_attempts.reset(attempt_token)
            switch_model_phase("response_preparation")
        if attempts.requests:
            # Adapters report physical usage; only this boundary combines it.
            # Claude pause may already return a combined logical response, so
            # replace these fields instead of adding its totals a second time.
            usage = attempts.usage_totals()
            response = replace(
                response,
                prompt_tokens=usage["prompt_tokens"],
                completion_tokens=usage["completion_tokens"],
                total_tokens=usage["total_tokens"],
                cached_prompt_tokens=usage["cached_prompt_tokens"],
                reasoning_tokens=usage["reasoning_tokens"],
                cache_creation_input_tokens=usage["cache_creation_input_tokens"],
                cache_creation_5m_input_tokens=usage["cache_creation_5m_input_tokens"],
                cache_creation_1h_input_tokens=usage["cache_creation_1h_input_tokens"],
            )
        if response.continuation is not None:
            response = replace(
                response,
                continuation=replace(response.continuation, profile_id=profile.id),
            )
        if profile.protocol is ModelProtocol.RESPONSES:
            logger.info(
                "responses_request_recorded task=%s profile_id=%s provider=%s protocol=%s "
                "response_status=%s input_tokens=%s output_tokens=%s reasoning_tokens=%s "
                "cached_tokens=%s native_action_count=%d citation_count=%d",
                task.value,
                profile.id,
                profile.provider,
                profile.protocol.value,
                response.status.value,
                response.prompt_tokens,
                response.completion_tokens,
                response.reasoning_tokens,
                response.cached_prompt_tokens,
                len(response.native_tool_events),
                len(response.citations),
            )
        if self._invocations is not None:
            await self._record_invocation(
                task=task,
                profile_id=profile.id,
                provider=profile.provider,
                model=profile.model,
                success=True,
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
                total_tokens=response.total_tokens,
                cached_prompt_tokens=response.cached_prompt_tokens,
                cache_creation_input_tokens=response.cache_creation_input_tokens,
                cache_creation_5m_input_tokens=response.cache_creation_5m_input_tokens,
                cache_creation_1h_input_tokens=response.cache_creation_1h_input_tokens,
                latency_seconds=time.perf_counter() - started,
                error_category=None,
                physical_request_count=attempts.requests,
                unknown_usage_request_count=attempts.unknown_usage_requests,
                native_search_requested=native_search_requested,
                canonical_conversation_id=canonical_conversation_id,
            )
        return response

    async def _record_invocation(
        self, *, original_failure: Exception | None = None, **values: Any
    ) -> None:
        if self._invocations is None:
            return
        try:
            await self._invocations.record(**values)
        except Exception as exc:
            # Telemetry is not the durable execution/HTTP budget. A database
            # outage must not discard a successful provider response; when the
            # provider failed, retain that original exception even if auditing
            # has an independent bug. Never retry an uncertain telemetry commit.
            self._invocation_record_failures += 1
            logger.error(
                "model_invocation_record_failed task=%s category=%s provider_success=%s "
                "coverage_incomplete=true record_failures_in_process=%d",
                values["task"].value,
                type(exc).__name__,
                values["success"],
                self._invocation_record_failures,
            )

    def prompt_shape_metrics(self) -> dict[str, int]:
        return {
            "conversation_prefix_shape_match_total": self._prefix_shape_match_total,
            "conversation_prefix_shape_split_total": self._prefix_shape_split_total,
        }

    def _observe_prompt_shape(
        self,
        request: ChatRequest,
        *,
        provider_shape_hash: str,
    ) -> None:
        fingerprint = request.prompt_snapshot_fingerprint
        if not fingerprint:
            return
        observed = (request.conversation_prefix_hash, provider_shape_hash)
        previous = self._prompt_shapes.get(fingerprint)
        if previous is not None:
            if previous == observed:
                self._prefix_shape_match_total += 1
            elif previous[0] == observed[0]:
                self._prefix_shape_split_total += 1
            self._prompt_shapes.move_to_end(fingerprint)
            return
        self._prompt_shapes[fingerprint] = observed
        if len(self._prompt_shapes) > 1024:
            self._prompt_shapes.popitem(last=False)

    async def _execute_provider(
        self,
        provider: ModelCompleter,
        request: ChatRequest,
        *,
        priority: ModelExecutionPriority,
    ) -> ChatResponse:
        if priority is ModelExecutionPriority.BEST_EFFORT_BACKGROUND:
            return await self._execute_background_provider(provider, request)
        if priority is ModelExecutionPriority.EXCLUSIVE:
            return await self._execute_exclusive_provider(provider, request)
        if priority is ModelExecutionPriority.MAINTENANCE:
            return await self._execute_maintenance_provider(provider, request)
        if priority is ModelExecutionPriority.BACKGROUND:
            return await self._execute_foreground_provider(provider, request, background=True)
        return await self._execute_foreground_provider(provider, request)

    def _cancel_best_effort_background(self) -> None:
        background = self._background_provider_task
        if background is not None and not background.done():
            background.cancel()

    async def _execute_foreground_provider(
        self,
        provider: ModelCompleter,
        request: ChatRequest,
        *,
        background: bool = False,
    ) -> ChatResponse:
        waiting = False
        active = False
        try:
            async with self._priority_condition:
                if not background:
                    self._foreground_waiting += 1
                    waiting = True
                    self._cancel_best_effort_background()
                self._priority_condition.notify_all()
                await self._priority_condition.wait_for(
                    lambda: (
                        self._exclusive_active == 0
                        and self._exclusive_waiting == 0
                        and (not background or self._foreground_waiting == 0)
                    )
                )
                if waiting:
                    self._foreground_waiting -= 1
                    waiting = False
                # Ordinary calls include durable background work: exclusive
                # operations drain them rather than cancelling their execution.
                self._ordinary_active += 1
                active = True
                self._priority_condition.notify_all()
            return await self._complete_provider(provider, request, background=background)
        finally:
            async with self._priority_condition:
                if active:
                    self._ordinary_active -= 1
                elif waiting:
                    self._foreground_waiting -= 1
                self._priority_condition.notify_all()

    async def _execute_exclusive_provider(
        self,
        provider: ModelCompleter,
        request: ChatRequest,
    ) -> ChatResponse:
        async with self._exclusive_slot:
            waiting = False
            active = False
            try:
                async with self._priority_condition:
                    self._exclusive_waiting += 1
                    waiting = True
                    self._cancel_best_effort_background()
                    maintenance = self._maintenance_provider_task
                    if maintenance is not None and not maintenance.done():
                        maintenance.cancel()
                    self._priority_condition.notify_all()
                    await self._priority_condition.wait_for(
                        lambda: (
                            self._ordinary_active == 0
                            and self._maintenance_provider_task is None
                            and self._background_provider_task is None
                        )
                    )
                    self._exclusive_waiting -= 1
                    waiting = False
                    self._exclusive_active += 1
                    active = True
                    self._priority_condition.notify_all()
                return await self._complete_provider(provider, request)
            finally:
                async with self._priority_condition:
                    if active:
                        self._exclusive_active -= 1
                    elif waiting:
                        self._exclusive_waiting -= 1
                    self._priority_condition.notify_all()

    async def _execute_background_provider(
        self,
        provider: ModelCompleter,
        request: ChatRequest,
    ) -> ChatResponse:
        async with self._background_slot:
            async with self._priority_condition:
                await self._priority_condition.wait_for(
                    lambda: (
                        self._ordinary_active == 0
                        and self._foreground_waiting == 0
                        and self._exclusive_active == 0
                        and self._exclusive_waiting == 0
                    )
                )
                provider_task = asyncio.create_task(
                    self._complete_provider(provider, request, background=True),
                    name="best-effort-model-provider",
                )
                self._background_provider_task = provider_task
            try:
                return await provider_task
            except asyncio.CancelledError as exc:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                raise BackgroundModelPreempted("background model request preempted") from exc
            finally:
                async with self._priority_condition:
                    if self._background_provider_task is provider_task:
                        self._background_provider_task = None
                    self._priority_condition.notify_all()

    async def _execute_maintenance_provider(
        self, provider: ModelCompleter, request: ChatRequest
    ) -> ChatResponse:
        # One protected maintenance call globally; remaining slots serve chat.
        async with self._maintenance_slot:
            async with self._priority_condition:
                await self._priority_condition.wait_for(
                    lambda: self._exclusive_active == 0 and self._exclusive_waiting == 0
                )
                task = asyncio.create_task(
                    self._complete_provider(provider, request, background=True)
                )
                self._maintenance_provider_task = task
            try:
                return await task
            except asyncio.CancelledError as exc:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                raise BackgroundModelPreempted(
                    "maintenance cancelled by exclusive operation"
                ) from exc
            finally:
                async with self._priority_condition:
                    self._maintenance_provider_task = None
                    self._priority_condition.notify_all()

    async def _complete_provider(
        self,
        provider: ModelCompleter,
        request: ChatRequest,
        *,
        background: bool = False,
    ) -> ChatResponse:
        async with self._priority_condition:
            if not background:
                self._provider_foreground_waiting += 1
            try:
                await self._priority_condition.wait_for(
                    lambda: (
                        (
                            self._max_concurrency is None
                            or self._provider_active < self._max_concurrency
                        )
                        and (
                            not background
                            or (
                                self._provider_foreground_waiting == 0
                                and (
                                    self._max_concurrency is None
                                    or self._nonforeground_active
                                    < max(1, self._max_concurrency - 1)
                                )
                            )
                        )
                    )
                )
                self._provider_active += 1
                if background:
                    self._nonforeground_active += 1
            finally:
                if not background:
                    self._provider_foreground_waiting -= 1
                self._priority_condition.notify_all()
        switch_model_phase("slot_hold")
        try:
            with model_detail("dispatch_preparation"):
                await check_model_dispatch()
            return await provider.complete(request)
        finally:
            async with self._priority_condition:
                self._provider_active -= 1
                if background:
                    self._nonforeground_active -= 1
                self._priority_condition.notify_all()
                switch_model_phase("response_preparation")

    def profile_id(self, task: ModelTask) -> str:
        route, _profile = self._runtime()[0].route(task)
        return route.profile_id

    def profile_revision(self, task: ModelTask) -> str:
        """Fingerprint routing/serialization settings without exposing configuration."""
        route, profile = self._runtime()[0].route(task)
        # Capacity policy is not serialized to the provider and must not split
        # a previously submitted prefix when operators tune numeric limits.
        excluded = {"max_input_tokens", "context_window_tokens"}
        if profile.max_output_tokens_limit is None:
            excluded.add("max_output_tokens_limit")
        if profile.wire_options is None:
            excluded.add("wire_options")
        if not profile.headers:
            excluded.add("headers")
        if profile.search_mode is None:
            excluded.add("search_mode")
        serialized = profile.model_dump(mode="json", exclude=excluded)
        serialized["capabilities"] = sorted(item.value for item in profile.capabilities)
        serialized_route = route.model_dump(mode="json")
        serialized_route["required_capabilities"] = sorted(
            item.value for item in route.required_capabilities
        )
        if profile.protocol is not ModelProtocol.RESPONSES:
            from qq_ai_bot.llm.vendor_policy import wire_options

            # Chat's changed vendor dialect deliberately establishes a new chain;
            # unchanged Responses defaults retain the previous persisted revision.
            effective_wire_options = wire_options(profile.provider.casefold(), profile.wire_options)
            serialized["wire_options"] = effective_wire_options.model_dump(
                mode="json",
                exclude={"gemini_schema_format"}
                if effective_wire_options.gemini_schema_format == "response_json_schema"
                else set(),
            )
        return _json_hash(
            {
                "route": serialized_route,
                "profile": serialized,
            }
        )

    def model_name(self, task: ModelTask) -> str:
        _route, profile = self._runtime()[0].route(task)
        return profile.model

    def capacity(self, task: ModelTask) -> ModelCapacity:
        _route, profile = self._runtime()[0].route(task)
        return ModelCapacity(
            input_tokens=profile.max_input_tokens,
            context_tokens=profile.context_window_tokens,
            output_tokens=profile.default_max_output_tokens,
        )

    def capacity_request(self, task: ModelTask, request: ChatRequest) -> ChatRequest:
        _route, profile = self._runtime()[0].route(task)
        return _profile_request(request, profile)

    def structured_output_mode(self, task: ModelTask) -> StructuredOutputMode:
        _route, profile = self._runtime()[0].route(task)
        return profile.structured_output_mode

    def protocol(self, task: ModelTask) -> ModelProtocol:
        _route, profile = self._runtime()[0].route(task)
        return profile.protocol

    def search_mode(self, task: ModelTask) -> ModelSearchMode | None:
        _route, profile = self._runtime()[0].route(task)
        return profile.search_mode

    def capabilities(self, task: ModelTask) -> frozenset[ModelCapability]:
        _route, profile = self._runtime()[0].route(task)
        from qq_ai_bot.llm.vendor_policy import supports_native_search

        if not supports_native_search(
            profile.provider.casefold(),
            profile.protocol.value,
            profile.wire_options,
            has_functions=ModelCapability.TOOLS in profile.capabilities,
        ):
            return profile.capabilities - {ModelCapability.NATIVE_WEB_SEARCH}
        return profile.capabilities

    async def close(self) -> None:
        self._closing = True
        async with self._priority_condition:
            background = self._background_provider_task
            if background is not None and not background.done():
                background.cancel()
            maintenance = self._maintenance_provider_task
            if maintenance is not None and not maintenance.done():
                maintenance.cancel()
        if background is not None:
            await asyncio.gather(background, return_exceptions=True)
        if maintenance is not None:
            await asyncio.gather(maintenance, return_exceptions=True)
        errors: list[BaseException] = []
        if self._pool_close_tasks:
            results = await asyncio.gather(*self._pool_close_tasks, return_exceptions=True)
            errors.extend(result for result in results if isinstance(result, BaseException))
        pools = (*self._retired_pools.values(), self._active_runtime[1])
        self._retired_pools.clear()
        closed: set[int] = set()
        for pool in pools:
            if id(pool) in closed:
                continue
            closed.add(id(pool))
            try:
                await pool.close()
            except BaseException as exc:
                errors.append(exc)
        if errors:
            cancellation = next(
                (error for error in errors if not isinstance(error, Exception)), None
            )
            if cancellation is not None:
                for error in errors:
                    if error is not cancellation:
                        cancellation.add_note(f"close failed: {type(error).__name__}")
                raise cancellation
            raise BaseExceptionGroup("model executor close failed", errors)
