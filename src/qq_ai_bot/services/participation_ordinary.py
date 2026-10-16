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
from yuki_participation.participation import ParticipationUnit, UnitBinding, UnitParticipation
from yuki_participation.self_report import SelfDelta, SelfReport

from qq_ai_bot.conversation.initiative_sources import source_revision
from qq_ai_bot.conversation.ordinary_admission import (
    OrdinaryAdmission,
    OrdinaryParticipationBinding,
)
from qq_ai_bot.domain.messages import ChatResponse
from qq_ai_bot.persistence.repository_records import EventRecord
from qq_ai_bot.services.agent_tools import ToolRuntime
from qq_ai_bot.services.participation_feedback import (
    admission_continuation,
    admission_unit_binding,
    sync_scope_effects,
)
from qq_ai_bot.services.participation_identity import actor_identity

if TYPE_CHECKING:
    from qq_ai_bot.services.semantic_participation import SemanticParticipationService, _Session

logger = logging.getLogger(__name__)


@asynccontextmanager
async def event_scope(
    service: SemanticParticipationService, event_id: int
) -> AsyncIterator[tuple[EventRecord, _Session, ScopedEvent, Callable[[str], str]] | None]:
    row = await service.app.ledger.get_event(event_id)
    if row is None or not row.canonical_conversation_id or service._store is None:
        yield None
        return
    scene = await service._scene(row.canonical_conversation_id)
    if scene is None or not scene.enabled:
        yield None
        return
    runtime = await service.app.runtime_config.snapshot(group_id=scene.space_id)
    if not runtime.conversation_policy().semantic_participation_enabled:
        yield None
        return
    item = await service._session(scene)
    item.pins += 1
    try:
        await sync_scope_effects(service, item)
        await service._hydrate(item)
        await service._validate_boundaries(item)
        identity = await actor_identity(
            service, item, (row.author_person_id,) if row.author_person_id else ()
        )
        event = service._event(row, item, actor_identity=identity)
        if (
            event is None
            or event.kind != "human"
            or not await service._source_current(item, event.ref)
        ):
            yield None
        else:
            item.controller.observe_committed_event(event)
            yield row, item, event, identity
    finally:
        item.pins -= 1


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
    service: SemanticParticipationService, event_id: int
) -> OrdinaryParticipationBinding | None:
    async with service._lock, event_scope(service, event_id) as found:
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
        return host_binding(
            row, item, ParticipationUnit(thread=event.thread, target=event.author), (event.ref,)
        )


async def continuation_for_event(
    service: SemanticParticipationService, event_id: int, *, check_budget: bool = True
) -> tuple[OrdinaryParticipationBinding, ImplicitReason] | None:
    """Classify an implicit source: a verified invitation first, then a matched unit."""
    async with service._lock, event_scope(service, event_id) as found:
        if found is None:
            return None
        row, item, event, identity = found
        view = item.controller.participation_view(
            event, time.time(), check_budget=check_budget, actor_identity=identity
        )
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
    check_budget: bool = True,
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
        check_budget=check_budget,
        actor_identity=await actor_identity(service, item, (event.author,), session=session),
    )


async def on_ordinary_admitted(
    service: SemanticParticipationService,
    binding: OrdinaryParticipationBinding | None,
    admission: OrdinaryAdmission,
) -> None:
    if binding is None:
        return
    async with service._lock, event_scope(service, admission.event_id) as found:
        if found is None:
            return
        _, item, event, _identity = found
        current = await service.ordinary.current(
            admission.event_id,
            conversation_id=binding.conversation_id,
            generation=binding.generation,
        )
        unit = admission_unit_binding(current) if current is not None else None
        if (
            current is not None
            and unit is not None
            and all((await service._sources_current(item, unit.basis)).values())
        ):
            item.controller.observe_unit_input(
                unit,
                event.ref,
                continuation=admission_continuation(current),
            )
            await service._save(item)


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
    async with service._lock, event_scope(service, event_id) as found:
        if found is None:
            return
        _, item, event, identity = found
        admission = await service.ordinary.current(
            event_id, conversation_id=item.scene.conversation_id, generation=snapshot.generation
        )
        unit = admission_unit_binding(admission) if admission is not None else None
        if admission is None or unit is None:
            return
        if not all((await service._sources_current(item, unit.basis)).values()):
            return
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
        prior = next(
            (
                u.engage
                for u in item.controller.participating_units(time.time(), actor_identity=identity)
                if u.unit.thread == unit.unit.thread
                and identity(u.unit.target) == identity(unit.unit.target)
            ),
            None,
        )
        if item.controller.observe_unit_hint(unit, report):
            if delta.engage == "quiet" and prior != "quiet" and item.observation is not None:
                interpreted = item.controller.state.observations.get(event.ref.event_id)
                if (
                    interpreted is None
                    or interpreted.snapshot.focus != event.ref
                    or not all(
                        (
                            await service._sources_current(item, interpreted.snapshot.context)
                        ).values()
                    )
                ):
                    item.observation.request_observation(event.ref)
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
