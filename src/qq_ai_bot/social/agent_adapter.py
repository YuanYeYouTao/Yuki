"""Translate event-bound runtime into social context without inventing actors."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError
from sqlalchemy import select

from qq_ai_bot.capabilities.invocation import current_invocation
from qq_ai_bot.identity.db_models import IdentityBindingModel
from qq_ai_bot.identity.routing import RouteSendError
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.social.models import SocialError
from qq_ai_bot.social.service import SocialContext, SocialService


async def invoke_social(
    service: SocialService, name: str, arguments: dict[str, Any], runtime: Any
) -> dict[str, Any]:
    if name == "send_message" and runtime.origin is TurnOrigin.PLUGIN_BACKGROUND:
        from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
        from qq_ai_bot.persistence.models import ChatEventModel

        invocation = current_invocation.get()
        event_id = runtime.effective_trigger_event_id
        conversation_id = runtime.effective_conversation_id
        if (
            invocation is None
            or not invocation.call_id
            or runtime.inbound is not None
            or runtime.read_only
            or runtime.tools_closed
            or not isinstance(event_id, int)
            or not conversation_id
            or arguments.get("target") is not None
        ):
            raise SocialError("permission_denied")
        async with service.database.sessions() as session:
            event = await session.get(ChatEventModel, event_id)
            conversation = await session.get(CanonicalConversationModel, conversation_id)
            if (
                event is None
                or conversation is None
                or event.canonical_conversation_id != conversation_id
                or event.direction != "external"
                or event.event_kind != "external_event"
                or event.origin != "plugin_background"
                or event.suppression_status != "keeper"
                or not event.source_plugin_id
                or (runtime.space_id or None) != (conversation.space_id or None)
                or (runtime.person_id or None) != (conversation.person_id or None)
            ):
                raise SocialError("permission_denied")
        context = SocialContext(
            turn_id=f"{conversation_id}:plugin-event:{event_id}",
            call_id=invocation.call_id,
            conversation_id=conversation_id,
            person_refs=(
                {"current_speaker": runtime.person_id} if runtime.person_id is not None else {}
            ),
            space_id=runtime.space_id,
            origin="plugin_background",
            caused_by_event_id=event_id,
            visible_event_ids=frozenset(getattr(runtime, "visible_event_ids", ())),
            runtime_snapshot=getattr(runtime, "runtime_config", None),
            turn_token=getattr(runtime, "turn_token", None),
            conversation_key=getattr(runtime, "conversation_key", ""),
            voice_delivery_allowed=bool(getattr(runtime, "voice_delivery_allowed", True)),
        )
        try:
            return await service.execute(name, arguments, context)
        except RouteSendError as exc:
            raise SocialError(exc.category) from exc
        except ValidationError as exc:
            raise SocialError("invalid_message_arguments") from exc
    if getattr(runtime, "read_scope", None) is not None and name in {
        "read_conversation_history",
        "find_contacts",
    }:
        # This grant is constructed by the Host, independently of the send route.
        # Sharing the implementation never grants arbitrary cross-conversation reads.
        scope = runtime.read_scope
        target_id = runtime.read_target_id
        invocation = current_invocation.get()
        if not target_id or invocation is None or runtime.tools_closed:
            raise SocialError("missing_read_scope")
        kind = "space" if scope.group_id else "person"
        if arguments.get("operation_id") or arguments.get("kind", kind) != kind:
            raise SocialError("history_scope_denied")
        context = SocialContext(
            turn_id=f"{runtime.effective_conversation_id}:{runtime.effective_execution_id}",
            call_id=invocation.call_id,
            conversation_id=runtime.effective_conversation_id or "",
            space_id=target_id if kind == "space" else None,
            person_refs={"current_speaker": target_id} if kind == "person" else {},
        )
        selected = dict(arguments)
        if any(selected.get(key) for key in ("target_id", "subject_ref", "display_name")):
            target = await service.target(kind, selected, context)
            if str(target.id) != target_id:
                raise SocialError("history_scope_denied")
        selected.pop("subject_ref", None)
        selected.pop("display_name", None)
        selected.update(kind=kind, target_id=target_id)
        if name == "read_conversation_history" and runtime.history_limit is not None:
            selected["limit"] = min(int(selected.get("limit", 20)), runtime.history_limit)
        return await service.execute(name, selected, context)
    if (
        runtime.origin
        not in {
            TurnOrigin.USER_MESSAGE,
            TurnOrigin.AUTONOMOUS_GROUP,
            TurnOrigin.SCHEDULED_AUTOMATION,
        }
        or (runtime.read_only and name != "read_conversation_history")
        or runtime.tools_closed
    ):
        raise SocialError("permission_denied")
    invocation = current_invocation.get()
    if invocation is None or not invocation.call_id:
        raise SocialError("missing_call_context")
    inbound = runtime.inbound
    actor = runtime.require_actor()
    refs = {"current_speaker": actor.user_id}
    for index, user_id in enumerate(runtime.mentioned_user_ids, 1):
        refs[f"mentioned_user_{index}"] = user_id
    if runtime.mentioned_user_ids:
        refs["mentioned_user"] = runtime.mentioned_user_ids[0]
    reply = getattr(inbound, "reply_sender_user_id", None)
    if reply:
        refs["replied_message_author"] = str(reply)
    async with service.database.sessions() as session:
        bindings = (
            await session.scalars(
                select(IdentityBindingModel).where(
                    IdentityBindingModel.platform == "qq",
                    IdentityBindingModel.status == "active",
                    IdentityBindingModel.external_account_id.in_(refs.values()),
                )
            )
        ).all()
    by_account = {row.external_account_id: row.person_id for row in bindings}
    conversation_id = runtime.effective_conversation_id
    if not conversation_id:
        raise SocialError("missing_call_context")
    execution_id = actor.source_key
    context = SocialContext(
        turn_id=f"{conversation_id}:{execution_id}",
        call_id=invocation.call_id,
        conversation_id=conversation_id,
        person_refs={key: by_account[value] for key, value in refs.items() if value in by_account},
        account_refs={key: value for key, value in refs.items() if value in by_account},
        space_id=runtime.space_id or getattr(inbound, "space_id", None),
        trigger_event_id=(
            getattr(runtime, "effective_trigger_event_id", None) if inbound is not None else None
        ),
        reply_presence_id=inbound.presence_id
        if inbound is not None and inbound.scope_type == "private"
        else None,
        visible_event_ids=frozenset(getattr(runtime, "visible_event_ids", ())),
        actor=actor,
        runtime_snapshot=getattr(runtime, "runtime_config", None),
        turn_token=getattr(runtime, "turn_token", None),
        conversation_key=getattr(runtime, "conversation_key", ""),
        voice_delivery_allowed=bool(getattr(runtime, "voice_delivery_allowed", True)),
        inbound=inbound,
    )
    try:
        return await service.execute(name, arguments, context)
    except RouteSendError as exc:
        raise SocialError(exc.category) from exc
    except ValidationError as exc:
        # Field names only: never expose input values or raw Pydantic payloads.
        fields = {str(error["loc"][0]) for error in exc.errors() if error["loc"]}
        raise SocialError(
            "invalid_target_id" if "id" in fields else "invalid_message_arguments"
        ) from None
