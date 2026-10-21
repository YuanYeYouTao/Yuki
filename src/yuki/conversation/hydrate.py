"""Canonical Conversation and primary-alias hydrate. Primary is frozen on first write."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.conversation.canonical_db_models import (
    CanonicalConversationModel,
    ConversationLegacyAliasModel,
)
from yuki.conversation.scope_models import ConversationScopeState
from yuki.domain.conversations import ConversationScope, ScopeType
from yuki.identity.canonical_repository import find_identity_binding, find_presence
from yuki.identity.db_models import IdentityBindingModel
from yuki.identity.errors import CanonicalIdentityError


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


async def require_primary_alias_for_conversation(
    session: AsyncSession,
    conversation_id: str,
) -> str:
    """Return the single frozen primary alias. Zero or many is state_mismatch."""

    keys = list(
        await session.scalars(
            select(ConversationLegacyAliasModel.scope_key).where(
                ConversationLegacyAliasModel.conversation_id == conversation_id,
                ConversationLegacyAliasModel.is_primary == 1,
            )
        )
    )
    if len(keys) != 1:
        raise CanonicalIdentityError("state_mismatch")
    return str(keys[0])


async def binding_for_scope(
    session: AsyncSession, scope: ConversationScope
) -> IdentityBindingModel | None:
    """Resolve the account in its ingress Presence's platform, never an old alias."""
    binding = await find_identity_binding(
        session, scope.private_peer_user_id or "", platform=scope.platform
    )
    return binding if binding is not None and binding.status == "active" else None


async def conversation_for_binding(
    session: AsyncSession,
    identity_binding_id: str,
    *,
    presence_id: str | None = None,
    thread_id: str | None = None,
) -> CanonicalConversationModel | None:
    query = select(CanonicalConversationModel).where(
        CanonicalConversationModel.kind == "private",
        CanonicalConversationModel.identity_binding_id == identity_binding_id,
        CanonicalConversationModel.thread_id == thread_id,
    )
    if presence_id is not None:
        query = query.where(CanonicalConversationModel.entry_presence_id == presence_id)
    return (await session.execute(query)).scalar_one_or_none()


async def conversation_for_scope(
    session: AsyncSession, scope: ConversationScope
) -> CanonicalConversationModel | None:
    if scope.conversation_id is not None:
        return await session.get(CanonicalConversationModel, scope.conversation_id)
    if scope.scope_type is ScopeType.PRIVATE:
        binding = await binding_for_scope(session, scope)
        presence_id = scope.presence_id
        if presence_id is None:
            presence = await find_presence(session, scope.bot_user_id, platform=scope.platform)
            presence_id = presence.id if presence is not None else None
        return (
            await conversation_for_binding(
                session, binding.id, presence_id=presence_id, thread_id=scope.thread_id
            )
            if binding
            else None
        )
    return cast(
        CanonicalConversationModel | None,
        await session.scalar(
            select(CanonicalConversationModel)
            .join(
                ConversationLegacyAliasModel,
                ConversationLegacyAliasModel.conversation_id == CanonicalConversationModel.id,
            )
            .where(ConversationLegacyAliasModel.scope_key == scope.key)
        ),
    )


async def conversation_for_owner(
    session: AsyncSession,
    *,
    kind: str,
    identity_binding_id: str | None = None,
    space_id: str | None = None,
    presence_id: str | None = None,
    space_binding_id: str | None = None,
    thread_id: str | None = None,
) -> CanonicalConversationModel | None:
    if kind == "private":
        if not identity_binding_id:
            raise CanonicalIdentityError("unclassified")
        return await conversation_for_binding(
            session, identity_binding_id, presence_id=presence_id, thread_id=thread_id
        )
    if not space_id:
        raise CanonicalIdentityError("unclassified")
    query = select(CanonicalConversationModel).where(
        CanonicalConversationModel.kind == "space",
        CanonicalConversationModel.space_id == space_id,
        CanonicalConversationModel.thread_id == thread_id,
    )
    if space_binding_id is not None:
        query = query.where(CanonicalConversationModel.space_binding_id == space_binding_id)
    else:
        query = query.where(CanonicalConversationModel.space_binding_id.is_(None))
    return cast(
        CanonicalConversationModel | None,
        await session.scalar(query),
    )


async def ensure_canonical_conversation(
    session: AsyncSession,
    *,
    kind: str,
    primary_scope_key: str,
    person_id: str | None = None,
    identity_binding_id: str | None = None,
    space_id: str | None = None,
    presence_id: str | None = None,
    space_binding_id: str | None = None,
    thread_id: str | None = None,
) -> HydratedConversation:
    """Create or reuse one Conversation. Primary alias is immutable after first write."""

    if kind not in {"private", "space"}:
        raise CanonicalIdentityError("unclassified")
    existing = await conversation_for_owner(
        session,
        kind=kind,
        identity_binding_id=identity_binding_id,
        space_id=space_id,
        presence_id=presence_id,
        space_binding_id=space_binding_id,
        thread_id=thread_id,
    )
    if existing is None and (presence_id is not None or space_binding_id is not None):
        # Real pre-entry records retain their original alias and history.
        legacy = await session.scalar(
            select(CanonicalConversationModel)
            .join(
                ConversationLegacyAliasModel,
                ConversationLegacyAliasModel.conversation_id == CanonicalConversationModel.id,
            )
            .where(
                ConversationLegacyAliasModel.scope_key == primary_scope_key,
                CanonicalConversationModel.kind == kind,
            )
        )
        if legacy is not None and (
            (
                kind == "private"
                and legacy.identity_binding_id == identity_binding_id
                and legacy.entry_presence_id is None
                and legacy.thread_id == thread_id
            )
            or (
                kind == "space"
                and legacy.space_id == space_id
                and legacy.space_binding_id is None
                and thread_id is None
            )
        ):
            existing = legacy
            existing.entry_presence_id = presence_id if kind == "private" else None
            existing.space_binding_id = space_binding_id if kind == "space" else None
    transport_key = primary_scope_key
    if kind == "private" and presence_id is None:
        primary_scope_key = f"binding:{identity_binding_id}"
    now = _utcnow()
    if existing is None:
        conversation_id = _new_id()
        if presence_id is not None or space_binding_id is not None:
            primary_scope_key = f"conversation:{conversation_id}"
        alias_id = _new_id()
        try:
            async with session.begin_nested():
                session.add(
                    CanonicalConversationModel(
                        id=conversation_id,
                        kind=kind,
                        person_id=person_id if kind == "private" else None,
                        identity_binding_id=identity_binding_id if kind == "private" else None,
                        space_id=space_id if kind == "space" else None,
                        entry_presence_id=presence_id if kind == "private" else None,
                        space_binding_id=space_binding_id if kind == "space" else None,
                        thread_id=thread_id,
                        primary_alias_id=alias_id,
                        primary_marker=1,
                        generation=1,
                        starts_after_event_id=0,
                        last_event_id=0,
                        last_generation_change_event_id=0,
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
                session,
                kind=kind,
                identity_binding_id=identity_binding_id,
                space_id=space_id,
                presence_id=presence_id,
                space_binding_id=space_binding_id,
                thread_id=thread_id,
            )
            if raced is None:
                raise CanonicalIdentityError("canonical_kind_mismatch") from exc
            existing = raced
        else:
            if transport_key != primary_scope_key:
                await ensure_legacy_alias(
                    session, conversation_id=conversation_id, scope_key=transport_key, primary=False
                )
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
        raise CanonicalIdentityError("canonical_kind_mismatch")
    if transport_key != primary:
        await ensure_legacy_alias(
            session,
            conversation_id=existing.id,
            scope_key=transport_key,
            primary=False,
        )
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
            conversation = await session.get(CanonicalConversationModel, conversation_id)
            if conversation is not None and conversation.kind == "private":
                return
            raise CanonicalIdentityError("canonical_owner_mismatch")
        return
    if primary:
        raise CanonicalIdentityError("canonical_kind_mismatch")
    now = _utcnow()
    from sqlalchemy.dialects.sqlite import insert

    await session.execute(
        insert(ConversationLegacyAliasModel)
        .values(
            id=_new_id(),
            conversation_id=conversation_id,
            scope_key=scope_key,
            is_primary=0,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(index_elements=["scope_key"])
    )
    owner = await session.scalar(
        select(ConversationLegacyAliasModel.conversation_id).where(
            ConversationLegacyAliasModel.scope_key == scope_key
        )
    )
    if owner != conversation_id:
        conversation = await session.get(CanonicalConversationModel, conversation_id)
        if conversation is None or conversation.kind != "private":
            raise CanonicalIdentityError("canonical_owner_mismatch")


async def bump_canonical_generation(
    session: AsyncSession,
    conversation_id: str,
    *,
    event_id: int,
    force: bool = False,
) -> int:
    """Apply /ai new semantics, including an explicitly audited offline reset."""

    row = await session.get(CanonicalConversationModel, conversation_id)
    if row is None:
        raise CanonicalIdentityError("unclassified")
    now = _utcnow()
    if not force and row.last_generation_change_event_id == event_id:
        return int(row.generation)
    row.generation += 1
    row.starts_after_event_id = event_id
    row.last_generation_change_event_id = event_id
    row.last_event_id = max(int(row.last_event_id), event_id)
    row.revision += 1
    row.updated_at = now
    from yuki.runtime.work_repository import WorkRepository

    await WorkRepository.cancel_in_session(session, conversation_id, generation=int(row.generation))
    await session.flush()
    return int(row.generation)


async def touch_canonical_watermarks(
    session: AsyncSession,
    conversation_id: str,
    *,
    event_id: int,
) -> None:
    row = await session.get(CanonicalConversationModel, conversation_id)
    if row is None:
        return
    row.last_event_id = max(int(row.last_event_id), event_id)
    row.updated_at = _utcnow()


def scope_state_from_canonical(
    scope: ConversationScope,
    conversation: CanonicalConversationModel,
    *,
    runtime_scope_key: str | None = None,
) -> ConversationScopeState:
    """Synthesize the legacy scope view from canonical watermarks. Does not write.

    ``scope`` is the transport ConversationScope (current Presence).
    ``runtime_scope_key`` is the frozen primary legacy alias when known.
    """

    return ConversationScopeState(
        id=conversation.id,
        scope=scope,
        generation=int(conversation.generation),
        starts_after_event_id=int(conversation.starts_after_event_id),
        last_event_id=int(conversation.last_event_id),
        last_generation_change_event_id=int(conversation.last_generation_change_event_id),
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        runtime_scope_key=runtime_scope_key,
    )


async def hydrate_scope_state_from_canonical(
    session: AsyncSession,
    scope: ConversationScope,
    conversation: CanonicalConversationModel,
) -> ConversationScopeState:
    """Unique v2 projection: transport scope plus frozen primary runtime key."""

    primary = await primary_alias_for_conversation(session, conversation.id)
    if not primary:
        raise CanonicalIdentityError("unclassified")
    return scope_state_from_canonical(scope, conversation, runtime_scope_key=primary)
