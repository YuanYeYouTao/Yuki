"""Explicit, read-only access to one's private entry ledgers."""

from typing import Any

from sqlalchemy import select, text

from yuki.client_api.views import public_events
from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.domain.messages import ChatTool
from yuki.gateway.local_clients import LOCAL_CLIENT_PLATFORMS
from yuki.identity.db_models import IdentityBindingModel
from yuki.identity.person_identity import person_family_condition
from yuki.persistence.database import Database
from yuki.persistence.models import ChatEventModel
from yuki.social.models import SocialError

ENTRY_TOOL = ChatTool(
    name="read_personal_entry",
    result_cacheable=False,
    description=(
        "仅在 App/ACP 个人会话显式读取本人私聊入口的账本窗口，含当前入口的旧事件。"
        "返回平台、绑定、会话及内部事件 ID，"
        "不读取群聊、不修改任何会话。"
        "遇到 [[yuki-ref conversation=<会话UUID> event=<内部事件ID>]] 引用时，"
        "按 conversation_id、after=事件ID-1、limit=1 读取，并核返回 event_id 等于原引用。"
        "空结果、not_found 或 ID 不相等都说明原引用无法读取，不得把下一则当作原引用。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "binding_id": {"type": "string", "format": "uuid"},
            "conversation_id": {"type": "string", "format": "uuid"},
            "after": {"type": "integer", "minimum": 0},
            "before": {"type": "integer", "minimum": 1},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        "oneOf": [{"required": ["binding_id"]}, {"required": ["conversation_id"]}],
        "additionalProperties": False,
    },
)


async def read_entry(database: Database, runtime: Any, args: dict[str, Any]) -> dict[str, Any]:
    actor = runtime.require_actor()
    limit = args.get("limit", 20)
    if not 1 <= limit <= 50 or bool(args.get("binding_id")) == bool(args.get("conversation_id")):
        raise SocialError("invalid_entry_query")
    async with database.sessions() as session:
        await session.execute(text("BEGIN"))
        source = (
            await session.execute(
                select(CanonicalConversationModel, IdentityBindingModel)
                .join(
                    IdentityBindingModel,
                    IdentityBindingModel.id == CanonicalConversationModel.identity_binding_id,
                )
                .where(
                    CanonicalConversationModel.id == runtime.effective_conversation_id,
                    CanonicalConversationModel.kind == "private",
                    person_family_condition(CanonicalConversationModel.person_id, actor.person_id),
                    IdentityBindingModel.platform.in_(LOCAL_CLIENT_PLATFORMS),
                )
            )
        ).first()
        if source is None:
            raise SocialError("main_entry_required")
        query = (
            select(CanonicalConversationModel, IdentityBindingModel)
            .join(
                IdentityBindingModel,
                IdentityBindingModel.id == CanonicalConversationModel.identity_binding_id,
            )
            .where(
                CanonicalConversationModel.kind == "private",
                person_family_condition(CanonicalConversationModel.person_id, actor.person_id),
            )
        )
        if getattr(runtime, "read_target_id", None) is not None:
            query = query.where(CanonicalConversationModel.id == runtime.read_scope.conversation_id)
        query = (
            query.where(IdentityBindingModel.id == args["binding_id"])
            if args.get("binding_id")
            else query.where(CanonicalConversationModel.id == args["conversation_id"])
        )
        targets = (await session.execute(query.order_by(CanonicalConversationModel.id))).all()
        if not targets:
            raise SocialError("not_found")
        if len(targets) > 1:
            return {
                "error": "conversation_required",
                "binding_id": args["binding_id"],
                "conversations": [
                    {
                        "conversation_id": conversation.id,
                        "platform": binding.platform,
                        "generation": conversation.generation,
                    }
                    for conversation, binding in targets
                ],
            }
        conversation, binding = targets[0]
        events = select(ChatEventModel).where(
            ChatEventModel.canonical_conversation_id == conversation.id,
            ChatEventModel.suppression_status == "keeper",
            ChatEventModel.id > args.get("after", 0),
        )
        if args.get("before") is not None:
            events = events.where(ChatEventModel.id < args["before"]).order_by(
                ChatEventModel.id.desc()
            )
        else:
            events = events.order_by(ChatEventModel.id)
        rows = list(await session.scalars(events.limit(limit + 1)))
        selected = rows[:limit]
        if args.get("before") is not None:
            selected.reverse()
        return {
            "platform": binding.platform,
            "binding_id": binding.id,
            "conversation_id": conversation.id,
            "generation": conversation.generation,
            "items": await public_events(session, selected, binding),
            "has_more": len(rows) > limit,
            "next_after": selected[-1].id if selected else args.get("after", 0),
        }
