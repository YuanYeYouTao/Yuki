"""Command behavior, cancellation, and send-failure semantics."""

from __future__ import annotations

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import (
    InboundMessage,
    SenderIdentity,
)


def inbound(
    text: str,
    *,
    message_id: str,
    user_id: str = "1001",
    group_id: str | None = None,
    mentions_bot: bool = False,
    unsupported: bool = False,
) -> InboundMessage:
    from qq_ai_bot.domain.messages import AttachmentKind, MessageAttachment

    return InboundMessage(
        message_id=message_id,
        event_type="message:test",
        scope_type=ScopeType.GROUP if group_id else ScopeType.PRIVATE,
        sender=SenderIdentity(user_id),
        text=text,
        bot_user_id="9999",
        group_id=group_id,
        mentions_bot=mentions_bot,
        attachments=(MessageAttachment(AttachmentKind.IMAGE, "image"),) if unsupported else (),
    )
