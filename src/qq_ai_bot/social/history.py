"""Read authorized canonical ledger history without platform credentials or remote imports."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.persistence.repository_helpers import keeper_event_clause
from qq_ai_bot.social.db_models import SocialOperationModel
from qq_ai_bot.social.models import SocialError, SocialTarget

if TYPE_CHECKING:
    from qq_ai_bot.social.service import SocialContext, SocialService


async def read_history(
    service: SocialService, args: dict[str, Any], context: SocialContext
) -> dict[str, Any]:
    limit = args.get("limit", 20)
    if type(limit) is not int or not 1 <= limit <= 50:
        raise SocialError("invalid_history_limit")
    conversation_id = args.get("conversation_id")
    anchor = args.get("event_id")
    async with service.database.sessions() as session:
        if args.get("operation_id"):
            if set(args) - {"operation_id", "limit"}:
                raise SocialError("target_selector_conflict")
            receipt = await session.get(SocialOperationModel, str(args["operation_id"]))
            if (
                receipt is None
                or receipt.source_conversation_id != context.conversation_id
                or receipt.event_id is None
            ):
                raise SocialError("history_receipt_unavailable")
            original = await session.get(ChatEventModel, receipt.event_id)
            if original is None:
                raise SocialError("history_anchor_unavailable")
            conversation_id = original.canonical_conversation_id
        elif anchor is not None:
            if type(anchor) is not int or anchor <= 0:
                raise SocialError("invalid_history_event_id")
            original = await session.get(ChatEventModel, anchor)
            if original is None or original.canonical_conversation_id != context.conversation_id:
                raise SocialError("history_scope_denied")
            conversation_id = original.canonical_conversation_id
        elif not conversation_id:
            if any(args.get(key) for key in ("target_id", "display_name", "subject_ref", "kind")):
                kind = args.get("kind", "space" if context.space_id else "person")
                target = await service.target(kind, args, context)
                await service.check_target(target)
                query = select(CanonicalConversationModel).where(
                    (
                        CanonicalConversationModel.person_id
                        if kind == "person"
                        else CanonicalConversationModel.space_id
                    )
                    == str(target.id)
                )
                if args.get("binding_id"):
                    query = query.where(
                        CanonicalConversationModel.identity_binding_id == args["binding_id"]
                    )
                if args.get("presence_id"):
                    query = query.where(
                        CanonicalConversationModel.entry_presence_id == args["presence_id"]
                    )
                entries = list(await session.scalars(query.order_by(CanonicalConversationModel.id)))
                if len(entries) != 1:
                    return {
                        "error": "conversation_ambiguous" if entries else "history_unavailable",
                        "conversations": [
                            {
                                "conversation_id": entry.id,
                                "kind": entry.kind,
                                "presence_id": entry.entry_presence_id,
                            }
                            for entry in entries
                        ],
                    }
                conversation_id = entries[0].id
            else:
                conversation_id = context.conversation_id
        conversation = await session.get(CanonicalConversationModel, conversation_id)
        if conversation is None:
            raise SocialError("history_unavailable")
        target = SocialTarget.model_validate(
            {
                "kind": "person" if conversation.kind == "private" else "space",
                "id": conversation.person_id or conversation.space_id,
            }
        )
        await service.check_target(target)
        # A delegated read contract is a real grant, independent of route/connection.
        from qq_ai_bot.capabilities.invocation import current_invocation

        invocation = current_invocation.get()
        if invocation is not None:
            allowed_target = getattr(invocation.runtime, "read_target_id", None)
            if allowed_target is not None and allowed_target != str(target.id):
                raise SocialError("history_scope_denied")
        events = select(ChatEventModel).where(
            ChatEventModel.canonical_conversation_id == conversation.id,
            keeper_event_clause(),
            ChatEventModel.id > conversation.starts_after_event_id,
        )
        if anchor is not None:
            events = events.where(ChatEventModel.id == anchor)
        rows = list(await session.scalars(events.order_by(ChatEventModel.id.desc()).limit(limit)))
    from qq_ai_bot.event_prompt import ChatEventPromptRenderer
    from qq_ai_bot.persistence.repository_helpers import _event_record

    records = tuple(_event_record(row) for row in reversed(rows))
    renderer = ChatEventPromptRenderer(records)
    return {
        "source": "canonical_ledger",
        "coverage": "observed",
        "conversation_id": conversation.id,
        "target_id": str(target.id),
        "kind": target.kind,
        "count": len(records),
        "messages": [
            {
                "event_id": row.id,
                "person_id": row.author_person_id,
                "presence_id": row.author_presence_id,
                "reply_to_event_id": row.reply_to_event_id,
                "content": renderer.render_reference_event(row),
            }
            for row in records
        ],
    }
