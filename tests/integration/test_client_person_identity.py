"""Actual App HTTP keeps original scopes while current Person ownership merges."""

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select
from tests.support.event_seed import seed_scoped_event
from tests.support.person_merge import merge_persons
from tests.unit.test_admin_api import admin as admin_fixture
from tests.unit.test_app_messages import app_env as app_fixture
from tests.unit.test_app_messages import drain
from tests.unit.test_client_api import bearer, pair, person_and_code
from tests.unit.test_client_files import upload
from tests.unit.test_client_sdk import DropAcknowledgement
from tests.unit.test_client_works import owner, seeded

from yuki.client_api.db_models import ClientDeviceModel
from yuki.client_api.identity import fingerprint
from yuki.client_sdk import Outbox, PersonalClient
from yuki.conversation.canonical_db_models import (
    CanonicalConversationModel,
    ControlCommandReceiptModel,
)
from yuki.identity.db_models import CanonicalPersonModel, IdentityBindingModel
from yuki.identity.person_identity import current_person_id
from yuki.persistence.models import ChatEventModel
from yuki.runtime.work_schema_v1 import journal

admin = admin_fixture
app_env = app_fixture


async def merged_pair(env, database):
    a, person_a = await owner(env, database, "1001")
    b, person_b = await owner(env, database, "1002")
    me_a = (await env.client.get("/client/v1/me", headers=bearer(a["token"]))).json()["data"]
    me_b = (await env.client.get("/client/v1/me", headers=bearer(b["token"]))).json()["data"]
    return a, b, person_a, person_b, me_a, me_b


async def merge(database, source, target):
    async def apply(session):
        return await merge_persons(session, source, target, now=datetime.now(UTC))

    async with database.immediate_session() as _writer:
        assert await apply(_writer) == target


def entry(device, me):
    return {**bearer(device["token"]), "X-Yuki-Entry": me["binding_id"]}


async def test_two_original_apps_replay_and_default_entries_survive_merge(app_env, database):
    env = app_env
    a, b, person_a, person_b, me_a, me_b = await merged_pair(env, database)
    request = str(uuid4())
    body = {"request_id": request, "text": "original request"}
    originals = []
    for device in (a, b):
        sent = await env.client.post(
            "/client/v1/messages", headers=bearer(device["token"]), json=body
        )
        assert sent.status_code == 200, sent.text
        originals.append(sent.json()["data"])
    await drain(env)
    calls = len(env.calls)
    async with database.read_session() as session:
        before = {
            item["event_id"]: (
                (event := await session.get(ChatEventModel, item["event_id"])).author_person_id,
                event.segments_json,
                event.canonical_conversation_id,
            )
            for item in originals
        }
        count = await session.scalar(select(func.count(CanonicalConversationModel.id)))
    await merge(database, person_a, person_b)
    for device, old in ((a, me_a), (b, me_b)):
        me = (await env.client.get("/client/v1/me", headers=bearer(device["token"]))).json()["data"]
        assert me["person_id"] == person_b
        assert me["source_person_id"] == me["request_person_id"] == old["person_id"]
        assert me["binding_id"] == old["binding_id"]
        assert me["conversation_id"] == old["conversation_id"]
        assert {row["binding_id"] for row in me["bindings"] if row["platform"] == "app"} == {
            me_a["binding_id"],
            me_b["binding_id"],
        }
        home = (await env.client.get("/client/v1/home", headers=bearer(device["token"]))).json()[
            "data"
        ]
        assert home["main"]["conversation_id"] == old["conversation_id"]
        assert any(item["conversation_id"] != old["conversation_id"] for item in home["entries"])
    for i, me in enumerate((me_a, me_b)):
        headers = entry(b, me)
        found = await env.client.get(f"/client/v1/requests/{request}", headers=headers)
        assert found.json()["data"]["event_id"] == originals[i]["event_id"]
        replay = await env.client.post("/client/v1/messages", headers=headers, json=body)
        assert replay.json()["data"] == {**originals[i], "replay": True}
        changed = await env.client.post(
            "/client/v1/messages", headers=headers, json={**body, "text": "changed"}
        )
        assert changed.status_code == 409
    assert len(env.calls) == calls
    async with database.read_session() as session:
        assert await session.scalar(select(func.count(CanonicalConversationModel.id))) == count
        for event_id, values in before.items():
            event = await session.get(ChatEventModel, event_id)
            assert (
                event.author_person_id,
                event.segments_json,
                event.canonical_conversation_id,
            ) == values
        for me in (me_a, me_b):
            binding = await session.get(IdentityBindingModel, me["binding_id"])
            assert binding.person_id == me["person_id"]
            assert await current_person_id(session, binding.person_id) == person_b
    _, other_code = await person_and_code(env.client)
    other = await pair(env.client, other_code)
    rejected = await env.client.get("/client/v1/me", headers=entry(other, me_a))
    assert rejected.status_code == 401
    code = await env.client.post("/client/v1/link-codes", headers=entry(b, me_a))
    paired = await pair(env.client, code.json()["data"]["code"])
    retained = (await env.client.get("/client/v1/me", headers=bearer(paired["token"]))).json()[
        "data"
    ]
    assert retained["person_id"] == person_b and retained["request_person_id"] == person_a
    assert retained["conversation_id"] == me_a["conversation_id"]


async def test_same_device_uuid_is_scoped_to_original_app_not_merged_root(app_env, database):
    env = app_env
    a, b, person_a, person_b, me_a, me_b = await merged_pair(env, database)
    request = str(uuid4())
    legacy = await upload(env, a, b"same original bytes", request_id=request)
    assert legacy.status_code == 200, legacy.text
    async with database.immediate_session() as session:
        row = await session.scalar(
            select(ControlCommandReceiptModel).where(
                ControlCommandReceiptModel.principal_id == a["device_id"],
                ControlCommandReceiptModel.request_id == request,
            )
        )
        row.request_person_id = None
        original_hash = row.payload_hash
        original_json = row.effective_state_json
    await merge(database, person_a, person_b)
    second = await env.client.post(
        "/client/v1/uploads",
        params={
            "request_id": request,
            "sha256": hashlib.sha256(b"B bytes").hexdigest(),
            "size": 7,
            "filename": "input.txt",
            "kind": "file",
        },
        content=b"B bytes",
        headers=entry(a, me_b),
    )
    assert second.status_code == 200, second.text
    assert second.json()["data"]["media_id"] != legacy.json()["data"]["media_id"]
    for me, expected in ((me_a, legacy), (me_b, second)):
        found = await env.client.get(f"/client/v1/requests/{request}", headers=entry(b, me))
        assert found.json()["data"]["result"] == {
            key: value for key, value in expected.json()["data"].items() if key != "replay"
        }
        conflict = await env.client.post(
            "/client/v1/messages",
            headers=entry(b, me),
            json={"request_id": request, "text": "same scope different operation"},
        )
        assert conflict.status_code == 409
    replay = await env.client.post(
        "/client/v1/uploads",
        params={
            "request_id": request,
            "sha256": hashlib.sha256(b"same original bytes").hexdigest(),
            "size": 19,
            "filename": "input.txt",
            "kind": "file",
        },
        content=b"unused replay bytes",
        headers=entry(b, me_a),
    )
    assert replay.status_code == 200 and replay.json()["data"]["replay"], replay.text
    async with database.read_session() as session:
        rows = list(
            await session.scalars(
                select(ControlCommandReceiptModel).where(
                    ControlCommandReceiptModel.request_id == request
                )
            )
        )
        assert len(rows) == 2
        original = next(row for row in rows if row.request_person_id is None)
        assert (
            original.payload_hash == original_hash
            and original.effective_state_json == original_json
        )
        assert (
            next(row for row in rows if row.request_person_id == person_b).principal_id
            == a["device_id"]
        )
    assert not env.calls


@pytest.mark.parametrize("state", ["draft", "submitted"])
@pytest.mark.parametrize("credential", ["original", "merged"])
async def test_legacy_outbox_restart_uses_original_entry_without_rewriting_request(
    app_env, database, tmp_path, state, credential
):
    env = app_env
    a, b, person_a, person_b, me_a, me_b = await merged_pair(env, database)
    transport = DropAcknowledgement(env.app)
    async with httpx.AsyncClient(transport=transport) as http:
        box_path = tmp_path / "legacy.sqlite"
        box = Outbox(box_path)
        original = PersonalClient("http://test", a["token"], box, http=http)
        request = await original.prepare_message("saved original body")
        if state == "submitted":
            with pytest.raises(httpx.ReadTimeout):
                await original.submit(request)
            await drain(env)
        before = box.get(request)
        with sqlite3.connect(box_path) as db:
            db.execute("ALTER TABLE owner RENAME TO old_owner")
            db.execute(
                "CREATE TABLE owner(id INTEGER PRIMARY KEY,server TEXT NOT NULL,"
                "person TEXT NOT NULL)"
            )
            db.execute("INSERT INTO owner SELECT id,server,person FROM old_owner")
            db.execute("DROP TABLE old_owner")
        await merge(database, person_a, person_b)
        # Both the original credential and another current same-person device retain A's scope.
        selected = a if credential == "original" else b
        restored = PersonalClient("http://test", selected["token"], Outbox(box_path), http=http)
        if state == "draft":
            with pytest.raises(httpx.ReadTimeout):
                await restored.submit(request)
            await drain(env)
        result = await restored.submit(request)
        assert result["state"] == "result"
        assert transport.posts == 1
        assert restored.outbox.owner() == {
            "server": "http://test",
            "person": person_a,
            "binding_id": me_a["binding_id"],
            "conversation_id": me_a["conversation_id"],
        }
        assert restored.outbox.get(request)["body"] == before["body"]
        assert result["result"]["conversation_id"] == me_a["conversation_id"]
        conflict = PersonalClient(
            "http://test",
            b["token"],
            Outbox(box_path),
            http=http,
            entry_binding_id=me_b["binding_id"],
        )
        with pytest.raises(ValueError, match="outbox_owner_mismatch"):
            await conflict.submit(request)
        assert transport.posts == 1


@pytest.mark.parametrize("legacy_me", [True, False])
async def test_same_process_legacy_me_refreshes_original_upload_target_after_merge(
    app_env, database, tmp_path, legacy_me
):
    env = app_env
    a, _b, person_a, person_b, me_a, _me_b = await merged_pair(env, database)
    source = tmp_path / "source.txt"
    source.write_bytes(b"original upload")
    sdk = PersonalClient("http://test", a["token"], Outbox(tmp_path / "outbox"), http=env.client)
    request = await sdk.prepare_upload(source)
    if legacy_me:
        sdk._me = {"person_id": person_a, "conversation_id": me_a["conversation_id"]}
    await merge(database, person_a, person_b)
    result = await sdk.submit(request)
    assert result["state"] == "result"
    assert sdk.outbox.owner()["person"] == person_a
    if legacy_me:
        assert sdk._me["person_id"] == person_b
    found = await env.client.get(f"/client/v1/requests/{request}", headers=bearer(a["token"]))
    assert found.json()["data"]["payload_hash"] == fingerprint(
        "client.upload",
        person_a,
        {
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "size": source.stat().st_size,
            "filename": source.name,
            "kind": "file",
        },
    )
    assert (await sdk.resolve(request))["state"] == "result"
    assert not env.calls


async def test_expired_upload_receipt_replays_after_real_cleanup_without_new_use(app_env, database):
    env = app_env
    a, b, person_a, person_b, me_a, _me_b = await merged_pair(env, database)
    request = str(uuid4())
    original = await upload(env, a, b"expire real bytes", request_id=request)
    assert original.status_code == 200
    media = original.json()["data"]
    async with database.immediate_session() as session:
        row = await session.scalar(
            select(ControlCommandReceiptModel).where(
                ControlCommandReceiptModel.principal_id == a["device_id"],
                ControlCommandReceiptModel.request_id == request,
            )
        )
        result = json.loads(row.effective_state_json)
        result["expires_at"] = (datetime.now(UTC) - timedelta(hours=25)).timestamp()
        row.effective_state_json = json.dumps(result)
        original_hash = row.payload_hash
    await env.media.cleanup()
    assert not env.files.uploads.path(media["sha256"]).exists()
    await merge(database, person_a, person_b)
    principal = await env.identity.authenticate(b["token"], binding_id=me_a["binding_id"])

    async def unread():
        raise AssertionError("original upload replay read replacement body")
        yield b""

    replay = await env.files.uploads.upload(
        principal,
        request,
        media["sha256"],
        media["size"],
        media["filename"],
        media["kind"],
        unread(),
    )
    assert replay == {**result, "replay": True}
    found = await env.client.get(f"/client/v1/requests/{request}", headers=entry(b, me_a))
    assert found.json()["data"]["payload_hash"] == original_hash
    rejected = await env.client.post(
        "/client/v1/messages",
        headers=entry(b, me_a),
        json={"request_id": str(uuid4()), "text": "new usage", "attachments": [media["media_id"]]},
    )
    assert rejected.status_code == 404, rejected.text
    assert not env.calls and not env.files.uploads.path(media["sha256"]).exists()


async def test_merged_person_can_resume_original_work_without_rewriting_source(app_env, database):
    env = app_env
    a, b, person_a, person_b, me_a, _me_b = await merged_pair(env, database)
    original = await seeded(env, database, group=True, state="waiting_user")
    await merge(database, person_a, person_b)
    for device in (a, b):
        found = await env.client.get(
            f"/client/v1/works/{original.row['id']}", headers=bearer(device["token"])
        )
        assert found.status_code == 200, found.text
    body = {"request_id": str(uuid4()), "text": "same person's answer"}
    sent = await env.client.post(
        f"/client/v1/works/{original.row['id']}/inputs", headers=entry(b, me_a), json=body
    )
    assert sent.status_code == 200, sent.text
    replay = await env.client.post(
        f"/client/v1/works/{original.row['id']}/inputs", headers=bearer(a["token"]), json=body
    )
    assert replay.status_code == 200 and replay.json()["data"]["replay"], replay.text
    found = await env.client.get(
        f"/client/v1/requests/{body['request_id']}", headers=bearer(a["token"])
    )
    assert (
        found.status_code == 200
        and found.json()["data"]["result"]["input_id"] == sent.json()["data"]["input_id"]
    )
    current = await original.repository.get(original.row["id"])
    assert current["source_json"] == original.row["source_json"]
    assert current["model_requests"] == 3 and current["tool_calls"] == 2
    async with database.read_session() as session:
        assert (
            await session.scalar(
                select(journal.c.payload_json).where(journal.c.work_id == current["id"])
            )
            == '{"session":"original-session"}'
        )
    assert not env.calls


async def test_disabled_source_and_revoked_device_are_not_revived_by_merge(app_env, database):
    env = app_env
    a, b, person_a, person_b, _me_a, _me_b = await merged_pair(env, database)
    await merge(database, person_a, person_b)
    async with database.immediate_session() as session:
        (await session.get(CanonicalPersonModel, person_a)).enabled = False
    denied = await env.client.get("/client/v1/me", headers=bearer(a["token"]))
    assert denied.status_code == 401
    assert (await env.client.get("/client/v1/me", headers=bearer(b["token"]))).status_code == 200
    async with database.immediate_session() as session:
        (await session.get(ClientDeviceModel, b["device_id"])).revoked_at = datetime.now(UTC)
    assert (await env.client.get("/client/v1/me", headers=bearer(b["token"]))).status_code == 401


async def test_claim_account_merges_two_apps_without_changing_their_original_credentials(
    app_env, database
):
    env = app_env
    a, b, person_a, person_b, me_a, me_b = await merged_pair(env, database)
    code = await env.identity.issue_for_device(
        await env.identity.authenticate(b["token"]), str(uuid4())
    )
    async with database.read_session() as session:
        from yuki.identity.canonical_repository import find_identity_binding

        original_binding = await find_identity_binding(session, "1001")
        original_id = original_binding.id
    claimed = await env.identity.claim_account(
        code["code"], platform="qq", account="1001", display_name="account name", message_id="claim"
    )
    assert claimed["person_id"] == person_b and claimed["merged"] and not claimed["ended"]
    assert claimed["binding_id"] == original_id
    for device, original in ((a, me_a), (b, me_b)):
        me = (await env.client.get("/client/v1/me", headers=bearer(device["token"]))).json()["data"]
        assert me["person_id"] == person_b and me["source_person_id"] == original["person_id"]
        assert me["binding_id"] == original["binding_id"]
        assert me["conversation_id"] == original["conversation_id"]
    async with database.read_session() as session:
        binding = await session.get(IdentityBindingModel, original_id)
        assert binding.person_id == person_a and binding.status == "active"
    again = await env.identity.issue_for_device(
        await env.identity.authenticate(b["token"]), str(uuid4())
    )
    result = await env.identity.claim_account(
        again["code"],
        platform="qq",
        account="1001",
        display_name="account name",
        message_id="already",
    )
    assert not result["merged"] and not result["ended"] and result["binding_id"] == original_id


async def test_original_work_cancel_receipt_remains_readable_in_source_scope(app_env, database):
    env = app_env
    a, b, person_a, person_b, me_a, _me_b = await merged_pair(env, database)
    original = await seeded(env, database)
    body = {"request_id": str(uuid4()), "expected_revision": original.row["revision"]}
    url = f"/client/v1/works/{original.row['id']}/cancel"
    cancelled = await env.client.post(url, headers=bearer(a["token"]), json=body)
    assert cancelled.status_code == 200, cancelled.text
    after_cancel = await original.repository.get(original.row["id"])
    await merge(database, person_a, person_b)
    replay = await env.client.post(url, headers=entry(b, me_a), json=body)
    assert replay.status_code == 200 and replay.json()["data"] == {
        **cancelled.json()["data"],
        "replay": True,
    }
    assert await original.repository.get(original.row["id"]) == after_cancel
    assert after_cancel["source_json"] == original.row["source_json"]
    assert after_cancel["model_requests"] == 3 and after_cancel["tool_calls"] == 2
    assert not env.calls


async def test_current_root_can_read_original_file_but_foreign_person_cannot(app_env, database):
    from tests.support.workspace_snapshots import snapshot_bytes
    from tests.unit.test_client_files import app_context

    env = app_env
    a, b, person_a, person_b, _me_a, _me_b = await merged_pair(env, database)
    context = await app_context(env, a)
    snapshot = snapshot_bytes(env.store, "original.txt", b"the immutable original file")
    receipt = await env.social.execute(
        "send",
        {
            "artifact_id": snapshot["artifact_id"],
            "attachment_kind": "file",
            "text": "original result",
        },
        context,
    )
    assert receipt["status"] == "succeeded", receipt
    url = f"/client/v1/artifacts/{snapshot['artifact_id']}?version={snapshot['sha256']}"
    denied = await env.client.get(url, headers=bearer(b["token"]))
    assert denied.status_code == 404
    await merge(database, person_a, person_b)
    read = await env.client.get(url, headers=bearer(b["token"]))
    assert read.status_code == 200 and read.content == b"the immutable original file"
    _, code = await person_and_code(env.client)
    foreign = await pair(env.client, code)
    assert (await env.client.get(url, headers=bearer(foreign["token"]))).status_code == 404


def test_cli_exposes_original_entry_binding(monkeypatch):
    import argparse

    from yuki.client_sdk.cli import add_client_parser

    monkeypatch.setenv("YUKI_CLIENT_ENTRY_BINDING", "environment-entry")
    parser = argparse.ArgumentParser()
    add_client_parser(parser.add_subparsers(dest="command"))
    assert parser.parse_args(["client", "me"]).entry_binding == "environment-entry"
    args = parser.parse_args(["client", "--entry-binding", "explicit-entry", "home"])
    assert args.entry_binding == "explicit-entry"


async def test_operator_transfer_pauses_current_root_route_without_moving_history(
    app_env, database
):
    from yuki.conversation.canonical_db_models import PersonActiveRouteModel
    from yuki.identity.canonical_repository import find_identity_binding, find_presence

    env = app_env
    _a, _b, person_a, person_b, _me_a, _me_b = await merged_pair(env, database)
    async with database.immediate_session() as session:
        binding = await find_identity_binding(session, "1001")
        presence = await find_presence(session, "8000")
        binding_id = binding.id
        session.add(
            PersonActiveRouteModel(
                person_id=person_a,
                identity_binding_id=binding_id,
                presence_id=presence.id,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
    from yuki.domain.conversations import ConversationScope

    accepted = await seed_scoped_event(
        env.harness.scoped_events,
        scope=ConversationScope.private("8000", "1001"),
        platform_message_id="original-before-transfer",
        sender_user_id="1001",
        direction="inbound",
        content="original owner",
    )
    original_event = accepted.event.id
    env.identity.router._registry.connect(
        SimpleNamespace(self_id="8000"), provider_id="snowluma", presence_id=presence.id
    )
    code = await env.identity.issue_for_operator(person_b, str(uuid4()), str(uuid4()))
    claimed = await env.identity.claim_account(
        code["code"], platform="qq", account="1001", display_name="", message_id="merge-route"
    )
    assert claimed["merged"] and claimed["person_id"] == person_b
    target, _code = await person_and_code(env.client)
    result = await env.identity.transfer_for_operator(
        binding_id, target, str(uuid4()), str(uuid4())
    )
    assert result["binding_id"] != binding_id and result["person_id"] == target
    async with database.read_session() as session:
        route = await session.get(PersonActiveRouteModel, person_b)
        original = await session.get(IdentityBindingModel, binding_id)
        assert route.paused and route.identity_binding_id == binding_id
        assert original.person_id == person_a and original.status == "ended"
        event = await session.get(ChatEventModel, original_event)
        assert event.author_person_id == person_a and event.content == "original owner"


async def test_disabled_app_does_not_revoke_device_person_or_original_work(app_env, database):
    env = app_env
    a, b, person_a, person_b, me_a, me_b = await merged_pair(env, database)
    body = {"request_id": str(uuid4()), "text": "already accepted"}
    accepted = await env.client.post("/client/v1/messages", headers=bearer(a["token"]), json=body)
    assert accepted.status_code == 200
    await drain(env)
    work = await seeded(env, database)
    work_input = {"request_id": str(uuid4()), "text": "original internal input"}
    input_url = f"/client/v1/works/{work.row['id']}/inputs"
    original_input = await env.client.post(input_url, headers=bearer(a["token"]), json=work_input)
    assert original_input.status_code == 200, original_input.text
    await merge(database, person_a, person_b)
    async with database.immediate_session() as session:
        binding = await session.get(IdentityBindingModel, me_a["binding_id"])
        binding.status = "disabled"
        binding.revision += 1
        events = await session.scalar(select(func.count(ChatEventModel.id)))
    models = len(env.calls)
    headers = entry(b, me_a)
    me = await env.client.get("/client/v1/me", headers=headers)
    assert me.status_code == 200 and me.json()["data"]["person_id"] == person_b, me.text
    original_device = await env.client.get("/client/v1/me", headers=bearer(a["token"]))
    assert original_device.status_code == 200
    found = await env.client.get(f"/client/v1/requests/{body['request_id']}", headers=headers)
    assert (
        found.status_code == 200
        and found.json()["data"]["event_id"] == accepted.json()["data"]["event_id"]
    )
    replay = await env.client.post("/client/v1/messages", headers=headers, json=body)
    assert replay.status_code == 200 and replay.json()["data"]["replay"]
    input_replay = await env.client.post(input_url, headers=headers, json=work_input)
    assert input_replay.status_code == 200 and input_replay.json()["data"]["replay"]
    view = await env.client.get(f"/client/v1/works/{work.row['id']}", headers=headers)
    assert view.status_code == 200
    rejected_input = await env.client.post(
        input_url,
        headers=headers,
        json={"request_id": str(uuid4()), "text": "new input through disabled entry"},
    )
    assert rejected_input.status_code == 401, rejected_input.text
    switched_input = await env.client.post(
        input_url,
        headers=entry(b, me_b),
        json={"request_id": str(uuid4()), "text": "same Work through legal original entry"},
    )
    assert switched_input.status_code == 200, switched_input.text
    current = await work.repository.get(work.row["id"])
    assert current["source_json"] == work.row["source_json"]
    cancelled = await env.client.post(
        f"/client/v1/works/{work.row['id']}/cancel",
        headers=headers,
        json={"request_id": str(uuid4()), "expected_revision": current["revision"]},
    )
    assert cancelled.status_code == 200 and cancelled.json()["data"]["state"] == "cancelled", (
        cancelled.text
    )
    new_message = await env.client.post(
        "/client/v1/messages",
        headers=headers,
        json={"request_id": str(uuid4()), "text": "new platform message"},
    )
    assert new_message.status_code == 401, new_message.text
    await drain(env)
    assert len(env.calls) == models
    async with database.read_session() as session:
        assert await session.scalar(select(func.count(ChatEventModel.id))) == events + 1
        assert (await session.get(ClientDeviceModel, a["device_id"])).revoked_at is None
        assert (await session.get(CanonicalPersonModel, person_a)).enabled
        assert (await session.get(CanonicalPersonModel, person_b)).enabled
