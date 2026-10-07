"""The Docker liveness route must remain responsive during slow diagnostics."""

from __future__ import annotations

import asyncio
from typing import get_origin, get_type_hints

import httpx
import pytest
from fastapi import FastAPI

from qq_ai_bot import main
from qq_ai_bot.health import HealthPayload


def _degraded_health_payload() -> HealthPayload:
    values: dict[str, object] = {}
    for name, annotation in get_type_hints(HealthPayload).items():
        if annotation is bool:
            values[name] = False
        elif annotation is int:
            values[name] = 0
        elif annotation is float:
            values[name] = 0.0
        elif annotation is str:
            values[name] = ""
        elif get_origin(annotation) is dict:
            values[name] = {}
        else:
            values[name] = None
    values.update(status="degraded", database="unavailable")
    return values  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_livez_responds_while_detailed_health_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_health(_container: object) -> HealthPayload:
        entered.set()
        await release.wait()
        return _degraded_health_payload()

    monkeypatch.setattr(main, "get_container", lambda: object())
    monkeypatch.setattr(main, "build_health_payload", slow_health)
    app = FastAPI()
    main.register_health_routes(app)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        pending_health = asyncio.create_task(client.get("/healthz"))
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            live = await asyncio.wait_for(client.get("/livez"), timeout=1)
            assert live.status_code == 200
            assert live.json() == {"status": "alive"}
        finally:
            release.set()
        detailed = await asyncio.wait_for(pending_health, timeout=1)

    assert detailed.status_code == 200
    assert detailed.json()["status"] == "degraded"
    assert detailed.json()["database"] == "unavailable"


@pytest.mark.asyncio
async def test_container_and_health_have_no_speech_dependency(database, tmp_path):
    from tests.conftest import make_settings

    from qq_ai_bot.container import ApplicationContainer
    from qq_ai_bot.health import build_health_payload

    plugin_directory = tmp_path / "plugins"
    plugin_directory.mkdir()
    app = ApplicationContainer(
        make_settings(database.url, plugin_directory=plugin_directory, plugin_system_enabled=False),
        database=database,
    )
    try:
        assert not any("speech" in name for name in app.lifecycle.names)
        assert not hasattr(app, "speech") and not hasattr(app, "voice_preferences")
        payload = await build_health_payload(app)
        assert "asr" in payload
        assert not any(key.startswith("speech") for key in payload)
    finally:
        await app.model_clients.close()
        await app.plugin_http.close()
