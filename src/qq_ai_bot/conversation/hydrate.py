"""Canonical Conversation and primary-alias hydrate. Primary is frozen on first write."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationModel,
    ConversationLegacyAliasModel,
)
from qq_ai_bot.conversation.rollup.db_models import ConversationScopeModel
from qq_ai_bot.identity.errors import IdentityDualWriteError


@dataclass(frozen=True, slots=True)
class HydratedConversation:
    conversation_id: str
    kind: str
    person_id: str | None
    space_id: str | None
    primary_alias: str
    generation: int


def _new_id() -> str:
    return str(uuid4())


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def primary_alias_for_conversation(
    session: AsyncSession,
    conversation_id: str,
) -> str | None:
    row = await session.scalar(
        select(ConversationLegacyAliasModel).where(
            ConversationLegacyAliasModel.conversation_id == conversation_id,
            ConversationLegacyAliasModel.is_primary == 1,
        )
    )
    return None if row is None else row.scope_key


async def conversation_for_owner(
    session: AsyncSession,
    *,
    kind: str,
    person_id: str | None = None,
    space_id: str | None = None,
) -> CanonicalConversationModel | None:
    if kind == "private":
        if not person_id:
            raise IdentityDualWriteError("unclassified")
        return cast(
            CanonicalConversationModel | None,
            await session.scalar(
                select(CanonicalConversationModel).where(
                    CanonicalConversationModel.kind == "private",
                    CanonicalConversationModel.person_id == person_id,
                )
            ),
        )
    if not space_id:
        raise IdentityDualWriteError("unclassified")
    return cast(
        CanonicalConversationModel | None,
        await session.scalar(
            select(CanonicalConversationModel).where(
                CanonicalConversationModel.kind == "space",
                CanonicalConversationModel.space_id == space_id,
            )
        ),
    )


async def ensure_canonical_conversation(
    session: AsyncSession,
    *,
    kind: str,
    primary_scope_key: str,
    person_id: str | None = None,
    space_id: str | None = None,
) -> HydratedConversation:
    """Create or reuse one Conversation. Primary alias is immutable after first write."""

    if kind not in {"private", "space"}:
        raise IdentityDualWriteError("unclassified")
    existing = await conversation_for_owner(
        session, kind=kind, person_id=person_id, space_id=space_id
    )
    now = _utcnow()
    if existing is None:
        conversation_id = _new_id()
        alias_id = _new_id()
        try:
            async with session.begin_nested():
                session.add(
                    CanonicalConversationModel(
                        id=conversation_id,
                        kind=kind,
                        person_id=person_id if kind == "private" else None,
                        space_id=space_id if kind == "space" else None,
                        primary_alias_id=alias_id,
                        primary_marker=1,
                        generation=1,
                        starts_after_event_id=0,
                        last_event_id=0,
                        last_generation_change_event_id=0,
                        covered_through_event_id=0,
                        uncovered_event_count=0,
                        uncovered_character_count=0,
                        revision=1,
                        created_at=now,
                        updated_at=now,
                    )
                )
                session.add(
                    ConversationLegacyAliasModel(
                        id=alias_id,
                        conversation_id=conversation_id,
                        scope_key=primary_scope_key,
                        is_primary=1,
                        created_at=now,
                        updated_at=now,
                    )
                )
                await session.flush()
        except IntegrityError as exc:
            raced = await conversation_for_owner(
                session, kind=kind, person_id=person_id, space_id=space_id
            )
            if raced is None:
                raise IdentityDualWriteError("canonical_kind_mismatch") from exc
            existing = raced
        else:
            await _attach_scope_shadow(session, primary_scope_key, conversation_id)
            return HydratedConversation(
                conversation_id=conversation_id,
                kind=kind,
                person_id=person_id if kind == "private" else None,
                space_id=space_id if kind == "space" else None,
                primary_alias=primary_scope_key,
                generation=1,
            )
    assert existing is not None
    primary = await primary_alias_for_conversation(session, existing.id)
    if primary is None:
        raise IdentityDualWriteError("canonical_kind_mismatch")
    if primary_scope_key != primary:
        await ensure_legacy_alias(
            session,
            conversation_id=existing.id,
            scope_key=primary_scope_key,
            primary=False,
        )
    await _attach_scope_shadow(session, primary_scope_key, existing.id)
    await _attach_scope_shadow(session, primary, existing.id)
    return HydratedConversation(
        conversation_id=existing.id,
        kind=existing.kind,
        person_id=existing.person_id,
        space_id=existing.space_id,
        primary_alias=primary,
        generation=int(existing.generation),
    )


async def ensure_legacy_alias(
    session: AsyncSession,
    *,
    conversation_id: str,
    scope_key: str,
    primary: bool,
) -> None:
    existing = await session.scalar(
        select(ConversationLegacyAliasModel).where(
            ConversationLegacyAliasModel.scope_key == scope_key
        )
    )
    if existing is not None:
        if existing.conversation_id != conversation_id:
            raise IdentityDualWriteError("canonical_owner_mismatch")
        return
    if primary:
        raise IdentityDualWriteError("canonical_kind_mismatch")
    now = _utcnow()
    session.add(
        ConversationLegacyAliasModel(
            id=_new_id(),
            conversation_id=conversation_id,
            scope_key=scope_key,
            is_primary=0,
            created_at=now,
            updated_at=now,
        )
    )
    try:
        await session.flush()
    except IntegrityError:
        raced = await session.scalar(
            select(ConversationLegacyAliasModel).where(
                ConversationLegacyAliasModel.scope_key == scope_key
            )
        )
        if raced is None or raced.conversation_id != conversation_id:
            raise IdentityDualWriteError("canonical_owner_mismatch") from None


async def bump_canonical_generation(
    session: AsyncSession,
    conversation_id: str,
    *,
    event_id: int,
) -> int:
    """Only /ai new may increment Conversation generation."""

    row = await session.get(CanonicalConversationModel, conversation_id)
    if row is None:
        raise IdentityDualWriteError("unclassified")
    now = _utcnow()
    if row.last_generation_change_event_id == event_id:
        return int(row.generation)
    row.generation += 1
    row.starts_after_event_id = event_id
    row.last_generation_change_event_id = event_id
    row.last_event_id = max(int(row.last_event_id), event_id)
    row.covered_through_event_id = max(int(row.covered_through_event_id), event_id)
    row.uncovered_event_count = 0
    row.uncovered_character_count = 0
    row.revision += 1
    row.updated_at = now
    await session.flush()
    return int(row.generation)


async def touch_canonical_watermarks(
    session: AsyncSession,
    conversation_id: str,
    *,
    event_id: int,
    characters: int,
) -> None:
    row = await session.get(CanonicalConversationModel, conversation_id)
    if row is None:
        return
    row.last_event_id = max(int(row.last_event_id), event_id)
    row.uncovered_event_count += 1
    row.uncovered_character_count += max(0, characters)
    row.updated_at = _utcnow()


async def _attach_scope_shadow(
    session: AsyncSession,
    scope_key: str,
    conversation_id: str,
) -> None:
    scope = await session.scalar(
        select(ConversationScopeModel).where(ConversationScopeModel.scope_key == scope_key)
    )
    if scope is None:
        return
    current = scope.canonical_conversation_id
    if current is None:
        scope.canonical_conversation_id = conversation_id
        return
    if current != conversation_id:
        raise IdentityDualWriteError("canonical_owner_mismatch")
