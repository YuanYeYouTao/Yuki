"""Actual SQLite, shared client access, file publication and hot activation."""

import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from tests.conftest import make_settings
from tests.unit.test_client_identity import code_for

from yuki.character.repository import CharacterRepository
from yuki.character.soul import CharacterSoul, document_revision
from yuki.client_api.http import ClientApi, attach_client_api
from yuki.client_api.views import ClientViews
from yuki.client_api.works import ClientWorks
from yuki.identity.db_models import CanonicalPersonModel


@pytest.fixture
async def character(database, tmp_path):
    path = tmp_path / "SOUL.md"
    path.write_bytes(b"Hello {{name}}\r\n")
    settings = make_settings(database.url, soul_file=path)
    identity, _person, issued = await code_for(database)
    async with database.immediate_session() as session:
        person = await session.get(CanonicalPersonModel, _person)
        person.is_superuser = True
    paired = await identity.pair(issued["code"], "owner")
    principal = await identity.authenticate(paired["token"])
    repository = CharacterRepository(identity, settings)
    await repository.initialize("Aria")
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


async def test_retired_grants_do_not_authorize_shared_edits_and_remain_unchanged(character):
    from datetime import UTC, datetime

    from yuki.character.db_models import CharacterOwnerGrantModel
    from yuki.identity.db_models import CanonicalPersonModel

    client, repository, _soul, _settings, principal, _path = character
    revoked = datetime.now(UTC)
    issuer = str(uuid4())
    async with repository.database.immediate_session() as session:
        person = await session.get(CanonicalPersonModel, principal.person_id)
        person.is_superuser = False
        session.add(
            CharacterOwnerGrantModel(
                person_id=person.id,
                granted_by_principal=issuer,
                granted_at=revoked,
                revoked_at=revoked,
            )
        )
    original = (await client.get("/client/v1/character")).json()["data"]
    assert "viewer" not in original and "viewer" not in await repository.get()
    changed = await client.patch(
        "/client/v1/character",
        json={
            "request_id": str(uuid4()),
            "expected_revision": original["revision"],
            "display_name": "Shared role",
        },
    )
    assert changed.status_code == 403, changed.text
    assert (await client.get("/client/v1/character")).json()["data"] == original
    async with repository.database.read_session() as session:
        historical = await session.get(CharacterOwnerGrantModel, principal.person_id)
        assert historical.granted_by_principal == issuer
        assert historical.revoked_at.replace(tzinfo=UTC) == revoked


async def test_character_hints_follow_commits_and_stop_after_device_revocation(
    character, monkeypatch
):
    client, repo, soul, settings, principal, _path = character
    views = ClientViews(repo.identity, ClientWorks(repo.identity, {}), character=repo, soul=soul)
    stream = views.stream(principal)

    def decode(frame):
        fields = dict(line.split(": ", 1) for line in frame.strip().splitlines())
        return fields["event"], json.loads(fields["data"])

    kind, initial = decode(await anext(stream))
    assert kind == "sync" and "is_owner" not in initial["character"]
    assert initial["character"]["soul"]["active_revision"] == settings.active_soul_revision
    assert "content" not in json.dumps(initial["character"])
    pending = asyncio.create_task(anext(stream))
    renamed = await client.patch(
        "/client/v1/character",
        json={
            "request_id": str(uuid4()),
            "expected_revision": 1,
            "display_name": "Updated",
        },
    )
    assert renamed.status_code == 200
    kind, changed = decode(await asyncio.wait_for(pending, 2))
    assert kind == "character" and changed["revision"] == 2

    file_written, activate = asyncio.Event(), asyncio.Event()
    apply_soul, sync = soul._activate, views.sync

    async def paused_activation(*args, **kwargs):
        file_written.set()
        await activate.wait()
        return await apply_soul(*args, **kwargs)

    async def written_snapshot(actor, *, active=False):
        await file_written.wait()
        return await sync(actor, active=active)

    monkeypatch.setattr(soul, "_activate", paused_activation)
    monkeypatch.setattr(views, "sync", written_snapshot)
    pending = asyncio.create_task(anext(stream))
    saving = asyncio.create_task(
        client.put(
            "/client/v1/character/soul",
            json={
                "request_id": str(uuid4()),
                "expected_revision": settings.active_soul_revision,
                "content": "private replacement",
            },
        )
    )
    kind, written = decode(await asyncio.wait_for(pending, 2))
    activate.set()
    saved = await saving
    assert saved.status_code == 200
    assert kind == "character" and written["soul"]["revision"] > 0
    assert written["soul"]["active_revision"] == initial["character"]["soul"]["active_revision"]
    assert "private replacement" not in json.dumps(written)
    kind, changed = decode(await asyncio.wait_for(anext(stream), 2))
    assert kind == "character" and changed["soul"]["revision"] > written["soul"]["revision"]
    assert changed["soul"]["active_revision"] == saved.json()["data"]["active_revision"]
    assert "private replacement" not in json.dumps(changed)
    pending = asyncio.create_task(anext(stream))
    await repo.identity.revoke(principal, principal.device_id, str(uuid4()))
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(pending, 2)
    assert (await client.get("/client/v1/character/soul")).status_code == 401
    await stream.aclose()


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


async def test_compare_and_swap_keeps_raw_bytes_and_disabled_person_stops_reads(character):
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

    async with repository.database.read_session() as session:
        audit = " ".join(await session.scalars(select(AdminOperationEventModel.after_json)))
    assert "Raw" not in audit and "{{name}}" not in audit
    from yuki.identity.db_models import CanonicalPersonModel

    async with repository.database.immediate_session() as session:
        person = await session.get(CanonicalPersonModel, principal.source_person_id)
        person.enabled = False
    denied = await client.get("/client/v1/character/soul")
    assert denied.status_code == 401 and body not in denied.text
    assert (await client.get("/client/v1/character")).status_code == 401


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


async def test_missing_selected_soul_keeps_original_request_for_file_recovery(
    character, monkeypatch
):
    from sqlalchemy import select

    from yuki.conversation.canonical_db_models import ControlCommandReceiptModel

    client, repository, _soul, settings, _principal, path = character
    original = (await client.get("/client/v1/character/soul")).json()["data"]
    retained = path.with_suffix(".retained")
    path.rename(retained)
    for variable, name in (
        ("SYSTEM_PROMPT_FILE", "system_prompt.md"),
        ("BOT_PERSONA_FILE", "persona.md"),
    ):
        old = path.parent / name
        old.write_text("Must not use this old source", encoding="utf-8")
        monkeypatch.setenv(variable, str(old))
    request = {
        "request_id": str(uuid4()),
        "expected_revision": original["revision"],
        "content": "Recovered selected SOUL",
    }
    unknown = await client.put("/client/v1/character/soul", json=request)
    assert unknown.status_code == 409 and unknown.json() == {"problem": {"code": "unknown"}}
    assert settings.active_soul_revision == original["active_revision"]
    assert settings.soul == retained.read_bytes().decode("utf-8")
    async with repository.database.read_session() as session:
        receipt = (
            await session.scalars(
                select(ControlCommandReceiptModel).where(
                    ControlCommandReceiptModel.request_id == request["request_id"]
                )
            )
        ).one()
        receipt_id, payload_hash = receipt.id, receipt.payload_hash
        assert receipt.status == "running"
    retained.rename(path)
    recovered = await client.put("/client/v1/character/soul", json=request)
    assert recovered.status_code == 200, recovered.text
    assert path.read_bytes() == b"Recovered selected SOUL"
    assert settings.soul == "Recovered selected SOUL"
    async with repository.database.read_session() as session:
        receipt = (
            await session.scalars(
                select(ControlCommandReceiptModel).where(
                    ControlCommandReceiptModel.request_id == request["request_id"]
                )
            )
        ).one()
        assert receipt.id == receipt_id and receipt.payload_hash == payload_hash
        assert receipt.status == "succeeded"


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
