"""Resolve real entry grants, then read the one canonical ledger query core."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import or_, select, text

from yuki.capabilities.invocation import current_invocation
from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.execution_trace.db_models import ExecutionTraceStateModel
from yuki.gateway.local_clients import LOCAL_CLIENT_PLATFORMS
from yuki.identity.db_models import IdentityBindingModel, SpaceBindingModel
from yuki.identity.person_identity import (
    current_person_id,
    person_enabled_condition,
    person_family_condition,
    same_person_id,
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
        source_binding = (
            await session.get(IdentityBindingModel, source.identity_binding_id)
            if source.identity_binding_id
            else None
        )
        personal_source = bool(
            actor_id
            and source.kind == "private"
            and source.person_id
            and source_binding is not None
            and source_binding.platform in LOCAL_CLIENT_PLATFORMS
            and (
                actor_id == source.person_id
                or await same_person_id(session, actor_id, source.person_id)
            )
        )
        selector = (
            request.target.model_dump(exclude_none=True, mode="json") if request.target else {}
        )
        allowed = getattr(runtime, "read_target_id", None)
        granted_scope = getattr(runtime, "read_scope", None)
        selected_subject = selector.get("person_id") or selector.get("space_id")
        if allowed is not None and selected_subject is not None:
            if selected_subject != allowed and (
                "person_id" not in selector
                or not await same_person_id(session, selected_subject, allowed)
            ):
                raise SocialError("history_scope_denied")
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
        elif selector:
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
            if "person_id" in selector:
                query = query.where(
                    person_family_condition(
                        CanonicalConversationModel.person_id, selector["person_id"]
                    )
                )
            elif "space_id" in selector:
                query = query.where(CanonicalConversationModel.space_id == selector["space_id"])
            else:
                if not personal_source:
                    raise SocialError("main_entry_required")
                query = query.where(
                    CanonicalConversationModel.identity_binding_id == selector["binding_id"],
                    CanonicalConversationModel.kind == "private",
                    person_family_condition(CanonicalConversationModel.person_id, actor_id),
                )
            if allowed is not None and granted_scope is not None and granted_scope.conversation_id:
                query = query.where(CanonicalConversationModel.id == granted_scope.conversation_id)
            entries = (await session.execute(query.order_by(CanonicalConversationModel.id))).all()
            if len(entries) != 1:
                return HistoryResult(
                    error="conversation_ambiguous" if entries else "history_unavailable",
                    conversations=[
                        {
                            "conversation_id": entry.id,
                            "kind": entry.kind,
                            "presence_id": entry.entry_presence_id,
                            "binding_id": personal.id if personal else space.id if space else None,
                            "platform": personal.platform
                            if personal
                            else space.platform
                            if space
                            else None,
                            "thread_id": entry.thread_id,
                        }
                        for entry, personal, space in entries
                    ],
                )
            conversation_id = entries[0][0].id
        else:
            conversation_id = context.conversation_id
        conversation = await session.get(CanonicalConversationModel, conversation_id)
        if conversation is None:
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
            raise SocialError("history_unavailable")
        if (
            runtime is not None
            and getattr(runtime, "origin", None) == "plugin_background"
            and getattr(runtime, "read_target_id", None) is None
            and conversation.id != context.conversation_id
        ):
            raise SocialError("history_scope_denied")
        allowed = getattr(runtime, "read_target_id", None)
        if allowed is not None:
            subject = conversation.person_id or conversation.space_id
            if subject != allowed and (
                conversation.kind != "private"
                or subject is None
                or not await same_person_id(session, subject, allowed)
            ):
                raise SocialError("history_scope_denied")
            scope = getattr(runtime, "read_scope", None)
            if (
                scope is not None
                and scope.conversation_id
                and scope.conversation_id != conversation.id
            ):
                raise SocialError("history_scope_denied")
        personal_target = bool(
            personal_source
            and conversation.kind == "private"
            and conversation.person_id
            and (
                actor_id == conversation.person_id
                or await same_person_id(session, str(actor_id), conversation.person_id)
            )
        )
        floor = 0 if personal_target else conversation.starts_after_event_id
        binding = (
            await session.get(IdentityBindingModel, conversation.identity_binding_id)
            if conversation.identity_binding_id
            else None
        )
        predicates: tuple[Any, ...] = ()
        if binding is not None and binding.platform == "acp":
            predicates = (
                or_(
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
            conversation_id=conversation.id,
            starts_after_event_id=floor,
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
            # An original receipt grants only its own anchor, never another old ID.
            special = anchor if anchor is not None and event_id == anchor.id else None
            if operation is not None and anchor is None and event_id == original_event_id:
                center, earlier, later = None, (), ()
            else:
                center, earlier, later = await ledger.history_around(
                    **common,
                    event_id=event_id,
                    before=(request.before or 0) if request.view == "around" else 0,
                    after=(request.after or 0) if request.view == "around" else 0,
                    anchor_event=special,
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
        rows = [event_view(row) for row in records]
        result = HistoryResult(
            source="canonical_ledger",
            coverage="observed",
            conversation_id=conversation.id,
            generation=conversation.generation,
            platform=binding.platform if binding is not None else None,
            binding_id=binding.id if binding is not None else None,
            target_id=(await current_person_id(session, conversation.person_id))
            if conversation.person_id
            else conversation.space_id,
            kind="person" if conversation.kind == "private" else "space",
            events=rows,
            returned_count=len(rows),
            order=request.order,
            start_at=request.start_at.isoformat() if request.start_at else None,
            end_at=request.end_at.isoformat() if request.end_at else None,
            matched_fields={str(key): list(value) for key, value in matched_fields.items()},
            context=[event_view(row) for row in neighbours],
            **payload,
        )
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
                        **common, event_id=anchor.id, before=0, after=0, anchor_event=anchor
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
                result["receipt_anchor"]["event"] = event_view(receipt_record)
                source_records = (*source_records, receipt_record)
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
