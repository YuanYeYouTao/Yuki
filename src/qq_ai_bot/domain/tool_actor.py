"""Authenticated execution source shared by chat and scheduled work.

This is not an incoming message. A scheduled source has an execution ID and
no invented event, message ID, mentions, or reply author.
"""

from dataclasses import dataclass
from typing import Literal

from qq_ai_bot.domain.messages import InboundMessage
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.runtime.principal import SELF, PrincipalRef


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
    principal_kind: Literal["person", "self"] = "person"
    initiative_run_id: str | None = None
    automation_run_id: int | None = None

    def __post_init__(self) -> None:
        if self.principal_kind == "self":
            if (
                self.origin not in {TurnOrigin.SELF_INITIATIVE, TurnOrigin.SCHEDULED_AUTOMATION}
                or (self.origin is TurnOrigin.SELF_INITIATIVE and not self.initiative_run_id)
                or (
                    self.origin is TurnOrigin.SELF_INITIATIVE and self.automation_run_id is not None
                )
                or (
                    self.origin is TurnOrigin.SCHEDULED_AUTOMATION
                    and (
                        self.initiative_run_id is not None
                        or not self.execution_id
                        or not self.automation_run_id
                    )
                )
                or not self.conversation_id
                or not self.presence_id
                or not self.group_id
                or not self.bot_user_id
                or self.user_id
                or self.person_id
                or self.event_id is not None
                or self.platform_message_id
                or self.mentioned_user_ids
            ):
                raise ValueError("invalid_self_tool_actor")
        elif (
            self.principal_kind != "person"
            or self.initiative_run_id is not None
            or self.automation_run_id is not None
            or self.origin is TurnOrigin.SELF_INITIATIVE
        ):
            raise ValueError("invalid_tool_actor_principal")

    @property
    def source_key(self) -> str:
        if self.principal_kind == "self":
            return (
                f"initiative:{self.initiative_run_id}"
                if self.initiative_run_id is not None
                else f"execution:{self.execution_id}"
            )
        if self.event_id is not None:
            return f"event:{self.event_id}"
        if self.execution_id:
            return f"execution:{self.execution_id}"
        raise ValueError("missing_internal_source_anchor")

    @property
    def principal(self) -> PrincipalRef:
        if self.principal_kind == "self":
            return SELF
        if self.person_id is None:
            raise ValueError("person_principal_unresolved")
        return PrincipalRef("person", self.person_id)

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
