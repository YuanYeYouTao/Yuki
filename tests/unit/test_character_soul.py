"""Actual SQLite, file publication, HTTP ownership and hot activation."""

from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from tests.conftest import make_settings
from tests.unit.test_client_identity import code_for

from yuki.character.repository import CharacterRepository
from yuki.character.soul import CharacterSoul, document_revision
from yuki.client_api.http import ClientApi, attach_client_api


@pytest.fixture
async def character(database, tmp_path):
    path = tmp_path / "SOUL.md"
    path.write_bytes(b"Hello {{name}}\r\n")
    settings = make_settings(database.url, soul_file=path)
    identity, person, issued = await code_for(database)
    paired = await identity.pair(issued["code"], "owner")
    principal = await identity.authenticate(paired["token"])
    repository = CharacterRepository(identity, settings)
    await repository.initialize("Aria")
    await repository.set_owner(str(uuid4()), person, str(uuid4()), granted=True)
    soul = CharacterSoul(identity, settings)
    app = FastAPI()
    attach_client_api(app, lambda: ClientApi(identity, character=repository, soul=soul))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.headers["Authorization"] = "Bearer " + paired["token"]
        yield client, repository, soul, settings, principal, path


async def test_empty_soul_survives_file_hot_activation_and_original_receipt(character):
    client, _repository, _soul, settings, _principal, path = character
    document = (await client.get("/client/v1/character/soul")).json()["data"]
    request = {"request_id": str(uuid4()), "expected_revision": document["revision"], "content": ""}
    response = await client.put("/client/v1/character/soul", json=request)
    assert response.status_code == 200, response.text
    assert path.read_bytes() == b"" and settings.soul == ""
    assert settings.active_soul_revision == document_revision(b"")
    replay = await client.put("/client/v1/character/soul", json=request)
    assert replay.json()["data"]["replay"] is True
    cold = make_settings(settings.database_url, soul_file=path)
    assert cold.soul == "" and cold.active_soul_revision == settings.active_soul_revision


async def test_forgetting_an_owner_without_accounts_removes_its_grant(character):
    from sqlalchemy import select
    from tests.unit.test_person_authority import person as new_person

    from yuki.character.db_models import CharacterOwnerGrantModel
    from yuki.identity.canonical_repository import forget_person_by_id

    _client, repository, _soul, _settings, _principal, _path = character
    person = await new_person(repository.database)
    await repository.set_owner(str(uuid4()), person, str(uuid4()), granted=True)
    async with repository.database.immediate_session() as session:
        await forget_person_by_id(session, person)
    async with repository.database.sessions() as session:
        assert (
            await session.scalar(
                select(CharacterOwnerGrantModel).where(CharacterOwnerGrantModel.person_id == person)
            )
            is None
        )


async def test_name_commit_replay_never_restores_an_old_name(character):
    client, repository, _soul, settings, _principal, _path = character
    original = await repository.current()
    first = {
        "request_id": str(uuid4()),
        "expected_revision": original.revision,
        "display_name": "A",
    }
    a = await client.patch("/client/v1/character", json=first)
    assert a.status_code == 200, a.text
    b = await client.patch(
        "/client/v1/character",
        json={
            "request_id": str(uuid4()),
            "expected_revision": a.json()["data"]["revision"],
            "display_name": "B",
        },
    )
    assert b.status_code == 200, b.text
    assert settings.character_identity.display_name == "B"
    replay = await client.patch("/client/v1/character", json=first)
    assert replay.json()["data"]["display_name"] == "A"
    assert settings.character_identity.display_name == "B"
    assert (await repository.current()).id == original.id


async def test_compare_and_swap_keeps_raw_bytes_and_owner_revoke_stops_reads(character):
    client, repository, _soul, settings, principal, path = character
    original = (await client.get("/client/v1/character/soul")).json()["data"]
    body = "Raw\r\n{{name}}\r\n"
    saved = await client.put(
        "/client/v1/character/soul",
        json={
            "request_id": str(uuid4()),
            "expected_revision": original["revision"],
            "content": body,
        },
    )
    assert saved.status_code == 200, saved.text
    conflict = await client.put(
        "/client/v1/character/soul",
        json={
            "request_id": str(uuid4()),
            "expected_revision": original["revision"],
            "content": "stale",
        },
    )
    assert conflict.status_code == 409 and conflict.json()["problem"]["code"] == "version_conflict"
    assert path.read_bytes() == body.encode() and settings.soul == body
    from sqlalchemy import select

    from yuki.persistence.models import AdminOperationEventModel

    async with repository.database.sessions() as session:
        audit = " ".join(await session.scalars(select(AdminOperationEventModel.after_json)))
    assert "Raw" not in audit and "{{name}}" not in audit
    await repository.set_owner(str(uuid4()), principal.person_id, str(uuid4()), granted=False)
    denied = await client.get("/client/v1/character/soul")
    assert denied.status_code == 403 and body not in denied.text
    assert (await client.get("/client/v1/character")).json()["data"]["viewer"]["is_owner"] is False


async def test_written_but_lost_file_ack_resumes_the_same_request(character, monkeypatch):
    from yuki.character import soul as module

    client, _repository, _soul, settings, _principal, path = character
    original = (await client.get("/client/v1/character/soul")).json()["data"]
    request = {
        "request_id": str(uuid4()),
        "expected_revision": original["revision"],
        "content": "Recovered",
    }
    replace = module._replace

    def lost(*args):
        replace(*args)
        raise OSError("lost acknowledgement")

    monkeypatch.setattr(module, "_replace", lost)
    unknown = await client.put("/client/v1/character/soul", json=request)
    assert unknown.status_code == 409 and unknown.json()["problem"]["code"] == "unknown"
    assert path.read_bytes() == b"Recovered" and settings.soul != "Recovered"
    monkeypatch.setattr(
        module, "_replace", lambda *_: pytest.fail("must not replace the accepted bytes twice")
    )
    recovered = await client.put("/client/v1/character/soul", json=request)
    assert recovered.status_code == 200, recovered.text
    assert settings.soul == "Recovered"


async def test_sdk_recovers_its_original_unknown_soul_write(character, tmp_path, monkeypatch):
    from yuki.character import soul as module
    from yuki.client_sdk import ClientProblem, Outbox, PersonalClient

    http, _repo, _soul, settings, _principal, _path = character
    sdk = PersonalClient(
        "http://test",
        http.headers["Authorization"].split()[1],
        Outbox(tmp_path / "sdk.sqlite3"),
        http=http,
    )
    remote = await sdk.get("character/soul")
    identity = await sdk.prepare_soul("SDK recovered", remote["revision"])
    replace = module._replace

    def lost(*args):
        replace(*args)
        raise OSError("lost file ack")

    monkeypatch.setattr(module, "_replace", lost)
    with pytest.raises(ClientProblem, match="unknown"):
        await sdk.submit(identity)
    assert (await sdk.resolve(identity))["state"] == "submitted"
    monkeypatch.setattr(module, "_replace", lambda *_: pytest.fail("original bytes already saved"))
    recovered = await sdk.submit(identity)
    assert recovered["state"] == "result"
    assert settings.soul == "SDK recovered"
