"""HTTP, original receipts, role state and Manager authority reconciliation."""

import asyncio
import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import update
from tests.unit.test_computer_commands import (
    admin as admin_fixture,
)
from tests.unit.test_computer_commands import (
    computer as computer_fixture,
)
from tests.unit.test_computer_commands import (
    personal as personal_fixture,
)
from tests.unit.test_computer_control import run

from yuki.character.repository import CharacterRepository
from yuki.client_api.computer import ClientComputer
from yuki.client_api.http import ClientApi, attach_client_api
from yuki.client_api.views import ClientViews
from yuki.client_api.works import ClientWorks
from yuki.identity.db_models import CanonicalPersonModel
from yuki.persistence.people_repository import PeopleRepository

admin = admin_fixture
personal = personal_fixture
computer = computer_fixture


async def until(service, check):
    queue = service.identity.database.commit_hints.subscribe()
    try:
        async with asyncio.timeout(5):
            while not check():
                await queue.get()
    finally:
        service.identity.database.commit_hints.unsubscribe(queue)


@pytest.fixture
async def desktop_http(computer, monkeypatch):
    # Only the Linux /proc host-memory probe is replaced in this macOS harness.
    monkeypatch.setattr(computer.manager, "available_memory", lambda: 0)
    service = ClientComputer(computer.identity, computer.sandbox)
    works = ClientWorks(computer.identity, {}, computer=service)
    views = ClientViews(computer.identity, works, computer=service)
    api = ClientApi(computer.identity, works=works, views=views, computer=service)
    app = FastAPI()
    attach_client_api(app, lambda: api)
    await service.start()
    await until(service, lambda: service.connected)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield SimpleNamespace(base=computer, client=client, service=service, app=app)
    finally:
        await service.close()


async def test_owner_http_schema_real_control_receipt_and_two_device_transfer(desktop_http):
    env = desktop_http
    actor, device = await env.base.paired()
    second, other = await env.base.paired(person=actor.person_id)
    headers = {"Authorization": "Bearer " + device["token"]}
    assert (await env.client.get("/client/v1/computer")).status_code == 401
    get = await env.client.get("/client/v1/computer", headers=headers)
    assert get.status_code == 200, get.text
    state = get.json()["data"]
    assert state["display"] == "stopped" and state["control"] == {"holder": "none"}
    request = str(uuid4())
    body = {"request_id": request, "action": "take"}
    result = await env.client.post("/client/v1/computer/control", json=body, headers=headers)
    assert result.status_code == 200, result.text
    lease = result.json()["data"]["control"]["lease_id"]
    original = await env.client.get("/client/v1/requests/" + request, headers=headers)
    assert original.json()["data"]["status"] == "succeeded"
    transferred = await env.client.post(
        "/client/v1/computer/control",
        json={"request_id": str(uuid4()), "action": "transfer", "lease_id": lease},
        headers={"Authorization": "Bearer " + other["token"]},
    )
    assert transferred.status_code == 200, transferred.text
    assert transferred.json()["data"]["control"]["device_id"] == second.device_id
    rejected = await env.client.post(
        "/client/v1/computer/control",
        json={**body, "actor": {"device_id": second.device_id}},
        headers=headers,
    )
    assert rejected.status_code == 422
    stale = await env.client.post(
        "/client/v1/computer/control",
        json={"request_id": str(uuid4()), "action": "return", "lease_id": lease},
        headers=headers,
    )
    assert stale.status_code == 409 and stale.json()["problem"]["code"] == "precondition_failed"


async def test_non_owner_sees_only_holder_and_help_boolean_and_cannot_issue_ticket(desktop_http):
    env = desktop_http
    actor, owner = await env.base.paired()
    _, viewer = await env.base.paired(owner=False)
    await env.base.commands.execute(actor, str(uuid4()), "take", {})
    identity = await run(env.base.manager)
    env.base.manager.computer.register(
        {"run_id": identity, "kind": "help", "note": "owner-only secret"}
    )
    await until(
        env.service,
        lambda: env.service.snapshot and len(env.service.snapshot["help_requests"]) == 1,
    )
    headers = {"Authorization": "Bearer " + viewer["token"]}
    denied = await env.client.post("/client/v1/computer/display-tickets", json={}, headers=headers)
    assert denied.status_code == 403
    status = await env.client.get("/client/v1/character/status", headers=headers)
    assert status.status_code == 200, status.text
    assert status.json()["data"]["environment"]["control"] == {"holder": "human"}
    assert status.json()["data"]["environment"]["help_requested"] is True
    assert "owner-only secret" not in status.text and owner["device_id"] not in status.text


@pytest.mark.parametrize(
    "change,reason", [("device", "device_revoked"), ("owner", "owner_revoked")]
)
async def test_revocation_releases_original_lease_once_and_finishes_other_person_wait_as_system(
    desktop_http, change, reason
):
    env = desktop_http
    actor, _ = await env.base.paired()
    lease = (await env.base.commands.execute(actor, str(uuid4()), "take", {}))["control"][
        "lease_id"
    ]
    other_work = str(uuid4())
    waiting = await run(env.base.manager, other_work)
    env.base.manager.computer.register(
        {"run_id": waiting, "kind": "help", "note": "help another person"}
    )
    await until(
        env.service,
        lambda: env.service.snapshot and env.service.snapshot["control"].get("lease_id") == lease,
    )
    if change == "device":
        await env.base.identity.revoke(actor, actor.device_id, str(uuid4()))
    else:
        await CharacterRepository(env.base.identity).set_owner(
            str(uuid4()), actor.person_id, str(uuid4()), granted=False
        )
    await until(env.service, lambda: env.base.manager.computer.control()["holder"] == "none")
    payload = json.loads(
        (env.base.manager.computer.root / "results" / (waiting + ".json")).read_text()
    )
    assert payload["by"] == "system" and payload["reason"] == reason
    assert payload["lease_id"] == lease
    assert env.base.manager.get(waiting)[
        "pending"
    ]  # Original CLI/completion handles run exit, not API.


async def test_http_mutable_file_is_current_not_artifact_and_cursor_is_bound_to_directory(
    desktop_http,
):
    env = desktop_http
    _, device = await env.base.paired()
    root = env.base.manager.files.root
    root.mkdir(exist_ok=True)
    (root / "current.txt").write_bytes(b"first bytes")
    (root / "other.txt").write_bytes(b"other")
    headers = {"Authorization": "Bearer " + device["token"]}
    listed = await env.client.get("/client/v1/computer/files", params={"limit": 1}, headers=headers)
    assert listed.status_code == 200, listed.text
    cursor = listed.json()["data"]["next_cursor"]
    assert cursor
    changed_folder = await env.client.get(
        "/client/v1/computer/files", params={"path": "other", "cursor": cursor}, headers=headers
    )
    assert changed_folder.status_code == 422
    first = await env.client.get(
        "/client/v1/computer/files/content", params={"path": "current.txt"}, headers=headers
    )
    assert (
        first.content == b"first bytes"
        and first.headers["X-Yuki-Sha256"] == hashlib.sha256(first.content).hexdigest()
    )
    (root / "current.txt").write_bytes(b"new current bytes")
    latest = await env.client.get(
        "/client/v1/computer/files/content", params={"path": "current.txt"}, headers=headers
    )
    assert (
        latest.content == b"new current bytes"
        and latest.headers["X-Yuki-Sha256"] != first.headers["X-Yuki-Sha256"]
    )
    assert latest.headers["Cache-Control"] == "no-store"
    assert (
        await env.client.get(
            "/client/v1/computer/files/content", params={"path": "../outside"}, headers=headers
        )
    ).status_code == 404


@pytest.mark.parametrize("entry", ["personal", "admin"])
async def test_forget_releases_other_work_while_person_still_exists_without_writer_lock(
    desktop_http, monkeypatch, entry
):
    env = desktop_http
    actor, _ = await env.base.paired()
    database = env.base.identity.database
    database.sandbox_client = env.base.sandbox
    taken = await env.base.commands.execute(actor, str(uuid4()), "take", {})
    waiting = await run(env.base.manager, str(uuid4()))
    env.base.manager.computer.register({"run_id": waiting, "kind": "help", "note": "other work"})
    observed = []
    original = env.base.sandbox.before_computer_person_forget

    async def before_delete(persons):
        async with database.immediate_session() as writer:
            assert await writer.get(CanonicalPersonModel, actor.person_id) is not None
            await writer.execute(
                update(CanonicalPersonModel)
                .where(CanonicalPersonModel.id == actor.person_id)
                .values(display_name="before deletion")
            )
        observed.append(persons)
        await original(persons)

    monkeypatch.setattr(env.base.sandbox, "before_computer_person_forget", before_delete)
    if entry == "personal":
        assert await PeopleRepository(database).delete_person_id(actor.person_id)
    else:
        deleted = await env.base.admin.post(
            "/admin/v1/persons/" + actor.person_id + "/forget", json={"request_id": str(uuid4())}
        )
        assert deleted.status_code == 200, deleted.text
    assert observed == [(actor.person_id,)]
    assert env.base.manager.computer.control()["holder"] == "none"
    value = json.loads(
        (env.base.manager.computer.root / "results" / (waiting + ".json")).read_text()
    )
    assert value["by"] == "system" and value["reason"] == "person_forgotten"
    assert value["lease_id"] == taken["control"]["lease_id"]
    async with database.sessions() as reader:
        assert await reader.get(CanonicalPersonModel, actor.person_id) is None
    assert env.base.manager.get(waiting)["pending"]


async def test_actual_operator_disable_releases_current_lease(desktop_http):
    env = desktop_http
    actor, _ = await env.base.paired()
    await env.base.commands.execute(actor, str(uuid4()), "take", {})
    async with env.base.identity.database.sessions() as reader:
        person = await reader.get(CanonicalPersonModel, actor.person_id)
        revision = person.revision
    disabled = await env.base.admin.post(
        "/admin/v1/persons/" + actor.person_id + "/disable",
        json={"request_id": str(uuid4()), "expected_revision": revision},
    )
    assert disabled.status_code == 200, disabled.text
    await until(env.service, lambda: env.base.manager.computer.control()["holder"] == "none")


async def test_restart_reconciles_forgotten_person_from_original_manager_lease(desktop_http):
    env = desktop_http
    actor, _ = await env.base.paired()
    lease = (await env.base.commands.execute(actor, str(uuid4()), "take", {}))["control"][
        "lease_id"
    ]
    await env.service.close()
    assert await PeopleRepository(env.base.identity.database).delete_person_id(actor.person_id)
    assert env.base.manager.computer.control()["lease_id"] == lease
    restarted = ClientComputer(env.base.identity, env.base.sandbox)
    await restarted.start()
    try:
        await until(restarted, lambda: env.base.manager.computer.control()["holder"] == "none")
    finally:
        await restarted.close()


async def test_sleep_replay_keeps_original_fact_and_new_sleep_checks_current_agent(desktop_http):
    from yuki.client_api.computer_commands import manager_key

    env = desktop_http
    actor, device = await env.base.paired()
    headers = {"Authorization": "Bearer " + device["token"]}
    request = str(uuid4())
    path = "/client/v1/computer/sleep"
    first = await env.client.post(path, json={"request_id": request}, headers=headers)
    assert first.status_code == 200, first.text
    identity = await run(env.base.manager, str(uuid4()))
    acquired = env.base.manager.computer.set(
        str(uuid4()),
        {
            "action": "agent_acquire",
            "actor": {"run_id": identity},
        },
    )
    assert acquired["control"]["holder"] == "agent"
    replay = await env.client.post(path, json={"request_id": request}, headers=headers)
    assert replay.status_code == 200, replay.text
    assert replay.json()["data"]["display"] == first.json()["data"]["display"]
    direct = await env.base.sandbox.execute(
        "display_sleep",
        {"confirm_active_work_tabs": False},
        request_id=manager_key(actor.request_scope, request),
    )
    assert direct["display"] == "stopped"
    fresh = await env.client.post(path, json={"request_id": str(uuid4())}, headers=headers)
    assert fresh.status_code == 409 and fresh.json()["problem"]["code"] == "precondition_failed"
    assert fresh.json()["problem"]["control"]["holder"] == "agent"
    assert env.base.manager.computer.control()["lease_id"] == acquired["control"]["lease_id"]
