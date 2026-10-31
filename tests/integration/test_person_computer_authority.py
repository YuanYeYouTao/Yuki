"""Current Person revocation closes registered streams and releases the original
real Manager lease. The Manager Unix transport is an explicit Linux boundary.
"""

import asyncio
import json
import os
import tempfile
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, WebSocket
from tests.unit.test_admin_api import admin as admin_fixture
from tests.unit.test_app_messages import app_env as app_fixture
from tests.unit.test_client_works import owner
from tests.unit.test_computer_authority import changed
from tests.unit.test_computer_control import manager_at

from yuki.character.repository import CharacterRepository
from yuki.client_api.computer import ClientComputer
from yuki.client_api.http import ClientApi, attach_client_api
from yuki.client_api.requests import ClientRequests
from yuki.identity.db_models import CanonicalPersonModel
from yuki.sandbox.client import SandboxClient
from yuki.sandbox.computer_keys import release_key

admin = admin_fixture
app_env = app_fixture


@pytest.mark.skipif(os.name != "posix", reason="Real Manager uses a Linux Unix socket")
async def test_superuser_revocation_keeps_original_lease_receipts_and_closes_stream(
    app_env, database, tmp_path
):
    device, person_id = await owner(app_env, database)
    async with database.immediate_session() as session:
        person = await session.get(CanonicalPersonModel, person_id)
        person.is_superuser = True
    character = CharacterRepository(app_env.identity, app_env.harness.settings)
    await character.initialize("Superuser computer")
    manager = manager_at(tmp_path)
    manager.files.root.mkdir()
    (manager.files.root / "original.txt").write_bytes(b"original file")
    short = tempfile.TemporaryDirectory(prefix="yc-superuser-", dir="/tmp")
    socket_path = Path(short.name) / "manager.sock"
    server = await asyncio.start_unix_server(manager.serve, str(socket_path))
    computer = ClientComputer(app_env.identity, SandboxClient(socket_path))
    app = FastAPI()
    attach_client_api(
        app,
        lambda: ClientApi(
            app_env.identity, computer=computer, requests=ClientRequests(app_env.identity)
        ),
    )
    frames = asyncio.Queue()

    async def send(frame):
        await frames.put(frame)

    async def receive():
        return {"type": "websocket.connect"}

    stream = WebSocket({"type": "websocket", "headers": []}, receive, send)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://computer",
            headers={"Authorization": "Bearer " + device["token"]},
        ) as client:
            content = await client.get("/client/v1/computer/files/content?path=original.txt")
            assert content.status_code == 200 and content.content == b"original file"
            request_id = str(uuid4())
            taken = await client.post(
                "/client/v1/computer/control", json={"request_id": request_id, "action": "take"}
            )
            assert taken.status_code == 200, taken.text
            lease_id = taken.json()["data"]["control"]["lease_id"]
            await changed(
                computer,
                lambda: (computer.snapshot or {}).get("control", {}).get("lease_id") == lease_id,
            )
            ticket = await client.post("/client/v1/computer/display-tickets")
            assert ticket.status_code == 200
            principal = await app_env.identity.authenticate(device["token"])
            await stream.accept()
            assert (await frames.get())["type"] == "websocket.accept"
            computer._streams[stream] = principal
            async with database.immediate_session() as session:
                person = await session.get(CanonicalPersonModel, person_id)
                person.is_superuser = False
            await computer._authority_changed()
            assert (await frames.get()) == {"type": "websocket.close", "code": 1008, "reason": ""}
            assert (await client.get("/client/v1/computer")).status_code == 403
            assert (
                await client.get("/client/v1/computer/files/content?path=original.txt")
            ).status_code == 403
            released = manager.computer.request_get(release_key(lease_id))
            assert released["control"] == {"holder": "none"}
            assert released["by"] == "system"
            assert released["reason"] == "superuser_revoked"
            original = await client.get("/client/v1/requests/" + request_id)
            assert original.status_code == 200
            assert original.json()["data"]["found"] is True
            assert original.json()["data"]["result"]["control"]["lease_id"] == lease_id
            await computer._authority_changed()
            assert manager.computer.request_get(release_key(lease_id)) == released
            (tmp_path / "computer-authority-evidence.json").write_text(
                json.dumps(
                    {
                        "person_id": person_id,
                        "device_id": principal.device_id,
                        "request_id": request_id,
                        "lease_id": lease_id,
                        "release_key": release_key(lease_id),
                        "release": released,
                        "stream_close": 1008,
                        "ticket_precedes_revocation": True,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
    finally:
        computer._streams.clear()
        await computer.close()
        server.close()
        await server.wait_closed()
        await manager.close()
        short.cleanup()
