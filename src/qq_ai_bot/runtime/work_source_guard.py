"""Permit enrichment of new events without weakening old-source/privacy fences."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from sqlalchemy import select

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
        async with control.repository.database.sessions() as session, session.begin():
            await control.repository._assert_lease(session, control.lease)
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
                return source.prompt_source_revision == version.prompt_source_revision
            rows = (
                await session.execute(
                    select(
                        ChatEventModel.id,
                        ChatEventModel.content,
                        ChatEventModel.segments_json,
                        ChatEventModel.visual_summary,
                        ChatEventModel.audio_transcript,
                        ChatEventModel.external_payload_json,
                        ChatEventModel.suppression_status,
                        ChatEventModel.canonical_conversation_id,
                    )
                    .where(ChatEventModel.id.in_(version.visible_event_ids))
                    .order_by(ChatEventModel.id)
                )
            ).all()
            if control.session is not None:
                added_ids = set(control.session.event_ids) - set(version.visible_event_ids)
                extra = (
                    await session.execute(
                        select(ChatEventModel.__table__).where(ChatEventModel.id.in_(added_ids))
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
                    self.additional_events[row.id] = digest
            values: list[object] = [rows]
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
            self.fingerprint = fingerprint
            if control.session is not None:
                control.session.source_revision = source.prompt_source_revision
            return True
