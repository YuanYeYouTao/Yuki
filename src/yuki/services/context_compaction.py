"""Select a paired prefix of the actual Main input for context maintenance."""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.conversation.frozen_fragments import EventFragment, FrozenFragments
from yuki.conversation.observation_models import ContextObservationModel
from yuki.conversation.projections import PromptProjectionRepository
from yuki.domain.messages import (
    ChatMessage,
    ChatRequest,
    FunctionCallOutput,
    ProviderContinuation,
)
from yuki.model_runtime.capacity import (
    InputSequenceCost,
    RequestTokenCounter,
    message_token_cost,
)
from yuki.persistence.event_repository import ConversationReadVersion
from yuki.runtime.work_repository import WorkCapacityError
from yuki.services.turn_transcript import TurnTranscript, tool_result_evidence

EXECUTION_COMPACT_INSTRUCTION = (
    "[Host 上下文压缩要求]\n"
    "本次只整理当前激活的私有执行进展，不执行工具、不发消息。"
    "不要复述前面的公共聊天历史或本轮运行资料。"
    "目标只认本次输入及明确接纳的当前 Work，不从公共群史、工作目录或引用中猜测原任务。"
    "保留当前目标、追加要求、有效约束、已经确认的结果、失败与未知状态、"
    "必要发现、文件/资料句柄、原来源引用和下一步。"
    "不得把未完成事项写成已完成，不要求额外汇报或所有子任务成功。"
    "仅返回可供本轮继续的文字检查点。"
)

PUBLIC_COMPACT_INSTRUCTION = (
    "[Host 会话上下文压缩要求]\n"
    "整理以上共同聊天历史，保留人物归属、决定、否定约束、最新纠正、"
    "未决问题及可回查的内部事件引用。"
    "不执行工具、不发送消息、不写长期记忆；仅返回可续接的文字检查点。"
)


@dataclass(frozen=True, slots=True)
class ExecutionCompactPlan:
    request: ChatRequest
    transcript: TurnTranscript
    retained: tuple[ChatMessage, ...]
    public: dict[str, Any] | None = None


@dataclass(slots=True)
class PublicCompactContext:
    """The actual public prefix and its existing projection owner."""

    messages: tuple[ChatMessage, ...]
    static_count: int
    fragments: FrozenFragments
    version: ConversationReadVersion
    context_key: str
    repository: PromptProjectionRepository
    adopt: Callable[[str, int, FrozenFragments], Awaitable[tuple[ChatMessage, ...]]]
    individual: tuple[EventFragment, ...] = ()


class CompactReceipt:
    """Private paid-input evidence for an activation without a business Work.

    This existing observation owner is never selected as shared history or
    Memory. The public result is published separately through the projection.
    """

    def __init__(self, context: PublicCompactContext, execution_id: str, actor_id: str) -> None:
        self.context = context
        self.key = f"compact:{execution_id}"
        self.actor_id = actor_id

    async def read(self) -> dict[str, Any] | None:
        import json

        from sqlalchemy import or_, select

        from yuki.conversation.canonical_db_models import CanonicalConversationModel
        from yuki.conversation.observations import privacy_generation
        from yuki.runtime.work_repository import WorkConflict

        async with self.context.repository.database.read_session() as reader:
            row = await reader.scalar(
                select(ContextObservationModel)
                .where(
                    ContextObservationModel.source_key.startswith(self.key + ":", autoescape=True),
                    ContextObservationModel.conversation_id == self.context.version.conversation_id,
                    ContextObservationModel.generation == self.context.version.generation,
                    or_(
                        ContextObservationModel.actor_id == self.actor_id,
                        ContextObservationModel.payload_json == '{"phase":"invalidated"}',
                    ),
                )
                .order_by(ContextObservationModel.created_at.desc())
                .limit(1)
            )
            if row is None:
                return None
            payload: dict[str, Any] = json.loads(row.payload_json)
            if payload["phase"] == "invalidated":
                raise WorkConflict("context_compaction_source_changed")
            owner = await reader.get(CanonicalConversationModel, row.conversation_id)
            if (
                owner is None
                or owner.generation != row.generation
                or owner.prompt_source_revision != payload["source_revision"]
                or await privacy_generation(reader) != payload["privacy_generation"]
            ):
                raise WorkConflict("context_compaction_source_changed")
            return payload

    async def write(
        self,
        payload: dict[str, Any],
        *,
        publication: Callable[[AsyncSession], Awaitable[Any]] | None = None,
    ) -> None:
        import json

        from yuki.conversation.canonical_db_models import CanonicalConversationModel
        from yuki.conversation.observations import privacy_generation
        from yuki.runtime.work_repository import WorkConflict

        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        version = self.context.version
        async with self.context.repository.database.immediate_session() as writer:
            source = await writer.get(CanonicalConversationModel, version.conversation_id)
            privacy = await privacy_generation(writer)
            if (
                source is None
                or source.generation != version.generation
                or (
                    payload["phase"] != "response"
                    and source.prompt_source_revision != payload["source_revision"]
                )
                or privacy != payload["privacy_generation"]
            ):
                raise WorkConflict("context_compaction_source_changed")
            if publication is not None:
                await publication(writer)
            statement = insert(ContextObservationModel).values(
                id=str(uuid4()),
                conversation_id=version.conversation_id,
                generation=version.generation,
                actor_id=self.actor_id,
                read_scope=version.scope.key,
                source_key=f"{self.key}:{payload['ordinal']}:{payload['phase']}",
                version=1,
                privacy_generation=payload["privacy_generation"],
                payload_json=encoded,
                parent_sources_json="[]",
                created_at=datetime.now(UTC),
            )
            result = await writer.execute(
                statement.on_conflict_do_nothing(
                    index_elements=[ContextObservationModel.source_key],
                ).returning(ContextObservationModel.id)
            )
            if result.scalar_one_or_none() is None:
                raise WorkConflict("context_compaction_owner_changed")


async def current_public_compact(
    context: PublicCompactContext, public: dict[str, Any], summary: str
) -> tuple[dict[str, Any], str]:
    """Keep frozen paid history and append currently authorised newer events."""
    previous = await context.repository.read_checkpoint(public["conversation_id"])
    coverage = public["coverage"]
    retained = FrozenFragments.load(public["items"])
    if (
        previous is not None
        and previous.selected_summary_text is not None
        and previous.selected_summary_coverage >= coverage
    ):
        # A later published checkpoint already owns this prefix. Restoring an
        # older paid response must not replace it with an earlier coverage.
        coverage = previous.selected_summary_coverage
        summary = previous.selected_summary_text
        retained = previous.fragments
    grouped: list[EventFragment] = []
    for fragment in context.fragments.iter_fragments():
        ids, message = fragment.event_ids, fragment.message
        fresh = tuple(identity for identity in ids if identity > coverage)
        if not fresh:
            continue
        if fresh == ids:
            grouped.append((ids, message))
        else:
            selected = tuple(
                (keys, value) for keys, value in context.individual if set(keys) <= set(fresh)
            )
            if {identity for keys, _ in selected for identity in keys} != set(fresh):
                from yuki.conversation.projections import ProjectionConflict

                raise ProjectionConflict("new event fragment coverage is incomplete")
            grouped.extend(selected)
    retained = retained.extend_history(tuple(grouped), context.individual)
    return {**public, "coverage": coverage, "items": list(retained.items)}, summary


async def public_compact_publication(
    repository: PromptProjectionRepository,
    public: dict[str, Any],
    summary: str,
) -> Callable[[AsyncSession], Awaitable[Any]]:
    from yuki.conversation.projections import SUMMARY_RENDERER_VERSION

    previous = await repository.read(public["conversation_id"])
    reason = await repository.invalidation_reason(public["conversation_id"])
    return await repository.prepare_commit(
        view_key=public["conversation_id"],
        conversation_id=public["conversation_id"],
        generation=public["generation"],
        expected_source_revision=public["source_revision"],
        starts_after_event_id=public["starts_after_event_id"],
        context_key=public["context_key"],
        fragments=FrozenFragments.load(public["items"]),
        expected_epoch=previous.epoch_id if previous else None,
        expected_revision=previous.revision if previous else 0,
        rebuild_reason=reason or "compact",
        selected_summary_text=summary,
        selected_summary_coverage=public["coverage"],
        selected_summary_kind="compact",
        selected_summary_renderer=SUMMARY_RENDERER_VERSION,
        previous_snapshot=previous,
    )


def render_context_checkpoint(summary: str, coverage: int) -> ChatMessage:
    return ChatMessage(
        "user",
        "[Conversation compact checkpoint]\n"
        f"[Source events through internal event_id={coverage}]\n" + summary,
    )


def public_compact_plan(
    context: PublicCompactContext,
    original: TurnTranscript,
    main_request: ChatRequest,
    *,
    initial: tuple[ChatMessage, ...],
    input_budget: int | None,
    prepare: Callable[[ChatRequest], ChatRequest],
    focus: str = "",
) -> ExecutionCompactPlan:
    instruction = PUBLIC_COMPACT_INSTRUCTION + ("\n整理重点：" + focus if focus else "")
    fragments = tuple(context.fragments.iter_fragments())
    leading = context.messages[: len(context.messages) - len(fragments)]
    boundary = 0
    if input_budget is None:
        boundary = len(fragments)
    else:
        counter = RequestTokenCounter(
            prepare(replace(main_request, messages=(), continuation=None, continuation_items=()))
        )
        instruction_cost = message_token_cost(ChatMessage("user", instruction))
        for message in leading:
            counter.append(message)
        for ordinal, fragment in enumerate(fragments, start=1):
            counter.append(fragment.message)
            if counter.estimate(instruction_cost) > input_budget:
                break
            boundary = ordinal
    if boundary:
        covered = fragments[:boundary]
        prefix = TurnTranscript((*leading, *(fragment.message for fragment in covered)))
        prefix.chain_id = original.chain_id
        prefix.append(ChatMessage("user", instruction))
        request = replace(
            main_request,
            messages=prefix.request().messages,
            continuation=None,
            continuation_items=(),
        )
        retained = FrozenFragments.from_blocks((fragments[boundary:],))
        private = original.entries()[len(initial) :]
        if any(not isinstance(item, ChatMessage) for item in private):
            raise WorkCapacityError("model_request_capacity")
        return ExecutionCompactPlan(
            request,
            prefix,
            tuple(item for item in private if isinstance(item, ChatMessage)),
            {
                "conversation_id": context.version.conversation_id,
                "generation": context.version.generation,
                "source_revision": context.version.prompt_source_revision,
                "starts_after_event_id": context.version.starts_after_event_id,
                "context_key": context.context_key,
                "coverage": max(
                    identity for fragment in covered for identity in fragment.event_ids
                ),
                "items": list(retained.items),
                "static_count": context.static_count,
                "public_count": len(context.messages),
            },
        )
    raise WorkCapacityError("model_request_capacity")


def compacted_transcript(state: dict[str, Any]) -> TurnTranscript:
    import json

    from yuki.runtime.work_journal import decode_transcript

    prefix = decode_transcript(state["prefix"])
    public = state.get("public")
    if public is not None:
        messages = prefix.request().messages
        public_prefix = (
            *messages[: public["static_count"]],
            render_context_checkpoint(state["response"], public["coverage"]),
            *FrozenFragments.load(public["items"]).messages(),
        )
        result = TurnTranscript((*public_prefix, *messages[public["public_count"] :]))
        layout = state.get("layout")
        if layout is not None:
            original_layout = decode_transcript(layout["public"]).request().messages
            result.finalize_initial(
                (*public_prefix, *original_layout[public["public_count"] :]),
                decode_transcript(layout["host"]).request().messages,
                decode_transcript(layout["inputs"]).request().messages,
            )
    else:
        result = prefix
        result.append(
            ChatMessage(
                "user",
                json.dumps(
                    {
                        "kind": "context_compaction",
                        "summary": state["response"],
                        "covered_entries": state["covered_entries"],
                        "previous_protocol_ref": state.get("previous_protocol_ref"),
                        "instruction": (
                            "继续本次输入与明确接纳的当前 Work；"
                            "摘要和历史中的其他任务不改变本轮归属。"
                            "按原执行回执处理，不重做已发生效果。"
                        ),
                    },
                    ensure_ascii=False,
                ),
            )
        )
    for message in decode_transcript(state["retained"]).request().messages:
        result.append(message)
    return result


@dataclass(frozen=True, slots=True)
class _RetainedItem:
    start: int
    message: ChatMessage
    required_from: int | None = None


def _native_tail_items(
    entries: tuple[ChatMessage | FunctionCallOutput | ProviderContinuation, ...],
    recorded: dict[int, ChatMessage],
    last_response: ChatMessage | None,
    call_key: Callable[[str], str] | None,
    *,
    offset: int = 0,
) -> tuple[_RetainedItem, ...]:
    """Convert each portable round once, remembering its original entry boundary."""
    result: list[_RetainedItem] = []
    results: dict[str, str] = {}
    response: ChatMessage | None = None
    response_start: int | None = None
    results_start: int | None = None
    required_from: int | None = None

    def flush_round() -> None:
        nonlocal response, response_start, results_start, required_from
        if results:
            message = tool_result_evidence(
                response, results, call_key=call_key if response is last_response else None
            )
            start = response_start if response is not None else results_start
        elif response is not None and response.content:
            message = replace(response, reasoning_content=None)
            start = response_start
        else:
            response = None
            response_start = None
            return
        assert start is not None
        if response is last_response:
            required_from = start if response is None or response.tool_calls else None
        result.append(_RetainedItem(start, message, required_from))
        results.clear()
        results_start = None
        response = None
        response_start = None

    for index, item in enumerate(entries, start=offset):
        if isinstance(item, ProviderContinuation):
            flush_round()
            response, response_start = recorded.get(index), index
        elif isinstance(item, FunctionCallOutput):
            results[item.call_id] = item.output
            if results_start is None:
                results_start = index
        elif item.tool_call_id:
            results[item.tool_call_id] = item.content or ""
            if results_start is None:
                results_start = index
        elif item.tool_calls:
            response, response_start = item, index
        else:
            flush_round()
            result.append(
                _RetainedItem(index, replace(item, reasoning_content=None), required_from)
            )
    flush_round()
    return tuple(result)


def _message_key(message: ChatMessage) -> tuple[object, ...]:
    # Opaque values can contain mutable JSON. Use the portable fields to index
    # candidates, then compare the complete message for exact membership.
    return (
        message.role,
        message.content,
        message.tool_calls,
        message.tool_call_id,
        message.reasoning_content,
        message.images,
    )


class _TailIndex:
    """Suffix membership and additive costs for every already-paired boundary."""

    def __init__(
        self,
        items: tuple[_RetainedItem, ...],
        count: int,
        initial: tuple[ChatMessage, ...],
        retained_public: tuple[ChatMessage, ...],
    ) -> None:
        self.items = items
        self.starts = tuple(item.start for item in items)
        initial_index: dict[tuple[object, ...], list[ChatMessage]] = {}
        for message in initial:
            initial_index.setdefault(_message_key(message), []).append(message)
        self.public = tuple(
            message
            for message in retained_public
            if message not in initial_index.get(_message_key(message), ())
        )
        public_index: dict[tuple[object, ...], list[int]] = {}
        for ordinal, message in enumerate(self.public):
            public_index.setdefault(_message_key(message), []).append(ordinal)
        self.last_public = [-1] * len(self.public)
        required = [InputSequenceCost()] * (count + 1)
        for item in items:
            for ordinal in public_index.get(_message_key(item.message), ()):
                if item.message == self.public[ordinal]:
                    self.last_public[ordinal] = item.start
            if item.required_from is not None:
                required[item.required_from] += message_token_cost(item.message)
        self.required_cost = [InputSequenceCost()] * (count + 1)
        running = InputSequenceCost()
        for boundary in range(count, -1, -1):
            running += required[boundary]
            self.required_cost[boundary] = running
        growth = [InputSequenceCost()] * (count + 1)
        for ordinal, message in enumerate(self.public):
            growth[self.last_public[ordinal] + 1] += message_token_cost(message)
        self.public_cost: list[InputSequenceCost] = []
        running = InputSequenceCost()
        for extra in growth:
            running += extra
            self.public_cost.append(running)

    def retained(self, boundary: int) -> tuple[ChatMessage, ...]:
        public = tuple(
            message
            for message, last in zip(self.public, self.last_public, strict=True)
            if last < boundary
        )
        return (
            *public,
            *(item.message for item in self.items[bisect_left(self.starts, boundary) :]),
        )


def execution_compact_plan(
    transcript: TurnTranscript,
    initial: tuple[ChatMessage, ...],
    main_request: ChatRequest,
    *,
    input_budget: int | None,
    prepare: Callable[[ChatRequest], ChatRequest],
    retained_public: tuple[ChatMessage, ...] = (),
    call_key: Callable[[str], str] | None = None,
    responses: tuple[ChatMessage, ...] = (),
    focus: str = "",
) -> ExecutionCompactPlan:
    """Select the latest fitting paired prefix without cloning every candidate.

    Checkpoints replace earlier protocol state, so candidate costs can decrease.
    Scan their actual serialized character/media costs once and select by the
    original boundary order; never binary-search across checkpoint changes.
    """
    entries = transcript.entries()
    checkpoints = [
        index for index, entry in enumerate(entries) if isinstance(entry, ProviderContinuation)
    ]
    recorded = (
        dict(zip(checkpoints[-len(responses) :], responses[-len(checkpoints) :], strict=False))
        if responses
        else {}
    )
    counter = (
        RequestTokenCounter(
            prepare(replace(main_request, messages=(), continuation=None, continuation_items=()))
        )
        if input_budget is not None
        else None
    )
    instruction = ChatMessage(
        "user", EXECUTION_COMPACT_INSTRUCTION + ("\n" + focus if focus else "")
    )
    instruction_cost = message_token_cost(instruction) if counter is not None else None
    pending: set[str] = set()
    candidates: list[int] = []
    for index, entry in enumerate(entries):
        if counter is not None:
            counter.append(entry)
        if index in recorded:
            pending.update(call.id for call in recorded[index].tool_calls)
        elif isinstance(entry, ChatMessage):
            pending.update(call.id for call in entry.tool_calls)
            if entry.tool_call_id:
                pending.discard(entry.tool_call_id)
        elif isinstance(entry, FunctionCallOutput):
            pending.discard(entry.call_id)
        if (
            index + 1 > len(initial)
            and not pending
            and not isinstance(entry, ProviderContinuation)
            and not (
                index + 1 < len(entries) and isinstance(entries[index + 1], FunctionCallOutput)
            )
            and (
                counter is None
                or (
                    instruction_cost is not None
                    and counter.estimate(instruction_cost) <= cast(int, input_budget)
                )
            )
        ):
            candidates.append(index + 1)
    if pending or not candidates:
        raise WorkCapacityError("model_request_capacity")
    last_response_index = next(
        (
            index
            for index in range(len(entries) - 1, len(initial) - 1, -1)
            if index in recorded
            or (isinstance(entry := entries[index], ChatMessage) and entry.role == "assistant")
        ),
        None,
    )
    if last_response_index is None:
        # A fresh/rebased private tail has had no normal model response yet.
        # It can include original portable receipts; user text is not a seen flag.
        raise WorkCapacityError("model_request_capacity")
    response_entry = entries[last_response_index]
    last_response = (
        recorded[last_response_index]
        if last_response_index in recorded
        else cast(ChatMessage, response_entry)
    )
    keep_from = last_response_index if last_response.tool_calls else len(entries)
    native_from = [False] * (len(entries) + 1)
    for index in range(len(entries) - 1, -1, -1):
        native_from[index] = native_from[index + 1] or not isinstance(entries[index], ChatMessage)
    if counter is None:
        boundary = candidates[-1]
        keep_boundary = min(boundary, keep_from)
        suffix = entries[keep_boundary:]
        kept = (
            tuple(
                item.message
                for item in _native_tail_items(
                    suffix, recorded, last_response, call_key, offset=keep_boundary
                )
            )
            if native_from[keep_boundary]
            else tuple(item for item in suffix if isinstance(item, ChatMessage))
        )
        public = tuple(
            message for message in retained_public if message not in initial and message not in kept
        )
        retained = (*public, *kept)
    else:
        first_candidate = min(candidates[0], keep_from)
        raw = tuple(
            _RetainedItem(
                index,
                item,
                last_response_index
                if last_response_index is not None
                and index >= last_response_index
                and last_response is not None
                and last_response.tool_calls
                else None,
            )
            for index, item in enumerate(entries)
            if index >= first_candidate and isinstance(item, ChatMessage)
        )
        raw_index = _TailIndex(raw, len(entries), initial, retained_public)
        native_index = (
            _TailIndex(
                _native_tail_items(
                    entries[first_candidate:],
                    recorded,
                    last_response,
                    call_key,
                    offset=first_candidate,
                ),
                len(entries),
                initial,
                retained_public,
            )
            if native_from[first_candidate]
            else raw_index
        )
        initial_cost = InputSequenceCost()
        for message in initial:
            initial_cost += message_token_cost(message)
        for boundary in reversed(candidates):
            keep_boundary = min(boundary, keep_from)
            selected = native_index if native_from[keep_boundary] else raw_index
            if counter.estimate_messages(
                initial_cost
                + selected.public_cost[keep_boundary]
                + selected.required_cost[keep_boundary]
            ) <= cast(int, input_budget):
                retained = selected.retained(keep_boundary)
                break
        else:
            raise WorkCapacityError("model_request_capacity")
    prefix = transcript.prefix(boundary)
    prefix.append(instruction)
    sequence = prefix.request()
    request = replace(
        main_request,
        messages=sequence.messages,
        continuation=sequence.continuation,
        continuation_items=sequence.items,
    )
    return ExecutionCompactPlan(request, prefix, retained)
