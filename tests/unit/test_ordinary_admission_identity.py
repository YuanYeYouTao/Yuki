"""A prepared ordinary input cannot outlive current source qualification."""

import pytest
from sqlalchemy import select
from tests.support.event_seed import seed_event
from tests.unit.test_ordinary_admission_origin import _prepare

from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.conversation.ordinary_admission import (
    OrdinaryAdmissionConflict,
    OrdinaryAdmissionRepository,
)
from yuki.domain.conversations import ScopeType
from yuki.identity.canonical_repository import find_identity_binding
from yuki.identity.db_models import (
    CanonicalPersonModel,
    CanonicalSpaceModel,
    PresenceModel,
    SpaceBindingModel,
)
from yuki.persistence.event_repository import EventLedgerRepository


@pytest.mark.parametrize("scope", [ScopeType.GROUP, ScopeType.PRIVATE])
@pytest.mark.parametrize("change", ["person", "root", "assignment", "presence"])
async def test_current_identity_revocation_does_not_publish_prepared_admission(
    database, scope, change
):
    event, _ = await seed_event(
        EventLedgerRepository(database),
        bot_user_id="8000",
        origin_key="prepared-current-identity",
        scope_type=scope,
        sender_user_id="1001",
        group_id="2001" if scope is ScopeType.GROUP else None,
        direction="inbound",
        content="Continue the original input",
    )
    repository = OrdinaryAdmissionRepository(database)
    prepared = _prepare(repository, event, "continuation")
    async with database.immediate_session() as writer:
        person = await writer.get(CanonicalPersonModel, event.author_person_id)
        presence = await writer.get(PresenceModel, event.ingress_presence_id)
        assignment = await find_identity_binding(writer, "1001")
        if change == "person":
            person.enabled = False
        elif change == "root":
            target = await find_identity_binding(writer, "1002")
            person.merged_into_person_id = target.person_id
            (await writer.get(CanonicalPersonModel, target.person_id)).enabled = False
        elif change == "assignment":
            assignment.status = "disabled"
        elif change == "presence":
            presence.status = "disabled"
    with pytest.raises(OrdinaryAdmissionConflict, match="ordinary_source_changed"):
        await repository.commit(prepared)
    assert await repository.get(event.id) is None


@pytest.mark.parametrize("change", ["space", "space_binding", "native_group"])
async def test_current_group_revocation_does_not_publish_prepared_admission(database, change):
    event, _ = await seed_event(
        EventLedgerRepository(database),
        bot_user_id="8000",
        origin_key="prepared-current-group",
        scope_type=ScopeType.GROUP,
        sender_user_id="1001",
        group_id="2001",
        direction="inbound",
        content="Continue the original group input",
    )
    repository = OrdinaryAdmissionRepository(database)
    prepared = _prepare(repository, event, "continuation")
    async with database.immediate_session() as writer:
        conversation = await writer.get(CanonicalConversationModel, event.canonical_conversation_id)
        binding = await writer.scalar(
            select(SpaceBindingModel).where(SpaceBindingModel.space_id == conversation.space_id)
        )
        if change == "space":
            (await writer.get(CanonicalSpaceModel, conversation.space_id)).enabled = False
        elif change == "space_binding":
            binding.status = "disabled"
        else:
            binding.external_space_id = "another-group"
    with pytest.raises(OrdinaryAdmissionConflict, match="ordinary_source_changed"):
        await repository.commit(prepared)
    assert await repository.get(event.id) is None
