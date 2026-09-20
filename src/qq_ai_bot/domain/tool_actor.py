"""Authenticated execution source shared by chat and scheduled work.

This is not an incoming message. A scheduled source has an execution ID and
no invented event, message ID, mentions, or reply author.
"""

from dataclasses import dataclass

from qq_ai_bot.domain.messages import InboundMessage
from qq_ai_bot.runtime.origin import TurnOrigin


@dataclass(frozen=True, slots=True)
class ToolActor:
    user_id: str
    bot_user_id: str
    group_id: str | None
    origin: TurnOrigin
    instruction: str
    event_id: int | None = None
    execution_id: str = ""
    platform_message_id: str = ""
    person_id: str | None = None
    conversation_id: str | None = None
    presence_id: str | None = None
    mentioned_user_ids: tuple[str, ...] = ()

    @property
    def source_key(self) -> str:
        if self.event_id is not None:
            return f"event:{self.event_id}"
        if self.execution_id:
            return f"execution:{self.execution_id}"
        raise ValueError("missing_internal_source_anchor")

    @classmethod
    def from_inbound(cls, inbound: InboundMessage) -> "ToolActor":
        return cls(
            user_id=inbound.sender.user_id,
            bot_user_id=inbound.bot_user_id,
            group_id=inbound.group_id,
            origin=TurnOrigin.USER_MESSAGE,
            instruction=inbound.text,
            event_id=inbound.source_event_id,
            execution_id=inbound.source_execution_id or "",
            platform_message_id=inbound.message_id,
            person_id=inbound.person_id,
            conversation_id=inbound.conversation_id,
            presence_id=inbound.presence_id,
            mentioned_user_ids=inbound.mentioned_user_ids,
        )
