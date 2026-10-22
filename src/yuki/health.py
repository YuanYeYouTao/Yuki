"""Safe health response construction."""

from __future__ import annotations

import time
from typing import TypedDict

from yuki import __version__
from yuki.container import ApplicationContainer


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
    automation_enabled: bool
    automation_worker_running: bool
    active_automation_count: int
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
    memory_embedding_coverage: float
    memory_embedding_pending_jobs: int
    memory_embedding_failed_jobs: int
    memory_maintenance_running: bool
    memory_contested_facts: int
    memory_active_contested_facts: int
    memory_consistency_healthy: bool
    memory_expired_active_facts: int
    memory_maintenance_last_success_at: str | None
    memory_rebuild: dict[str, object]
    memory_self_reflection: dict[str, object]
    memory_dream: dict[str, object]
    conversation_generation_superseded_effects: int
    uptime_seconds: int


async def build_health_payload(container: ApplicationContainer) -> HealthPayload:
    """Check dependencies without probing or billing the LLM provider."""

    database_ok = await container.database.ping()
    plugin_manager = getattr(container, "plugin_manager", None)
    plugin_running_count = int(getattr(plugin_manager, "running_count", 0))
    background_health = await container.plugin_background_turns.health()
    embedding_health = await container.memory_embeddings.health()
    memory_health = await container.memory_audit.health()
    rebuild_health = await container.persistence.memory_rebuilds.health(
        enabled=container.settings.memory_rebuild_enabled,
        active_in_flight_calls=container.memory_rebuild_service.active_in_flight_calls,
    )
    self_reflection_health = await container.memory_self_reflection_worker.health()
    dream_health = await container.memory_dream_worker.health()
    telegram_health: dict[str, object] = (
        await container.telegram_gateway.health()
        if container.telegram_gateway is not None
        else {"enabled": False, "running": False, "healthy": None}
    )
    return HealthPayload(
        status="ok"
        if database_ok
        and container.database.writer_available
        and background_health["running"]
        and (not telegram_health["enabled"] or telegram_health["healthy"])
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
        llm_configured=container.settings.llm_configured,
        web_configured=container.settings.web_configured,
        vision_configured=container.settings.vision_configured,
        asr=container.asr.health(),
        onebot_connected=container.onebot_connected(),
        telegram_gateway=telegram_health,
        automation_enabled=container.settings.automation_enabled,
        automation_worker_running=container.automation_worker.running,
        active_automation_count=await container.automation_repository.active_count(),
        plugin_system_enabled=container.settings.plugin_system_enabled,
        plugin_running_count=plugin_running_count,
        plugin_background_turns=background_health,
        sandbox_completions=await container.sandbox_completions.health(),
        sandbox_continuations=await container.sandbox_continuations.health(),
        runtime_work=await container.work_scheduler.health(),
        subagents=await container.subagent_scheduler.health(),
        main_agent_manifest=container.main_agent_contract.health(),
        memory_embedding_enabled=embedding_health.enabled,
        memory_embedding_configured=embedding_health.provider_configured,
        memory_embedding_coverage=embedding_health.coverage_ratio,
        memory_embedding_pending_jobs=embedding_health.pending_job_count,
        memory_embedding_failed_jobs=embedding_health.failed_job_count,
        memory_maintenance_running=container.memory_maintenance_worker.running,
        memory_contested_facts=memory_health.contested_fact_count,
        memory_active_contested_facts=memory_health.active_contested_count,
        memory_consistency_healthy=memory_health.healthy,
        memory_expired_active_facts=memory_health.expired_active_count,
        memory_maintenance_last_success_at=(
            memory_health.maintenance_last_success_at.isoformat()
            if memory_health.maintenance_last_success_at
            else None
        ),
        memory_rebuild=rebuild_health.model_dump(mode="json"),
        memory_self_reflection=self_reflection_health.model_dump(mode="json"),
        memory_dream=dream_health.model_dump(mode="json"),
        conversation_generation_superseded_effects=(
            container.conversation_effect_gate.superseded_rejections
        ),
        uptime_seconds=max(0, int(time.monotonic() - container.started_at)),
    )
