"""Safe health response construction."""

from __future__ import annotations

import time
from typing import TypedDict

from yuki import __version__
from yuki.container import ApplicationContainer
from yuki.gateway.health import platform_health
from yuki.model_runtime import ModelTask


class HealthPayload(TypedDict):
    """Public, credential-free health response."""

    status: str
    version: str
    database: str
    database_read: str
    database_writer: str
    sqlite_diagnostics: dict[str, object]
    llm_configured: bool
    web_configured: bool
    vision_configured: bool
    asr: dict[str, object]
    onebot_connected: bool
    telegram_gateway: dict[str, object]
    gateways: dict[str, dict[str, object]]
    automation_enabled: bool
    automation_worker_running: bool
    plugin_system_enabled: bool
    plugin_running_count: int
    plugin_background_turns: dict[str, object]
    sandbox_completions: dict[str, object]
    sandbox_continuations: dict[str, object]
    runtime_work: dict[str, object]
    subagents: dict[str, object]
    main_agent_manifest: dict[str, object]
    memory_embedding_enabled: bool
    memory_embedding_configured: bool
    memory_maintenance_running: bool
    memory_maintenance_last_success_at: str | None
    memory_self_reflection: dict[str, object]
    conversation_generation_superseded_effects: int
    uptime_seconds: int


async def build_health_payload(container: ApplicationContainer) -> HealthPayload:
    """Check dependencies without probing or billing the LLM provider."""

    database_ok = await container.database.ping()
    plugin_manager = getattr(container, "plugin_manager", None)
    plugin_running_count = int(getattr(plugin_manager, "running_count", 0))
    background_health = await container.plugin_background_turns.health()
    maintenance_last_success_at = container.memory_metrics.maintenance_last_success_at
    gateways = await platform_health(
        container.database,
        container.gateway_providers,
        container.gateway_registry,
        container.gateway_health_sources,
    )
    telegram = gateways.get("telegram")
    telegram_health: dict[str, object] = (
        telegram.owner
        if telegram is not None and telegram.owner is not None
        else {"enabled": False, "running": False, "healthy": None}
    )
    return HealthPayload(
        status="ok"
        if database_ok
        and container.database.writer_available
        and background_health["running"]
        and all(
            not item.owner["enabled"] or item.owner["healthy"]
            for item in gateways.values()
            if item.owner is not None
        )
        else "degraded",
        version=__version__,
        database="ok" if database_ok and container.database.writer_available else "unavailable",
        database_read="ok" if database_ok else "unavailable",
        database_writer="accepting" if container.database.writer_available else "stopped",
        sqlite_diagnostics=(
            container.database.sqlite_diagnostics.snapshot()
            if container.database.sqlite_diagnostics is not None
            else {"available": False}
        ),
        llm_configured=container.models.configured(ModelTask.CHAT_AGENT),
        web_configured=container.settings.web_configured,
        vision_configured=container.settings.vision_configured,
        asr=container.asr.health(),
        onebot_connected=gateways["qq"].connected if "qq" in gateways else False,
        telegram_gateway=telegram_health,
        gateways={platform: facts.summary() for platform, facts in gateways.items()},
        automation_enabled=container.settings.automation_enabled,
        automation_worker_running=container.automation_worker.running,
        plugin_system_enabled=container.settings.plugin_system_enabled,
        plugin_running_count=plugin_running_count,
        plugin_background_turns=background_health,
        sandbox_completions=await container.sandbox_completions.health(),
        sandbox_continuations=await container.sandbox_continuations.health(),
        runtime_work=container.work_scheduler.runtime_status(),
        subagents=await container.subagent_scheduler.health(),
        main_agent_manifest=container.main_agent_contract.health(),
        memory_embedding_enabled=container.memory_embeddings.enabled,
        memory_embedding_configured=container.memory_embeddings.provider is not None,
        memory_maintenance_running=container.memory_maintenance_worker.running,
        memory_maintenance_last_success_at=(
            maintenance_last_success_at.isoformat() if maintenance_last_success_at else None
        ),
        memory_self_reflection={
            "enabled": container.settings.memory_self_reflection_enabled,
            "running": container.memory_self_reflection_worker.running,
        },
        conversation_generation_superseded_effects=(
            container.conversation_effect_gate.superseded_rejections
        ),
        uptime_seconds=max(0, int(time.monotonic() - container.started_at)),
    )
