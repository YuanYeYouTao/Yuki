"""Management API assembly from the running application's domain owners."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from yuki.admin.authority.operators import ControlOperatorAccess
from yuki.admin.operations import AdminOperations
from yuki.admin.queries import AdminQueries
from yuki.admin_api.http import AdminApi
from yuki.gateway.health import platform_health

if TYPE_CHECKING:
    from yuki.container import ApplicationContainer


async def runtime_health(app: ApplicationContainer) -> list[dict[str, Any]]:
    checked_at = datetime.now(UTC)
    rows: list[dict[str, Any]] = []
    for name, enabled, running in (
        ("work", True, app.work_scheduler.running),
        ("automation", app.settings.automation_enabled, app.automation_worker.running),
        ("plugins", app.plugin_manager.system_enabled, app.plugin_manager.running_count > 0),
        ("gateway", None, app.gateway_registry.has_any_active()),
        (
            "subagents",
            app.database.subagents_enabled,
            app.subagent_scheduler.task is not None and not app.subagent_scheduler.task.done(),
        ),
    ):
        rows.append(
            {
                "name": name,
                "enabled": enabled,
                "running": running,
                "healthy": None,
                "checked_at": checked_at,
            }
        )
    platforms = await platform_health(
        app.database, app.gateway_providers, app.gateway_registry, app.gateway_health_sources
    )
    for platform, facts in platforms.items():
        rows.append(
            {
                "name": platform,
                **facts.summary(),
                "checked_at": checked_at,
            }
        )
    completion = await app.sandbox_completions.health()
    rows.append(
        {
            "name": "persistent_environment",
            "enabled": True,
            "running": None,
            "healthy": None,
            "checked_at": checked_at,
            "completion_receiver_running": completion["running"],
            "error_category": completion["last_error_category"],
        }
    )
    try:
        from sqlalchemy import text

        async with app.database.read_session() as session:
            await session.execute(text("SELECT 1"))
        database_healthy = True
    except Exception:
        database_healthy = False
    rows.append(
        {
            "name": "database",
            "enabled": True,
            "running": None,
            "healthy": database_healthy and app.database.writer_available,
            "read_available": database_healthy,
            "writer_accepting": app.database.writer_available,
            "checked_at": checked_at,
        }
    )
    try:
        memory = (await app.runtime_config.snapshot()).memory.maintenance_enabled
        memory_error = None
    except Exception:
        memory, memory_error = None, "config_unavailable"
    rows.append(
        {
            "name": "memory_maintenance",
            "enabled": memory,
            "running": app.memory_maintenance_worker.running,
            "healthy": None,
            "checked_at": checked_at,
            "error_category": memory_error,
        }
    )
    try:
        semantic = await app.semantic_participation.health()
        rows.append(
            {
                "name": "semantic_participation",
                "enabled": semantic.get("configured"),
                "running": semantic.get("running"),
                "healthy": None,
                "checked_at": checked_at,
                "error_category": "model_config_invalid"
                if semantic.get("model_config_error")
                else None,
            }
        )
    except Exception:
        rows.append(
            {
                "name": "semantic_participation",
                "enabled": None,
                "running": None,
                "healthy": None,
                "checked_at": checked_at,
                "error_category": "health_unavailable",
            }
        )
    return rows


def build_admin_api(app: ApplicationContainer) -> AdminApi:
    files = app.config_files
    return AdminApi(
        ControlOperatorAccess(app.database, app.settings.control_operators_file),
        AdminOperations(
            app.database,
            app.plugin_manager,
            files,
            identities=app.client_identity,
            people=app.people,
            config=app.runtime_config,
            groups=app.groups,
            router=app.presence_router,
        ),
        app.client_identity,
        AdminQueries(app.database, app.plugin_manager, files, lambda: runtime_health(app)),
        app.character,
        app.soul,
    )
