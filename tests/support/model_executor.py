"""Test-only provider injection; production services require ModelExecutor."""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from dataclasses import replace

from qq_ai_bot.domain.messages import ChatRequest, ChatResponse, minimum_reasoning_effort
from qq_ai_bot.execution_trace.recorder import TraceRecorder
from qq_ai_bot.model_runtime.capacity import ModelCapacity
from qq_ai_bot.model_runtime.dispatch_guard import check_model_dispatch
from qq_ai_bot.model_runtime.executor import ModelCompleter, ModelExecutor, request_shape_hash
from qq_ai_bot.model_runtime.models import (
    ModelCapability,
    ModelExecutionPriority,
    ModelProtocol,
    ModelSearchMode,
    ModelTask,
    StructuredOutputMode,
)


class InjectedModelExecutor:
    """Adapt an injected test provider without leaking it into business services."""

    def __init__(self, provider: ModelCompleter, *, model: str = "fake") -> None:
        self._provider = provider
        self._model = model

    async def execute(
        self,
        task: ModelTask,
        request: ChatRequest,
        *,
        priority: ModelExecutionPriority = ModelExecutionPriority.FOREGROUND,
        canonical_conversation_id: str | None = None,
    ) -> ChatResponse:
        del task, priority, canonical_conversation_id
        normalized = replace(
            request,
            thinking_enabled=True,
            reasoning_effort=minimum_reasoning_effort(request.reasoning_effort),
            request_shape_hash=request_shape_hash(
                request,
                provider="fake",
                model=self._model,
                profile_id="legacy",
                protocol=ModelProtocol.CHAT_COMPLETIONS.value,
            ),
        )
        await check_model_dispatch()
        return await self._provider.complete(normalized)

    def model_name(self, task: ModelTask) -> str:
        del task
        return self._model

    def capacity_request(self, task: ModelTask, request: ChatRequest) -> ChatRequest:
        del task
        return replace(
            request,
            thinking_enabled=True,
            reasoning_effort=minimum_reasoning_effort(request.reasoning_effort),
        )

    def structured_output_mode(self, task: ModelTask) -> StructuredOutputMode:
        del task
        return StructuredOutputMode.TEXT_JSON

    def protocol(self, task: ModelTask) -> ModelProtocol:
        del task
        return ModelProtocol.CHAT_COMPLETIONS

    def capabilities(self, task: ModelTask) -> frozenset[ModelCapability]:
        del task
        return frozenset(ModelCapability)

    def capacity(self, task: ModelTask) -> ModelCapacity:
        del task
        return ModelCapacity()

    traces: TraceRecorder | None = None

    def profile_revision(self, task: ModelTask) -> str:
        return "legacy"

    def search_mode(self, task: ModelTask) -> ModelSearchMode | None:
        return None

    def pin(self) -> AbstractContextManager[None]:
        return nullcontext()


def require_model_executor(
    model_executor: ModelExecutor | None,
    *,
    provider: ModelCompleter | None = None,
    model: str = "fake",
) -> ModelExecutor:
    """Normalize old test injection at one migration boundary."""

    if model_executor is not None:
        return model_executor
    if provider is None:
        raise TypeError("model_executor is required")
    return InjectedModelExecutor(provider, model=model)
