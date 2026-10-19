"""Real HTTP and SQLite coverage for character identity and ownership."""

import asyncio
import json
from uuid import uuid4

import pytest
from fastapi import FastAPI
from sqlalchemy import text
from tests.unit.test_client_api import (
    TOKEN,
    admin,
    bearer,
    pair,
    person_and_code,
    personal,
)
from tests.unit.test_client_identity import code_for

from yuki.character.db_models import CharacterOwnerGrantModel
from yuki.character.repository import CharacterRepository
from yuki.client_api.http import ClientApi, attach_client_api
from yuki.client_api.identity import ClientIdentityService
from yuki.client_api.openapi import client_openapi, export_openapi
from yuki.client_api.routes import ClientProactiveRoute
from yuki.identity.db_models import CanonicalPersonModel
from yuki.identity.person_identity import merge_persons
from yuki.persistence.people_repository import PeopleRepository

__all__ = ["admin", "personal"]


async def owner_device(client):
    person, code = await person_and_code(client)
    device = await pair(client, code)
    granted = await client.post(
        "/admin/v1/character/owners",
        headers=bearer(TOKEN),
        json={
            "request_id": str(uuid4()),
            "person_id": person,
        },
    )
    assert granted.status_code == 200, granted.text
    return person, device


async def test_meta_is_public_and_profile_changes_keep_identity_and_original_receipt(personal):
    client = personal
    meta = (await client.get("/client/v1/meta")).json()["data"]
    assert set(meta) == {"api_revision", "server_version", "character", "min_client"}
    assert set(meta["character"]) == {"id", "display_name", "accent", "avatar_url"}
    assert meta["character"]["avatar_url"] is None
    assert (await client.get("/client/v1/character")).status_code == 401
    _, code = await person_and_code(client)
    other = await pair(client, code)
    body = {"request_id": str(uuid4()), "expected_revision": 1, "display_name": "Mika"}
    assert (
        await client.patch("/client/v1/character", json=body, headers=bearer(other["token"]))
    ).status_code == 403
    person, device = await owner_device(client)
    headers = bearer(device["token"])
    before = (await client.get("/client/v1/me", headers=headers)).json()["data"]
    assert before["person_id"] == person
    changed = await client.patch("/client/v1/character", json=body, headers=headers)
    assert changed.status_code == 200, changed.text
    current = changed.json()["data"]
    assert current["id"] == meta["character"]["id"]
    assert current["revision"] == 2 and current["display_name"] == "Mika"
    assert current["viewer"]["is_owner"]
    again = await client.patch("/client/v1/character", json=body, headers=headers)
    assert again.json()["data"]["replay"]
    conflict = await client.patch(
        "/client/v1/character", json={**body, "bio": "changed"}, headers=headers
    )
    assert conflict.json()["problem"]["code"] == "request_conflict"
    assert (await client.get("/client/v1/me", headers=headers)).json()["data"] == before
    original = await client.get(f"/client/v1/requests/{body['request_id']}", headers=headers)
    assert original.json()["data"]["result"]["id"] == current["id"]


async def test_two_owners_compete_on_actual_revision_and_revocation_is_immediate(personal):
    client = personal
    person, first = await owner_device(client)
    _, second = await owner_device(client)
    responses = await asyncio.gather(
        *[
            client.patch(
                "/client/v1/character",
                headers=bearer(device["token"]),
                json={
                    "request_id": str(uuid4()),
                    "expected_revision": 1,
                    "bio": bio,
                },
            )
            for device, bio in ((first, "first"), (second, "second"))
        ]
    )
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert (
        next(response for response in responses if response.status_code == 409).json()["problem"][
            "code"
        ]
        == "version_conflict"
    )
    revoke = await client.post(
        f"/admin/v1/character/owners/{person}/revoke",
        headers=bearer(TOKEN),
        json={"request_id": str(uuid4())},
    )
    assert revoke.status_code == 200
    view = (await client.get("/client/v1/character", headers=bearer(first["token"]))).json()["data"]
    assert not view["viewer"]["is_owner"]
    assert (
        await client.patch(
            "/client/v1/character",
            headers=bearer(first["token"]),
            json={
                "request_id": str(uuid4()),
                "expected_revision": view["revision"],
                "bio": "forbidden",
            },
        )
    ).status_code == 403


async def test_owner_follows_person_merge_but_revoked_grant_does_not_revive(database):
    identity, source, issued = await code_for(database)
    device = await identity.pair(issued["code"], "source")
    _, target, other = await code_for(database, "1002")
    target_device = await identity.pair(other["code"], "target")
    repository = CharacterRepository(identity)
    operator, request = str(uuid4()), str(uuid4())
    await repository.set_owner(operator, source, request, granted=True)
    async with database.immediate_session() as session:
        await merge_persons(session, source, target)
    assert (await identity.authenticate(device["token"])).is_owner
    assert (await identity.authenticate(target_device["token"])).is_owner
    await repository.set_owner(operator, target, str(uuid4()), granted=False)
    await repository.set_owner(operator, source, request, granted=True)
    assert not (await identity.authenticate(target_device["token"])).is_owner
    await repository.set_owner(operator, target, str(uuid4()), granted=True)
    async with database.immediate_session() as session:
        root = await session.get(CanonicalPersonModel, target)
        root.enabled = False
    from yuki.client_api.errors import ClientError

    with pytest.raises(ClientError, match="unauthenticated"):
        await identity.authenticate(device["token"])


async def test_forget_removes_owner_without_changing_character(database):
    identity, person, issued = await code_for(database)
    await identity.pair(issued["code"], "owner")
    repository = CharacterRepository(identity)
    await repository.set_owner(str(uuid4()), person, str(uuid4()), granted=True)
    before = await repository.meta()
    assert await PeopleRepository(database).delete_person_id(person)
    async with database.sessions() as session:
        assert await session.get(CharacterOwnerGrantModel, person) is None
    assert await repository.meta() == before


async def test_proactive_route_uses_original_person_and_compare_and_swap(database):
    identity, _, issued = await code_for(database)
    device = await identity.pair(issued["code"], "desktop")
    actor = await identity.authenticate(device["token"])
    service = ClientProactiveRoute(identity)
    initial = await service.get(actor)
    request = str(uuid4())
    result = await service.set(actor, request, initial["revision"], actor.binding_id)
    assert result["binding_id"] == actor.binding_id and not result["paused"]
    assert (await service.set(actor, request, initial["revision"], actor.binding_id))["replay"]
    from yuki.client_api.errors import ClientError

    with pytest.raises(ClientError, match="version_conflict"):
        await service.set(actor, str(uuid4()), initial["revision"], actor.binding_id)
    _, _, other = await code_for(database, "1002")
    other_device = await identity.pair(other["code"], "other")
    other_actor = await identity.authenticate(other_device["token"])
    with pytest.raises(ClientError, match="not_found"):
        await service.set(actor, str(uuid4()), result["revision"], other_actor.binding_id)
    async with database.sessions() as session:
        assert (
            await session.execute(
                text("SELECT route_generation FROM person_active_routes WHERE person_id=:id"),
                {"id": actor.person_id},
            )
        ).scalar_one() >= 1
    async with database.immediate_session() as session:
        await merge_persons(session, actor.person_id, other_actor.person_id)
    merged = await identity.authenticate(device["token"])
    assert merged.person_id == other_actor.person_id
    assert merged.request_person_id == actor.request_person_id
    # Replaying the original command keeps its original namespace after a merge.
    assert (await service.set(merged, request, initial["revision"], actor.binding_id))["replay"]


def test_schema_export_is_stable_and_matches_attached_routes(tmp_path):
    first, second = tmp_path / "one.json", tmp_path / "two.json"
    export_openapi(first)
    export_openapi(second)
    assert first.read_bytes() == second.read_bytes()
    schema = json.loads(first.read_text())
    app = FastAPI(title="Yuki personal API", version=schema["info"]["version"])
    attach_client_api(app, lambda: ClientApi(ClientIdentityService(None)))
    assert app.openapi() == client_openapi()
    for path in ("meta", "character", "me", "home"):
        assert (
            "$ref"
            in schema["paths"][f"/client/v1/{path}"]["get"]["responses"]["200"]["content"][
                "application/json"
            ]["schema"]
        )
