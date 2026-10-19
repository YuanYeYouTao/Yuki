"""Host-only identities and durable claims for explicit SDK sends.

No message-content deduplication: each explicit call occupies a distinct ordinal.
Only a trusted persisted callback identity can be replayed across Host restarts.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.domain.tool_actor import ToolActor
from yuki.identity.canonical_repository import (
    find_identity_binding,
    find_presence,
    find_space_binding,
)
from yuki.identity.db_models import CanonicalPersonModel
from yuki.identity.person_identity import current_person_id
from yuki.persistence.models import ChatEventModel
from yuki.persistence.repositories import EventLedgerRepository
from yuki.social.models import OperationStatus, SocialError, SocialReceipt, SocialTarget
from yuki.social.repository import SocialOperationRepository, payload_hash


@dataclass(slots=True)
class DirectDeliveryScope:
    identity: str
    ordinal: int = 0
    memory_ordinal: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def next_call(self) -> str:
        self.ordinal += 1
        return f"send:{self.ordinal}"

    def next_memory_call(self) -> str:
        self.memory_ordinal += 1
        return f"{self.identity}:memory:{self.memory_ordinal}"


async def prepare_delivery(
    ledger: EventLedgerRepository,
    *,
    source_conversation_id: str,
    source_event_id: int | None,
    actor_user_id: str,
    actor: ToolActor | None,
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
    payload = {
        "onebot_action": action,
        "params": params,
        "source_event_id": source_event_id,
        "actor_user_id": actor_user_id,
        "bot_user_id": bot_user_id,
        "origin": origin,
    }
    prior = await repository.find(source_turn_id, call_id)
    if prior is not None and prior.status is not OperationStatus.PREPARED:
        if prior.payload_hash != payload_hash(
            "send_message", prior.target, payload, source_conversation_id
        ):
            raise SocialError("idempotency_conflict")
        return repository, prior, prior.presence_id or ""
    async with ledger._database.sessions() as session:
        target, presence_id = await validate_delivery_source(
            session,
            source_conversation_id=source_conversation_id,
            source_event_id=source_event_id,
            actor_user_id=actor_user_id,
            actor=actor,
            bot_user_id=bot_user_id,
            group_id=group_id,
            user_id=user_id,
        )
    receipt = await repository.prepare(
        source_turn_id=source_turn_id,
        tool_call_id=call_id,
        source_conversation_id=source_conversation_id,
        action="send_message",
        target=target,
        payload=payload,
    )
    return repository, receipt, presence_id


async def validate_delivery_source(
    session: AsyncSession,
    *,
    source_conversation_id: str,
    source_event_id: int | None,
    actor_user_id: str,
    actor: ToolActor | None,
    bot_user_id: str,
    group_id: str | None,
    user_id: str | None,
) -> tuple[SocialTarget, str]:
    """Current source facts for a new send, also checked beside its durable claim."""
    conversation = await session.get(CanonicalConversationModel, source_conversation_id)
    if conversation is None:
        raise SocialError("plugin_send_source_unavailable")
    actor_id = actor.person_id if actor is not None else None
    if source_event_id is not None:
        event = await session.get(ChatEventModel, source_event_id)
        if (
            event is None
            or event.canonical_conversation_id != source_conversation_id
            or event.sender_user_id != actor_user_id
            or event.bot_user_id != bot_user_id
            or event.direction != "inbound"
            or event.author_person_id is None
            or event.id <= conversation.starts_after_event_id
        ):
            raise SocialError("plugin_send_source_mismatch")
        actor_id = event.author_person_id
    elif actor is None:
        raise SocialError("plugin_send_source_unavailable")
    if actor_id is not None:
        original = await session.get(CanonicalPersonModel, actor_id)
        root = await session.get(CanonicalPersonModel, await current_person_id(session, actor_id))
        if original is None or not original.enabled or root is None or not root.enabled:
            raise SocialError("plugin_send_source_mismatch")
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
    return SocialTarget(kind=kind, id=UUID(target_id)), presence.id


def delivery_unknown(receipt: SocialReceipt) -> bool:
    return receipt.status in {OperationStatus.EXECUTING, OperationStatus.UNCERTAIN}
