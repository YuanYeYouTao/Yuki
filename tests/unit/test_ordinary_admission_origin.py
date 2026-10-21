"""The original admission retains continuation provenance across restart/handoff."""

import pytest
from tests.support.semantic_participation_host_helpers import _event_and_route

from yuki.conversation.initiative_sources import source_revision
from yuki.conversation.ordinary_admission import (
    OrdinaryAdmissionRepository,
    OrdinaryParticipationBinding,
)
from yuki.persistence.event_repository import EventLedgerRepository


def _prepare(repository, event, reason):
    return repository.prepare(
        event,
        1,
        1,
        OrdinaryParticipationBinding(
            event.canonical_conversation_id,
            1,
            event.id,
            str(source_revision(event)),
            "original-topic",
            event.author_person_id,
            ((f"event:{event.id}", 1),),
        ),
        reason=reason,
    )


@pytest.mark.parametrize("reason", [None, "continuation"])
async def test_current_admission_keeps_original_reason_and_legacy_basis(database, reason):
    event = await _event_and_route(database, EventLedgerRepository(database))
    repository = OrdinaryAdmissionRepository(database)
    prepared = _prepare(repository, event, reason)
    assert await repository.commit(prepared)

    # No process-local controller callback is needed to recover the entry fact.
    restarted = OrdinaryAdmissionRepository(database)
    assert await restarted.current(event.id) == prepared.admission
    assert not await restarted.commit(prepared)
    assert (await restarted.get(event.id)).reason == reason


async def test_context_handoff_keeps_each_inputs_origin_in_the_original_activation(database):
    ledger = EventLedgerRepository(database)
    a = await _event_and_route(database, ledger, content="original activation")
    c = await _event_and_route(database, ledger, content="unconfirmed continuation")
    repository = OrdinaryAdmissionRepository(database)
    original = _prepare(repository, a, "group_open")
    incoming = _prepare(repository, c, "continuation")
    assert await repository.commit(original)

    async with database.immediate_session() as writer:
        assert await repository.commit_to_activation(incoming, original.admission, session=writer)
    saved = await OrdinaryAdmissionRepository(database).current(c.id)
    assert saved.activation_id == original.admission.activation_id
    assert saved.reason == "continuation"
    assert saved.binding == incoming.admission.binding
    assert saved.actor_person_id == c.author_person_id
