"""Bridge real human admissions into the existing participation snapshot."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, cast

from sqlalchemy import DateTime, and_, bindparam, func, literal_column, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement
from yuki_participation.controller import ContinuationFacts, original_actor
from yuki_participation.models import CandidateKind, ScopedEvent, SourceRef
from yuki_participation.participation import (
    ParticipationUnit,
    UnitBinding,
    UnitParticipation,
)
from yuki_participation.self_report import SelfDelta, SelfReport

from yuki.conversation.autonomy_binding import InitiativeSource, InitiativeSourceKind
from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.conversation.initiative_sources import (
    EVENT_IDENTITY_REVISION_FIELDS,
    EVENT_TEXT_REVISION_FIELDS,
    MEMORY_REVISION_FIELDS,
    MEMORY_TIME_REVISION_FIELDS,
    event_mentions_expression,
    memory_revision,
    memory_revision_time_columns,
    source_revision,
)
from yuki.conversation.ordinary_admission import (
    OrdinaryAdmission,
    OrdinaryAdmissionConflict,
    OrdinaryParticipationBinding,
)
from yuki.domain.messages import ChatResponse
from yuki.identity.db_models import CanonicalPersonModel
from yuki.memory.repository import readable_evidence_predicate
from yuki.memory.self_origin import (
    self_execution_source_sql,
    self_initiative_fact_condition,
    self_initiative_fact_visibility,
)
from yuki.persistence.models import (
    ChatEventModel,
    MemoryEvidenceModel,
    MemoryFactModel,
    MemoryToolReceiptModel,
)
from yuki.persistence.repository_helpers import keeper_event_clause
from yuki.persistence.repository_records import EventRecord
from yuki.runtime.work_schema_v1 import work
from yuki.services.agent_tools import ToolRuntime
from yuki.services.participation_feedback import (
    admission_continuation,
    admission_unit_binding,
    sync_scope_effects,
)
from yuki.services.participation_identity import actor_identity
from yuki.time.formatting import stored_utc

if TYPE_CHECKING:
    from yuki.admin.models import RuntimeConfigSnapshot
    from yuki.services.semantic_participation import Scene, SemanticParticipationService, _Session

logger = logging.getLogger(__name__)


@asynccontextmanager
async def event_scope(
    service: SemanticParticipationService,
    event_id: int,
    *,
    record: EventRecord | None = None,
    runtime: RuntimeConfigSnapshot | None = None,
) -> AsyncIterator[tuple[EventRecord, _Session, ScopedEvent, Callable[[str], str]] | None]:
    # These immutable reads do not own a Controller. Another conversation can
    # prepare its input while the existing application lock is occupied.
    row = record if record is not None else await service.ledger.get_event(event_id)
    if row is None or not row.canonical_conversation_id:
        yield None
        return
    scene = await service._scene(row.canonical_conversation_id)
    if scene is None or not scene.enabled:
        yield None
        return
    if runtime is None:
        runtime = await service.runtime_config.snapshot(group_id=scene.space_id)
    if not runtime.conversation_policy().semantic_participation_enabled:
        yield None
        return
    async with service._lock:
        if service._store is None:
            yield None
            return
        # Preparation may have waited behind another application. Do not replace
        # a newer route/generation with the scene read before that wait.
        current = await service._scene(scene.conversation_id)
        if current is None or not current.enabled or current.generation != scene.generation:
            yield None
            return
        if current.space_id != scene.space_id:
            runtime = await service.runtime_config.snapshot(group_id=current.space_id)
        async with hydrated_scope(service, row, current, runtime) as found:
            yield found


@asynccontextmanager
async def hydrated_scope(
    service: SemanticParticipationService,
    row: EventRecord,
    scene: Scene,
    runtime: RuntimeConfigSnapshot,
) -> AsyncIterator[tuple[EventRecord, _Session, ScopedEvent, Callable[[str], str]] | None]:
    """Recover an owner under the existing application/session locks."""
    item = await service._session(scene)
    item.pins += 1
    try:
        await sync_scope_effects(service, item)
        if await service._hydrate(item, runtime=runtime) is None:
            yield None
            return
        await service._validate_boundaries(item)
        identity = await actor_identity(
            service, item, (row.author_person_id,) if row.author_person_id else ()
        )
        event = service._event(row, item, actor_identity=identity)
        if event is None or event.kind != "human":
            yield None
        else:
            item.controller.observe_committed_event(event)
            yield row, item, event, identity
    finally:
        item.pins -= 1


@asynccontextmanager
async def admitted_scope(
    service: SemanticParticipationService, conversation_id: str, generation: int, event_id: int
) -> AsyncIterator[
    tuple[OrdinaryAdmission, _Session, ScopedEvent, UnitBinding, Callable[[str], str] | None] | None
]:
    """Apply feedback to its real admitted owner; hydrate only on cold recovery."""
    async with service._lock:
        if service._store is None:
            yield None
            return
        sources = await service.ordinary.current_sources(conversation_id, generation, (event_id,))
        if not sources:
            yield None
            return
        admission, row = sources[0]
        item = service._sessions.get((conversation_id, generation))
        if item is None:
            scene = await service._scene(conversation_id)
            if scene is None or not scene.enabled or scene.generation != generation:
                yield None
                return
            runtime = await service.runtime_config.snapshot(group_id=scene.space_id)
            if not runtime.conversation_policy().semantic_participation_enabled:
                yield None
                return
            async with hydrated_scope(service, row, scene, runtime) as found:
                if found is None:
                    yield None
                    return
                unit = await observe_admitted_input(service, found[1], admission, found[2])
                yield (admission, found[1], found[2], unit, found[3]) if unit is not None else None
            return
        item.pins += 1
        try:
            event = service._event(row, item)
            if event is None:
                yield None
                return
            unit = await observe_admitted_input(service, item, admission, event)
            yield (admission, item, event, unit, None) if unit is not None else None
        finally:
            item.pins -= 1


async def observe_admitted_input(
    service: SemanticParticipationService,
    item: _Session,
    admission: OrdinaryAdmission,
    event: ScopedEvent,
) -> UnitBinding | None:
    unit = admission_unit_binding(admission)
    if unit is None or not all((await service._sources_current(item, unit.basis)).values()):
        return None
    # The admission may have committed while its derived feedback failed. Both
    # input feedback and the later hint recover that exact input idempotently.
    item.controller.observe_committed_event(event)
    item.controller.observe_unit_input(
        unit, event.ref, continuation=admission_continuation(admission)
    )
    return unit


def invitation_unit(
    item: _Session,
    event: ScopedEvent,
    *,
    actor_identity: Callable[[str], str] = original_actor,
    family: frozenset[str] | None = None,
) -> ParticipationUnit | None:
    candidate = item.controller.state.candidates.get(event.ref.event_id)
    observation = item.controller.state.observations.get(event.ref.event_id)
    if (
        candidate is None
        or observation is None
        or candidate.kind != CandidateKind.CONVERSATION
        or candidate.event.ref != event.ref
        or candidate.support.kind != "observed"
        or candidate.support.strength(time.time()) <= 0
        or not (
            item.controller.invitation_current(event, time.time(), actor_identity=actor_identity)
            if family is None
            else item.controller.invitation_current_for_family(
                event, time.time(), family=family, actor_identity=actor_identity
            )
        )
    ):
        return None
    interaction = observation.answers.get("interaction_mark")
    floor = observation.answers.get("floor_state")
    if (
        interaction is None
        or interaction.choice not in {"invite_yuki", "extend_yuki"}
        or floor is None
        or floor.choice != "yuki"
    ):
        return None
    return ParticipationUnit(thread=candidate.event.thread, target=candidate.event.target)


def host_binding(
    row: EventRecord,
    item: _Session,
    unit: ParticipationUnit,
    basis: tuple[SourceRef, ...],
) -> OrdinaryParticipationBinding:
    return OrdinaryParticipationBinding(
        conversation_id=item.scene.conversation_id,
        generation=item.scene.generation,
        event_id=row.id,
        source_revision=str(source_revision(row)),
        unit_key=unit.thread,
        target_hint=row.author_person_id or unit.target,
        basis=tuple((ref.event_id, ref.revision) for ref in dict.fromkeys(basis)),
    )


def invitation_basis(item: _Session, event: ScopedEvent) -> tuple[SourceRef, ...]:
    observation = item.controller.state.observations.get(event.ref.event_id)
    if observation is None:
        return (event.ref,)
    return tuple(dict.fromkeys((event.ref, *observation.snapshot.context)))


ImplicitReason = Literal["semantic_invitation", "continuation"]


def _continuation_basis(
    item: _Session, matched: UnitParticipation, event: ScopedEvent
) -> tuple[SourceRef, ...]:
    # A logical current unit may represent another original Person. A new input
    # still belongs to its own original actor's existing unit and source proof.
    own = item.controller.participation_own_basis(matched.unit.thread, event.author, time.time())
    return tuple(dict.fromkeys((*matched.binding.basis, *own, event.ref)))


async def binding_for_event(
    service: SemanticParticipationService,
    event_id: int,
    *,
    record: EventRecord | None = None,
    runtime: RuntimeConfigSnapshot | None = None,
) -> OrdinaryParticipationBinding | None:
    async with event_scope(service, event_id, record=record, runtime=runtime) as found:
        if found is None:
            return None
        row, item, event, identity = found
        view = item.controller.participation_view(event, time.time(), actor_identity=identity)
        if not view.source_valid:
            return None
        matched = view.matched_unit
        if matched is not None:
            basis = _continuation_basis(item, matched, event)
            if all((await service._sources_current(item, basis)).values()):
                return host_binding(row, item, matched.unit, basis)
            return None
        invited = invitation_unit(item, event, actor_identity=identity)
        if invited is not None:
            basis = invitation_basis(item, event)
            if all((await service._sources_current(item, basis)).values()):
                return host_binding(row, item, invited, basis)
            return None
        # A direct real input can establish its own unit. This is Host admission,
        # never an observed semantic interpretation of an unscored focus.
        if not await service._source_current(item, event.ref):
            return None
        return host_binding(
            row, item, ParticipationUnit(thread=event.thread, target=event.author), (event.ref,)
        )


async def continuation_for_event(
    service: SemanticParticipationService,
    event_id: int,
    *,
    source_basis: tuple[SourceRef, ...] = (),
    invalid_refs: tuple[SourceRef, ...] = (),
) -> tuple[OrdinaryParticipationBinding, ImplicitReason] | None:
    """Classify an implicit source: a verified invitation first, then a matched unit."""
    async with event_scope(service, event_id) as found:
        if found is None:
            return None
        row, item, event, identity = found
        if invalid_refs:
            await service._sources_current(item, invalid_refs)
        if source_basis and not all((await service._sources_current(item, source_basis)).values()):
            return None
        view = item.controller.participation_view(event, time.time(), actor_identity=identity)
        if not view.source_valid:
            return None
        invited = invitation_unit(item, event, actor_identity=identity)
        if invited is not None:
            basis = invitation_basis(item, event)
            if all((await service._sources_current(item, basis)).values()):
                return host_binding(row, item, invited, basis), "semantic_invitation"
        matched = view.matched_unit
        if matched is not None:
            basis = _continuation_basis(item, matched, event)
            if all((await service._sources_current(item, basis)).values()):
                return host_binding(row, item, matched.unit, basis), "continuation"
        return None


@dataclass(frozen=True)
class PreparedContinuation:
    root: str | None
    family: frozenset[str]
    facts: ContinuationFacts | None
    unit: UnitBinding | None
    source_digests: tuple[tuple[SourceRef, str], ...]
    space_id: str
    event_checks: tuple[tuple[int, ColumnElement[bool]], ...]
    memory_checks: tuple[tuple[int, ColumnElement[bool]], ...]
    memory_windows: tuple[tuple[datetime | None, datetime | None], ...]
    memory_condition: ColumnElement[bool] | None
    automation_ids_json: str
    evidence_ids_json: str
    sources_valid: bool
    invalid_refs: tuple[SourceRef, ...] = ()


def _invalid_continuation(space_id: str) -> PreparedContinuation:
    return PreparedContinuation(
        None, frozenset(), None, None, (), space_id, (), (), (), None, "[]", "[]", False
    )


async def _current_family(session: AsyncSession, actor: str) -> tuple[str, frozenset[str]]:
    original = aliased(CanonicalPersonModel)
    root_value = func.coalesce(
        select(func.coalesce(original.merged_into_person_id, original.id))
        .where(original.id == actor)
        .scalar_subquery(),
        actor,
    )
    roots: dict[str, str] = dict(
        (
            await session.execute(
                select(
                    CanonicalPersonModel.id,
                    func.coalesce(
                        CanonicalPersonModel.merged_into_person_id, CanonicalPersonModel.id
                    ),
                ).where(
                    or_(
                        CanonicalPersonModel.id == root_value,
                        CanonicalPersonModel.merged_into_person_id == root_value,
                    )
                )
            )
        )
        .tuples()
        .all()
    )
    root = roots.get(actor, actor)
    members = set(roots)
    members.update((actor, root))
    return root, frozenset(members)


def _automation_run_id(source: object) -> int | None:
    if not isinstance(source, str) or not source.startswith("automation:"):
        return None
    value, separator, _ = source.removeprefix("automation:").partition(":")
    if not separator:
        return None
    try:
        identity = int(value)
    except ValueError:
        return None
    if str(identity) != value or not -(2**63) <= identity < 2**63:
        return None
    return identity


async def _automation_source_ids(session: AsyncSession, evidence_ids: tuple[int, ...]) -> str:
    """Prepare original automation run keys from this batch's own evidence receipts."""

    sources: set[str] = set()
    source_expression: ColumnElement[str | None] = literal_column(
        self_execution_source_sql("memory_tool_receipts.execution_id")
    )
    for start in range(0, len(evidence_ids), 32):
        values = await session.scalars(
            select(source_expression)
            .select_from(MemoryEvidenceModel)
            .join(
                MemoryToolReceiptModel,
                MemoryToolReceiptModel.id == MemoryEvidenceModel.tool_receipt_id,
            )
            .where(
                MemoryEvidenceModel.id.in_(evidence_ids[start : start + 32]),
                MemoryEvidenceModel.event_id.is_(None),
                MemoryToolReceiptModel.trigger_event_id.is_(None),
                MemoryToolReceiptModel.initiative_run_id.is_(None),
            )
        )
        sources.update(value for value in values if isinstance(value, str))
    parents: set[str] = set()
    source_ids = tuple(sources)
    for start in range(0, len(source_ids), 128):
        values = await session.scalars(
            select(
                func.coalesce(
                    func.json_extract(work.c.source_json, "$.execution_source.parent_execution_id"),
                    func.json_extract(work.c.source_json, "$.parent_execution_id"),
                )
            ).where(work.c.id.in_(source_ids[start : start + 128]))
        )
        parents.update(value for value in values if isinstance(value, str))
    identities = {
        identity
        for source in (*sources, *parents)
        if (identity := _automation_run_id(source)) is not None
    }
    return json.dumps(sorted(identities), separators=(",", ":"))


async def prepare_continuation(
    service: SemanticParticipationService,
    binding: OrdinaryParticipationBinding,
) -> PreparedContinuation | None:
    """Prepare this family's current facts and sources outside the writer."""
    prepared, invalid = await _prepare_continuation(service, binding)
    if prepared is None or not invalid:
        return prepared
    return replace(prepared, sources_valid=False, invalid_refs=invalid)


async def _prepare_continuation(
    service: SemanticParticipationService,
    binding: OrdinaryParticipationBinding,
) -> tuple[PreparedContinuation | None, tuple[SourceRef, ...]]:
    item = service._sessions.get((binding.conversation_id, binding.generation))
    if item is None:
        return None, ()
    space_id = item.scene.space_id
    event = item.controller.state.events.get(f"event:{binding.event_id}")
    if event is None:
        return _invalid_continuation(space_id), ()
    try:
        basis = tuple(SourceRef(event_id=key, revision=revision) for key, revision in binding.basis)
        unit = UnitBinding(
            scope=event.scope,
            unit=ParticipationUnit(thread=binding.unit_key, target=binding.target_hint),
            actor=event.author,
            basis=basis,
        )
    except ValueError:
        return _invalid_continuation(space_id), ()
    basis_set = frozenset(basis)
    event_checks: list[tuple[int, ColumnElement[bool]]] = []
    memory_checks: list[tuple[int, ColumnElement[bool]]] = []
    memory_windows: list[tuple[datetime | None, datetime | None]] = []
    valid = True
    invalid: set[SourceRef] = set()
    async with service.database.read_session() as reader:
        await reader.execute(text("BEGIN"))
        conversation = await reader.get(CanonicalConversationModel, binding.conversation_id)
        if (
            conversation is None
            or conversation.generation != binding.generation
            or conversation.kind != "space"
            or conversation.person_id is not None
            or conversation.space_id != space_id
        ):
            return _invalid_continuation(space_id), ()
        root, family = await _current_family(reader, event.author)
        current = service._sessions.get((binding.conversation_id, binding.generation))
        if current is None:
            return None, ()
        current_event = current.controller.state.events.get(event.ref.event_id)
        if (
            current.scene.space_id != space_id
            or current_event is None
            or current_event.ref != event.ref
            or current_event.scope != event.scope
            or current_event.author != event.author
        ):
            return _invalid_continuation(space_id), ()
        item = current
        moment = time.time()
        facts = item.controller.continuation_family_facts(
            event.ref, moment, family=family, binding=unit
        )
        refs = basis_set | frozenset(
            ref for ref, _event_ref, _seen, _invalidated, usable in facts.sources if usable
        )
        sources: list[tuple[SourceRef, InitiativeSource]] = []
        for ref in sorted(refs, key=lambda value: (value.event_id, value.revision)):
            try:
                sources.append((ref, service._source(item, ref)))
            except ValueError:
                if ref in basis_set:
                    valid = False
                else:
                    invalid.add(ref)
        event_ids = tuple(
            int(source.source_id)
            for _, source in sources
            if source.kind is InitiativeSourceKind.EVENT
        )
        memory_ids = tuple(
            int(source.source_id)
            for _, source in sources
            if source.kind is InitiativeSourceKind.MEMORY
        )
        memory_condition = (
            self_initiative_fact_condition(
                space_id,
                bindparam("participation_basis_now", type_=DateTime(timezone=True)),
                prepared_automation_ids=True,
                prepared_evidence_ids=True,
            )
            if memory_ids
            else None
        )
        events: dict[int, ChatEventModel] = {}
        mentions: dict[int, str] = {}
        mention_values = event_mentions_expression()
        for start in range(0, len(event_ids), 128):
            page = event_ids[start : start + 128]
            rows = await reader.execute(
                select(ChatEventModel, mention_values).where(
                    ChatEventModel.id.in_(page),
                    ChatEventModel.canonical_conversation_id == binding.conversation_id,
                    ChatEventModel.id > conversation.starts_after_event_id,
                    keeper_event_clause(),
                )
            )
            for row, mention in rows:
                events[row.id] = row
                mentions[row.id] = mention
        memories: dict[int, MemoryFactModel] = {}
        evidence_ids: dict[int, int] = {}
        memory_raw_windows: dict[int, tuple[str | None, str | None]] = {}
        raw_valid_from, raw_valid_until = memory_revision_time_columns()
        if memory_ids:
            evidence = aliased(MemoryEvidenceModel, name="continuation_evidence")
            witness = (
                select(evidence.id)
                .where(
                    evidence.fact_id == MemoryFactModel.id,
                    readable_evidence_predicate(
                        evidence=evidence,
                        evidence_table=evidence,
                        evidence_alias="continuation_evidence",
                    ),
                )
                .order_by(evidence.id)
                .limit(1)
                .correlate(MemoryFactModel)
                .scalar_subquery()
            )
            for start in range(0, len(memory_ids), 32):
                page = memory_ids[start : start + 32]
                rows = await reader.execute(
                    select(MemoryFactModel, MemoryEvidenceModel.id, raw_valid_from, raw_valid_until)
                    .join(MemoryEvidenceModel, MemoryEvidenceModel.id == witness)
                    .where(
                        MemoryFactModel.id.in_(page),
                        self_initiative_fact_visibility(
                            space_id,
                            bindparam("participation_basis_now", type_=DateTime(timezone=True)),
                        ),
                    ),
                    {"participation_basis_now": datetime.now(UTC)},
                )
                for row, evidence_id, valid_from, valid_until in rows:
                    memories[row.id] = row
                    evidence_ids[row.id] = evidence_id
                    memory_raw_windows[row.id] = (valid_from, valid_until)
        automation_ids_json = await _automation_source_ids(reader, tuple(evidence_ids.values()))

        def utc(value: datetime | None) -> datetime | None:
            return None if value is None else stored_utc(value)

        for ref, source in sources:
            identity = int(source.source_id)
            if source.kind is InitiativeSourceKind.EVENT:
                row = events.get(identity)
                if row is None or str(source_revision(row)) != source.revision:
                    if ref in basis_set:
                        valid = False
                    else:
                        invalid.add(ref)
                    continue
                event_checks.append(
                    (
                        identity,
                        and_(
                            ChatEventModel.id == identity,
                            mention_values.is_(mentions[identity]),
                            *(
                                func.coalesce(getattr(ChatEventModel, name), "")
                                == (getattr(row, name) or "")
                                for name in EVENT_TEXT_REVISION_FIELDS
                            ),
                            *(
                                getattr(ChatEventModel, name).is_(getattr(row, name))
                                for name in EVENT_IDENTITY_REVISION_FIELDS
                            ),
                        ),
                    )
                )
            else:
                memory_row = memories.get(identity)
                if memory_row is None or memory_revision(memory_row) != source.revision:
                    if ref in basis_set:
                        valid = False
                    else:
                        invalid.add(ref)
                    continue
                memory_windows.append((utc(memory_row.valid_from), utc(memory_row.valid_until)))
                memory_checks.append(
                    (
                        identity,
                        and_(
                            MemoryFactModel.id == identity,
                            *(
                                getattr(MemoryFactModel, name).is_(getattr(memory_row, name))
                                for name in MEMORY_REVISION_FIELDS
                                if name not in MEMORY_TIME_REVISION_FIELDS
                            ),
                            raw_valid_from.is_(memory_raw_windows[identity][0]),
                            raw_valid_until.is_(memory_raw_windows[identity][1]),
                        ),
                    )
                )
    return PreparedContinuation(
        root,
        family,
        facts,
        unit,
        tuple((ref, source.revision) for ref, source in sources),
        space_id,
        tuple(event_checks),
        tuple(memory_checks),
        tuple(memory_windows),
        memory_condition,
        automation_ids_json,
        json.dumps(sorted(evidence_ids.values()), separators=(",", ":")),
        valid,
    ), tuple(sorted(invalid, key=lambda value: (value.event_id, value.revision)))


async def _continuation_sources_current(
    binding: OrdinaryParticipationBinding,
    prepared: PreparedContinuation,
    session: AsyncSession,
) -> bool:
    conversation = await session.get(CanonicalConversationModel, binding.conversation_id)
    if (
        conversation is None
        or conversation.generation != binding.generation
        or conversation.kind != "space"
        or conversation.person_id is not None
        or conversation.space_id != prepared.space_id
    ):
        return False
    for start in range(0, len(prepared.event_checks), 128):
        page = prepared.event_checks[start : start + 128]
        actual = set(
            await session.scalars(
                select(ChatEventModel.id).where(
                    ChatEventModel.id > conversation.starts_after_event_id,
                    or_(*(condition for _, condition in page)),
                )
            )
        )
        if actual != {identity for identity, _ in page}:
            return False
    for start in range(0, len(prepared.memory_checks), 32):
        page = prepared.memory_checks[start : start + 32]
        actual = set(
            await session.scalars(
                select(MemoryFactModel.id).where(
                    cast(ColumnElement[bool], prepared.memory_condition),
                    or_(*(condition for _, condition in page)),
                ),
                {
                    "participation_basis_now": datetime.now(UTC),
                    "participation_automation_ids": prepared.automation_ids_json,
                    "participation_evidence_ids": prepared.evidence_ids_json,
                },
            )
        )
        if actual != {identity for identity, _ in page}:
            return False
    now = datetime.now(UTC)
    if any(
        (valid_from is not None and valid_from > now)
        or (valid_until is not None and valid_until <= now)
        for valid_from, valid_until in prepared.memory_windows
    ):
        return False
    return True


async def continuation_matches(
    service: SemanticParticipationService,
    binding: OrdinaryParticipationBinding,
    prepared: PreparedContinuation | None,
    *,
    session: AsyncSession,
) -> bool:
    """Recheck this family's actual facts at the existing selection boundary."""
    if prepared is None:
        if (binding.conversation_id, binding.generation) in service._sessions:
            raise OrdinaryAdmissionConflict("ordinary_participation_preparation_changed")
        return False
    if not prepared.sources_valid:
        raise OrdinaryAdmissionConflict("ordinary_participation_preparation_changed")
    unit = cast(UnitBinding, prepared.unit)
    root, family = await _current_family(session, unit.actor)
    if root != prepared.root or family != prepared.family:
        raise OrdinaryAdmissionConflict("ordinary_participation_preparation_changed")
    if not await _continuation_sources_current(binding, prepared, session):
        raise OrdinaryAdmissionConflict("ordinary_participation_preparation_changed")
    item = service._sessions.get((binding.conversation_id, binding.generation))
    if item is None:
        return False

    event = item.controller.state.events.get(f"event:{binding.event_id}")
    versions = cast(
        dict[str, Any], item.controller.state.host_checkpoint.get("source_versions", {})
    )
    if event is None or versions.get(event.ref.event_id) != [
        event.ref.revision,
        binding.source_revision,
    ]:
        return False
    if any(
        versions.get(ref.event_id) != [ref.revision, digest]
        for ref, digest in prepared.source_digests
    ):
        raise OrdinaryAdmissionConflict("ordinary_participation_preparation_changed")
    now = time.time()
    if (
        item.controller.continuation_family_facts(event.ref, now, family=family, binding=unit)
        != prepared.facts
    ):
        raise OrdinaryAdmissionConflict("ordinary_participation_preparation_changed")

    def identity(actor: str) -> str:
        return root if actor in family else actor

    invited = invitation_unit(item, event, actor_identity=identity, family=family)
    if (
        invited is not None
        and invited.thread == binding.unit_key
        and identity(invited.target) == identity(binding.target_hint)
    ):
        return True
    return item.controller.continuation_current_for_family(
        event.ref,
        unit,
        now,
        family=family,
        actor_identity=identity,
    )


async def continuation_current(
    service: SemanticParticipationService, binding: OrdinaryParticipationBinding
) -> bool:
    """Read eligibility without borrowing a writer for controller preparation."""
    try:
        basis = tuple(SourceRef(event_id=key, revision=revision) for key, revision in binding.basis)
    except ValueError:
        return False
    while True:
        prepared = await prepare_continuation(service, binding)
        async with service.database.read_session() as reader:
            await reader.execute(text("BEGIN"))
            try:
                return await continuation_matches(service, binding, prepared, session=reader)
            except OrdinaryAdmissionConflict as exc:
                if str(exc) != "ordinary_participation_preparation_changed":
                    raise
        selected = await continuation_for_event(
            service,
            binding.event_id,
            source_basis=basis,
            invalid_refs=prepared.invalid_refs if prepared is not None else (),
        )
        if selected is None:
            return False


async def on_ordinary_admitted(
    service: SemanticParticipationService,
    binding: OrdinaryParticipationBinding | None,
    admission: OrdinaryAdmission,
) -> None:
    if binding is None:
        return
    async with admitted_scope(
        service, binding.conversation_id, binding.generation, admission.event_id
    ) as found:
        if found is None:
            return
        _, item, _, _, _ = found
        await service._save(item)


async def runtime_context(
    service: SemanticParticipationService, runtime: ToolRuntime
) -> dict[str, Any] | None:
    """Read applied facts for the original admission, without hydration or an observer lock."""
    conversation = runtime.effective_conversation_id
    event_id = runtime.effective_trigger_event_id
    if conversation is None or event_id is None:
        return None
    snapshot = runtime.turn_snapshot
    if snapshot is not None and (conversation, snapshot.generation) not in service._sessions:
        return None
    if (
        runtime.runtime_config is not None
        and not runtime.runtime_config.conversation_policy().semantic_participation_enabled
    ):
        return None
    admission = await service.ordinary.get(
        event_id,
        conversation_id=conversation,
        generation=snapshot.generation if snapshot is not None else None,
    )
    if admission is None or admission.binding is None:
        return None
    item = service._sessions.get((admission.conversation_id, admission.generation))
    if item is None:
        return None
    identity = await actor_identity(service, item, (admission.actor_person_id,))
    binding = admission.binding
    reports: list[dict[str, Any]] = []
    for report in item.controller.participation_context_reports(
        binding.unit_key, binding.target_hint, actor_identity=identity
    ):
        facts: dict[str, Any] = {"actor_person_id": report.actor}
        if (hint := report.hint) is not None:
            facts["self_intent"] = {
                "engage": hint.engage,
                "source": hint.source.model_dump(mode="json") if hint.source else None,
                "basis": [ref.model_dump(mode="json") for ref in hint.basis],
                "source_at": hint.at,
                "sequence": hint.sequence,
            }
        if (review := report.review) is not None:
            facts["semantic_review"] = {
                "interaction": review.interaction,
                "floor": review.floor,
                "source": review.source.model_dump(mode="json"),
                "source_at": review.at,
                "source_order": review.source_order,
                "sequence": review.sequence,
            }
            if review.boundary_scope is not None:
                facts["semantic_review"]["boundary_scope"] = review.boundary_scope
        reports.append(facts)
    boundaries = []
    for boundary in item.controller.state.boundaries.values():
        if (
            boundary.released_by is not None
            or boundary.thread != binding.unit_key
            or (
                not boundary.group_wide
                and identity(boundary.target) != identity(binding.target_hint)
            )
        ):
            continue
        stop: dict[str, Any] = {
            "source": boundary.source.model_dump(mode="json"),
            "actor_person_id": boundary.author,
            "scope": "group_thread" if boundary.group_wide else "target_thread",
            "interaction": "ask_yuki_stop" if boundary.explicit_stop else "close_topic",
        }
        source = item.controller.state.seen.get(boundary.source.event_id)
        if source is not None and source.revision == boundary.source.revision:
            stop.update(source_at=source.at, source_order=source.source_order)
        boundaries.append(stop)
    if not reports and not boundaries:
        return None
    context: dict[str, Any] = {
        "thread": binding.unit_key,
        "target_person_id": binding.target_hint,
    }
    if reports:
        context["reports"] = reports
    if boundaries:
        context["boundaries"] = boundaries
    return context


async def observe_main_response(
    service: SemanticParticipationService,
    runtime: ToolRuntime,
    request_sequence: int,
    response: ChatResponse,
    delta: SelfDelta | None,
) -> None:
    snapshot = runtime.turn_snapshot
    event_id = runtime.effective_trigger_event_id
    if delta is None or snapshot is None or event_id is None:
        return
    if not await service.validate_turn_snapshot(snapshot):
        return
    async with admitted_scope(
        service, snapshot.conversation_id, snapshot.generation, event_id
    ) as found:
        if found is None:
            return
        admission, item, event, unit, identity = found
        if identity is None:
            identity = await actor_identity(service, item, (event.author,))
        # Dispatch ordering is source time, not the arrival time of a late response.
        report = SelfReport(
            run_ref=admission.activation_id,
            sequence=request_sequence,
            response_id=response.provider_request_id
            or f"{admission.activation_id}:{request_sequence}",
            at=event.at,
            source=event.ref,
            delta=delta,
        )
        if delta.engage == "quiet" and runtime.visible_event_ids:
            continuations = item.controller.participation_continuation_sources(
                unit.unit.thread,
                admission.actor_person_id,
                unit.unit.target,
                (f"event:{event_id}" for event_id in runtime.visible_event_ids),
                actor_identity=identity,
            )
            refs = {
                event_id: continuations[f"event:{event_id}"]
                for event_id in runtime.visible_event_ids
                if f"event:{event_id}" in continuations
            }
            observed = await service.ordinary.current_admissions(
                admission.conversation_id, admission.generation, tuple(refs)
            )
            covered = tuple(
                refs[source.event_id]
                for source in observed
                if source.activation_id == admission.activation_id
                and identity(source.actor_person_id) == identity(admission.actor_person_id)
                and source.binding is not None
                and source.binding.unit_key == unit.unit.thread
                and identity(source.binding.target_hint) == identity(unit.unit.target)
            )
            unit = unit.model_copy(update={"basis": tuple(dict.fromkeys((*unit.basis, *covered)))})
        if item.controller.observe_unit_hint(unit, report, actor_identity=identity):
            await service._save(item)


async def promote_invitations(service: SemanticParticipationService, item: _Session) -> None:
    callback = service._promote
    conversation = item.scene.conversation_id
    if conversation in service._promotions:
        return
    identity = await actor_identity(
        service,
        item,
        (candidate.event.author for candidate in item.controller.state.candidates.values()),
    )
    for candidate in tuple(item.controller.state.candidates.values()):
        event = candidate.event
        unit = invitation_unit(item, event, actor_identity=identity)
        if unit is None or not await service._source_current(item, event.ref):
            continue
        key, _, event_identity = event.ref.event_id.partition(":")
        if key != "event" or not event_identity.isdecimal():
            continue
        if (
            await service.ordinary.get(int(event_identity), generation=item.scene.generation)
            is not None
        ):
            item.controller.set_state_entry("consumed", event.ref.event_id, event.ref.revision)
            item.controller.remove_state_entry("candidates", event.ref.event_id)
            continue
        row = await service.ledger.get_event(int(event_identity))
        if row is None:
            continue
        basis = invitation_basis(item, event)
        if not all((await service._sources_current(item, basis)).values()):
            continue
        binding = host_binding(row, item, unit, basis)

        async def run(event_id: int, frozen: OrdinaryParticipationBinding) -> None:
            try:
                await callback(event_id, frozen)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("participation_promotion_failed category=%s", type(exc).__name__)
            finally:
                service._promotions.pop(frozen.conversation_id, None)

        service._promotions[conversation] = asyncio.create_task(
            run(row.id, binding), name="participation-human-admission"
        )
        return
