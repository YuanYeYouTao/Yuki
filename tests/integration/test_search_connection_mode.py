"""A real admin HTTP save and a fresh application share one explicit WebMode."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from tests.conftest import make_settings
from tests.unit.test_admin_api import TOKEN, command
from tests.unit.test_admin_api import admin as admin

from yuki.admin_api.http import attach_admin_api
from yuki.application.lifecycle import LifecycleRegistry
from yuki.container import ApplicationContainer
from yuki.model_runtime.models import ModelSearchMode
from yuki.web.models import WebMode


@pytest.fixture
async def search_admin(admin, database, tmp_path, legacy_mode):
    _, api, _, _, profiles = admin
    settings = make_settings(
        database.url,
        model_profiles_file=profiles,
        web_mode=WebMode(legacy_mode),
        memory_embedding_enabled=False,
        vision_enabled=False,
        asr_enabled=False,
        plugin_system_enabled=False,
        plugin_directory=tmp_path / "plugins-disabled",
        workspace_directory=tmp_path / "workspace",
        conversation_media_cache_directory=tmp_path / "media",
        social_transfer_directory=tmp_path / "transfer",
        web_search_bridge_state_path=tmp_path / "search.sqlite3",
    )
    api.commands.model.files._settings = settings
    app = FastAPI()
    attach_admin_api(app, lambda: api)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    with config.bind_socket() as listener:
        server = uvicorn.Server(config)
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            while not server.started:
                if serving.done():
                    await serving
                    raise AssertionError("isolated HTTP server exited before startup")
                await asyncio.sleep(0.01)
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{listener.getsockname()[1]}",
                headers={"Authorization": f"Bearer {TOKEN}"},
                trust_env=False,
            ) as client:
                yield client, settings
        finally:
            server.should_exit = True
            await serving


def evidence(name, value):
    path = Path("test-results/search-connection-mode")
    path.mkdir(parents=True, exist_ok=True)
    (path / f"{name}.json").write_text(json.dumps(value, indent=2), encoding="utf-8")


@pytest.mark.parametrize("mode", [None, "native", "tavily", "both", "disabled"])
@pytest.mark.parametrize("legacy_mode", ["disabled", "native"])
@pytest.mark.parametrize("factory", ["constructor", "create"])
async def test_explicit_mode_real_http_save_then_fresh_composition(
    search_admin, database, mode, legacy_mode, factory, monkeypatch
):
    client, settings = search_admin
    before = (await client.get("/admin/v1/models")).json()["data"]
    document = before["document"]
    document["search_backend"] = "tavily" if mode in {None, "tavily", "both"} else "model"
    document["search"] = {"tavily_api_key_env": "TEST_SEARCH_KEY"}
    if mode is not None:
        document["search"]["mode"] = mode
    document["profiles"]["main"].update(
        provider="gemini",
        protocol="gemini",
        base_url="http://127.0.0.1:9/v1beta",
        api_key_env="TEST_SEARCH_KEY",
        model="explicit-test-model",
    )
    response = await client.post(
        "/admin/v1/models/apply",
        json=command(
            before["revision"],
            document=document,
            api_keys={"TEST_SEARCH_KEY": "synthetic-search-only"},
        ),
    )
    assert response.status_code == 200, response.text
    saved = (await client.get("/admin/v1/models")).json()["data"]
    assert saved["apply_mode"] == "restart"
    assert "synthetic-search-only" not in json.dumps(saved)
    requests = []

    async def no_upstream(_client, request, **_kwargs):
        requests.append({"method": request.method, "path": request.url.path})
        raise AssertionError("composition must not make an upstream request")

    monkeypatch.setattr(httpx.AsyncClient, "send", no_upstream)
    app = (
        await ApplicationContainer.create(settings)
        if factory == "create"
        else ApplicationContainer(settings, database=database)
    )
    try:
        await app.runtime_config.initialize()
        runtime = await app.runtime_config.snapshot()
        expected = mode or legacy_mode
        report = {
            "saved_mode": saved["document"]["search"].get("mode"),
            "legacy_mode": settings.web.mode.value,
            "runtime_mode": runtime.web.mode,
            "provider_available": app.web_provider is not None,
            "tool_catalog_enabled": app.agent_tools._web_catalog_enabled(),
            "http_status": response.status_code,
            "apply_mode": saved["apply_mode"],
            "composition": factory,
            "upstream_requests": requests,
        }
        evidence(f"{factory}-{legacy_mode}-{mode or 'legacy-absent'}", report)
        assert runtime.web.mode == expected
        assert (app.web_provider is not None) == (expected != "disabled")
        assert app.agent_tools._web_catalog_enabled() == (expected != "disabled")
        assert requests == []
        if mode == "native":
            assert app.model_profiles.profiles["main"].search_mode is ModelSearchMode.BRIDGE
    finally:
        # No background lifecycle starts or search/upstream request is needed to
        # observe the constructed runtime and tool contract after restart.
        errors = await LifecycleRegistry._close_entries(app.lifecycle._entries)
        assert not errors
