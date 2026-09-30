"""Permit enrichment of new events without weakening old-source/privacy fences."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from sqlalchemy import select, text

from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationModel,
    CanonicalConversationRollupEmergencyOverlayModel,
    CanonicalConversationRollupModel,
)
from qq_ai_bot.persistence.event_repository import ConversationReadVersion
from qq_ai_bot.persistence.models import ChatEventModel

if TYPE_CHECKING:
    from qq_ai_bot.runtime.work_control import WorkControl


class WorkSourceGuard:
    def __init__(self, version: ConversationReadVersion) -> None:
        self.version = version
        self.fingerprint: str | None = None
        self.additional_events: dict[int, str] = {}

    async def check(self, control: WorkControl) -> bool:
        version = self.version
        if (control.lease.conversation_id, control.lease.generation) != (
            version.conversation_id,
            version.generation,
        ):
            return False
        # aiosqlite's legacy transaction mode does not BEGIN for SELECT. Establish
        # one real read snapshot, without reserving the writer during scans/hashing.
        async with control.repository.database.sessions() as session:
            await session.execute(text("BEGIN"))
            source = await session.get(CanonicalConversationModel, version.conversation_id)
            if source is None or (source.generation, source.starts_after_event_id) != (
                version.generation,
                version.starts_after_event_id,
            ):
                return False
            if (
                self.fingerprint is None
                and source.prompt_source_revision != version.prompt_source_revision
            ):
                return False
            # A missing selection contract must keep the original strict check.
            if not version.visible_event_ids:
                if source.prompt_source_revision != version.prompt_source_revision:
                    return False
            rows = (
                await session.execute(
                    select(
                        ChatEventModel.__table__,
                    )
                    .where(
                        ChatEventModel.id.in_(version.visible_event_ids),
                        ChatEventModel.canonical_conversation_id == version.conversation_id,
                    )
                    .order_by(ChatEventModel.id)
                )
            ).all()
            if {row.id for row in rows} != set(version.visible_event_ids):
                return False
            additional = dict(self.additional_events)
            if control.session is not None:
                added_ids = set(control.session.event_ids) - set(version.visible_event_ids)
                extra = (
                    await session.execute(
                        select(ChatEventModel.__table__).where(
                            ChatEventModel.id.in_(added_ids),
                            ChatEventModel.canonical_conversation_id == version.conversation_id,
                        )
                    )
                ).all()
                if {row.id for row in extra} != added_ids:
                    return False
                for row in extra:
                    digest = hashlib.sha256(repr(row).encode()).hexdigest()
                    if (
                        row.id in self.additional_events
                        and self.additional_events[row.id] != digest
                    ):
                        return False
                    additional[row.id] = digest
            identity = (source.kind, source.person_id, source.space_id)
            values: list[object] = [identity, rows]
            for model in (
                CanonicalConversationRollupModel,
                CanonicalConversationRollupEmergencyOverlayModel,
            ):
                values.append(
                    (
                        await session.execute(
                            select(model.__table__).where(
                                model.conversation_id == version.conversation_id,
                            )
                        )
                    ).all()
                )
            fingerprint = hashlib.sha256(repr(values).encode()).hexdigest()
            if self.fingerprint is not None and self.fingerprint != fingerprint:
                return False
            revision = source.prompt_source_revision
        # All fingerprint sources advance this existing revision on mutation.
        # The reader is closed before waiting for the writer; only scalar rechecks
        # and the original lease/cancellation fence remain in the write transaction.
        async with control.repository.database.sessions() as session, session.begin():
            await control.repository._assert_lease(session, control.lease)
            current = await session.get(CanonicalConversationModel, version.conversation_id)
            if current is None or (
                current.generation,
                current.starts_after_event_id,
                current.prompt_source_revision,
                current.kind,
                current.person_id,
                current.space_id,
            ) != (
                version.generation,
                version.starts_after_event_id,
                revision,
                *identity,
            ):
                return False
        self.fingerprint = fingerprint
        self.additional_events = additional
        if control.session is not None:
            control.session.source_revision = revision
        return True
