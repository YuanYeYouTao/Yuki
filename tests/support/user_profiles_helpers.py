"""Profile persistence, OneBot resolution, and privacy-boundary tests."""

from __future__ import annotations

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity


def inbound(
    text: str,
    *,
    message_id: str,
    nickname: str = "",
    group_card: str = "",
    group_id: str | None = None,
    mentions_bot: bool = False,
    user_id: str = "1001",
    mentioned_user_ids: tuple[str, ...] = (),
) -> InboundMessage:
    return InboundMessage(
        message_id=message_id,
        event_type="message:test",
        scope_type=ScopeType.GROUP if group_id is not None else ScopeType.PRIVATE,
        sender=SenderIdentity(
            user_id=user_id,
            nickname=nickname,
            group_card=group_card,
        ),
        text=text,
        bot_user_id="9999",
        group_id=group_id,
        mentions_bot=mentions_bot,
        mentioned_user_ids=mentioned_user_ids,
    )
