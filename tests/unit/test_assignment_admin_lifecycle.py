"""Explicit assignment changes share the canonical kernel and original receipts."""

from datetime import UTC, datetime

import pytest
from tests.unit.test_admin_api import admin as admin_fixture
from tests.unit.test_admin_api import command

from yuki.identity.canonical_repository import create_presence, ensure_account, find_assignment
from yuki.identity.db_models import CanonicalPersonModel, IdentityBindingModel, PresenceModel
from yuki.identity.errors import CanonicalIdentityError

admin = admin_fixture


async def person(client, *, enabled=True):
    created = await client.post("/admin/v1/persons", json=command(display_name="permanent person"))
    assert created.status_code == 200, created.text
    identity = created.json()["data"]["resource_id"]
    if not enabled:
        disabled = await client.post(f"/admin/v1/persons/{identity}/disable", json=command(1))
        assert disabled.status_code == 200, disabled.text
    return identity


async def register(client, account, *, expected=None, revision=0):
    payload = command(
        revision, platform="qq", external_account_id=account, expected_assignment_id=expected
    )
    response = await client.post("/admin/v1/presences", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["data"], payload


async def test_pause_unbind_reuse_and_explicit_registration_are_distinct(admin, database):
    client, *_ = admin
    owner = await person(client)
    original, original_request = await register(client, "80901")
    identity = original["resource_id"]
    paused = await client.post(
        f"/admin/v1/presences/{identity}", json=command(original["revision"], enabled=False)
    )
    assert paused.status_code == 200, paused.text
    async with database.immediate_session() as session:
        again = await create_presence(
            session, external_account_id="80901", platform="qq", now=datetime.now(UTC)
        )
        assert again.id == identity and again.status == "disabled"
    conflict = await client.post(
        f"/admin/v1/persons/{owner}/bindings",
        json=command(1, platform="qq", external_account_id="80901"),
    )
    assert conflict.status_code == 409 and conflict.json()["problem"]["code"] == "binding_ambiguous"
    retired = await client.post(
        f"/admin/v1/presences/{identity}/unregister",
        json=command(paused.json()["data"]["revision"]),
    )
    assert retired.status_code == 200, retired.text
    async with database.immediate_session() as session:
        assert await find_assignment(session, "80901") is None
        with pytest.raises(CanonicalIdentityError, match="canonical identity"):
            await create_presence(
                session, external_account_id="80901", platform="qq", now=datetime.now(UTC)
            )
    renewed, _ = await register(client, "80901")
    assert renewed["resource_id"] != identity
    old_view = (await client.get(f"/admin/v1/presences/{identity}")).json()["data"]
    new_view = (await client.get(f"/admin/v1/presences/{renewed['resource_id']}")).json()["data"]
    assert old_view["status"] == "ended" and old_view["ended_at"] is not None
    assert new_view["status"] == "active" and new_view["ended_at"] is None
    assert old_view["account_id"] == new_view["account_id"]
    replay = await client.post("/admin/v1/presences", json=original_request)
    assert replay.status_code == 200 and replay.json()["data"] == original


async def test_binding_directory_pages_current_people_without_rewriting_assignments(
    admin, database
):
    from tests.support.canonical_identity import ensure_person
    from tests.support.person_merge import merge_persons

    from yuki.identity.canonical_repository import find_identity_binding, replace_assignment

    client, *_ = admin
    now = datetime.now(UTC)
    async with database.immediate_session() as session:
        source = await ensure_person(session, "directory-person")
        target = await ensure_person(session, "directory-target")
        original = await find_identity_binding(session, "directory-person")
        await merge_persons(session, source, target, now=now)
        owner = await session.get(CanonicalPersonModel, source)
        for number in range(16):
            account = await ensure_account(
                session, f"directory-extra-{number}", platform="qq", now=now
            )
            await session.flush()
            await replace_assignment(session, account, None, owner, now=now)
    for limit in (4, 20):
        response = await client.get(
            "/admin/v1/bindings", params={"person_id": source, "limit": limit}
        )
        assert response.status_code == 200, response.text
        items = response.json()["data"]["items"]
        assert all(item["current_person_id"] == target for item in items)
        assert len(items) == (limit if limit == 4 else 18)
        if limit == 20:
            preserved = next(item for item in items if item["id"] == original.id)
            assert preserved["person_id"] == source
            assert preserved["account_id"] == original.account_id


async def test_role_conversion_and_binding_unbind_preserve_original_stage(admin, database):
    client, *_ = admin
    owner = await person(client, enabled=False)
    original, _ = await register(client, "80902")
    identity = original["resource_id"]
    converted = await client.post(
        f"/admin/v1/presences/{identity}/transfer",
        json=command(original["revision"], to_person=owner),
    )
    assert converted.status_code == 200, converted.text
    binding = converted.json()["data"]["resource_id"]
    async with database.read_session() as session:
        assert (await session.get(PresenceModel, identity)).status == "ended"
        assert (await session.get(IdentityBindingModel, binding)).person_id == owner
        assert not (await session.get(CanonicalPersonModel, owner)).enabled
    # Converting a person account to Yuki is a fresh explicit relation change.
    yuki, _ = await register(client, "80902", expected=binding, revision=1)
    async with database.read_session() as session:
        assert (await session.get(IdentityBindingModel, binding)).status == "ended"
    converted = await client.post(
        f"/admin/v1/presences/{yuki['resource_id']}/transfer",
        json=command(yuki["revision"], to_person=owner),
    )
    assert converted.status_code == 200, converted.text
    rebound = converted.json()["data"]["resource_id"]
    assert rebound != binding
    unbound = await client.post(f"/admin/v1/bindings/{rebound}/unbind", json=command(1))
    assert unbound.status_code == 200, unbound.text
    async with database.read_session() as session:
        assert await find_assignment(session, "80902") is None
        assert (await session.get(IdentityBindingModel, rebound)).status == "ended"
        assert not (await session.get(CanonicalPersonModel, owner)).enabled


async def test_observed_account_without_any_assignment_can_bootstrap(database):
    now = datetime.now(UTC)
    async with database.immediate_session() as session:
        account = await ensure_account(session, "observed-only", platform="telegram", now=now)
        presence = await create_presence(
            session, external_account_id="observed-only", platform="telegram", now=now
        )
        assert presence.account_id == account.id and presence.status == "active"


async def test_explicit_register_refreshes_live_pin_and_old_replay_does_not_repin(admin):
    from types import SimpleNamespace

    from yuki.gateway.registry import RegistryClosed

    client, api, *_ = admin
    registry = api.commands.identity.router._registry
    first, original_request = await register(client, "80903")
    handle = SimpleNamespace(self_id="80903")
    physical = registry.connect(handle, provider_id="snowluma")
    assert physical.presence_id == first["resource_id"]
    retired = await client.post(
        f"/admin/v1/presences/{first['resource_id']}/unregister", json=command(first["revision"])
    )
    assert retired.status_code == 200, retired.text
    assert registry.has_any_active(platform="qq")
    with pytest.raises(RegistryClosed):
        registry.resolve_active(first["resource_id"])
    assert registry.resolve_by_handle(handle).snapshot.presence_id is None
    second, _ = await register(client, "80903")
    current = registry.resolve_active(second["resource_id"])
    assert current.bot is handle
    assert current.snapshot.connection_id == physical.connection_id
    assert current.snapshot.generation == physical.generation
    replay = await client.post("/admin/v1/presences", json=original_request)
    assert replay.status_code == 200
    assert registry.resolve_active(second["resource_id"]).bot is handle
    assert (
        registry.snapshot_presence(
            presence_id=first["resource_id"], platform="qq", external_account_id="80903"
        ).live_count
        == 0
    )
