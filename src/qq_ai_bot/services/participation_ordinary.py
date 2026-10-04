"""Bridge real human admissions into the existing participation snapshot."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from yuki_participation.models import CandidateKind, ScopedEvent, SourceRef
from yuki_participation.participation import ParticipationUnit
from yuki_participation.self_report import SelfDelta, SelfReport

from qq_ai_bot.conversation.initiative_sources import source_revision
from qq_ai_bot.conversation.ordinary_admission import (
    OrdinaryAdmission,
    OrdinaryParticipationBinding,
)
from qq_ai_bot.domain.messages import ChatResponse
from qq_ai_bot.persistence.repository_records import EventRecord
from qq_ai_bot.services.agent_tools import ToolRuntime
from qq_ai_bot.services.participation_feedback import admission_unit_binding, sync_scope_effects

if TYPE_CHECKING:
    from qq_ai_bot.services.semantic_participation import SemanticParticipationService, _Session

logger = logging.getLogger(__name__)


@asynccontextmanager
async def event_scope(
    service: SemanticParticipationService, event_id: int
) -> AsyncIterator[tuple[EventRecord, _Session, ScopedEvent] | None]:
    row = await service.app.ledger.get_event(event_id)
    if row is None or not row.canonical_conversation_id or service._store is None:
        yield None
        return
    scene = await service._scene(row.canonical_conversation_id)
    if scene is None or not scene.enabled:
        yield None
        return
    runtime = await service.app.runtime_config.snapshot(group_id=scene.group_id)
    if not runtime.conversation_policy().semantic_participation_enabled:
        yield None
        return
    item = await service._session(scene)
    item.pins += 1
    try:
        await sync_scope_effects(service, item)
        await service._hydrate(item)
        await service._validate_boundaries(item)
        event = service._event(row, item)
        if (
            event is None
            or event.kind != "human"
            or not await service._source_current(item, event.ref)
        ):
            yield None
        else:
            item.controller.observe_committed_event(event)
            yield row, item, event
    finally:
        item.pins -= 1


def invitation_unit(item: _Session, event: ScopedEvent) -> ParticipationUnit | None:
    candidate = item.controller.state.candidates.get(event.ref.event_id)
    observation = item.controller.state.observations.get(event.ref.event_id)
    if (
        candidate is None
        or observation is None
        or candidate.kind != CandidateKind.CONVERSATION
        or candidate.event.ref != event.ref
        or candidate.support.kind != "observed"
        or candidate.support.strength(time.time()) <= 0
        or not item.controller.participation_view(event, time.time()).addressed
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
        target_hint=unit.target,
        basis=tuple((ref.event_id, ref.revision) for ref in dict.fromkeys(basis)),
    )


def invitation_basis(item: _Session, event: ScopedEvent) -> tuple[SourceRef, ...]:
    observation = item.controller.state.observations.get(event.ref.event_id)
    if observation is None:
        return (event.ref,)
    return tuple(dict.fromkeys((event.ref, *observation.snapshot.context)))


async def binding_for_event(
    service: SemanticParticipationService, event_id: int, *, continuation_only: bool
) -> OrdinaryParticipationBinding | None:
    async with service._lock, event_scope(service, event_id) as found:
        if found is None:
            return None
        row, item, event = found
        view = item.controller.participation_view(event, time.time())
        if not view.source_valid:
            return None
        matched = view.matched_unit
        if matched is not None:
            basis = tuple(dict.fromkeys((*matched.binding.basis, event.ref)))
            if all((await service._sources_current(item, basis)).values()):
                return host_binding(row, item, matched.unit, basis)
            return None
        invited = invitation_unit(item, event)
        if invited is not None:
            basis = invitation_basis(item, event)
            if all((await service._sources_current(item, basis)).values()):
                return host_binding(row, item, invited, basis)
            return None
        if continuation_only:
            return None
        # A direct real input can establish its own unit. This is Host admission,
        # never an observed semantic interpretation of an unscored focus.
        return host_binding(
            row, item, ParticipationUnit(thread=event.thread, target=event.author), (event.ref,)
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
        _, item, event = found
        current = await service.ordinary.current(
            admission.event_id,
            conversation_id=binding.conversation_id,
            generation=binding.generation,
        )
        unit = admission_unit_binding(current) if current is not None else None
        if unit is not None and all((await service._sources_current(item, unit.basis)).values()):
            item.controller.observe_unit_input(unit, event.ref)
            await service._save(item)


async def context_for_event(
    service: SemanticParticipationService, event_id: int
) -> dict[str, object] | None:
    """Project meaningful state of the original admitted unit into this new tail."""
    async with service._lock, event_scope(service, event_id) as found:
        if found is None:
            return None
        _, item, _ = found
        admission = await service.ordinary.current(
            event_id,
            conversation_id=item.scene.conversation_id,
            generation=item.scene.generation,
        )
        binding = admission_unit_binding(admission) if admission is not None else None
        if binding is None or not all(
            (await service._sources_current(item, binding.basis)).values()
        ):
            return None
        unit = next(
            (u for u in item.controller.participating_units(time.time()) if u.unit == binding.unit),
            None,
        )
        if unit is None or (unit.engage is None and not unit.expressed and not unit.closed):
            return None
        data: dict[str, object] = {"thread": binding.unit.thread, "person_id": binding.actor}
        if unit.engage is not None:
            data["engage"] = unit.engage
        if unit.expressed:
            data["expressed"] = True
        if unit.closed:
            data["closed"] = True
        return data


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
        _, item, event = found
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
            delta=delta,
        )
        prior = next(
            (
                u.engage
                for u in item.controller.participating_units(time.time())
                if u.unit == unit.unit
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
    for candidate in tuple(item.controller.state.candidates.values()):
        event = candidate.event
        unit = invitation_unit(item, event)
        if unit is None or not await service._source_current(item, event.ref):
            continue
        key, _, identity = event.ref.event_id.partition(":")
        if key != "event" or not identity.isdecimal():
            continue
        if await service.ordinary.get(int(identity), generation=item.scene.generation) is not None:
            item.controller.state.consumed[event.ref.event_id] = event.ref.revision
            item.controller.state.candidates.pop(event.ref.event_id, None)
            continue
        row = await service.app.ledger.get_event(int(identity))
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
