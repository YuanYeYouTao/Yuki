"""Public diagnostic and chat projections; no journal or execution capabilities."""

from __future__ import annotations

import hashlib
import json
import zlib
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from qq_ai_bot.control_plane.paging import Page, PageRequest
from qq_ai_bot.control_plane.problems import Problem, ProblemCode
from qq_ai_bot.control_plane.query_cursors import decode_query_cursor, encode_query_cursor
from qq_ai_bot.control_plane.query_types import (
    ChatEventView,
    ChatHistoryFilter,
    ControlQueryError,
    ExecutionTraceFilter,
    ExecutionTraceView,
    QueryCursorPhase,
    QueryResourceKind,
    SocialReceiptView,
)
from qq_ai_bot.domain.identity import ConversationId, PersonId, PresenceId
from qq_ai_bot.execution_trace.db_models import ExecutionTraceEntryModel
from qq_ai_bot.execution_trace.payload import decode_payload
from qq_ai_bot.persistence.models import ChatEventModel, ConversationMediaItemModel
from qq_ai_bot.social.db_models import SocialOperationModel


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _key(request: PageRequest, kind: QueryResourceKind, scope: str) -> str | None:
    if type(request) is not PageRequest:
        raise TypeError("request must be PageRequest")
    if request.cursor is None:
        return None
    phase, value = decode_query_cursor(request.cursor, expected_kind=kind)
    prefix = hashlib.sha256(scope.encode()).hexdigest()[:32] + ":"
    if phase is not QueryCursorPhase.CANONICAL or not value.startswith(prefix):
        raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))
    key = value[len(prefix) :]
    if not key:
        raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))
    return key


def _page[T](
    items: list[T],
    rows: Sequence[object],
    request: PageRequest,
    kind: QueryResourceKind,
    scope: str,
    key: str | None,
) -> Page[T]:
    more = len(rows) > request.limit
    cursor = None
    if more and key is not None:
        prefix = hashlib.sha256(scope.encode()).hexdigest()[:32]
        cursor = encode_query_cursor(kind, QueryCursorPhase.CANONICAL, f"{prefix}:{key}")
    return Page(items, next_cursor=cursor, snapshot_at=datetime.now(UTC))


def _trace_view(row: ExecutionTraceEntryModel, include_content: bool) -> ExecutionTraceView:
    try:
        return _build_trace_view(row, include_content)
    except (ValueError, TypeError, zlib.error) as exc:
        raise ControlQueryError(Problem(ProblemCode.STATE_MISMATCH)) from exc


def _build_trace_view(row: ExecutionTraceEntryModel, include_content: bool) -> ExecutionTraceView:
    payload = None
    if include_content and (
        (row.payload_status in {"recorded", "redacted"}) != (row.payload_gzip is not None)
    ):
        raise ValueError("inconsistent trace payload status")
    if include_content and row.payload_gzip is not None:
        try:
            payload = decode_payload(
                row.payload_gzip, size=row.payload_bytes, digest=row.payload_sha256 or ""
            )
        except (ValueError, TypeError, zlib.error) as exc:
            raise ControlQueryError(Problem(ProblemCode.STATE_MISMATCH)) from exc
    return ExecutionTraceView(
        id=row.id,
        conversation_id=ConversationId.parse(row.conversation_id) if row.conversation_id else None,
        turn_id=row.turn_id,
        operation_id=row.operation_id,
        parent_operation_id=row.parent_operation_id,
        work_id=row.work_id,
        activation_id=row.activation_id,
        execution_id=row.execution_id,
        source_event_id=row.source_event_id,
        delivered_event_id=row.delivered_event_id,
        generation=row.generation,
        origin=row.origin,
        kind=row.kind,
        payload_status=row.payload_status,
        payload_bytes=row.payload_bytes,
        created_at=_aware(row.created_at),
        expires_at=_aware(row.expires_at),
        payload=payload,
    )


class ControlExecutionQueryAdapter:
    def __init__(self, reader: Callable[[], AbstractAsyncContextManager[AsyncSession]]) -> None:
        self._read_sessions = reader

    @asynccontextmanager
    async def _reader(self) -> AsyncIterator[AsyncSession]:
        try:
            async with self._read_sessions() as session:
                yield session
        except SQLAlchemyError as exc:
            raise ControlQueryError(Problem(ProblemCode.OPERATION_UNAVAILABLE)) from exc

    async def list_execution_trace(
        self, request: PageRequest, *, scope: ExecutionTraceFilter, include_content: bool = False
    ) -> Page[ExecutionTraceView]:
        if type(scope) is not ExecutionTraceFilter:
            raise TypeError("scope must be ExecutionTraceFilter")
        partition = json.dumps(
            [
                scope.conversation_id.text if scope.conversation_id else None,
                scope.turn_id,
                scope.work_id,
                scope.execution_id,
                scope.source_event_id,
                scope.delivered_event_id,
                scope.descending,
            ]
        )
        key = _key(request, QueryResourceKind.EXECUTION_TRACE, partition)
        if key is not None and (
            not key.isascii()
            or not key.isdigit()
            or key != str(int(key))
            or int(key) < 1
            or int(key) > 2**63 - 1
        ):
            raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))
        stmt = select(ExecutionTraceEntryModel).where(
            ExecutionTraceEntryModel.expires_at > datetime.now(UTC)
        )
        if not include_content:
            stmt = stmt.options(defer(ExecutionTraceEntryModel.payload_gzip, raiseload=True))
        if scope.conversation_id and scope.delivered_event_id is None:
            stmt = stmt.where(
                ExecutionTraceEntryModel.conversation_id == scope.conversation_id.text
            )
        if scope.turn_id:
            stmt = stmt.where(ExecutionTraceEntryModel.turn_id == scope.turn_id)
        if scope.work_id:
            related_turns = select(ExecutionTraceEntryModel.turn_id).where(
                ExecutionTraceEntryModel.work_id == scope.work_id,
                ExecutionTraceEntryModel.expires_at > datetime.now(UTC),
            )
            stmt = stmt.where(ExecutionTraceEntryModel.turn_id.in_(related_turns))
        if scope.execution_id:
            stmt = stmt.where(ExecutionTraceEntryModel.execution_id == scope.execution_id)
        if scope.source_event_id:
            stmt = stmt.where(ExecutionTraceEntryModel.source_event_id == scope.source_event_id)
        if scope.delivered_event_id:
            # The confirmed outgoing event may belong to a different destination
            # than the original turn. Only its durable diagnostic link locates it.
            deliveries = (
                select(ExecutionTraceEntryModel.turn_id)
                .join(
                    ChatEventModel, ChatEventModel.id == ExecutionTraceEntryModel.delivered_event_id
                )
                .where(
                    ExecutionTraceEntryModel.delivered_event_id == scope.delivered_event_id,
                    ExecutionTraceEntryModel.expires_at > datetime.now(UTC),
                )
            )
            if scope.conversation_id:
                deliveries = deliveries.where(
                    ChatEventModel.canonical_conversation_id == scope.conversation_id.text
                )
            stmt = stmt.where(ExecutionTraceEntryModel.turn_id.in_(deliveries))
        if key:
            stmt = stmt.where(
                ExecutionTraceEntryModel.id < int(key)
                if scope.descending
                else ExecutionTraceEntryModel.id > int(key)
            )
        try:
            async with self._reader() as session:
                rows = list(
                    await session.scalars(
                        stmt.order_by(
                            ExecutionTraceEntryModel.id.desc()
                            if scope.descending
                            else ExecutionTraceEntryModel.id.asc()
                        ).limit(request.limit + 1)
                    )
                )
                selected = rows[: request.limit]
                return _page(
                    [_trace_view(row, include_content) for row in selected],
                    rows,
                    request,
                    QueryResourceKind.EXECUTION_TRACE,
                    partition,
                    str(selected[-1].id) if selected else None,
                )
        except SQLAlchemyError as exc:
            raise ControlQueryError(Problem(ProblemCode.OPERATION_UNAVAILABLE)) from exc

    async def read_execution_trace(
        self, entry_id: int, *, conversation_id: ConversationId | None = None
    ) -> ExecutionTraceView:
        if type(entry_id) is not int or not 1 <= entry_id <= 2**63 - 1:
            raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))
        if conversation_id is not None and type(conversation_id) is not ConversationId:
            raise TypeError("conversation_id must be ConversationId")
        stmt = select(ExecutionTraceEntryModel).where(
            ExecutionTraceEntryModel.id == entry_id,
            ExecutionTraceEntryModel.expires_at > datetime.now(UTC),
        )
        if conversation_id:
            stmt = stmt.where(ExecutionTraceEntryModel.conversation_id == conversation_id.text)
        async with self._reader() as session:
            row = await session.scalar(stmt)
            if row is None:
                raise ControlQueryError(Problem(ProblemCode.NOT_FOUND))
            return _trace_view(row, True)

    async def list_chat_events(
        self,
        request: PageRequest,
        *,
        conversation_id: ConversationId,
        include_content: bool = False,
        history: ChatHistoryFilter | None = None,
    ) -> Page[ChatEventView]:
        if type(conversation_id) is not ConversationId:
            raise TypeError("conversation_id must be ConversationId")
        history = history or ChatHistoryFilter()
        partition = json.dumps(
            [
                conversation_id.text,
                history.descending,
                history.event_id,
                history.since.isoformat() if history.since else None,
                history.until.isoformat() if history.until else None,
            ]
        )
        key = _key(request, QueryResourceKind.CHAT_EVENT, partition)
        if key is not None and (
            not key.isascii()
            or not key.isdigit()
            or key != str(int(key))
            or int(key) < 1
            or int(key) > 2**63 - 1
        ):
            raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))
        stmt = select(ChatEventModel).where(
            ChatEventModel.canonical_conversation_id == conversation_id.text
        )
        stmt = stmt.options(
            defer(ChatEventModel.segments_json, raiseload=True),
            defer(ChatEventModel.external_payload_json, raiseload=True),
        )
        if not include_content:
            stmt = stmt.options(
                *(
                    defer(column, raiseload=True)
                    for column in (
                        ChatEventModel.content,
                        ChatEventModel.audio_transcript,
                        ChatEventModel.visual_summary,
                        ChatEventModel.sender_group_card,
                        ChatEventModel.sender_nickname,
                    )
                )
            )
        if history.event_id is not None:
            stmt = stmt.where(ChatEventModel.id == history.event_id)
        if history.since is not None:
            stmt = stmt.where(ChatEventModel.occurred_at >= history.since)
        if history.until is not None:
            stmt = stmt.where(ChatEventModel.occurred_at <= history.until)
        if key:
            stmt = stmt.where(
                ChatEventModel.id < int(key) if history.descending else ChatEventModel.id > int(key)
            )
        order = ChatEventModel.id.desc() if history.descending else ChatEventModel.id.asc()
        async with self._reader() as session:
            rows = list(await session.scalars(stmt.order_by(order).limit(request.limit + 1)))
            selected = rows[: request.limit]
            attachments: dict[int, list[int]] = {}
            if selected:
                media = await session.execute(
                    select(
                        ConversationMediaItemModel.source_event_id,
                        ConversationMediaItemModel.attachment_index,
                    )
                    .where(
                        ConversationMediaItemModel.conversation_id == conversation_id.text,
                        ConversationMediaItemModel.source_event_id.in_(
                            [row.id for row in selected]
                        ),
                    )
                    .order_by(ConversationMediaItemModel.attachment_index)
                )
                for event_id, index in media:
                    attachments.setdefault(event_id, []).append(index)
            items = [
                ChatEventView(
                    event_id=row.id,
                    conversation_id=conversation_id,
                    direction=row.direction,
                    event_kind=row.event_kind,
                    origin=row.origin,
                    author_kind=row.author_kind,
                    author_person_id=PersonId.parse(row.author_person_id)
                    if row.author_person_id
                    else None,
                    author_presence_id=PresenceId.parse(row.author_presence_id)
                    if row.author_presence_id
                    else None,
                    occurred_at=_aware(row.occurred_at),
                    observed_at=_aware(row.observed_at),
                    reply_to_event_id=row.reply_to_event_id,
                    caused_by_event_id=row.caused_by_event_id,
                    content=row.content if include_content else None,
                    audio_transcript=row.audio_transcript if include_content else None,
                    visual_summary=row.visual_summary if include_content else None,
                    sender_display_name=(row.sender_group_card or row.sender_nickname)
                    if include_content
                    else None,
                    attachment_indexes=tuple(attachments.get(row.id, ())),
                    suppression_status=row.suppression_status,
                )
                for row in selected
            ]
            return _page(
                items,
                rows,
                request,
                QueryResourceKind.CHAT_EVENT,
                partition,
                str(selected[-1].id) if selected else None,
            )

    async def list_social_receipts(
        self, request: PageRequest, *, conversation_id: ConversationId
    ) -> Page[SocialReceiptView]:
        if type(conversation_id) is not ConversationId:
            raise TypeError("conversation_id must be ConversationId")
        partition = conversation_id.text
        key = _key(request, QueryResourceKind.SOCIAL_RECEIPT, partition)
        stmt = select(SocialOperationModel).where(
            SocialOperationModel.source_conversation_id == partition
        )
        if key:
            # Receipt UUID ordering is stable; its original lifecycle stays untouched.
            stmt = stmt.where(SocialOperationModel.id > key)
        async with self._reader() as session:
            rows = list(
                await session.scalars(
                    stmt.order_by(SocialOperationModel.id).limit(request.limit + 1)
                )
            )
            selected = rows[: request.limit]
            items = [
                SocialReceiptView(
                    operation_id=row.id,
                    source_conversation_id=conversation_id,
                    source_execution_key=row.source_turn_id,
                    tool_call_id=row.tool_call_id,
                    target_kind=row.target_kind,
                    target_id=row.target_id,
                    presence_id=PresenceId.parse(row.presence_id) if row.presence_id else None,
                    action=row.action,
                    status=row.status,
                    event_id=row.event_id,
                    error_category=row.error_category,
                    created_at=_aware(row.created_at),
                    updated_at=_aware(row.updated_at),
                )
                for row in selected
            ]
            return _page(
                items,
                rows,
                request,
                QueryResourceKind.SOCIAL_RECEIPT,
                partition,
                selected[-1].id if selected else None,
            )
