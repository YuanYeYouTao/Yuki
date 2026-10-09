"""Unified memory read entry (R2 §6).

The query kernel stays pure-read.  ``consumer`` is chosen by the backend
entry, never by the model.  Plugin/Admin reads are always side-effect free;
only ``AUTOMATIC_CONTEXT`` / ``AGENT_TOOL`` may later ``publish_exposure``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.memory.authorized_scope import AuthorizedMemoryScope
from qq_ai_bot.memory.enums import (
    MemoryContextMode,
    MemoryRetrievalMode,
    MemorySubjectRole,
)
from qq_ai_bot.memory.models import (
    MemoryEntityTarget,
    MemoryQueryIntent,
    MemoryRetrievalHit,
    MemoryRetrievalResult,
)


class MemoryReadConsumer(StrEnum):
    """Who initiated a read.  Not a model-writable field."""

    AGENT_TOOL = "agent_tool"
    PLUGIN = "plugin"
    ADMIN = "admin"


class ResolvedReadScope(BaseModel):
    """Host-resolved targets.  Models never submit raw QQ or group ids here."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    targets: tuple[MemoryEntityTarget, ...]
    complete: bool = True


class MemoryReadRequest(BaseModel):
    """One consumer-facing read.  Quantity lives here, not on the intent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    intent: MemoryQueryIntent | None = None
    requested_limit: int | None = Field(default=None, ge=1, le=100)
    resolved_scope: ResolvedReadScope
    authorized_scope: AuthorizedMemoryScope | None = None
    # Backend-only automatic projection options, never exposed in a tool schema.


class MemoryQueryKernel(Protocol):
    """Read/exposure operations the query plane may call."""

    async def search(
        self,
        *,
        text: str,
        mode: MemoryRetrievalMode,
        targets: tuple[MemoryEntityTarget, ...],
        runtime: RuntimeConfigSnapshot,
        limit: int | None = None,
        intent: MemoryQueryIntent | None = None,
    ) -> MemoryRetrievalResult: ...

    async def search_authorized(
        self,
        *,
        text: str,
        scope: AuthorizedMemoryScope,
        runtime: RuntimeConfigSnapshot,
        limit: int,
        intent: MemoryQueryIntent | None = None,
    ) -> MemoryRetrievalResult: ...


def retrieval_mode_for_request(request: MemoryReadRequest) -> MemoryRetrievalMode:
    """Map structured intent mode onto the retriever's two retrieval modes."""

    if request.intent is not None and request.intent.mode is MemoryContextMode.OVERVIEW:
        return MemoryRetrievalMode.OVERVIEW
    return MemoryRetrievalMode.RELEVANT


def resolve_read_limit(
    consumer: MemoryReadConsumer,
    request: MemoryReadRequest,
    runtime: RuntimeConfigSnapshot,
) -> int:
    """Consumer budget.  Automatic never reads ``requested_limit``."""

    memory = runtime.memory
    if request.requested_limit is not None:
        return request.requested_limit
    return memory.context_limit_per_entity


def apply_total_hit_limit(result: MemoryRetrievalResult, total_limit: int) -> MemoryRetrievalResult:
    """Cap unique facts while preserving retriever order."""

    selected: list[MemoryRetrievalHit] = []
    seen: set[int] = set()
    for hit in result.hits:
        if len(selected) >= total_limit:
            break
        if hit.fact.id in seen:
            continue
        selected.append(hit)
        seen.add(hit.fact.id)
    selected_ids = {hit.fact.id for hit in selected}
    blocks = tuple(
        block.model_copy(
            update={"hits": tuple(hit for hit in block.hits if hit.fact.id in selected_ids)}
        )
        for block in result.blocks
    )
    final_hits = tuple(selected)
    output_truncated = len(final_hits) < len(result.hits)
    return result.model_copy(
        update={
            "blocks": blocks,
            "hits": final_hits,
            "selected_count": len(final_hits),
            "truncated": result.truncated or output_truncated,
            "exhaustive": result.exhaustive and not output_truncated,
            "partial_reason": (
                "result_limit"
                if output_truncated and not result.truncated
                else result.partial_reason
            ),
        }
    )


class MemoryQueryPlane:
    """Single turn-query entry.  Dream/Rebuild/Maintenance stay on domain ports."""

    def __init__(self, kernel: MemoryQueryKernel) -> None:
        self._kernel = kernel

    async def read(
        self,
        consumer: MemoryReadConsumer,
        request: MemoryReadRequest,
        *,
        runtime: RuntimeConfigSnapshot,
    ) -> MemoryRetrievalResult:
        """Pure retrieve.  Never writes receipts, activation, or injected flags."""

        intent = (
            None
            if consumer in {MemoryReadConsumer.PLUGIN, MemoryReadConsumer.ADMIN}
            else (request.intent)
        )
        if intent is not None and not intent.subjects and consumer is MemoryReadConsumer.AGENT_TOOL:
            subjects = tuple(
                dict.fromkeys(
                    MemorySubjectRole(target.role.value.removesuffix("_group"))
                    if target.role.value in {"current_person_group", "referenced_person_group"}
                    else MemorySubjectRole(target.role.value)
                    for target in request.resolved_scope.targets
                )
            )
            intent = intent.model_copy(update={"subjects": subjects})
        if request.authorized_scope is not None:
            return await self._kernel.search_authorized(
                text=request.text,
                scope=request.authorized_scope,
                runtime=runtime,
                limit=resolve_read_limit(consumer, request, runtime),
                intent=intent,
            )
        result = await self._kernel.search(
            text=request.text,
            mode=retrieval_mode_for_request(request),
            targets=request.resolved_scope.targets,
            runtime=runtime,
            limit=resolve_read_limit(consumer, request, runtime),
            intent=intent,
        )
        if consumer in {MemoryReadConsumer.PLUGIN, MemoryReadConsumer.ADMIN}:
            return result
        return apply_total_hit_limit(result, resolve_read_limit(consumer, request, runtime))
