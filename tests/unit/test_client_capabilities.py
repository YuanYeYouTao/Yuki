"""Actual plugin approval lifecycle and real Unix Manager protocol projections."""

import asyncio
import json

import httpx
from fastapi import FastAPI
from tests.unit.test_admin_api import admin as admin_fixture
from tests.unit.test_app_messages import app_env as app_fixture
from tests.unit.test_client_api import bearer
from tests.unit.test_client_works import owner
from tests.unit.test_plugin_offline_configuration import manager_for, save, write_configured_plugin

from yuki.client_api.capabilities import ClientCapabilities
from yuki.client_api.http import ClientApi, attach_client_api
from yuki.sandbox.client import SandboxClient

admin = admin_fixture
app_env = app_fixture


async def test_only_approved_enabled_public_fields_and_actual_manifest(app_env, database, tmp_path):
    root = tmp_path / "plugins"
    plugin = root / "test.configured"
    write_configured_plugin(plugin)
    manager, _registry, configuration = manager_for(database, root)
    manifest = {
        "desktop": True,
        "tools": [{"name": "chromium", "version": "actual-fixture-version"}],
    }
    observed = []

    async def rpc(reader, writer):
        request = json.loads(await reader.readline())
        observed.append(request)
        writer.write(json.dumps({"manifest": manifest}).encode() + b"\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    socket = tmp_path / "m.sock"
    server = await asyncio.start_unix_server(rpc, str(socket))
    device, _ = await owner(app_env, database)
    service = ClientCapabilities(app_env.identity, manager, SandboxClient(socket))
    app = FastAPI()
    attach_client_api(app, lambda: ClientApi(app_env.identity, capabilities=service))
    await manager.start()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:

            async def get():
                response = await client.get(
                    "/client/v1/character/capabilities", headers=bearer(device["token"])
                )
                assert response.status_code == 200, response.text
                return response.json()["data"]

            assert (await get())["plugins"] == []
            await manager.approve("test.configured", actor_user_id="operator")
            assert (await get())["plugins"] == []
            await save(configuration, {"endpoint": "configured"})
            await manager.enable("test.configured", actor_user_id="operator")
            data = await get()
            assert data["plugins"] == [
                {
                    "id": "test.configured",
                    "name": "Offline configurable fixture",
                    "description": "Configuration lifecycle fixture",
                }
            ]
            assert data["environment"] == manifest
            assert all(request["method"] == "environment_status" for request in observed)
            assert "endpoint" not in json.dumps(data)
            assert "approved_permissions" not in json.dumps(data)
            write_configured_plugin(plugin, version=2)
            await manager.discover()
            assert (await get())["plugins"] == []
            await manager.disable("test.configured", actor_user_id="operator")
            assert (await get())["plugins"] == []
            manifest = None
            assert (await get())["environment"] == {"desktop": False, "tools": []}
    finally:
        await manager.stop()
        server.close()
        await server.wait_closed()
