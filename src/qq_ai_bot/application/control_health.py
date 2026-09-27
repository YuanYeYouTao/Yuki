"""Observe existing runtime components; being alive does not assert semantic health."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from qq_ai_bot.control_plane.query_types import ComponentHealthView

if TYPE_CHECKING:
    from qq_ai_bot.container import ApplicationContainer


async def control_runtime_health(app: ApplicationContainer) -> tuple[ComponentHealthView, ...]:
    stamp = datetime.now(UTC)
    try:
        maintenance_enabled = (await app.runtime_config.snapshot()).memory.maintenance_enabled
        maintenance_error = None
    except Exception:
        maintenance_enabled = None
        maintenance_error = "config_unavailable"
    components = [
        ComponentHealthView(
            "automation",
            app.settings.automation_enabled,
            app.automation_worker.running,
            None,
            stamp,
        ),
        ComponentHealthView(
            "work",
            True,
            app.work_scheduler.running,
            None,
            stamp,
        ),
        ComponentHealthView(
            "subagents",
            app.database.subagents_enabled,
            app.subagent_scheduler.task is not None and not app.subagent_scheduler.task.done(),
            None,
            stamp,
        ),
        ComponentHealthView(
            "plugins",
            app.plugin_manager.system_enabled,
            app.plugin_manager.running_count > 0,
            None,
            stamp,
        ),
        ComponentHealthView(
            "mcp",
            app.settings.mcp_enabled,
            app.mcp_manager.health().connected_servers > 0,
            None,
            stamp,
        ),
        ComponentHealthView("gateway", None, app.onebot_connected(), None, stamp),
        ComponentHealthView(
            "memory_maintenance",
            maintenance_enabled,
            app.memory_maintenance_worker.running,
            None,
            stamp,
            maintenance_error,
        ),
        ComponentHealthView(
            "rollup",
            app.settings.conversation_rollup_enabled,
            app.conversation_rollup_worker.running,
            None,
            stamp,
        ),
    ]
    try:
        semantic = await app.semantic_participation.health()
        configured = semantic.get("configured")
        running = semantic.get("running")
        components.append(
            ComponentHealthView(
                "semantic_participation",
                configured if type(configured) is bool else None,
                running if type(running) is bool else None,
                None,
                datetime.now(UTC),
                "model_config_invalid" if semantic.get("model_config_error") else None,
            )
        )
    except Exception:
        components.append(
            ComponentHealthView(
                "semantic_participation", None, None, None, datetime.now(UTC), "health_unavailable"
            )
        )
    return tuple(components)
