"""Translate event-bound runtime into social context without inventing actors."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from yuki.capabilities.invocation import current_invocation
from yuki.identity.routing import RouteSendError
from yuki.persistence.models import ChatEventModel
from yuki.runtime.origin import TurnOrigin
from yuki.social.models import SocialError
from yuki.social.service import SocialContext, SocialService


async def invoke_social(
    service: SocialService, name: str, arguments: dict[str, Any], runtime: Any
) -> dict[str, Any]:
    if runtime.actor_context is not None and runtime.actor_context.principal_kind == "self":
        invocation = current_invocation.get()
        actor = runtime.require_actor()
        if (
            invocation is None
            or not invocation.invocation_key
            or runtime.tools_closed
            or actor.principal_kind != "self"
            or runtime.inbound is not None
        ):
            raise SocialError("permission_denied")
        scheduled_run = actor.automation_run_id
        context = SocialContext(
            turn_id=f"{actor.conversation_id}:{actor.source_key}",
            call_id=invocation.invocation_key,
            conversation_id=actor.conversation_id or "",
            space_id=runtime.space_id,
            origin=runtime.origin.value,
            initiative_run_id=actor.initiative_run_id,
            automation_run_id=scheduled_run,
            presence_id=actor.presence_id,
            actor=actor,
            runtime_snapshot=getattr(runtime, "runtime_config", None),
            turn_token=getattr(runtime, "turn_token", None),
            conversation_key=getattr(runtime, "conversation_key", ""),
        )
        try:
            return await service.execute(name, arguments, context)
        except RouteSendError as exc:
            raise SocialError(exc.category) from exc
        except ValidationError as exc:
            raise SocialError("invalid_message_arguments") from exc
    if name == "send" and runtime.origin is TurnOrigin.PLUGIN_BACKGROUND:
        from yuki.conversation.canonical_db_models import CanonicalConversationModel

        invocation = current_invocation.get()
        event_id = runtime.effective_trigger_event_id
        conversation_id = runtime.effective_conversation_id
        if (
            invocation is None
            or not invocation.invocation_key
            or runtime.inbound is not None
            or runtime.tools_closed
            or not isinstance(event_id, int)
            or not conversation_id
            or arguments.get("target") is not None
        ):
            raise SocialError("permission_denied")
        async with service.database.read_session() as session:
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
            call_id=invocation.invocation_key,
            conversation_id=conversation_id,
            person_refs=(
                {"current_speaker": runtime.person_id} if runtime.person_id is not None else {}
            ),
            space_id=runtime.space_id,
            origin="plugin_background",
            caused_by_event_id=event_id,
            runtime_snapshot=getattr(runtime, "runtime_config", None),
            turn_token=getattr(runtime, "turn_token", None),
            conversation_key=getattr(runtime, "conversation_key", ""),
        )
        try:
            return await service.execute(name, arguments, context)
        except RouteSendError as exc:
            raise SocialError(exc.category) from exc
        except ValidationError as exc:
            raise SocialError("invalid_message_arguments") from exc
    if getattr(runtime, "read_target_id", None) is not None and (
        name == "query_chat_history"
        or (name == "find_contacts" and arguments.get("view") == "entries")
    ):
        actor = runtime.actor_context
        invocation = current_invocation.get()
        if invocation is None or runtime.tools_closed:
            raise SocialError("missing_read_scope")
        context = SocialContext(
            turn_id=f"{runtime.effective_conversation_id}:{runtime.effective_execution_id}",
            call_id=invocation.invocation_key,
            conversation_id=runtime.effective_conversation_id or "",
            space_id=runtime.space_id,
            person_refs={"current_speaker": actor.person_id}
            if actor is not None and actor.person_id
            else {},
            actor=actor,
            origin=runtime.origin.value,
        )
        return await service.execute(name, arguments, context)
    if getattr(runtime, "read_target_id", None) is not None and name == "find_contacts":
        from yuki.identity.person_identity import same_person_id

        scope = runtime.read_scope
        target_id = runtime.read_target_id
        invocation = current_invocation.get()
        if scope is None or invocation is None or runtime.tools_closed:
            raise SocialError("missing_read_scope")
        kind = "space" if scope.group_id else "person"
        if arguments.get("kind", kind) != kind:
            raise SocialError("history_scope_denied")
        context = SocialContext(
            turn_id=f"{runtime.effective_conversation_id}:{runtime.effective_execution_id}",
            call_id=invocation.invocation_key,
            conversation_id=runtime.effective_conversation_id or "",
            space_id=target_id if kind == "space" else None,
            person_refs={"current_speaker": target_id} if kind == "person" else {},
        )
        selected = dict(arguments)
        if any(selected.get(key) for key in ("target_id", "subject_ref", "display_name")):
            target = await service.target(kind, selected, context, validate=False)
            async with service.database.read_session() as session:
                if str(target.id) != target_id and (
                    kind != "person" or not await same_person_id(session, str(target.id), target_id)
                ):
                    raise SocialError("history_scope_denied")
        selected.pop("subject_ref", None)
        selected.pop("display_name", None)
        selected.update(kind=kind, target_id=target_id)
        return await service.execute(name, selected, context)
    if (
        runtime.origin
        not in {
            TurnOrigin.USER_MESSAGE,
            TurnOrigin.AUTONOMOUS_GROUP,
            TurnOrigin.SCHEDULED_AUTOMATION,
            TurnOrigin.PLUGIN_SESSION,
        }
        or runtime.tools_closed
    ):
        raise SocialError("permission_denied")
    invocation = current_invocation.get()
    if invocation is None or not invocation.invocation_key:
        raise SocialError("missing_call_context")
    inbound = runtime.inbound
    actor = runtime.require_actor()
    refs = {"current_speaker": actor.person_id} if actor.person_id is not None else {}
    if name == "find_contacts":
        for index, person_id in enumerate(actor.mentioned_person_ids, 1):
            refs[f"mentioned_user_{index}"] = person_id
        if actor.mentioned_person_ids:
            refs["mentioned_user"] = actor.mentioned_person_ids[0]
        reply_id = getattr(inbound, "reply_to_event_id", None)
        if reply_id is not None:
            async with service.database.read_session() as session:
                reply = await session.get(ChatEventModel, reply_id)
                if (
                    reply is not None
                    and reply.canonical_conversation_id == runtime.effective_conversation_id
                    and reply.author_person_id is not None
                ):
                    refs["replied_message_author"] = reply.author_person_id
    conversation_id = runtime.effective_conversation_id
    if not conversation_id:
        raise SocialError("missing_call_context")
    execution_id = actor.source_key
    context = SocialContext(
        turn_id=f"{conversation_id}:{execution_id}",
        call_id=invocation.invocation_key,
        conversation_id=conversation_id,
        origin=runtime.origin.value,
        person_refs=refs,
        space_id=runtime.space_id or getattr(inbound, "space_id", None),
        trigger_event_id=(
            getattr(runtime, "effective_trigger_event_id", None) if inbound is not None else None
        ),
        reply_presence_id=inbound.presence_id
        if inbound is not None and inbound.scope_type == "private"
        else None,
        actor=actor,
        presence_id=actor.presence_id,
        runtime_snapshot=getattr(runtime, "runtime_config", None),
        turn_token=getattr(runtime, "turn_token", None),
        conversation_key=getattr(runtime, "conversation_key", ""),
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
