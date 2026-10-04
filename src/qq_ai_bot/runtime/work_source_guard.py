"""Permit enrichment of new events without weakening old-source/privacy fences."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, replace
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import select, text

from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationModel,
    CanonicalConversationRollupEmergencyOverlayModel,
    CanonicalConversationRollupModel,
)
from qq_ai_bot.conversation.rollup.coverage import (
    valid_same_generation_overlay,
    valid_same_generation_semantic,
)
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.execution_trace.db_models import ExecutionTraceStateModel
from qq_ai_bot.persistence.event_repository import ConversationReadVersion
from qq_ai_bot.persistence.models import ChatEventModel

if TYPE_CHECKING:
    from qq_ai_bot.runtime.work_control import WorkControl


class WorkSourceGuard:
    def __init__(self, version: ConversationReadVersion) -> None:
        self.version = version
        self.fingerprint: str | None = None
        self.additional_events: dict[int, str] = {}

    def snapshot(self) -> dict[str, object]:
        """Persist the selected read dependencies, not newly prepared history."""
        return {
            "version": asdict(self.version),
            "fingerprint": self.fingerprint,
            "additional_events": self.additional_events,
        }

    @classmethod
    def restore(cls, payload: dict[str, object]) -> WorkSourceGuard:
        data = dict(cast(dict[str, Any], payload["version"]))
        scope = dict(data["scope"])
        scope["scope_type"] = ScopeType(scope["scope_type"])
        data["scope"] = ConversationScope(**scope)
        data["visible_event_ids"] = tuple(data.get("visible_event_ids", ()))
        data["rollup_stamp"] = tuple(data.get("rollup_stamp", (0, 0)))
        if "observation_sources" in data:
            data["observation_sources"] = tuple(tuple(item) for item in data["observation_sources"])
        result = cls(ConversationReadVersion(**data))
        fingerprint = payload.get("fingerprint")
        result.fingerprint = fingerprint if isinstance(fingerprint, str) else None
        extra = payload.get("additional_events", {})
        if not isinstance(extra, dict):
            raise ValueError("work_source_guard_invalid")
        result.additional_events = {int(key): str(value) for key, value in extra.items()}
        return result

    async def check(
        self,
        control: WorkControl,
        *,
        observation_sources: tuple[tuple[str, int], ...] = (),
        event_ids: frozenset[int] = frozenset(),
    ) -> bool:
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
            added_ids = set(additional)
            added_ids.update(event_ids - set(version.visible_event_ids))
            if control.session is not None:
                added_ids.update(set(control.session.event_ids) - set(version.visible_event_ids))
            if added_ids:
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
            selected_summary = version.selected_summary_text
            summary: object = selected_summary
            if selected_summary is None:
                semantic = await session.get(
                    CanonicalConversationRollupModel, version.conversation_id
                )
                overlay = await session.get(
                    CanonicalConversationRollupEmergencyOverlayModel, version.conversation_id
                )
                # Match prompt snapshot selection. A background semantic checkpoint
                # below the still-effective overlay is not a new input to this Work.
                if semantic is not None and semantic.generation != source.generation:
                    return False
                effective: (
                    CanonicalConversationRollupEmergencyOverlayModel
                    | CanonicalConversationRollupModel
                    | None
                )
                if valid_same_generation_overlay(
                    overlay,
                    generation=source.generation,
                    starts_after=source.starts_after_event_id,
                    last_event_id=source.last_event_id,
                    semantic_revision=semantic.revision if semantic is not None else 0,
                ):
                    effective = overlay
                    kind = "emergency"
                elif valid_same_generation_semantic(
                    semantic,
                    generation=source.generation,
                    starts_after=source.starts_after_event_id,
                    last_event_id=source.last_event_id,
                ):
                    effective = semantic
                    assert semantic is not None
                    kind = semantic.summary_kind
                else:
                    if semantic is not None:
                        # Snapshot fallback still carries an existing semantic
                        # summary. An invalid coverage must not turn it into an
                        # apparently empty read set and omit its payload fence.
                        return False
                    effective = None
                    kind = None
                summary = (
                    (
                        effective.conversation_id,
                        effective.generation,
                        effective.covered_through_event_id,
                        effective.summary_text,
                        kind,
                        effective.source_fingerprint,
                        effective.revision,
                    )
                    if effective is not None
                    else None
                )
            privacy = (
                await session.scalar(
                    select(ExecutionTraceStateModel.privacy_generation).where(
                        ExecutionTraceStateModel.id == 1
                    )
                )
                or 0
            )
            selected_summary = getattr(version, "selected_summary_text", None)
            # Frozen selection owns the summary used by this request. A newer
            # derived Rollup is not an edit of that already submitted snapshot.
            if selected_summary is not None:
                summary = selected_summary
            from qq_ai_bot.conversation.observations import validate_observations

            original_observations = version.observation_sources
            if original_observations:
                assert version.conversation_id is not None
                if not await validate_observations(
                    session,
                    version.conversation_id,
                    version.generation,
                    version.observation_actor_id,
                    version.observation_read_scope,
                    original_observations,
                ):
                    return False
            values: list[object] = [identity, rows, summary, original_observations, privacy]
            fingerprint = hashlib.sha256(repr(values).encode()).hexdigest()
            if self.fingerprint is not None and self.fingerprint != fingerprint:
                return False
            # Only after proving the saved sources, extend this same read snapshot
            # with observations actually selected by the just-committed dispatch.
            merged = dict(original_observations)
            for observation_id, observation_version in observation_sources:
                if observation_id in merged and merged[observation_id] != observation_version:
                    return False
                merged[observation_id] = observation_version
            selected_version = version
            if tuple(merged.items()) != original_observations:
                assert version.conversation_id is not None
                if not await validate_observations(
                    session,
                    version.conversation_id,
                    version.generation,
                    version.observation_actor_id,
                    version.observation_read_scope,
                    tuple(merged.items()),
                ):
                    return False
                selected_version = replace(version, observation_sources=tuple(merged.items()))
                values[3] = selected_version.observation_sources
                fingerprint = hashlib.sha256(repr(values).encode()).hexdigest()
            revision = source.prompt_source_revision
        # All fingerprint sources advance this existing revision on mutation.
        # Recheck the lease and scalar dependencies in one fresh read snapshot.
        # This is preparation, not mutation authority: journal/publication/effect
        # writers keep their original execution-time fences and source CAS.
        async with control.repository.database.sessions() as session:
            await session.execute(text("BEGIN"))
            await control.repository._assert_lease_readonly(session, control.lease)
            current = await session.get(CanonicalConversationModel, version.conversation_id)
            current_privacy = (
                await session.scalar(
                    select(ExecutionTraceStateModel.privacy_generation).where(
                        ExecutionTraceStateModel.id == 1
                    )
                )
                or 0
            )
            if (
                current is None
                or (
                    current.generation,
                    current.starts_after_event_id,
                    current.prompt_source_revision,
                    current.kind,
                    current.person_id,
                    current.space_id,
                )
                != (
                    version.generation,
                    version.starts_after_event_id,
                    revision,
                    *identity,
                )
                or current_privacy != privacy
            ):
                return False
        self.version = selected_version
        self.fingerprint = fingerprint
        self.additional_events = additional
        if control.session is not None:
            control.session.source_revision = revision
        return True
