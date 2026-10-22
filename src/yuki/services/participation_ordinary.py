"""Bridge real human admissions into the existing participation snapshot."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Literal, cast

from sqlalchemy.ext.asyncio import AsyncSession
from yuki_participation.controller import original_actor
from yuki_participation.models import CandidateKind, ScopedEvent, SourceRef
from yuki_participation.participation import (
    ParticipationCheckpoint,
    ParticipationUnit,
    UnitBinding,
    UnitParticipation,
)
from yuki_participation.self_report import SelfDelta, SelfReport

from yuki.conversation.initiative_sources import source_revision
from yuki.conversation.ordinary_admission import (
    OrdinaryAdmission,
    OrdinaryParticipationBinding,
)
from yuki.domain.messages import ChatResponse
from yuki.persistence.repository_records import EventRecord
from yuki.services.agent_tools import ToolRuntime
from yuki.services.participation_feedback import (
    admission_continuation,
    admission_unit_binding,
    sync_scope_effects,
)
from yuki.services.participation_identity import actor_identity

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
    row = record if record is not None else await service.app.ledger.get_event(event_id)
    if row is None or not row.canonical_conversation_id:
        yield None
        return
    scene = await service._scene(row.canonical_conversation_id)
    if scene is None or not scene.enabled:
        yield None
        return
    if runtime is None:
        runtime = await service.app.runtime_config.snapshot(group_id=scene.space_id)
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
            runtime = await service.app.runtime_config.snapshot(group_id=current.space_id)
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
            runtime = await service.app.runtime_config.snapshot(group_id=scene.space_id)
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
        or not item.controller.participation_view(
            event, time.time(), actor_identity=actor_identity
        ).addressed
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
    own = next(
        (
            unit.binding.basis
            for unit in item.controller.participating_units(time.time())
            if unit.unit.thread == matched.unit.thread
            and unit.unit.target == event.author
            and unit.binding.actor == event.author
        ),
        (),
    )
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
    service: SemanticParticipationService, event_id: int
) -> tuple[OrdinaryParticipationBinding, ImplicitReason] | None:
    """Classify an implicit source: a verified invitation first, then a matched unit."""
    async with event_scope(service, event_id) as found:
        if found is None:
            return None
        row, item, event, identity = found
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


async def continuation_current(
    service: SemanticParticipationService,
    binding: OrdinaryParticipationBinding,
    *,
    session: AsyncSession | None = None,
) -> bool:
    """Read the current derived eligibility at the original admission boundary."""
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
    identity = await actor_identity(service, item, (event.author,), session=session)
    invited = invitation_unit(item, event, actor_identity=identity)
    if (
        invited is not None
        and invited.thread == binding.unit_key
        and identity(invited.target) == identity(binding.target_hint)
    ):
        return True
    return item.controller.continuation_current(
        event.ref,
        UnitBinding(
            scope=event.scope,
            unit=ParticipationUnit(thread=binding.unit_key, target=binding.target_hint),
            actor=event.author,
            basis=tuple(
                SourceRef(event_id=key, revision=revision) for key, revision in binding.basis
            ),
        ),
        time.time(),
        actor_identity=identity,
    )


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
    checkpoint = ParticipationCheckpoint.model_validate(
        item.controller.state.host_checkpoint.get("participation_v1", {})
    )
    reports: list[dict[str, Any]] = []
    for unit in checkpoint.units.values():
        if (
            unit.binding.unit.thread != binding.unit_key
            or identity(unit.binding.unit.target) != identity(binding.target_hint)
            or not item.controller._binding_valid(unit.binding, checkpoint)
            or not item.controller._unit_interpretation_valid(unit)
        ):
            continue
        facts: dict[str, Any] = {"actor_person_id": unit.binding.actor}
        hint = unit.hint
        if hint is not None and all(
            item.controller._unit_ref_valid(ref, unit) for ref in unit.hint_basis
        ):
            facts["self_intent"] = {
                "engage": hint.delta.engage,
                "source": hint.source.model_dump(mode="json") if hint.source else None,
                "basis": [ref.model_dump(mode="json") for ref in unit.hint_basis],
                "source_at": hint.at,
                "sequence": hint.sequence,
            }
        review = unit.review
        if review is not None and all(
            item.controller._unit_ref_valid(ref, unit) for ref in review.basis
        ):
            facts["semantic_review"] = {
                "interaction": review.interaction.choice if review.interaction else None,
                "floor": review.floor.choice if review.floor else None,
                "source": review.source.model_dump(mode="json"),
                "source_at": review.at,
                "source_order": review.source_order,
                "sequence": review.sequence,
            }
            observation = item.controller.state.observations.get(review.source.event_id)
            if (
                observation is not None
                and observation.observation_id == review.observation_id
                and observation.snapshot.sequence == review.sequence
            ):
                boundary_scope = observation.answers.get("boundary_scope")
                if boundary_scope is not None:
                    facts["semantic_review"]["boundary_scope"] = boundary_scope.choice
        if len(facts) > 1:
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
    if not await service.app.chat.validate_turn_snapshot(snapshot):
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
            checkpoint = ParticipationCheckpoint.model_validate(
                item.controller.state.host_checkpoint.get("participation_v1", {})
            )
            continuations = {
                ref.event_id: ref
                for current in checkpoint.units.values()
                if current.binding.unit.thread == unit.unit.thread
                and identity(current.binding.actor) == identity(admission.actor_person_id)
                and identity(current.binding.unit.target) == identity(unit.unit.target)
                for ref in current.continuation_refs
            }
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
    if callback is None or conversation in service._promotions:
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
            item.controller.state.consumed[event.ref.event_id] = event.ref.revision
            item.controller.state.candidates.pop(event.ref.event_id, None)
            continue
        row = await service.app.ledger.get_event(int(event_identity))
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
