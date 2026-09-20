"""Bounded, account-explicit OneBot history reads without ledger side effects."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from qq_ai_bot.identity.db_models import IdentityBindingModel, PresenceModel, SpaceBindingModel
from qq_ai_bot.identity.routing import RouteSendError
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.social.db_models import SocialOperationModel
from qq_ai_bot.social.models import SocialError, SocialTarget

if TYPE_CHECKING:
    from qq_ai_bot.social.service import SocialContext, SocialService


@dataclass(frozen=True)
class HistorySelection:
    target: SocialTarget
    presence_id: str | None
    binding_id: str | None


async def resolve_history_target(
    service: SocialService, args: dict[str, Any], context: SocialContext
) -> HistorySelection:
    operation_id = args.get("operation_id")
    if not operation_id:
        kind = args.get("kind")
        if kind not in {"person", "space"}:
            raise SocialError("history_kind_required")
        target = await service.target(kind, args, context)
        presence = args.get("presence_id")
        if (
            not presence
            and kind == "person"
            and str(target.id) == context.person_refs.get("current_speaker")
        ):
            presence = context.reply_presence_id
        return HistorySelection(target, presence, args.get("binding_id"))
    if set(args) - {"operation_id", "limit"}:
        raise SocialError("target_selector_conflict")
    async with service.database.sessions() as session:
        receipt = await session.get(SocialOperationModel, str(operation_id))
        if (
            receipt is None
            or receipt.source_conversation_id != context.conversation_id
            or receipt.action
            not in {
                "send_message", "send_private_message", "send_group_message", "send_file_caption"
            }
            or not receipt.presence_id
        ):
            raise SocialError("history_receipt_unavailable")
        events = list(
            await session.scalars(
                select(ChatEventModel).where(
                    ChatEventModel.platform_message_id
                    == (receipt.platform_reference or f"social-operation:{receipt.id}"),
                    ChatEventModel.author_presence_id == receipt.presence_id,
                    ChatEventModel.direction == "outbound",
                )
            )
        )
        if len(events) != 1:
            raise SocialError("history_anchor_unavailable")
        event = events[0]
        if receipt.target_kind == "person":
            bindings = list(
                await session.scalars(
                    select(IdentityBindingModel.id).where(
                        IdentityBindingModel.person_id == receipt.target_id,
                        IdentityBindingModel.platform == "qq",
                        IdentityBindingModel.status == "active",
                        IdentityBindingModel.external_account_id == event.private_peer_user_id,
                    )
                )
            )
        else:
            bindings = list(
                await session.scalars(
                    select(SpaceBindingModel.id).where(
                        SpaceBindingModel.space_id == receipt.target_id,
                        SpaceBindingModel.platform == "qq",
                        SpaceBindingModel.status == "active",
                        SpaceBindingModel.external_space_id == event.group_id,
                    )
                )
            )
        if len(bindings) != 1:
            raise SocialError("binding_unavailable")
        target = SocialTarget.model_validate({"kind": receipt.target_kind, "id": receipt.target_id})
        selection = HistorySelection(target, receipt.presence_id, bindings[0])
    await service.check_target(target)
    return selection


async def read_history(
    service: SocialService, args: dict[str, Any], context: SocialContext
) -> dict[str, Any]:
    limit = args.get("limit", 20)
    if type(limit) is not int or not 1 <= limit <= 50:
        raise SocialError("invalid_history_limit")
    selection = await resolve_history_target(service, args, context)
    target = selection.target
    if target.kind == "space":
        routes = await service.router.accessible_group_connections(
            str(target.id), binding_id=selection.binding_id
        )
        if selection.presence_id:
            routes = [route for route in routes if route.presence_id == selection.presence_id]
    else:
        binding_args = dict(args)
        if selection.binding_id:
            binding_args["binding_id"] = selection.binding_id
        binding = await service.person_binding(target, binding_args, context)
        async with service.database.sessions() as session:
            query = select(PresenceModel.id).where(
                PresenceModel.enabled.is_(True), PresenceModel.platform == "qq"
            )
            if selection.presence_id:
                query = query.where(PresenceModel.id == selection.presence_id)
            presences = list(await session.scalars(query.order_by(PresenceModel.id)))
        routes = []
        for presence_id in presences:
            try:
                route = await service.router.resolve_presence(presence_id)
            except RouteSendError:
                continue
            routes.append(
                replace(
                    route,
                    binding_id=binding.id,
                    external_target_id=binding.external_account_id,
                    kind="person",
                )
            )
    if not routes:
        raise SocialError("history_presence_unavailable")
    if len(routes) != 1:
        return {
            "error": "presence_ambiguous",
            "target_id": str(target.id),
            "presences": [
                {"presence_id": route.presence_id, "bot_user_id": route.sender_account_id}
                for route in routes
            ],
        }
    route = routes[0]
    private = target.kind == "person"
    try:
        payload = await service._call(
            route,
            "get_friend_msg_history" if private else "get_group_msg_history",
            {"user_id" if private else "group_id": int(route.external_target_id), "count": limit},
        )
    except SocialError:
        raise
    except Exception as exc:
        # Gateway failures are not empty history; do not expose transport payloads.
        # CancelledError is a BaseException and must still propagate.
        raise SocialError("history_provider_failed") from exc
    # Reuse the bounded, text-only projection; never use its current-inbound
    # import path for records fetched from a different conversation.
    from qq_ai_bot.services.agent_tools import AgentToolService

    raw: Any = payload
    if isinstance(raw, dict):
        if raw.get("status") == "failed" or raw.get("retcode", 0) != 0:
            raise SocialError("history_provider_failed")
        raw = raw.get("messages", raw.get("message_list", raw.get("data")))
        if isinstance(raw, dict):
            raw = raw.get("messages", raw.get("message_list"))
    if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
        raise SocialError("invalid_history_result")
    messages = [
        AgentToolService._history_item_for_model(
            {**item, "message": item.get("message", item.get("raw_message"))}
        )
        for item in raw[-limit:]
    ]
    return {
        "source": "onebot",
        "provider": route.connection.snapshot.provider,
        "fetched_at": datetime.now(UTC).isoformat(),
        "content_trust": "untrusted_data",
        "target_id": str(target.id),
        "kind": target.kind,
        "binding_id": route.binding_id,
        "presence_id": route.presence_id,
        "bot_user_id": route.sender_account_id,
        "external_target_id": route.external_target_id,
        "count": len(messages),
        "requested_limit": limit,
        "window_limited": len(raw) >= limit,
        "messages": messages,
    }
