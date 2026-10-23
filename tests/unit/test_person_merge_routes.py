"""The real merge command inherits only an existing available route."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from tests.support.canonical_identity import ensure_person, ensure_presence
from tests.support.client_identity import client_identity_service
from tests.support.gateway import builtin_registry

from yuki.admin.authority.principal import ControlPrincipal, PrincipalSource
from yuki.admin.operations.base import CommandExecutor
from yuki.admin.operations.identity import IdentityOperations
from yuki.client_api.errors import ClientError
from yuki.conversation.canonical_db_models import PersonActiveRouteModel
from yuki.domain.identity import PrincipalId
from yuki.identity.canonical_repository import find_identity_binding
from yuki.identity.db_models import CanonicalPersonModel, PresenceModel
from yuki.identity.routing import PresenceRouter
from yuki.persistence.people_repository import PeopleRepository


@pytest.mark.parametrize(
    ("target_state", "source_live"),
    [
        ("live", True),
        ("paused", False),
        ("missing", False),
        ("missing", True),
        ("disabled_binding", False),
        ("disabled_binding", True),
    ],
)
@pytest.mark.parametrize("entry", ["admin", "claim"])
async def test_merge_keeps_live_target_or_inherits_only_available_source(
    database, target_state, source_live, entry
):
    registry = builtin_registry(gateway_instance_id="merge-route-fixture")
    router = PresenceRouter(database, registry)
    now = datetime.now(UTC)
    async with database.immediate_session() as writer:
        source = await ensure_person(writer, "1001")
        target = await ensure_person(writer, "1002")
        source_binding = await find_identity_binding(writer, "1001")
        target_binding = await find_identity_binding(writer, "1002")
        source_binding_id, target_binding_id = source_binding.id, target_binding.id
        source_presence = await ensure_presence(writer, "8000")
        target_presence = await ensure_presence(writer, "8001")
        writer.add(
            PersonActiveRouteModel(
                person_id=source,
                identity_binding_id=source_binding.id,
                presence_id=source_presence,
                route_generation=3,
                revision=2,
                paused=False,
                created_at=now,
                updated_at=now,
            )
        )
        if target_state != "missing":
            writer.add(
                PersonActiveRouteModel(
                    person_id=target,
                    identity_binding_id=target_binding.id,
                    presence_id=target_presence,
                    route_generation=8,
                    revision=4,
                    paused=target_state == "paused",
                    created_at=now,
                    updated_at=now,
                )
            )
        if target_state == "disabled_binding":
            target_binding.status = "disabled"
        if not source_live:
            (await writer.get(CanonicalPersonModel, source)).enabled = False
    for account, presence in (("8000", source_presence), ("8001", target_presence)):
        registry.connect(SimpleNamespace(self_id=account), provider_id="snowluma")
        registry.bind_presence(platform="qq", external_account_id=account, presence_id=presence)
    identities = client_identity_service(database, router)
    operations = IdentityOperations(
        CommandExecutor(database),
        identities,
        PeopleRepository(database),
        router=router,
    )
    principal = ControlPrincipal(
        principal_id=PrincipalId(uuid4()),
        person_id=None,
        source=PrincipalSource.CLI,
        granted_capabilities=("identity.person.merge",),
        authenticated=True,
        active=True,
    )
    request_id = str(uuid4())
    if entry == "admin":
        first = await operations.change_person(principal, source, request_id, to_person=target)
    else:
        code = await identities.issue_for_operator(target, str(principal.principal_id), request_id)
        first = await identities.claim_account(
            code["code"], platform="qq", account="1001", display_name="", message_id="claim"
        )
    assert first["person_id"] == target and not first.get("replay", False)
    async with database.read_session() as reader:
        assert await reader.get(PersonActiveRouteModel, source) is None
        route = await reader.get(PersonActiveRouteModel, target)
        if target_state in {"live", "paused"}:
            assert route.identity_binding_id == target_binding_id
            assert route.presence_id == target_presence
            assert route.route_generation == 8 and route.revision == 4
            assert route.paused is (target_state == "paused")
        elif source_live:
            assert route.identity_binding_id == source_binding_id
            assert route.presence_id == source_presence and not route.paused
            assert route.route_generation == (1 if target_state == "missing" else 9)
        else:
            assert route is None
        assert (await reader.get(PresenceModel, source_presence)).enabled
    if entry == "admin":
        replay = await operations.change_person(principal, source, request_id, to_person=target)
        assert replay["replay"] and replay["person_id"] == target
    else:
        with pytest.raises(ClientError, match="unauthenticated"):
            await identities.claim_account(
                code["code"], platform="qq", account="1001", display_name="", message_id="retry"
            )
    async with database.read_session() as reader:
        assert len(tuple(await reader.scalars(select(PersonActiveRouteModel)))) == (
            route is not None
        )
