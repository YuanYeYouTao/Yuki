"""Resolve real entry grants, then read the one canonical ledger query core."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import or_, select, text

from yuki.capabilities.invocation import current_invocation
from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.execution_trace.db_models import ExecutionTraceStateModel
from yuki.identity.db_models import IdentityBindingModel, SpaceBindingModel
from yuki.identity.person_identity import (
    current_person_id,
    person_enabled_condition,
    person_family_condition,
)
from yuki.persistence.database import Database
from yuki.persistence.event_view import event_view
from yuki.persistence.models import ChatEventModel
from yuki.persistence.repositories import EventLedgerRepository
from yuki.persistence.repository_helpers import _event_record
from yuki.persistence.repository_records import EventRecord
from yuki.services.history_query import HistoryQuery
from yuki.social.db_models import SocialOperationModel
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
    runtime = (
        runtime if runtime is not None else invocation.runtime if invocation is not None else None
    )
    async with database.sessions() as session:
        await session.execute(text("BEGIN"))
        source = await session.get(CanonicalConversationModel, context.conversation_id)
        if source is None:
            raise SocialError("history_unavailable")
        actor_id = getattr(context.actor, "person_id", None)
        if actor_id is not None and not await session.scalar(
            select(person_enabled_condition(actor_id))
        ):
            raise SocialError("permission_denied")
        selector = (
            request.target.model_dump(exclude_none=True, mode="json") if request.target else {}
        )
        allowed = getattr(runtime, "read_target_id", None)
        granted_scope = getattr(runtime, "read_scope", None)
        anchor = None
        operation = None
        original_event_id = None
        original_conversation_id = None
        receipt_anchor_status = None
        if "operation_id" in selector:
            operation = await session.get(SocialOperationModel, selector["operation_id"])
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
            # The nullable FK proves a live link. A retained original integer must
            # never resolve a replacement row after the original was deleted.
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
            if original_conversation_id is None:
                result = HistoryResult(
                    operation_id=operation.id,
                    receipt_status=operation.status,
                    event_id=original_event_id,
                    conversation_id=original_conversation_id,
                    anchor_status=receipt_anchor_status,
                    events=[],
                    has_more=False,
                    returned_count=0,
                    source="canonical_ledger",
                    coverage="observed",
                    receipt_anchor={
                        "event_id": original_event_id,
                        "conversation_id": original_conversation_id,
                        "status": receipt_anchor_status,
                    },
                )
                result.source_privacy_generation = (
                    await session.scalar(
                        select(ExecutionTraceStateModel.privacy_generation).where(
                            ExecutionTraceStateModel.id == 1
                        )
                    )
                    or 0
                )
                return result
            conversation_id = original_conversation_id
        elif "conversation_id" in selector:
            conversation_id = selector["conversation_id"]
        elif selector or request.view == "search":
            conversation_id = None
        elif request.view == "around" and request.event_id is not None:
            original = await session.get(ChatEventModel, request.event_id)
            conversation_id = (
                original.canonical_conversation_id
                if original is not None
                else context.conversation_id
            )
        else:
            conversation_id = context.conversation_id
        query = (
            select(CanonicalConversationModel, IdentityBindingModel, SpaceBindingModel)
            .outerjoin(
                IdentityBindingModel,
                CanonicalConversationModel.identity_binding_id == IdentityBindingModel.id,
            )
            .outerjoin(
                SpaceBindingModel,
                CanonicalConversationModel.space_binding_id == SpaceBindingModel.id,
            )
        )
        if conversation_id is not None:
            query = query.where(CanonicalConversationModel.id == conversation_id)
        elif "person_id" in selector:
            query = query.where(
                person_family_condition(CanonicalConversationModel.person_id, selector["person_id"])
            )
        elif "space_id" in selector:
            query = query.where(CanonicalConversationModel.space_id == selector["space_id"])
        elif "binding_id" in selector:
            query = query.where(
                or_(
                    CanonicalConversationModel.identity_binding_id == selector["binding_id"],
                    CanonicalConversationModel.space_binding_id == selector["binding_id"],
                )
            )
        # A read delegation limits the whole query, independently of target spelling.
        if allowed is not None:
            query = query.where(
                or_(
                    person_family_condition(CanonicalConversationModel.person_id, allowed),
                    CanonicalConversationModel.space_id == allowed,
                )
            )
            if granted_scope is not None and granted_scope.conversation_id:
                query = query.where(CanonicalConversationModel.id == granted_scope.conversation_id)
        elif runtime is not None and getattr(runtime, "origin", None) == "plugin_background":
            query = query.where(CanonicalConversationModel.id == context.conversation_id)
        entries = (await session.execute(query.order_by(CanonicalConversationModel.id))).all()
        if not entries:
            if operation is not None:
                return HistoryResult(
                    operation_id=operation.id,
                    receipt_status=operation.status,
                    event_id=original_event_id,
                    conversation_id=original_conversation_id,
                    anchor_status=receipt_anchor_status,
                    events=[],
                    has_more=False,
                    returned_count=0,
                    source="canonical_ledger",
                    coverage="observed",
                    receipt_anchor={
                        "event_id": original_event_id,
                        "conversation_id": original_conversation_id,
                        "status": receipt_anchor_status,
                    },
                )
            if allowed is not None or (
                runtime is not None and getattr(runtime, "origin", None) == "plugin_background"
            ):
                raise SocialError("history_scope_denied")
            raise SocialError("history_unavailable")
        sources = {
            entry.id: {
                "conversation_id": entry.id,
                "platform": personal.platform if personal else space.platform if space else None,
                "scope": "private" if entry.kind == "private" else "group",
                "conversation_name": personal.display_name
                if personal
                else space.display_name
                if space
                else "",
                "binding_id": personal.id if personal else space.id if space else None,
                "presence_id": entry.entry_presence_id,
                "thread_id": entry.thread_id,
                "person_id": entry.person_id,
                "space_id": entry.space_id,
            }
            for entry, personal, space in entries
        }
        conversation = entries[0][0] if len(entries) == 1 else None
        conversation_ids = tuple(sources)
        predicates: tuple[Any, ...] = ()
        acp_ids = tuple(key for key, value in sources.items() if value["platform"] == "acp")
        if acp_ids:
            predicates = (
                or_(
                    ChatEventModel.canonical_conversation_id.not_in(acp_ids),
                    ChatEventModel.direction == "inbound",
                    select(SocialOperationModel.id)
                    .where(
                        SocialOperationModel.event_id == ChatEventModel.id,
                        SocialOperationModel.status == "succeeded",
                        ChatEventModel.direction == "outbound",
                    )
                    .exists(),
                ),
            )
        ledger = EventLedgerRepository(database)
        common: dict[str, Any] = dict(
            session=session,
            conversation_id=conversation_ids,
            event_kind=request.event_kind,
            predicates=predicates,
        )
        earlier: tuple[EventRecord, ...]
        later: tuple[EventRecord, ...]
        neighbours: tuple[EventRecord, ...]
        matched_fields: dict[int, tuple[str, ...]]
        payload: dict[str, Any]
        if request.view == "around":
            event_id = request.event_id or original_event_id
            if event_id is None:
                raise ValueError("around requires event_id")
            event = await session.get(ChatEventModel, event_id)
            center_conversation_id = (
                event.canonical_conversation_id
                if event is not None and event.canonical_conversation_id in sources
                else None
            )
            if center_conversation_id is None or (
                operation is not None and anchor is None and event_id == original_event_id
            ):
                center, earlier, later = None, (), ()
            else:
                center, earlier, later = await ledger.history_around(
                    **{**common, "conversation_id": center_conversation_id},
                    event_id=event_id,
                    before=(request.before or 0) if request.view == "around" else 0,
                    after=(request.after or 0) if request.view == "around" else 0,
                )
            records = (*earlier, *((center,) if center else ()), *later)
            payload = dict(
                center_event_id=center.id if center else event_id,
                before=len(earlier),
                after=len(later),
                has_more=False,
            )
            payload["center_status"] = "available" if center is not None else "unavailable"
            if center is None:
                payload["anchor_status"] = "unavailable"
                if (
                    operation is not None
                    and event_id == original_event_id
                    and receipt_anchor_status == "available"
                ):
                    receipt_anchor_status = "unavailable"
            matched_fields = {}
            neighbours = ()
        else:
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
            records = page.events
            neighbours = page.context
            matched_fields = page.matched_fields
            payload = dict(has_more=page.has_more, next_cursor=page.next_cursor)

        def render(row: EventRecord) -> dict[str, Any]:
            source = sources[str(row.canonical_conversation_id)]
            return {
                **event_view(row),
                "platform": source["platform"],
                "conversation_name": source["conversation_name"],
                "thread_id": source["thread_id"],
            }

        rows = [render(row) for row in records]
        result = HistoryResult(
            source="canonical_ledger",
            coverage="observed",
            conversation_id=conversation.id if conversation is not None else None,
            events=rows,
            returned_count=len(rows),
            order=request.order,
            start_at=request.start_at.isoformat() if request.start_at else None,
            end_at=request.end_at.isoformat() if request.end_at else None,
            matched_fields={str(key): list(value) for key, value in matched_fields.items()},
            context=[render(row) for row in neighbours],
            **payload,
        )
        if conversation is not None:
            entry_source = sources[conversation.id]
            result["platform"] = entry_source["platform"]
            result["binding_id"] = entry_source["binding_id"]
            result["target_id"] = (
                await current_person_id(session, conversation.person_id)
                if conversation.person_id
                else conversation.space_id
            )
            result["generation"] = conversation.generation
            result["kind"] = "person" if conversation.kind == "private" else "space"
        source_records = (*records, *neighbours)
        if operation is not None:
            result["operation_id"] = operation.id
            result["receipt_status"] = operation.status
            result["event_id"] = original_event_id
            result["original_conversation_id"] = original_conversation_id
            receipt_record = None
            if anchor is not None:
                receipt_record = next((row for row in source_records if row.id == anchor.id), None)
                if receipt_record is None:
                    receipt_record, _earlier, _later = await ledger.history_around(
                        **{**common, "conversation_id": original_conversation_id},
                        event_id=anchor.id,
                        before=0,
                        after=0,
                    )
                if receipt_record is None:
                    receipt_anchor_status = "unavailable"
            result["anchor_status"] = receipt_anchor_status
            result["receipt_anchor"] = {
                "event_id": original_event_id,
                "conversation_id": original_conversation_id,
                "status": receipt_anchor_status,
            }
            displayed_ids = {row.id for row in source_records}
            if receipt_record is not None and receipt_record.id not in displayed_ids:
                result["receipt_anchor"]["event"] = render(receipt_record)
                source_records = (*source_records, receipt_record)
        result.source_event_ids = tuple(dict.fromkeys(row.id for row in source_records))
        result["sources"] = [
            sources[identity]
            for identity in dict.fromkeys(
                str(row.canonical_conversation_id) for row in source_records
            )
        ]
        result.source_privacy_generation = (
            await session.scalar(
                select(ExecutionTraceStateModel.privacy_generation).where(
                    ExecutionTraceStateModel.id == 1
                )
            )
            or 0
        )
        return result
