"""Permit enrichment of new events without weakening old-source/privacy fences."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Set
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import func, select, text

from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.domain.conversations import ConversationScope, ScopeType
from yuki.execution_trace.db_models import ExecutionTraceStateModel
from yuki.execution_trace.phases import model_detail
from yuki.identity.person_identity import same_person_condition
from yuki.persistence.database import Database
from yuki.persistence.event_repository import ConversationReadVersion
from yuki.persistence.models import ChatEventModel

if TYPE_CHECKING:
    from yuki.runtime.work_control import WorkControl


def _source_value(value: object) -> object:
    if hasattr(value, "_mapping"):
        return ["row", [[str(key), _source_value(item)] for key, item in value._mapping.items()]]
    if isinstance(value, (list, tuple)):
        return [type(value).__name__, [_source_value(item) for item in value]]
    if isinstance(value, (datetime, date)):
        return [type(value).__name__, value.isoformat()]
    if isinstance(value, bytes):
        return ["bytes", value.hex()]
    if value is None or isinstance(value, (str, bool, int, float)):
        return [type(value).__name__, value]
    raise TypeError(f"unsupported_source_value:{type(value).__name__}")


def _source_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            _source_value(value), ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class _VerifiedSources:
    """Instance-local proof of the original immutable dependency projection.

    This is never journalled or restored. Source writers advance the existing
    revision for every ChatEvent column; observations and cross-conversation
    inputs retain their independent live checks.
    """

    database: Database
    version: ConversationReadVersion
    revision: int
    identity: tuple[str, str | None, str | None]
    privacy: int
    fingerprint: str
    additional_events: dict[int, str]
    visible_ids: frozenset[int]
    eligible_images: frozenset[int]
    cross_ids: frozenset[int]


class WorkSourceGuard:
    def __init__(self, version: ConversationReadVersion) -> None:
        self.version = version
        self.codec = 2
        self.fingerprint: str | None = None
        self.additional_events: dict[int, str] = {}
        self._verified: _VerifiedSources | None = None

    def fork(self) -> WorkSourceGuard:
        """Prepare a provisional boundary from the same proven immutable sources."""
        result = WorkSourceGuard(self.version)
        result.adopt(self)
        return result

    def adopt(self, candidate: WorkSourceGuard) -> None:
        """Apply or roll back an in-process preparation without a journal round trip."""
        self.version = candidate.version
        self.codec = candidate.codec
        self.fingerprint = candidate.fingerprint
        self.additional_events = dict(candidate.additional_events)
        self._verified = candidate._verified

    def snapshot(self) -> dict[str, object]:
        """Persist the selected read dependencies, not newly prepared history."""
        version = asdict(replace(self.version, visible_event_ids=()))
        version["visible_event_ids"] = tuple(self.version.visible_event_ids)
        return {
            "codec": self.codec,
            "version": version,
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
        data.pop("rollup_stamp", None)
        if "observation_sources" in data:
            data["observation_sources"] = tuple(tuple(item) for item in data["observation_sources"])
        result = cls(ConversationReadVersion(**data))
        codec = payload.get("codec", 1)
        if not isinstance(codec, int):
            raise ValueError("work_source_guard_invalid")
        result.codec = codec
        fingerprint = payload.get("fingerprint")
        result.fingerprint = fingerprint if isinstance(fingerprint, str) else None
        extra = payload.get("additional_events", {})
        if not isinstance(extra, dict):
            raise ValueError("work_source_guard_invalid")
        result.additional_events = {int(key): str(value) for key, value in extra.items()}
        return result

    async def check(
        self,
        control: WorkControl | None,
        *,
        database: Database | None = None,
        observation_sources: tuple[tuple[str, int], ...] = (),
        event_ids: Set[int] = frozenset(),
    ) -> bool:
        if self.codec != 2:
            return False
        source_database = control.repository.database if control is not None else database
        assert source_database is not None
        work_session = control.session if control is not None else None
        version = self.version
        from yuki.services.turn_transcript import dispatch_request

        dispatch = dispatch_request()
        selected_images = tuple(
            image
            for message in ((*dispatch.messages, *dispatch.items) if dispatch else ())
            for image in getattr(message, "images", ())
            if image.source_event_id is not None
        )
        if any(
            image.conversation_id != version.conversation_id
            or image.generation != version.generation
            for image in selected_images
        ):
            return False
        image_event_ids = {image.source_event_id for image in selected_images}
        event_ids = event_ids | frozenset(image_event_ids)
        cross_ids: set[int] = set()
        # aiosqlite's legacy transaction mode does not BEGIN for SELECT. Establish
        # one real read snapshot, without reserving the writer during scans/hashing.
        with model_detail("source_guard_read"):
            async with source_database.sessions() as session:
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
                identity = (source.kind, source.person_id, source.space_id)
                privacy = (
                    await session.scalar(
                        select(ExecutionTraceStateModel.privacy_generation).where(
                            ExecutionTraceStateModel.id == 1
                        )
                    )
                    or 0
                )
                verified = self._verified
                original_observations = version.observation_sources
                observation_versions = dict(original_observations)
                reused = (
                    verified is not None
                    and verified.database is source_database
                    and verified.version is version
                    and verified.revision == source.prompt_source_revision
                    and verified.identity == identity
                    and verified.privacy == privacy
                    and verified.fingerprint == self.fingerprint
                    and verified.additional_events == self.additional_events
                    and all(
                        observation_versions.get(key) == value for key, value in observation_sources
                    )
                )
                visible_ids = (
                    verified.visible_ids
                    if reused and verified is not None
                    else frozenset(version.visible_event_ids)
                )
                rows = (
                    []
                    if reused
                    else (
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
                )
                if not reused and {row.id for row in rows} != visible_ids:
                    return False
                eligible_images = (
                    verified.eligible_images
                    if reused and verified is not None
                    else frozenset(
                        row.id
                        for row in rows
                        if row.suppression_status == "keeper"
                        and row.direction == "inbound"
                        and row.id > version.starts_after_event_id
                    )
                )
                if reused and any(
                    event_id not in eligible_images
                    for event_id in image_event_ids
                    if event_id in visible_ids or event_id in self.additional_events
                ):
                    return False
                if any(
                    row.id in image_event_ids
                    and (
                        row.suppression_status != "keeper"
                        or row.direction != "inbound"
                        or row.id <= version.starts_after_event_id
                    )
                    for row in rows
                ):
                    return False
                additional = dict(self.additional_events)
                added_ids = set(additional)
                added_ids.update(event_ids - visible_ids)
                if work_session is not None:
                    added_ids.update(set(work_session.event_ids) - visible_ids)
                query_ids = (
                    (added_ids - additional.keys()) | verified.cross_ids
                    if reused and verified is not None
                    else added_ids
                )
                new_eligible: set[int] = set()
                if query_ids:
                    extra = (
                        await session.execute(
                            select(ChatEventModel.__table__).where(
                                ChatEventModel.id.in_(query_ids),
                            )
                        )
                    ).all()
                    if {row.id for row in extra} != query_ids:
                        return False
                    cross_ids = {
                        row.id
                        for row in extra
                        if row.canonical_conversation_id != version.conversation_id
                    }
                    if cross_ids:
                        from yuki.runtime.work_schema_v1 import inputs

                        if control is None or control.current is None:
                            return False
                        actor = control.source.get("actor_person_id")
                        if not actor:
                            return False
                        accepted_query = (
                            select(ChatEventModel.id)
                            .join(inputs, inputs.c.event_id == ChatEventModel.id)
                            .join(
                                CanonicalConversationModel,
                                CanonicalConversationModel.id
                                == ChatEventModel.canonical_conversation_id,
                            )
                            .where(
                                inputs.c.work_id == control.current["id"],
                                ChatEventModel.id.in_(cross_ids),
                                func.json_extract(inputs.c.payload_json, "$.origin")
                                == "client_input",
                                func.json_extract(inputs.c.payload_json, "$.text")
                                == ChatEventModel.content,
                                func.json_extract(inputs.c.payload_json, "$.source_conversation_id")
                                == ChatEventModel.canonical_conversation_id,
                                ChatEventModel.origin == "work_input",
                                ChatEventModel.direction == "inbound",
                                ChatEventModel.suppression_status == "keeper",
                                ChatEventModel.author_kind == "person",
                                same_person_condition(ChatEventModel.author_person_id, actor),
                                CanonicalConversationModel.kind == "private",
                                same_person_condition(CanonicalConversationModel.person_id, actor),
                            )
                        )
                        accepted = set(await session.scalars(accepted_query))
                        if accepted != cross_ids:
                            return False
                    for row in extra:
                        if row.id in image_event_ids and (
                            row.suppression_status != "keeper"
                            or row.direction != "inbound"
                            or row.id <= version.starts_after_event_id
                        ):
                            return False
                        digest = _source_digest(row)
                        if (
                            row.id in self.additional_events
                            and self.additional_events[row.id] != digest
                        ):
                            return False
                        additional[row.id] = digest
                        if (
                            row.suppression_status == "keeper"
                            and row.direction == "inbound"
                            and row.id > version.starts_after_event_id
                        ):
                            new_eligible.add(row.id)
                selected_summary = version.selected_summary_text
                summary: object = selected_summary
                from yuki.conversation.observations import validate_observations

                if original_observations:
                    assert version.conversation_id is not None
                    if not await validate_observations(
                        session,
                        version.conversation_id,
                        version.generation,
                        version.observation_actor_id,
                        version.observation_read_scope,
                        original_observations,
                        snapshot_privacy_generation=privacy,
                    ):
                        return False
                values: list[object] = [identity, rows, summary, original_observations, privacy]
                fingerprint = (
                    verified.fingerprint
                    if reused and verified is not None
                    else _source_digest(values)
                )
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
                        snapshot_privacy_generation=privacy,
                    ):
                        return False
                    selected_version = replace(version, observation_sources=tuple(merged.items()))
                    values[3] = selected_version.observation_sources
                    fingerprint = _source_digest(values)
                revision = source.prompt_source_revision
        # All fingerprint sources advance this existing revision on mutation.
        # Recheck the lease and scalar dependencies in one fresh read snapshot.
        # This is preparation, not mutation authority: journal/publication/effect
        # writers keep their original execution-time fences and source CAS.
        with model_detail("source_guard_final_read"):
            async with source_database.sessions() as session:
                await session.execute(text("BEGIN"))
                if control is not None:
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
                if cross_ids:
                    fresh_extra = (
                        await session.execute(
                            select(ChatEventModel.__table__).where(ChatEventModel.id.in_(cross_ids))
                        )
                    ).all()
                    if {row.id: _source_digest(row) for row in fresh_extra} != {
                        identity: additional[identity] for identity in cross_ids
                    }:
                        return False
                    if set(await session.scalars(accepted_query)) != cross_ids:
                        return False
        self.version = selected_version
        self.fingerprint = fingerprint
        self.additional_events = additional
        self._verified = _VerifiedSources(
            source_database,
            selected_version,
            revision,
            identity,
            privacy,
            fingerprint,
            dict(additional),
            visible_ids,
            eligible_images | new_eligible if new_eligible else eligible_images,
            frozenset(cross_ids),
        )
        if work_session is not None:
            work_session.source_revision = revision
        return True
