"""Host-only identities and durable claims for explicit SDK sends.

No message-content deduplication: each explicit call occupies a distinct ordinal.
Only a trusted persisted callback identity can be replayed across Host restarts.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.identity.canonical_repository import (
    find_identity_binding,
    find_presence,
    find_space_binding,
)
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.persistence.repositories import EventLedgerRepository
from qq_ai_bot.social.models import OperationStatus, SocialError, SocialReceipt, SocialTarget
from qq_ai_bot.social.repository import SocialOperationRepository


@dataclass(slots=True)
class DirectDeliveryScope:
    identity: str
    ordinal: int = 0
    unknown: SocialReceipt | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def next_call(self) -> str:
        self.ordinal += 1
        return f"send:{self.ordinal}"


async def prepare_delivery(
    ledger: EventLedgerRepository,
    *,
    source_conversation_id: str,
    source_event_id: int | None,
    actor_user_id: str,
    origin: str,
    bot_user_id: str,
    group_id: str | None,
    user_id: str | None,
    source_turn_id: str,
    call_id: str,
    action: str,
    params: dict[str, Any],
) -> tuple[SocialOperationRepository, SocialReceipt, str]:
    """Resolve existing identity before the short durable claim transaction."""

    repository = SocialOperationRepository(ledger._database)
    async with ledger._database.sessions() as session:
        conversation = await session.get(CanonicalConversationModel, source_conversation_id)
        if conversation is None:
            raise SocialError("plugin_send_source_unavailable")
        if source_event_id is not None:
            event = await session.get(ChatEventModel, source_event_id)
            if (
                event is None
                or event.canonical_conversation_id != source_conversation_id
                or event.sender_user_id != actor_user_id
                or event.bot_user_id != bot_user_id
                or event.direction != "inbound"
                or event.id <= conversation.starts_after_event_id
            ):
                raise SocialError("plugin_send_source_mismatch")
        elif origin != "scheduled_automation":
            raise SocialError("plugin_send_source_unavailable")
        presence = await find_presence(session, bot_user_id)
        if presence is None or not presence.enabled:
            raise SocialError("plugin_send_presence_unavailable")
        kind: Literal["person", "space"]
        if group_id is not None:
            space_binding = await find_space_binding(session, group_id)
            target_id = space_binding.space_id if space_binding is not None else None
            active = space_binding is not None and space_binding.status == "active"
            kind = "space"
        else:
            person_binding = await find_identity_binding(session, user_id or "")
            target_id = person_binding.person_id if person_binding is not None else None
            active = person_binding is not None and person_binding.status == "active"
            kind = "person"
        if not active or not target_id:
            raise SocialError("plugin_send_target_unavailable")
        target = SocialTarget(kind=kind, id=UUID(target_id))
        presence_id = presence.id
    receipt = await repository.prepare(
        source_turn_id=source_turn_id,
        tool_call_id=call_id,
        source_conversation_id=source_conversation_id,
        action="send_message",
        target=target,
        payload={
            "onebot_action": action,
            "params": params,
            "source_event_id": source_event_id,
            "actor_user_id": actor_user_id,
            "bot_user_id": bot_user_id,
            "origin": origin,
        },
    )
    return repository, receipt, presence_id


def delivery_unknown(receipt: SocialReceipt) -> bool:
    return receipt.status in {OperationStatus.EXECUTING, OperationStatus.UNCERTAIN}
