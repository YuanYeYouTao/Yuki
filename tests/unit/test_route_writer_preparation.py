"""Route preparation cannot commit an account that changed while awaiting admission."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from tests.support.canonical_identity import ensure_person, ensure_presence, ensure_space
from tests.support.gateway import builtin_registry

from yuki.conversation.canonical_db_models import PersonActiveRouteModel, SpaceActiveRouteModel
from yuki.identity.canonical_repository import find_identity_binding
from yuki.identity.db_models import PresenceModel, SpaceBindingModel
from yuki.identity.routing import PresenceRouter


@pytest.mark.parametrize("kind", ["person", "space"])
@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("change", ["binding", "presence"])
async def test_takeover_checks_prepared_account_after_writer_admission(
    database, kind, existing, change
):
    registry = builtin_registry(gateway_instance_id="writer-route-proof")

    async def is_member(*_args):
        return True

    router = PresenceRouter(database, registry, membership_probe=is_member)
    async with database.immediate_session() as writer:
        person_id = await ensure_person(writer, "1001")
        presence_id = await ensure_presence(writer, "8000")
        space_id = await ensure_space(writer, "2001")
        binding = (
            await find_identity_binding(writer, "1001")
            if kind == "person"
            else await writer.scalar(
                select(SpaceBindingModel).where(SpaceBindingModel.space_id == space_id)
            )
        )
        binding_id = binding.id
        binding_type = type(binding)
        model = PersonActiveRouteModel if kind == "person" else SpaceActiveRouteModel
        target_id = person_id if kind == "person" else space_id
        if existing:
            writer.add(
                model(
                    **(
                        {"person_id": person_id, "identity_binding_id": binding_id}
                        if kind == "person"
                        else {"space_id": space_id, "space_binding_id": binding_id}
                    ),
                    presence_id=presence_id,
                    route_generation=1,
                    revision=1,
                    paused=True,
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
    registry.connect(SimpleNamespace(self_id="8000"), provider_id="snowluma")
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence_id)

    async def change_source():
        async with database.immediate_session() as writer:
            row = await writer.get(
                binding_type if change == "binding" else PresenceModel,
                binding_id if change == "binding" else presence_id,
            )
            row.status = "disabled"

    router._cas_hold = change_source
    takeover = router.cas_takeover_person if kind == "person" else router.cas_takeover_space
    assert await takeover(target_id, expected_generation=1 if existing else None) == "conflict"
    async with database.read_session() as reader:
        route = await reader.get(model, target_id)
        if existing:
            assert route.paused and route.route_generation == 1
        else:
            assert route is None
