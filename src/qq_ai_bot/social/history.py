"""Bounded, account-explicit OneBot history reads without ledger side effects."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
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
                "send_message",
                # Immutable receipts created before contract v5 keep their
                # original action label. They are history anchors only; the
                # old tools are not executable.
                "send_private_message",
                "send_group_message",
                "send_file_caption",
            }
            or not receipt.presence_id
        ):
            raise SocialError("history_receipt_unavailable")
        if receipt.event_id is None:
            raise SocialError("history_anchor_unavailable")
        event = await session.get(ChatEventModel, receipt.event_id)
        if (
            event is None
            or event.canonical_conversation_id is None
            or event.author_presence_id != receipt.presence_id
            or event.direction != "outbound"
            or event.author_kind != "yuki"
            or event.event_kind != "message"
            or event.suppression_status != "keeper"
        ):
            raise SocialError("history_anchor_unavailable")
        conversation = await session.get(
            CanonicalConversationModel, event.canonical_conversation_id
        )
        if (
            conversation is None
            or (
                conversation.person_id if receipt.target_kind == "person" else conversation.space_id
            )
            != receipt.target_id
        ):
            raise SocialError("history_anchor_unavailable")
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
    if "event_id" in args:
        return await read_forward_history(service, args, context)
    limit = args.get("limit", 20)
    if type(limit) is not int or not 1 <= limit <= 50:
        raise SocialError("invalid_history_limit")
    selection = await resolve_history_target(service, args, context)
    target = selection.target
    if target.kind == "space":
        routes = await service.router.accessible_group_connections(
            str(target.id),
            binding_id=selection.binding_id,
            presence_id=selection.presence_id,
        )
    else:
        binding_args = dict(args)
        if selection.binding_id:
            binding_args["binding_id"] = selection.binding_id
        binding = await service.person_binding(target, binding_args, context)
        async with service.database.sessions() as session:
            query = select(PresenceModel).where(
                PresenceModel.enabled.is_(True), PresenceModel.platform == "qq"
            )
            if selection.presence_id:
                query = query.where(PresenceModel.id == selection.presence_id)
            presences = list(await session.scalars(query.order_by(PresenceModel.id)))
        routes = []
        for presence in presences:
            try:
                route = service.router._presence_connection(presence)
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


def forward_messages(payload: Any) -> list[Any]:
    """Choose the first actual list in the supported OneBot envelopes."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        if payload.get("status") == "failed" or payload.get("retcode", 0) != 0:
            raise SocialError("history_provider_failed")
        data = payload.get("data")
        candidates = [payload.get("messages"), payload.get("message_list"), data]
        if isinstance(data, dict):
            candidates.extend([data.get("list"), data.get("messages"), data.get("message_list")])
        for candidate in candidates:
            if isinstance(candidate, list):
                return candidate
    raise SocialError("invalid_history_result")


async def read_forward_history(
    service: SocialService, args: dict[str, Any], context: SocialContext
) -> dict[str, Any]:
    from qq_ai_bot.capabilities.invocation import current_invocation
    from qq_ai_bot.execution_trace.db_models import ExecutionTraceStateModel
    from qq_ai_bot.persistence.event_repository import EventLedgerRepository
    from qq_ai_bot.tool_results.access import access_from_runtime

    event_id = args["event_id"]
    if type(event_id) is not int or event_id <= 0:
        raise SocialError("invalid_history_event_id")
    if "operation_id" in args:
        raise SocialError("target_selector_conflict")
    invocation = current_invocation.get()
    if invocation is None:
        raise SocialError("missing_call_context")
    runtime = invocation.runtime
    try:
        access = access_from_runtime(runtime)
    except (ValueError, PermissionError) as exc:
        raise SocialError("missing_read_scope") from exc
    if access.conversation_id != context.conversation_id:
        raise SocialError("history_scope_denied")
    event = await EventLedgerRepository(service.database).get_reply_event(
        event_id, conversation_id=access.conversation_id, current_generation_only=True
    )
    if event is None:
        raise SocialError("history_anchor_unavailable")
    async with service.database.sessions() as reader:
        conversation = await reader.get(CanonicalConversationModel, access.conversation_id)
        if conversation is None or conversation.generation != access.generation:
            raise SocialError("history_source_changed")
        kind = "space" if conversation.space_id else "person"
        owner_id = conversation.space_id or conversation.person_id
        target_id = runtime.read_target_id
        if target_id is not None and target_id != owner_id:
            raise SocialError("history_scope_denied")
        if kind == "space" and context.space_id != owner_id:
            raise SocialError("history_scope_denied")
        target = SocialTarget.model_validate({"kind": kind, "id": owner_id})
        await service.check_target(target, session=reader)
        privacy = (
            await reader.scalar(
                select(ExecutionTraceStateModel.privacy_generation).where(
                    ExecutionTraceStateModel.id == 1
                )
            )
            or 0
        )
    presence_id = (
        event.ingress_presence_id if event.direction == "inbound" else event.author_presence_id
    )
    if not presence_id:
        raise SocialError("history_presence_unavailable")
    route = await service.router.resolve_presence(presence_id)
    if route.sender_account_id != event.bot_user_id:
        raise SocialError("history_presence_unavailable")
    segments = [
        (index, segment)
        for index, segment in enumerate(event.segments)
        if isinstance(segment, dict) and segment.get("type") == "forward"
    ]
    if not segments:
        raise SocialError("history_anchor_unavailable")

    async def fetch(locator: str) -> dict[str, Any]:
        try:
            raw = await service._call(route, "get_forward_msg", {"id": locator})
            return {"status": "ok", "messages": forward_messages(raw)}
        except SocialError as exc:
            return {"status": "failed", "error": str(exc)}
        except Exception:
            return {"status": "failed", "error": "history_provider_failed"}

    locators: list[str] = []
    groups: list[dict[str, Any]] = []
    for index, segment in segments:
        data = segment.get("data")
        locator = data.get("id") if isinstance(data, dict) else None
        valid = isinstance(locator, str) and bool(locator.strip())
        group = {"outer_segment_index": index, "forward_id": locator if valid else None}
        if isinstance(locator, str) and locator.strip():
            if locator not in locators:
                locators.append(locator)
        else:
            group.update(status="failed", error="history_anchor_unavailable")
        groups.append(group)
    values = await asyncio.gather(*(fetch(locator) for locator in locators))
    fetched = dict(zip(locators, values, strict=True))
    for group in groups:
        if group.get("forward_id") is not None:
            group.update(fetched[group["forward_id"]])
    # Publish under the original generation, privacy and owner authorization.
    # Metadata and temporary account changes do not erase a completed read.
    async with service.database.sessions() as reader:
        conversation = await reader.get(CanonicalConversationModel, access.conversation_id)
        current_privacy = (
            await reader.scalar(
                select(ExecutionTraceStateModel.privacy_generation).where(
                    ExecutionTraceStateModel.id == 1
                )
            )
            or 0
        )
        if (
            conversation is None
            or conversation.generation != access.generation
            or current_privacy != privacy
        ):
            raise SocialError("history_source_changed")
        await service.check_target(target, session=reader)
    successes = [group for group in groups if group["status"] == "ok"]
    result = {
        "source": "onebot",
        "mode": "forward",
        "outer_event_id": event.id,
        "presence_id": presence_id,
        "bot_user_id": route.sender_account_id,
        "content_trust": "untrusted_data",
        "complete": len(successes) == len(groups),
        "forwards": groups,
    }
    if not successes:
        result["error"] = groups[0]["error"]
    return result
