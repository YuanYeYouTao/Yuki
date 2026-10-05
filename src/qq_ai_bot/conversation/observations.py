"""Publish trusted clues once; freeze their selection at real dispatch admission."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, or_, select, text
from sqlalchemy.engine import Row, RowMapping
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

        async def checked_note(
            session: AsyncSession,
        ) -> tuple[RowMapping, CanonicalConversationModel, int, ContextObservationModel | None]:
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
            privacy = await privacy_generation(session)
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
            return stored, conversation, privacy, old

        async with self.database.sessions() as reader:
            await reader.execute(text("BEGIN"))
            _, _, _, old = await checked_note(reader)
            if old is not None:
                return old.id

        async with self.database.immediate_session() as session:
            stored, conversation, privacy, old = await checked_note(session)
            if old is not None:
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
            await session.execute(text("BEGIN"))
            privacy = await privacy_generation(session)
            conditions = (
                ContextObservationModel.conversation_id == conversation_id,
                ContextObservationModel.generation == generation,
                CanonicalConversationModel.generation == generation,
                ContextObservationModel.actor_id == actor_id,
                ContextObservationModel.read_scope == read_scope,
                ContextObservationModel.privacy_generation == privacy,
                or_(
                    ContextObservationModel.summary_view_key.is_(None),
                    ContextObservationModel.summary_view_key == view_key,
                ),
            )
            rows: list[Row[tuple[str, int, str, str, datetime]]] = []
            cursor: tuple[datetime, str] | None = None
            while True:
                query = (
                    select(
                        ContextObservationModel.id,
                        ContextObservationModel.version,
                        ContextObservationModel.source_key,
                        ContextObservationModel.parent_sources_json,
                        ContextObservationModel.created_at,
                    )
                    .join(
                        CanonicalConversationModel,
                        ContextObservationModel.conversation_id == CanonicalConversationModel.id,
                    )
                    .where(*conditions)
                    .order_by(ContextObservationModel.created_at, ContextObservationModel.id)
                    .limit(256)
                )
                if cursor is not None:
                    query = query.where(
                        or_(
                            ContextObservationModel.created_at > cursor[0],
                            and_(
                                ContextObservationModel.created_at == cursor[0],
                                ContextObservationModel.id > cursor[1],
                            ),
                        )
                    )
                page = (await session.execute(query)).all()
                if not page:
                    break
                rows.extend(page)
                cursor = page[-1].created_at, page[-1].id
            selected_versions: dict[str, set[int]] = {}
            if view_key is not None:
                after = 0
                while True:
                    selection_page = (
                        await session.execute(
                            select(
                                ContextSelectionModel.id,
                                ContextSelectionModel.observation_sources_json,
                            )
                            .where(
                                ContextSelectionModel.view_key == view_key,
                                ContextSelectionModel.conversation_id == conversation_id,
                                ContextSelectionModel.generation == generation,
                                ContextSelectionModel.actor_id == actor_id,
                                ContextSelectionModel.read_scope == read_scope,
                                ContextSelectionModel.id > after,
                            )
                            .order_by(ContextSelectionModel.id)
                            .limit(256)
                        )
                    ).all()
                    if not selection_page:
                        break
                    for row in selection_page:
                        for identity, version in json.loads(row.observation_sources_json):
                            selected_versions.setdefault(identity, set()).add(version)
                    after = selection_page[-1].id
            parents_by_id = {
                row.id: tuple(
                    (str(identity), int(version))
                    for identity, version in json.loads(row.parent_sources_json)
                )
                for row in rows
            }
            versions = {row.id: row.version for row in rows}
            valid: dict[str, bool] = {}
            selected_ids = {
                identity
                for identity, expected in selected_versions.items()
                if expected == {versions.get(identity)}
                and _valid_observation_root(identity, versions, parents_by_id, valid)
            }
            unselected_snapshots = {
                row.id
                for row in rows
                if row.source_key.startswith("snapshot:") and row.id not in selected_ids
            }
            pending = list(selected_ids)
            covered: set[str] = set()
            while pending:
                for identity, _ in parents_by_id.get(pending.pop(), ()):
                    if identity not in covered:
                        covered.add(identity)
                        pending.append(identity)
            needed = [
                row
                for row in rows
                if row.id not in covered
                and row.id not in unselected_snapshots
                and (not parents_by_id[row.id] or row.id in selected_ids)
            ]
            payloads: dict[str, str] = {}
            for offset in range(0, len(needed), 256):
                loaded = await session.execute(
                    select(ContextObservationModel.id, ContextObservationModel.payload_json).where(
                        ContextObservationModel.id.in_(
                            [row.id for row in needed[offset : offset + 256]]
                        )
                    )
                )
                payloads.update((identity, payload) for identity, payload in loaded.all())
            # Unselected work notes have no parents and remain valid new inputs.
            # Unselected snapshots/paid summary candidates do not establish coverage.
            return tuple(
                ContextObservation(row.id, row.version, payloads[row.id], parents_by_id[row.id])
                for row in needed
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

        async with self.database.sessions() as reader:
            await reader.execute(text("BEGIN"))
            owner = await reader.get(CanonicalConversationModel, conversation_id)
            privacy = await privacy_generation(reader)
            if (
                owner is None
                or owner.generation != generation
                or owner.prompt_source_revision != expected_source_revision
            ):
                raise ProjectionConflict("context observation summary source changed")
            if not await validate_observations(
                reader, conversation_id, generation, actor_id, read_scope, parents
            ):
                raise ProjectionConflict("context observation summary parents changed")
            old = await reader.scalar(
                select(ContextObservationModel).where(ContextObservationModel.source_key == intent)
            )
            if old is not None:
                return ContextObservation(old.id, old.version, old.payload_json, parents)
            handles_by_id: dict[str, None] = {}
            identities = tuple(row.id for row in observations)
            for start in range(0, len(identities), 256):
                handles_by_id.update(
                    (handle, None)
                    for handle in await reader.scalars(
                        select(artifact_refs.c.handle_id)
                        .where(
                            artifact_refs.c.owner_kind == "observation",
                            artifact_refs.c.owner_id.in_(identities[start : start + 256]),
                        )
                        .distinct()
                    )
                )
            handles = tuple(handles_by_id)

        # Observation edits/deletions advance the canonical source revision;
        # privacy deletion has its own scalar fence. Parent traversal/encoding
        # and reference collection above belong to one read snapshot, not writer.
        async with self.database.immediate_session() as session:
            owner = await session.get(CanonicalConversationModel, conversation_id)
            if (
                owner is None
                or owner.generation != generation
                or owner.prompt_source_revision != expected_source_revision
                or await privacy_generation(session) != privacy
            ):
                raise ProjectionConflict("context observation summary source changed")
            old = await session.scalar(
                select(ContextObservationModel).where(ContextObservationModel.source_key == intent)
            )
            if old is not None:
                return ContextObservation(old.id, old.version, old.payload_json, parents)
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
        visible_event_ids: frozenset[int] | None = None,
        allowed_observation_ids: frozenset[str] | None = None,
    ) -> list[dict[str, Any]]:
        async with self.database.sessions() as session:
            await session.execute(text("BEGIN"))
            needed = []
            after = 0
            while True:
                page = (
                    await session.execute(
                        select(
                            ContextSelectionModel.id,
                            ContextSelectionModel.event_ids_json,
                            ContextSelectionModel.observation_sources_json,
                        )
                        .where(
                            ContextSelectionModel.view_key == view_key,
                            ContextSelectionModel.conversation_id == conversation_id,
                            ContextSelectionModel.generation == generation,
                            ContextSelectionModel.actor_id == actor_id,
                            ContextSelectionModel.read_scope == read_scope,
                            ContextSelectionModel.id > after,
                        )
                        .order_by(ContextSelectionModel.id)
                        .limit(256)
                    )
                ).all()
                if not page:
                    break
                for row in page:
                    if (
                        visible_event_ids is not None
                        and not set(json.loads(row.event_ids_json)) <= visible_event_ids
                    ):
                        continue
                    if (
                        allowed_observation_ids is not None
                        and not {
                            identity for identity, _ in json.loads(row.observation_sources_json)
                        }
                        <= allowed_observation_ids
                    ):
                        continue
                    needed.append(row.id)
                after = page[-1].id
            payloads: dict[int, str] = {}
            for offset in range(0, len(needed), 256):
                loaded = await session.execute(
                    select(ContextSelectionModel.id, ContextSelectionModel.payload_json).where(
                        ContextSelectionModel.id.in_(needed[offset : offset + 256])
                    )
                )
                payloads.update((identity, payload) for identity, payload in loaded.all())
            return [json.loads(payloads[identity]) for identity in needed]


def _valid_observation_root(
    identity: str,
    versions: dict[str, int],
    parents: dict[str, tuple[tuple[str, int], ...]],
    valid: dict[str, bool],
) -> bool:
    """Coverage needs complete same-grant metadata, not just a selected ID.

    Iterative postorder avoids recursion limits; memoization visits shared
    ancestors once. Missing/changed parents and cycles grant no coverage.
    """
    pending = [(identity, False)]
    active = set()
    while pending:
        current, finish = pending.pop()
        if current in valid:
            continue
        if current not in versions:
            valid[current] = False
            continue
        if finish:
            active.discard(current)
            valid[current] = all(
                versions.get(parent) == version and valid.get(parent, False)
                for parent, version in parents[current]
            )
        elif current in active:
            valid[current] = False
        elif any(versions.get(parent) != version for parent, version in parents[current]):
            valid[current] = False
        else:
            active.add(current)
            pending.append((current, True))
            pending.extend((parent, False) for parent, _ in parents[current])
    return valid[identity]


async def validate_observations(
    session: AsyncSession,
    conversation_id: str,
    generation: int,
    actor_id: str,
    read_scope: str,
    sources: tuple[tuple[str, int], ...],
    *,
    snapshot_privacy_generation: int | None = None,
) -> bool:
    """Validate the DAG in this snapshot; supplied privacy must come from this transaction."""
    if not sources:
        return True
    ids = tuple(identity for identity, _ in sources)
    if len(set(ids)) != len(ids) or not actor_id or not read_scope:
        return False
    pending = dict(sources)
    verified: dict[str, int] = {}
    parents: dict[str, tuple[tuple[str, int], ...]] = {}
    privacy = (
        await privacy_generation(session)
        if snapshot_privacy_generation is None
        else snapshot_privacy_generation
    )
    while pending:
        rows: list[Row[tuple[str, int, str]]] = []
        identities = tuple(pending)
        for start in range(0, len(identities), 256):
            rows.extend(
                (
                    await session.execute(
                        select(
                            ContextObservationModel.id,
                            ContextObservationModel.version,
                            ContextObservationModel.parent_sources_json,
                        ).where(
                            ContextObservationModel.id.in_(identities[start : start + 256]),
                            ContextObservationModel.conversation_id == conversation_id,
                            ContextObservationModel.generation == generation,
                            ContextObservationModel.actor_id == actor_id,
                            ContextObservationModel.read_scope == read_scope,
                            ContextObservationModel.privacy_generation == privacy,
                        )
                    )
                ).all()
            )
        if {identity: version for identity, version, _ in rows} != pending:
            return False
        verified.update(pending)
        pending = {}
        for root, _, parent_json in rows:
            parents[root] = tuple(
                (identity, int(version)) for identity, version in json.loads(parent_json)
            )
            for identity, version in parents[root]:
                if identity in verified:
                    if verified[identity] != version:
                        return False
                elif identity in pending and pending[identity] != version:
                    return False
                else:
                    pending[identity] = version
    # Published summaries point to previously verified sources, so valid
    # factory output is a DAG. Corrupt cyclic metadata must not authorize a
    # projection which the observation reader would correctly reject.
    valid: dict[str, bool] = {}
    return all(_valid_observation_root(identity, verified, parents, valid) for identity in ids)


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
