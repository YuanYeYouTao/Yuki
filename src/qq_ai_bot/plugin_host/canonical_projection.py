"""Host-only canonical identity projection for Plugin API 3.0 SDK contexts.

Plugins, manifests, and caller payloads never supply these fields. The Host
copies trusted ingress/runtime stamps or an already-bound automation context.
This module does not create Conversation, Person, Space, or Presence rows.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.conversation.hydrate import require_primary_alias_for_conversation
from qq_ai_bot.domain.identity import AuthorKind
from qq_ai_bot.domain.messages import InboundMessage
from qq_ai_bot.persistence.repository_records import EventRecord


@dataclass(frozen=True, slots=True)
class HostCanonicalProjection:
    """Narrow Host stamp shared by invocation, CurrentMessage, and admission."""

    person_id: str | None = None
    space_id: str | None = None
    conversation_id: str | None = None
    presence_id: str | None = None
    conversation_key: str | None = None

    def sdk_fields(self) -> dict[str, str | None]:
        return {
            "person_id": self.person_id,
            "space_id": self.space_id,
            "conversation_id": self.conversation_id,
            "presence_id": self.presence_id,
        }


def projection_from_inbound(message: InboundMessage) -> HostCanonicalProjection:
    """Copy Host-stamped inbound identity. Never invent a Conversation or alias."""

    return HostCanonicalProjection(
        person_id=message.person_id,
        space_id=message.space_id,
        conversation_id=message.conversation_id,
        presence_id=message.presence_id,
        conversation_key=message.legacy_conversation_key,
    )


def projection_from_automation(
    *,
    conversation_key: str,
    conversation_id: str | None,
    person_id: str | None,
    space_id: str | None,
) -> HostCanonicalProjection:
    """Scheduled capability context is Host-trusted. Missing Conversation stays None."""

    if conversation_id:
        return HostCanonicalProjection(
            person_id=person_id,
            space_id=space_id,
            conversation_id=conversation_id,
            presence_id=None,
            conversation_key=conversation_key,
        )
    return HostCanonicalProjection(conversation_key=conversation_key)


def projection_from_event(record: EventRecord) -> HostCanonicalProjection:
    """History rows keep author_kind: external_bot/system/yuki never become Person."""

    person_id = record.author_person_id if record.author_kind == AuthorKind.PERSON.value else None
    return HostCanonicalProjection(
        person_id=person_id,
        space_id=None,
        conversation_id=record.canonical_conversation_id,
        presence_id=record.ingress_presence_id,
    )


async def projection_from_existing_conversation(
    session: AsyncSession,
    *,
    conversation_id: str,
    person_id: str | None = None,
    space_id: str | None = None,
    presence_id: str | None = None,
) -> HostCanonicalProjection:
    """Resolve SDK conversation_key from exactly one primary alias. No row create."""

    conversation_key = await require_primary_alias_for_conversation(session, conversation_id)
    return HostCanonicalProjection(
        person_id=person_id,
        space_id=space_id,
        conversation_id=conversation_id,
        presence_id=presence_id,
        conversation_key=conversation_key,
    )
