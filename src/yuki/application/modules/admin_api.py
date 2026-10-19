"""Management API assembly from the running application's domain owners."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from yuki.admin.authority.operators import ControlOperatorAccess
from yuki.admin.operations import AdminOperations
from yuki.admin.queries import AdminQueries
from yuki.admin_api.http import AdminApi

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
    from sqlalchemy import select

    from yuki.identity.db_models import PresenceModel

    async with app.database.sessions() as session:
        presences = list(await session.scalars(select(PresenceModel)))
    for platform in ("qq", "app", "acp"):
        configured = [presence for presence in presences if presence.platform == platform]
        enabled_ids = {presence.id for presence in configured if presence.enabled}
        connected = bool(
            enabled_ids.intersection(app.gateway_registry.connected_presence_ids(platform))
        )
        rows.append(
            {
                "name": platform,
                "enabled": bool(enabled_ids) if configured else None,
                "running": connected,
                "healthy": connected if enabled_ids else None,
                "checked_at": checked_at,
            }
        )
    telegram = (
        await app.telegram_gateway.health()
        if app.telegram_gateway is not None
        else {"enabled": False, "running": False, "healthy": None}
    )
    rows.append({"name": "telegram", **telegram, "checked_at": checked_at})
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

        async with app.database.sessions() as session:
            await session.execute(text("SELECT 1"))
        database_healthy = True
    except Exception:
        database_healthy = False
    rows.append(
        {
            "name": "database",
            "enabled": True,
            "running": None,
            "healthy": database_healthy,
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
        ),
        app.client_identity,
        AdminQueries(app.database, app.plugin_manager, files, lambda: runtime_health(app)),
        app.character_avatars,
        app.character_prompts,
    )
