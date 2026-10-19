"""Explicit accepted historical facts for tests, not a second ingress implementation."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.conversation.canonical_db_models import (
    CanonicalConversationModel,
    CanonicalEventReceiptModel,
)
from yuki.conversation.hydrate import (
    ensure_canonical_conversation,
    hydrate_scope_state_from_canonical,
    touch_canonical_watermarks,
)
from yuki.conversation.media_index import index_event_media
from yuki.domain.conversations import ConversationScope, ScopeType
from yuki.identity.canonical_repository import (
    find_identity_binding,
    find_presence,
    find_space_binding,
)
from yuki.identity.db_models import PresenceModel
from yuki.identity.receipt_compat import transport_namespace
from yuki.persistence.models import ChatEventModel
from yuki.persistence.repository_helpers import _event_record
from yuki.persistence.scoped_event_uow import ScopedAppendResult
from yuki.persistence.unit_of_work import optional_session


async def seed_event(
    ledger,
    *,
    bot_user_id: str,
    scope_type: ScopeType,
    sender_user_id: str,
    group_id: str | None = None,
    private_peer_user_id: str | None = None,
    **fields: Any,
):
    scope = (
        ConversationScope.group(bot_user_id, group_id or "")
        if scope_type is ScopeType.GROUP
        else ConversationScope.private(bot_user_id, private_peer_user_id or sender_user_id)
    )
    result = await seed_scoped_event(
        ledger._writer, scope=scope, sender_user_id=sender_user_id, **fields
    )
    return result.event, result.created


async def seed_scoped_event(
    writer,
    *,
    scope: ConversationScope,
    platform_message_id: str,
    sender_user_id: str,
    direction: str,
    content: str,
    segments: tuple[dict[str, Any], ...] = (),
    sender_is_bot: bool = False,
    occurred_at: datetime | None = None,
    session: AsyncSession | None = None,
    **fields: Any,
) -> ScopedAppendResult:
    if direction != "inbound":
        async with optional_session(writer._database, session, write=False) as reader:
            presence = (
                await reader.get(PresenceModel, scope.presence_id)
                if scope.presence_id
                else await find_presence(reader, scope.bot_user_id, platform=scope.platform)
            )
            assert presence is not None, "seed the original receiving Presence first"
            scope = replace(scope, presence_id=presence.id)
            if (
                scope.scope_type is ScopeType.PRIVATE
                and scope.conversation_id is None
                and "identity_binding_id" not in fields
            ):
                binding = await find_identity_binding(
                    reader, scope.private_peer_user_id or sender_user_id, platform=scope.platform
                )
                assert binding is not None, "seed the original private peer Assignment first"
                fields = {**fields, "identity_binding_id": binding.id}
            if scope.scope_type is ScopeType.GROUP and "space_binding_id" not in fields:
                space = await find_space_binding(
                    reader, scope.group_id or "", platform=scope.platform
                )
                assert space is not None, "seed the original SpaceBinding first"
                fields = {**fields, "space_binding_id": space.id}
        return await writer.append(
            scope=scope,
            platform_message_id=platform_message_id,
            sender_user_id=sender_user_id,
            direction=direction,
            content=content,
            segments=segments,
            occurred_at=occurred_at,
            session=session,
            **fields,
        )

    # These fixtures explicitly prove today's seeded owner even when their
    # historical event time predates the fixture's Assignment. Native ingress
    # timing/admission is tested through CanonicalIngressUnitOfWork instead.
    async def insert(active: AsyncSession) -> ScopedAppendResult:
        presence = (
            await active.get(PresenceModel, scope.presence_id)
            if scope.presence_id
            else await find_presence(active, scope.bot_user_id, platform=scope.platform)
        )
        assert presence is not None, "seed the original receiving Presence first"
        binding = await find_identity_binding(active, sender_user_id, platform=scope.platform)
        peer = (
            await find_identity_binding(
                active, scope.private_peer_user_id or sender_user_id, platform=scope.platform
            )
            if scope.scope_type is ScopeType.PRIVATE
            else None
        )
        space = (
            await find_space_binding(active, scope.group_id or "", platform=scope.platform)
            if scope.scope_type is ScopeType.GROUP
            else None
        )
        conversation = (
            await active.get(CanonicalConversationModel, scope.conversation_id)
            if scope.conversation_id
            else None
        )
        receipt = await active.scalar(
            select(CanonicalEventReceiptModel).where(
                CanonicalEventReceiptModel.ingress_account_id == presence.account_id,
                CanonicalEventReceiptModel.transport_namespace == transport_namespace(scope),
                CanonicalEventReceiptModel.event_type == "message",
                CanonicalEventReceiptModel.platform_message_id == platform_message_id,
            )
        )
        if receipt is not None:
            row = await active.scalar(
                select(ChatEventModel).where(
                    ChatEventModel.canonical_event_id == receipt.canonical_event_id,
                    ChatEventModel.suppression_status == "keeper",
                )
            )
            assert row is not None
            conversation = await active.get(
                CanonicalConversationModel, row.canonical_conversation_id
            )
            assert conversation is not None
            return ScopedAppendResult(
                _event_record(row),
                await hydrate_scope_state_from_canonical(active, scope, conversation),
                False,
            )
        if sender_user_id == scope.bot_user_id:
            author_kind, person_id, author_presence_id = "yuki", None, presence.id
        elif sender_is_bot:
            author_kind, person_id, author_presence_id = "external_bot", None, None
        else:
            assert binding is not None, "seed the original author Person/Assignment first"
            author_kind, person_id, author_presence_id = "person", binding.person_id, None
        if conversation is None:
            assert peer is not None or space is not None
            hydrated = await ensure_canonical_conversation(
                active,
                kind="private" if peer is not None else "space",
                primary_scope_key=scope.key,
                person_id=peer.person_id if peer else None,
                identity_binding_id=peer.id if peer else None,
                presence_id=presence.id if peer else None,
                space_id=space.space_id if space else None,
                space_binding_id=space.id if space else None,
                thread_id=scope.thread_id,
            )
            conversation = await active.get(CanonicalConversationModel, hydrated.conversation_id)
        assert conversation is not None
        now = datetime.now(UTC)
        row = ChatEventModel(
            bot_user_id=scope.bot_user_id,
            platform_message_id=platform_message_id,
            scope_type=scope.scope_type.value,
            group_id=scope.group_id,
            private_peer_user_id=scope.private_peer_user_id,
            sender_user_id=sender_user_id,
            direction="inbound",
            event_kind="message",
            content=content,
            visual_summary="",
            segments_json=json.dumps(segments, ensure_ascii=False, separators=(",", ":")),
            occurred_at=occurred_at or now,
            observed_at=now,
            canonical_event_id=str(uuid4()),
            canonical_conversation_id=conversation.id,
            author_kind=author_kind,
            author_person_id=person_id,
            author_presence_id=author_presence_id,
            ingress_presence_id=presence.id,
            suppression_status="keeper",
            **fields,
        )
        active.add_all(
            (
                row,
                CanonicalEventReceiptModel(
                    ingress_presence_id=presence.id,
                    ingress_account_id=presence.account_id,
                    transport_namespace=transport_namespace(scope),
                    event_type="message",
                    platform_message_id=platform_message_id,
                    canonical_event_id=row.canonical_event_id,
                    created_at=now,
                    observed_at=now,
                ),
            )
        )
        await active.flush()
        await touch_canonical_watermarks(active, conversation.id, event_id=row.id)
        await index_event_media(
            active,
            event_id=row.id,
            conversation_id=conversation.id,
            generation=conversation.generation,
            segments=segments,
        )
        return ScopedAppendResult(
            _event_record(row),
            await hydrate_scope_state_from_canonical(active, scope, conversation),
            True,
        )

    if session is not None:
        return await insert(session)
    return await writer._database.apply_snapshot_write(insert)
