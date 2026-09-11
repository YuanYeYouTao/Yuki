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
    if (
        runtime.origin not in {TurnOrigin.USER_MESSAGE, TurnOrigin.AUTONOMOUS_GROUP}
        or (runtime.read_only and name != "read_conversation_history")
        or runtime.tools_closed
    ):
        raise SocialError("permission_denied")
    invocation = current_invocation.get()
    if invocation is None or not invocation.call_id or runtime.inbound is None:
        raise SocialError("missing_call_context")
    inbound = runtime.inbound
    refs = {"current_speaker": inbound.sender.user_id}
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
    if not conversation_id or not runtime.trigger_message_id:
        raise SocialError("missing_call_context")
    execution_id = getattr(runtime, "execution_id", "") or runtime.trigger_message_id
    context = SocialContext(
        turn_id=f"{conversation_id}:{execution_id}",
        call_id=invocation.call_id,
        conversation_id=conversation_id,
        person_refs={key: by_account[value] for key, value in refs.items() if value in by_account},
        account_refs={key: value for key, value in refs.items() if value in by_account},
        space_id=runtime.space_id or getattr(inbound, "space_id", None),
        reply_message_id=inbound.message_id if inbound.scope_type == "private" else None,
        reply_presence_id=inbound.presence_id if inbound.scope_type == "private" else None,
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
