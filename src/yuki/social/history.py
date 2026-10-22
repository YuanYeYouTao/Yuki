"""Read the canonical ledger with real source grants and returned-source metadata."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import select, text

from yuki.capabilities.invocation import current_invocation
from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.execution_trace.db_models import ExecutionTraceStateModel
from yuki.identity.person_identity import current_person_id
from yuki.persistence.database import Database
from yuki.persistence.event_view import event_view
from yuki.persistence.models import ChatEventModel
from yuki.persistence.repositories import EventLedgerRepository
from yuki.persistence.repository_helpers import _event_record
from yuki.persistence.repository_records import EventRecord
from yuki.services.history_query import HistoryQuery
from yuki.social.db_models import SocialOperationModel
from yuki.social.history_sources import history_delivery_predicates, history_source_statement
from yuki.social.models import SocialError

if TYPE_CHECKING:
    from yuki.social.service import SocialContext


class HistoryResult(dict[str, Any]):
    """The model payload plus Host-only evidence for the original submit boundary."""

    source_event_ids: tuple[int, ...] = ()
    source_privacy_generation: int = 0


async def read_history(
    database: Database, args: dict[str, Any], context: SocialContext, *, runtime: Any = None
) -> HistoryResult:
    request = HistoryQuery.model_validate(args)
    invocation = current_invocation.get()
    runtime = runtime if runtime is not None else invocation.runtime if invocation else None
    async with database.read_session() as session:
        await session.execute(text("BEGIN"))
        operation = None
        anchor = None
        original_event_id = None
        original_conversation_id = None
        receipt_anchor_status = None
        if request.target is not None and request.target.operation_id is not None:
            operation = await session.get(SocialOperationModel, str(request.target.operation_id))
            if operation is None or operation.source_conversation_id != context.conversation_id:
                raise SocialError("history_receipt_unavailable")
            original = (
                await session.get(ChatEventModel, operation.event_id)
                if operation.event_id is not None
                else None
            )
            original_event_id = operation.original_event_id or operation.event_id
            original_conversation_id = operation.original_conversation_id or (
                original.canonical_conversation_id if original is not None else None
            )
            anchor = (
                _event_record(original)
                if original is not None
                and original.id == original_event_id
                and original.canonical_conversation_id == original_conversation_id
                else None
            )
            receipt_anchor_status = (
                "available"
                if anchor is not None
                else "missing"
                if original_event_id is not None and operation.event_id is None
                else "unavailable"
            )

        source_selected = request.source is not None and bool(
            request.source.model_dump(exclude_none=True)
        )
        event_id = request.event_id or original_event_id
        center_conversation_id = None
        if request.view == "around":
            if event_id is None:
                raise ValueError("around requires event_id")
            if operation is not None and event_id == original_event_id:
                center_conversation_id = original_conversation_id
            else:
                center_conversation_id = await session.scalar(
                    select(ChatEventModel.canonical_conversation_id).where(
                        ChatEventModel.id == event_id
                    )
                )
            conversation_id = center_conversation_id or ""
        elif operation is not None:
            conversation_id = original_conversation_id or ""
        elif request.target is not None or request.view == "search" or source_selected:
            conversation_id = None
        else:
            conversation_id = context.conversation_id

        statement = await history_source_statement(
            session,
            context=context,
            target=request.target,
            source=request.source,
            runtime=runtime,
            conversation_id=conversation_id,
        )
        # Only IDs and facts required by the candidate predicates are prepared upfront.
        entries = (
            await session.execute(
                statement.with_only_columns(
                    statement.selected_columns.conversation_id,
                    statement.selected_columns.platform,
                ).order_by(CanonicalConversationModel.id)
            )
        ).all()
        conversation_ids = tuple(row.conversation_id for row in entries)
        if not entries and operation is None and not source_selected and request.view != "around":
            if getattr(runtime, "read_target_id", None) is not None or (
                runtime is not None and getattr(runtime, "origin", None) == "plugin_background"
            ):
                raise SocialError("history_scope_denied")
            if request.target is not None:
                raise SocialError("history_unavailable")
        acp_ids = tuple(row.conversation_id for row in entries if row.platform == "acp")
        predicates = history_delivery_predicates(acp_ids)
        ledger = EventLedgerRepository(database)
        common: dict[str, Any] = dict(
            session=session,
            conversation_id=conversation_ids,
            event_kind=request.event_kind,
            predicates=predicates,
        )
        records: tuple[EventRecord, ...]
        neighbours: tuple[EventRecord, ...] = ()
        matched_fields: dict[int, tuple[str, ...]] = {}
        payload: dict[str, Any]
        if request.view == "around":
            earlier: tuple[EventRecord, ...]
            later: tuple[EventRecord, ...]
            if center_conversation_id not in conversation_ids or (
                operation is not None and anchor is None and event_id == original_event_id
            ):
                center, earlier, later = None, (), ()
            else:
                center, earlier, later = await ledger.history_around(
                    **{**common, "conversation_id": center_conversation_id},
                    event_id=cast(int, event_id),
                    before=request.before or 0,
                    after=request.after or 0,
                )
            records = (*earlier, *((center,) if center else ()), *later)
            payload = dict(
                center_event_id=center.id if center else event_id,
                before=len(earlier),
                after=len(later),
                has_more=False,
                center_status="available" if center is not None else "unavailable",
            )
            if center is None:
                payload["anchor_status"] = "unavailable"
                if (
                    operation is not None
                    and event_id == original_event_id
                    and receipt_anchor_status == "available"
                ):
                    receipt_anchor_status = "unavailable"
        elif conversation_ids:
            page = await ledger.query_history(
                **common,
                order=request.order,
                limit=request.limit or 20,
                cursor=request.cursor,
                person_id=str(request.person_id) if request.person_id else None,
                author_kind=request.author_kind,
                start_at=request.start_at,
                end_at=request.end_at,
                has=request.has,
                keyword=request.keyword,
                match=request.match,
                exclude=request.exclude,
                context_before=request.context_before,
                context_after=request.context_after,
            )
            records, neighbours, matched_fields = page.events, page.context, page.matched_fields
            payload = dict(has_more=page.has_more, next_cursor=page.next_cursor)
        else:
            records = ()
            payload = dict(has_more=False, next_cursor=None)

        source_records = (*records, *neighbours)
        receipt_record = None
        if operation is not None and anchor is not None:
            receipt_record = next((row for row in source_records if row.id == anchor.id), None)
            if receipt_record is None and original_conversation_id in conversation_ids:
                receipt_record, _, _ = await ledger.history_around(
                    **{**common, "conversation_id": original_conversation_id},
                    event_id=anchor.id,
                    before=0,
                    after=0,
                )
            if receipt_record is None:
                receipt_anchor_status = "unavailable"
            elif receipt_record.id not in {row.id for row in source_records}:
                source_records = (*source_records, receipt_record)
        returned_ids = tuple(
            dict.fromkeys(str(row.canonical_conversation_id) for row in source_records)
        )
        single_id = conversation_ids[0] if len(conversation_ids) == 1 else None
        metadata_ids = (*returned_ids, *((single_id,) if single_id is not None else ()))
        sources: dict[str, dict[str, Any]] = {}
        if metadata_ids:
            metadata = (
                await session.execute(
                    statement.where(CanonicalConversationModel.id.in_(metadata_ids))
                )
            ).mappings()
            for row in metadata:
                sources[row["conversation_id"]] = {
                    "conversation_id": row["conversation_id"],
                    "platform": row["platform"],
                    "scope": "private" if row["conversation_kind"] == "private" else "group",
                    "conversation_name": row["conversation_name"] or "",
                    "binding_id": row["binding_id"],
                    "presence_id": row["presence_id"],
                    "thread_id": row["thread_id"],
                    "person_id": row["person_id"],
                    "space_id": row["space_id"],
                    "generation": row["generation"],
                }

        def render(row: EventRecord) -> dict[str, Any]:
            source = sources[str(row.canonical_conversation_id)]
            return {
                **event_view(row),
                "platform": source["platform"],
                "conversation_name": source["conversation_name"],
                "thread_id": source["thread_id"],
            }

        result = HistoryResult(
            source="canonical_ledger",
            coverage="observed",
            conversation_id=single_id,
            events=[render(row) for row in records],
            returned_count=len(records),
            order=request.order,
            start_at=request.start_at.isoformat() if request.start_at else None,
            end_at=request.end_at.isoformat() if request.end_at else None,
            matched_fields={str(key): list(value) for key, value in matched_fields.items()},
            context=[render(row) for row in neighbours],
            **payload,
        )
        if single_id is not None:
            selected_source = sources[single_id]
            result.update(
                platform=selected_source["platform"],
                binding_id=selected_source["binding_id"],
                target_id=await current_person_id(session, selected_source["person_id"])
                if selected_source["person_id"]
                else selected_source["space_id"],
                generation=selected_source["generation"],
                kind="person" if selected_source["scope"] == "private" else "space",
            )
        if operation is not None:
            result.update(
                operation_id=operation.id,
                receipt_status=operation.status,
                event_id=original_event_id,
                original_conversation_id=original_conversation_id,
                anchor_status=receipt_anchor_status,
                receipt_anchor={
                    "event_id": original_event_id,
                    "conversation_id": original_conversation_id,
                    "status": receipt_anchor_status,
                },
            )
            if receipt_record is not None and receipt_record.id not in {
                row.id for row in (*records, *neighbours)
            }:
                result["receipt_anchor"]["event"] = render(receipt_record)
        result["sources"] = [sources[identity] for identity in returned_ids]
        result.source_event_ids = tuple(dict.fromkeys(row.id for row in source_records))
        result.source_privacy_generation = (
            await session.scalar(
                select(ExecutionTraceStateModel.privacy_generation).where(
                    ExecutionTraceStateModel.id == 1
                )
            )
            or 0
        )
        return result
