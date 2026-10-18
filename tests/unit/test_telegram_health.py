"""Real native lifecycle facts; diagnostics neither call the API nor gate work."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from tests.conftest import make_settings

from yuki.application.lifecycle import LifecycleRegistry
from yuki.gateway.providers import builtin_provider_catalog
from yuki.gateway.providers.telegram import TelegramClient
from yuki.gateway.providers.telegram_ingress import TelegramGateway
from yuki.gateway.registry import GatewayConnectionRegistry
from yuki.health import build_health_payload
from yuki.identity.routing import PresenceRouter


def health_container(database, gateway):
    json_health = SimpleNamespace(model_dump=lambda **_: {})

    def worker():
        return SimpleNamespace(health=AsyncMock(return_value={}))

    return SimpleNamespace(
        database=database,
        settings=make_settings(database.url),
        telegram_gateway=gateway,
        plugin_background_turns=SimpleNamespace(health=AsyncMock(return_value={"running": True})),
        memory_embeddings=SimpleNamespace(
            health=AsyncMock(
                return_value=SimpleNamespace(
                    enabled=False,
                    provider_configured=False,
                    coverage_ratio=0,
                    pending_job_count=0,
                    failed_job_count=0,
                )
            )
        ),
        memory_audit=SimpleNamespace(
            health=AsyncMock(
                return_value=SimpleNamespace(
                    contested_fact_count=0,
                    active_contested_count=0,
                    healthy=True,
                    expired_active_count=0,
                    maintenance_last_success_at=None,
                )
            )
        ),
        persistence=SimpleNamespace(
            memory_rebuilds=SimpleNamespace(health=AsyncMock(return_value=json_health))
        ),
        memory_rebuild_service=SimpleNamespace(active_in_flight_calls=0),
        memory_self_reflection_worker=SimpleNamespace(health=AsyncMock(return_value=json_health)),
        memory_dream_worker=SimpleNamespace(health=AsyncMock(return_value=json_health)),
        asr=SimpleNamespace(health=lambda: {}),
        onebot_connected=lambda: False,
        automation_worker=SimpleNamespace(running=False),
        automation_repository=SimpleNamespace(active_count=AsyncMock(return_value=0)),
        sandbox_completions=worker(),
        sandbox_continuations=worker(),
        work_scheduler=worker(),
        subagent_scheduler=worker(),
        main_agent_contract=SimpleNamespace(health=lambda: {}),
        memory_maintenance_worker=SimpleNamespace(running=False),
        conversation_effect_gate=SimpleNamespace(superseded_rejections=0),
        started_at=time.monotonic(),
    )


async def test_absent_telegram_is_disabled_and_does_not_degrade_health(database):
    payload = await build_health_payload(health_container(database, None))
    assert payload["status"] == "ok"
    assert payload["telegram_gateway"] == {"enabled": False, "running": False, "healthy": None}
    json.dumps(payload)


async def test_native_poll_failure_recovery_and_shutdown_are_visible(database):
    retry_started, release_retry, recovered = (asyncio.Event() for _ in range(3))
    calls = []

    async def http(request):
        method = request.url.path.rsplit("/", 1)[-1]
        calls.append(method)
        if method == "getMe":
            result = {"id": 80001, "is_bot": True, "username": "yuki_health"}
        elif calls.count("getUpdates") == 1:
            return httpx.Response(500, json={"ok": False, "description": "private-token"})
        elif calls.count("getUpdates") == 2:
            retry_started.set()
            await release_retry.wait()
            result = []
        else:
            recovered.set()
            await asyncio.Event().wait()
        return httpx.Response(200, json={"ok": True, "result": result})

    registry = GatewayConnectionRegistry(providers=builtin_provider_catalog())
    async with httpx.AsyncClient(transport=httpx.MockTransport(http)) as transport:
        gateway = TelegramGateway(
            database,
            registry,
            PresenceRouter(database, registry),
            None,
            None,
            TelegramClient("private-token", client=transport),
            poll_interval=0,
        )
        lifecycle = LifecycleRegistry()
        lifecycle.register(
            "telegram_gateway", start=gateway.start, close=gateway.close, health=gateway.health
        )
        container = health_container(database, gateway)
        try:
            await lifecycle.start()
            await asyncio.wait_for(retry_started.wait(), 5)
            before = tuple(calls)
            payload = await build_health_payload(container)
            assert payload["status"] == "degraded"
            assert payload["telegram_gateway"]["running"] is True
            assert payload["telegram_gateway"]["healthy"] is False
            assert payload["telegram_gateway"]["last_error_category"] == "TelegramAPIError"
            assert payload["telegram_gateway"]["last_success_at"]
            assert "private-token" not in json.dumps(payload)
            assert tuple(calls) == before
            assert (await lifecycle.health())["telegram_gateway"] == payload["telegram_gateway"]
            release_retry.set()
            await asyncio.wait_for(recovered.wait(), 5)
            payload = await build_health_payload(container)
            assert payload["status"] == "ok" and payload["telegram_gateway"]["healthy"] is True
            assert payload["telegram_gateway"]["last_error_category"] is None
        finally:
            await lifecycle.close()
        assert (await gateway.health())["running"] is False
        assert (await gateway.health())["healthy"] is False
