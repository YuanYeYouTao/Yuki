"""Operator-visible lifecycle and discovery through real HTTP/SQLite/domain owners."""

from uuid import uuid4

import pytest
from sqlalchemy import select
from tests.unit.test_admin_api import admin as admin_fixture
from tests.unit.test_admin_api import command, plugin_action

from yuki.client_api.errors import ClientError
from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.identity.db_models import CanonicalPersonModel, IdentityBindingModel, PresenceModel
from yuki.identity.person_identity import person_enabled_condition
from yuki.runtime.work_repository import WorkRepository

admin = admin_fixture


async def create(client, name="Offline person"):
    response = await client.post("/admin/v1/persons", json=command(display_name=name))
    assert response.status_code == 200
    return response.json()["data"]["resource_id"]


async def shown(client, person):
    response = await client.get(f"/admin/v1/persons/{person}")
    assert response.status_code == 200
    return response.json()["data"]


async def test_role_revoke_original_grant_replay_and_person_restore(admin):
    client, *_ = admin
    person = await create(client)
    grant = command((await shown(client, person))["revision"])
    first = await client.post(f"/admin/v1/persons/{person}/superuser/grant", json=grant)
    assert first.status_code == 200, first.text
    assert (await shown(client, person))["is_superuser"]
    revoke = await client.post(
        f"/admin/v1/persons/{person}/superuser/revoke",
        json=command((await shown(client, person))["revision"]),
    )
    assert revoke.status_code == 200, revoke.text
    assert (
        await client.post(f"/admin/v1/persons/{person}/superuser/grant", json=grant)
    ).json() == first.json()
    assert not (await shown(client, person))["is_superuser"]
    for action, enabled in (("disable", False), ("enable", True)):
        response = await client.post(
            f"/admin/v1/persons/{person}/{action}",
            json=command((await shown(client, person))["revision"]),
        )
        assert response.status_code == 200, response.text
        assert (await shown(client, person))["enabled"] is enabled


async def test_disabled_account_discovery_and_recovery(admin):
    client, *_ = admin
    person = await create(client)
    attached = await client.post(
        f"/admin/v1/persons/{person}/bindings",
        json=command(
            (await shown(client, person))["revision"],
            platform="telegram",
            external_account_id="external-account-1",
        ),
    )
    assert attached.status_code == 200, attached.text
    binding = attached.json()["data"]["resource_id"]
    for action in ("disable", "enable"):
        row = (await client.get(f"/admin/v1/bindings/{binding}")).json()["data"]
        response = await client.post(
            f"/admin/v1/bindings/{binding}/{action}", json=command(row["revision"])
        )
        assert response.status_code == 200, response.text
    found = (
        await client.get(
            "/admin/v1/bindings",
            params={"platform": "telegram", "external_account_id": "external-account-1"},
        )
    ).json()["data"]["items"]
    assert [row["id"] for row in found] == [binding]
    assert found[0]["person_id"] == person
    assert (await shown(client, person))["bindings"][0]["id"] == binding


async def test_new_binding_uses_current_person_without_reviving_disabled_original(admin, database):
    client, *_ = admin
    original, current = await create(client, "Original"), await create(client, "Current")
    old = await client.post(
        f"/admin/v1/persons/{original}/bindings",
        json=command(
            (await shown(client, original))["revision"],
            platform="telegram",
            external_account_id="old-source-account",
        ),
    )
    assert old.status_code == 200, old.text
    old_binding = old.json()["data"]["resource_id"]
    disabled = await client.post(
        f"/admin/v1/persons/{original}/disable",
        json=command((await shown(client, original))["revision"]),
    )
    assert disabled.status_code == 200, disabled.text
    merged = await client.post(
        f"/admin/v1/persons/{original}/merge",
        json={"request_id": str(uuid4()), "to_person": current},
    )
    assert merged.status_code == 200, merged.text
    request = command(
        (await shown(client, original))["revision"],
        platform="telegram",
        external_account_id="new-post-merge-account",
    )
    attached = await client.post(f"/admin/v1/persons/{original}/bindings", json=request)
    assert attached.status_code == 200, attached.text
    result = attached.json()
    assert result["data"]["effective_state"]["person_id"] == current
    latest = await create(client, "Later current")
    moved = await client.post(
        f"/admin/v1/persons/{current}/merge",
        json={"request_id": str(uuid4()), "to_person": latest},
    )
    assert moved.status_code == 200, moved.text
    assert (
        await client.post(f"/admin/v1/persons/{original}/bindings", json=request)
    ).json() == result
    async with database.sessions() as session:
        new = await session.get(IdentityBindingModel, result["data"]["resource_id"])
        assert new.person_id == current and new.status == "active"
        assert await session.scalar(select(person_enabled_condition(new.person_id)))
        assert (await session.get(IdentityBindingModel, old_binding)).person_id == original
        assert not (await session.get(CanonicalPersonModel, original)).enabled


async def test_app_binding_restore_allows_new_pair_without_reviving_revoked_device(admin):
    client, api, *_ = admin
    person = await create(client)
    first_code = await api.identities.issue_for_operator(person, str(uuid4()), str(uuid4()))
    device = await api.identities.pair(first_code["code"], "first")
    principal = await api.identities.authenticate(device["token"])
    binding = principal.binding_id
    row = (await client.get(f"/admin/v1/bindings/{binding}")).json()["data"]
    disabled = await client.post(
        f"/admin/v1/bindings/{binding}/disable", json=command(row["revision"])
    )
    assert disabled.status_code == 200
    row = (await client.get(f"/admin/v1/bindings/{binding}")).json()["data"]
    restored = await client.post(
        f"/admin/v1/bindings/{binding}/enable", json=command(row["revision"])
    )
    assert restored.status_code == 200
    second_code = await api.identities.issue_for_operator(person, str(uuid4()), str(uuid4()))
    second = await api.identities.pair(second_code["code"], "second")
    assert (await api.identities.authenticate(second["token"])).binding_id == binding
    revoked = await client.post(
        f"/admin/v1/devices/{principal.device_id}/revoke", json={"request_id": str(uuid4())}
    )
    assert revoked.status_code == 200
    with pytest.raises(ClientError):
        await api.identities.authenticate(device["token"])
    assert (await api.identities.authenticate(second["token"])).binding_id == binding


async def test_presence_management_preserves_original_identity(admin, database):
    client, *_ = admin
    async with database.sessions() as session:
        presence = await session.scalar(
            select(PresenceModel).where(PresenceModel.external_account_id == "7777")
        )
        identity, rev = presence.id, presence.revision
    first = await client.post(
        f"/admin/v1/presences/{identity}", json=command(rev, enabled=False, ingest_eligible=False)
    )
    assert first.status_code == 200, first.text
    state = (await client.get(f"/admin/v1/presences/{identity}")).json()["data"]
    assert not state["enabled"] and not state["ingest_eligible"]
    assert state["external_account_id"] == "7777"
    restored = await client.post(
        f"/admin/v1/presences/{identity}", json=command(state["revision"], enabled=True)
    )
    assert restored.status_code == 200, restored.text
    state = (await client.get(f"/admin/v1/presences/{identity}")).json()["data"]
    assert state["enabled"] and not state["ingest_eligible"]


async def test_work_discovery_supplies_actual_revision_for_cancel(admin, database):
    client, api, *_ = admin
    person = await create(client)
    issued = await api.identities.issue_for_operator(person, str(uuid4()), str(uuid4()))
    device = await api.identities.pair(issued["code"], "work-owner")
    principal = await api.identities.authenticate(device["token"])
    async with database.sessions() as session:
        conversation = await session.get(CanonicalConversationModel, principal.conversation_id)
        generation = conversation.generation
    repository = WorkRepository(database)
    lease = await repository.acquire(principal.conversation_id, generation)
    assert lease is not None
    row = await repository.accept(
        lease,
        source_key=f"operator-read:{uuid4()}",
        source={"origin": "message", "owner": person},
        goal="A real accepted work",
    )
    await repository.release(lease)
    found = (await client.get("/admin/v1/works")).json()["data"]["items"]
    assert row["id"] in {item["id"] for item in found}
    detail = (await client.get(f"/admin/v1/works/{row['id']}")).json()["data"]
    assert "checkpoint_json" not in detail and "source_json" not in detail
    cancelled = await client.post(
        f"/admin/v1/works/{row['id']}/cancel", json=command(detail["revision"])
    )
    assert cancelled.status_code == 200, cancelled.text
    assert (await client.get(f"/admin/v1/works/{row['id']}")).json()["data"]["state"] == "cancelled"


async def test_existing_key_rotates_and_doctor_keeps_diagnostics(admin):
    client, api, _, models, *_ = admin
    current = (await client.get("/admin/v1/models")).json()["data"]
    document = current["document"]
    document["profiles"]["main"]["api_key_env"] = "ROTATION_KEY"
    applied = await client.post(
        "/admin/v1/models/apply", json=command(current["revision"], document=document)
    )
    assert applied.status_code == 200
    for value in ("synthetic-first", "synthetic-second"):
        current = (await client.get("/admin/v1/models")).json()["data"]
        saved = await client.post(
            "/admin/v1/models/secrets",
            json=command(current["revision"], name="ROTATION_KEY", value=value),
        )
        assert saved.status_code == 200, saved.text
        assert value not in saved.text
    assert (
        models._pool.api_key_for(api.commands.model.files.loaded_catalog.profiles["main"])
        == "synthetic-second"
    )
    await plugin_action(client, "approve", permissions=["tool.register"])
    report = await plugin_action(client, "doctor")
    assert "disabled" in report["effective_state"]["diagnostics"]["problems"]
