"""Publish trusted clues once; freeze their selection at real dispatch admission."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.conversation.observation_models import ContextObservationModel, ContextSelectionModel
from qq_ai_bot.conversation.projections import ProjectionConflict
from qq_ai_bot.domain.messages import ChatMessage
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.runtime.work_schema_v1 import work


def encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class ObservationSummaryError(ValueError):
    """An optional derived summary cannot establish source coverage."""


@dataclass(frozen=True, slots=True)
class ContextObservation:
    id: str
    version: int
    payload_json: str
    parent_sources: tuple[tuple[str, int], ...] = ()

    def message(self) -> ChatMessage:
        return ChatMessage(
            role="user",
            content=("[Observation; derived notes]\n" + self.payload_json),
        )


class ContextObservationRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def publish_note(
        self,
        work_record: dict[str, Any],
        note_version: int,
        payload: dict[str, Any],
        artifact_handles: tuple[str, ...] = (),
    ) -> str:
        """Pure idempotent publication after the note was saved by its Work.

        The Work and its original source own the actor and read contract. Model
        arguments never grant scope. The prepared note must still match storage.
        """
        if type(note_version) is not int or note_version < 1:
            raise ValueError("invalid_context_note_version")
        source = json.loads(work_record["source_json"])
        encoded = encode(payload)
        identity, intent = str(uuid4()), f"work-note:{work_record['id']}:{note_version}"
        now = datetime.now(UTC)
        # Decode the already saved checkpoint before reserving the writer.
        async with self.database.sessions() as prepared_session:
            prepared_checkpoint = await prepared_session.scalar(
                select(work.c.checkpoint_json).where(work.c.id == work_record["id"])
            )
        if prepared_checkpoint is None:
            raise ProjectionConflict("context observation work missing")
        note = json.loads(prepared_checkpoint).get("context_note", {})
        if (
            note.get("revision") != note_version
            or encode(note.get("payload")) != encoded
            or tuple(note.get("artifact_handles", ())) != artifact_handles
        ):
            raise ProjectionConflict("context observation note not saved")
        access = note.get("access", {})
        actor = access.get("actor_person_id")
        if access.get("principal_kind") == "self":
            actor = "self"
        read_scope = access.get("read_scope")
        if (
            not isinstance(actor, str)
            or not actor
            or not isinstance(read_scope, str)
            or not read_scope
        ):
            raise ProjectionConflict("context observation trusted scope missing")
        if source.get("actor_person_id") and source["actor_person_id"] != access.get(
            "actor_person_id"
        ):
            raise ProjectionConflict("context observation actor changed")
        if source.get("principal_kind", "person") != access.get("principal_kind"):
            raise ProjectionConflict("context observation principal changed")
        if (
            access.get("conversation_id") != work_record["conversation_id"]
            or access.get("generation") != work_record["generation"]
        ):
            raise ProjectionConflict("context observation access owner changed")
        async with self.database.sessions() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            stored = (
                (await session.execute(select(work).where(work.c.id == work_record["id"])))
                .mappings()
                .first()
            )
            conversation = await session.get(
                CanonicalConversationModel, work_record["conversation_id"]
            )
            if (
                stored is None
                or conversation is None
                or conversation.generation != work_record["generation"]
            ):
                raise ProjectionConflict("context observation owner changed")
            if stored["source_json"] != work_record["source_json"]:
                raise ProjectionConflict("context observation scope changed")
            if stored["checkpoint_json"] != prepared_checkpoint:
                raise ProjectionConflict("context observation note changed")
            from qq_ai_bot.execution_trace.db_models import ExecutionTraceStateModel

            privacy = int(
                await session.scalar(
                    select(ExecutionTraceStateModel.privacy_generation).where(
                        ExecutionTraceStateModel.id == 1
                    )
                )
                or 0
            )
            if (
                note.get("privacy_generation") != privacy
                or access.get("privacy_generation") != privacy
            ):
                raise ProjectionConflict("context observation privacy changed")
            old = await session.scalar(
                select(ContextObservationModel).where(ContextObservationModel.source_key == intent)
            )
            if old is not None:
                if (
                    old.payload_json != encoded
                    or old.actor_id != actor
                    or old.read_scope != read_scope
                ):
                    raise ProjectionConflict("context observation intent changed")
                return old.id
            # Ownership registration has no file IO. Unknown/deleting handles
            # reject publication without consuming another model/tool request.
            if artifact_handles:
                from qq_ai_bot.mcp.repository import ToolArtifactRepository

                await ToolArtifactRepository.add_refs(
                    session, "observation", identity, artifact_handles
                )
            session.add(
                ContextObservationModel(
                    id=identity,
                    conversation_id=conversation.id,
                    generation=conversation.generation,
                    actor_id=actor,
                    read_scope=read_scope,
                    source_key=intent,
                    source_work_id=stored["id"],
                    source_event_id=source.get("trigger_event_id"),
                    version=note_version,
                    privacy_generation=privacy,
                    payload_json=encoded,
                    created_at=now,
                )
            )
            await session.commit()
            return identity

    async def read(
        self,
        *,
        conversation_id: str,
        generation: int,
        actor_id: str,
        read_scope: str,
        view_key: str | None = None,
    ) -> tuple[ContextObservation, ...]:
        """Select only sources inside the caller's independently verified grant."""
        async with self.database.sessions() as session:
            privacy = await privacy_generation(session)
            rows = (
                await session.scalars(
                    select(ContextObservationModel)
                    .join(
                        CanonicalConversationModel,
                        ContextObservationModel.conversation_id == CanonicalConversationModel.id,
                    )
                    .where(
                        ContextObservationModel.conversation_id == conversation_id,
                        ContextObservationModel.generation == generation,
                        CanonicalConversationModel.generation == generation,
                        ContextObservationModel.actor_id == actor_id,
                        ContextObservationModel.read_scope == read_scope,
                        ContextObservationModel.privacy_generation == privacy,
                    )
                    .order_by(ContextObservationModel.created_at, ContextObservationModel.id)
                )
            ).all()
            observations = tuple(
                ContextObservation(
                    row.id,
                    row.version,
                    row.payload_json,
                    tuple(
                        (str(identity), int(version))
                        for identity, version in json.loads(row.parent_sources_json)
                    ),
                )
                for row in rows
                if row.summary_view_key is None or row.summary_view_key == view_key
            )
            selected_ids = set()
            if view_key is not None:
                selected_payloads = (
                    await session.scalars(
                        select(ContextSelectionModel.observation_sources_json).where(
                            ContextSelectionModel.view_key == view_key,
                            ContextSelectionModel.conversation_id == conversation_id,
                            ContextSelectionModel.generation == generation,
                            ContextSelectionModel.actor_id == actor_id,
                            ContextSelectionModel.read_scope == read_scope,
                        )
                    )
                ).all()
                selected_ids = {
                    identity for payload in selected_payloads for identity, _ in json.loads(payload)
                }
            unselected_snapshots = {
                row.id
                for row in rows
                if row.source_key.startswith("snapshot:") and row.id not in selected_ids
            }
            parents_by_id = {row.id: row.parent_sources for row in observations}
            pending = list(selected_ids)
            covered: set[str] = set()
            while pending:
                for identity, _ in parents_by_id.get(pending.pop(), ()):
                    if identity not in covered:
                        covered.add(identity)
                        pending.append(identity)
            return tuple(
                row
                for row in observations
                if row.id not in covered
                and row.id not in unselected_snapshots
                and (not row.parent_sources or row.id in selected_ids)
            )

    async def prepared_summary(
        self,
        *,
        view_key: str,
        observations: tuple[ContextObservation, ...],
        conversation_id: str,
        generation: int,
        actor_id: str,
        read_scope: str,
    ) -> ContextObservation | None:
        key = summary_key(view_key, observations)
        async with self.database.sessions() as session:
            row = await session.scalar(
                select(ContextObservationModel).where(
                    ContextObservationModel.source_key == key,
                    ContextObservationModel.conversation_id == conversation_id,
                    ContextObservationModel.generation == generation,
                    ContextObservationModel.actor_id == actor_id,
                    ContextObservationModel.read_scope == read_scope,
                )
            )
            if row is None:
                return None
            return ContextObservation(
                row.id,
                row.version,
                row.payload_json,
                tuple(
                    (identity, int(version))
                    for identity, version in json.loads(row.parent_sources_json)
                ),
            )

    async def publish_scope_summary(
        self,
        *,
        view_key: str,
        observations: tuple[ContextObservation, ...],
        payload: dict[str, Any],
        conversation_id: str,
        generation: int,
        actor_id: str,
        read_scope: str,
        expected_source_revision: int,
    ) -> ContextObservation:
        """Save a paid derived candidate; coverage starts only at selection CAS."""
        validate_summary(payload, observations)
        encoded = encode(payload)
        parents = tuple((row.id, row.version) for row in observations)
        parent_json = encode(parents)
        identity, intent, now = str(uuid4()), summary_key(view_key, observations), datetime.now(UTC)
        from qq_ai_bot.mcp.artifact_schema import artifact_refs
        from qq_ai_bot.mcp.repository import ToolArtifactRepository

        async with self.database.sessions() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            owner = await session.get(CanonicalConversationModel, conversation_id)
            privacy = await privacy_generation(session)
            if (
                owner is None
                or owner.generation != generation
                or owner.prompt_source_revision != expected_source_revision
            ):
                raise ProjectionConflict("context observation summary source changed")
            if not await validate_observations(
                session, conversation_id, generation, actor_id, read_scope, parents
            ):
                raise ProjectionConflict("context observation summary parents changed")
            old = await session.scalar(
                select(ContextObservationModel).where(ContextObservationModel.source_key == intent)
            )
            if old is not None:
                return ContextObservation(old.id, old.version, old.payload_json, parents)
            handles = tuple(
                (
                    await session.scalars(
                        select(artifact_refs.c.handle_id)
                        .where(
                            artifact_refs.c.owner_kind == "observation",
                            artifact_refs.c.owner_id.in_([row.id for row in observations]),
                        )
                        .distinct()
                    )
                ).all()
            )
            await ToolArtifactRepository.add_refs(session, "observation", identity, handles)
            session.add(
                ContextObservationModel(
                    id=identity,
                    conversation_id=conversation_id,
                    generation=generation,
                    actor_id=actor_id,
                    read_scope=read_scope,
                    source_key=intent,
                    version=1,
                    privacy_generation=privacy,
                    payload_json=encoded,
                    parent_sources_json=parent_json,
                    summary_view_key=view_key,
                    created_at=now,
                )
            )
            await session.commit()
        return ContextObservation(identity, 1, encoded, parents)

    async def validate_sources(
        self,
        conversation_id: str,
        generation: int,
        actor_id: str,
        read_scope: str,
        sources: tuple[tuple[str, int], ...],
    ) -> bool:
        async with self.database.sessions() as session:
            return await validate_observations(
                session, conversation_id, generation, actor_id, read_scope, sources
            )

    async def selected(
        self,
        *,
        view_key: str,
        conversation_id: str,
        generation: int,
        actor_id: str,
        read_scope: str,
    ) -> list[dict[str, Any]]:
        async with self.database.sessions() as session:
            payloads = (
                await session.scalars(
                    select(ContextSelectionModel.payload_json)
                    .where(
                        ContextSelectionModel.view_key == view_key,
                        ContextSelectionModel.conversation_id == conversation_id,
                        ContextSelectionModel.generation == generation,
                        ContextSelectionModel.actor_id == actor_id,
                        ContextSelectionModel.read_scope == read_scope,
                    )
                    .order_by(ContextSelectionModel.id)
                )
            ).all()
            return [json.loads(payload) for payload in payloads]


async def validate_observations(
    session: AsyncSession,
    conversation_id: str,
    generation: int,
    actor_id: str,
    read_scope: str,
    sources: tuple[tuple[str, int], ...],
) -> bool:
    if not sources:
        return True
    ids = tuple(identity for identity, _ in sources)
    if len(set(ids)) != len(ids) or not actor_id or not read_scope:
        return False
    pending = dict(sources)
    verified: dict[str, int] = {}
    privacy = await privacy_generation(session)
    while pending:
        rows = (
            await session.execute(
                select(
                    ContextObservationModel.id,
                    ContextObservationModel.version,
                    ContextObservationModel.parent_sources_json,
                ).where(
                    ContextObservationModel.id.in_(pending),
                    ContextObservationModel.conversation_id == conversation_id,
                    ContextObservationModel.generation == generation,
                    ContextObservationModel.actor_id == actor_id,
                    ContextObservationModel.read_scope == read_scope,
                    ContextObservationModel.privacy_generation == privacy,
                )
            )
        ).all()
        if {identity: version for identity, version, _ in rows} != pending:
            return False
        verified.update(pending)
        pending = {}
        for _, _, parent_json in rows:
            for identity, version in json.loads(parent_json):
                if identity in verified:
                    if verified[identity] != version:
                        return False
                elif identity in pending and pending[identity] != version:
                    return False
                else:
                    pending[identity] = version
    return True


async def privacy_generation(session: AsyncSession) -> int:
    from qq_ai_bot.execution_trace.db_models import ExecutionTraceStateModel

    return int(
        await session.scalar(
            select(ExecutionTraceStateModel.privacy_generation).where(
                ExecutionTraceStateModel.id == 1
            )
        )
        or 0
    )


def summary_key(view_key: str, observations: tuple[ContextObservation, ...]) -> str:
    return (
        "scope-summary:"
        + hashlib.sha256(
            encode([view_key, [(row.id, row.version) for row in observations]]).encode()
        ).hexdigest()
    )


def validate_summary(payload: dict[str, Any], observations: tuple[ContextObservation, ...]) -> None:
    allowed = {"observation:" + row.id for row in observations}
    if (
        set(payload) != {"version", "facts", "unresolved", "next_steps"}
        or type(payload["version"]) is not int
        or payload["version"] != 1
    ):
        raise ObservationSummaryError("invalid_observation_summary")
    found = False
    referenced: set[str] = set()
    for key in ("facts", "unresolved", "next_steps"):
        if not isinstance(payload[key], list):
            raise ObservationSummaryError("invalid_observation_summary")
        for fact in payload[key]:
            if (
                not isinstance(fact, dict)
                or set(fact) != {"text", "refs"}
                or not isinstance(fact["text"], str)
                or not fact["text"].strip()
                or not isinstance(fact["refs"], list)
                or not fact["refs"]
                or any(not isinstance(ref, str) or ref not in allowed for ref in fact["refs"])
            ):
                raise ObservationSummaryError("invalid_observation_summary_reference")
            found = True
            referenced.update(fact["refs"])
    if not found:
        raise ObservationSummaryError("empty_observation_summary")
    if referenced != allowed:
        raise ObservationSummaryError("incomplete_observation_summary_coverage")
