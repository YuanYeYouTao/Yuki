"""Shared control services assembled from the running Bot's dependencies."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.application.control_access import ControlOperatorAccess
from qq_ai_bot.automation.service import AutomationService
from qq_ai_bot.config import Settings
from qq_ai_bot.control_plane.command_service import ControlCommandService
from qq_ai_bot.control_plane.query_service import ControlQueryService
from qq_ai_bot.control_plane.query_types import ComponentHealthView
from qq_ai_bot.gateway.registry import GatewayConnectionRegistry
from qq_ai_bot.mcp.manager import MCPManager
from qq_ai_bot.memory.embedding.runtime import MemoryEmbeddingRuntime
from qq_ai_bot.memory.maintenance import MemoryMaintenanceWorker
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.persistence.control_command import ControlCommandAdapter
from qq_ai_bot.persistence.control_query import ControlQueryAdapter
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.plugin_host.manager import PluginManager


@dataclass(frozen=True, slots=True)
class ControlPlaneBundle:
    access: ControlOperatorAccess
    queries: ControlQueryService
    commands: ControlCommandService
    recover_interrupted: Callable[[], Awaitable[int]]


class ControlPlaneModule:
    @staticmethod
    def build(
        *,
        settings: Settings,
        database: Database,
        runtime_config: RuntimeConfigService,
        connections: GatewayConnectionRegistry,
        mcp: MCPManager,
        automation: AutomationService,
        memories: MemoryFactService,
        maintenance: MemoryMaintenanceWorker,
        embeddings: MemoryEmbeddingRuntime,
        plugins: PluginManager,
        runtime_health: Callable[[], Awaitable[tuple[ComponentHealthView, ...]]] | None = None,
    ) -> ControlPlaneBundle:
        writer = ControlCommandAdapter(
            database,
            settings=settings,
            runtime_config=runtime_config,
            mcp_manager=mcp,
            automation=automation,
            memories=memories,
            maintenance=maintenance,
            embeddings=embeddings,
            plugins=plugins,
        )
        return ControlPlaneBundle(
            access=ControlOperatorAccess(database, settings.control_operators_file),
            queries=ControlQueryService(
                ControlQueryAdapter(
                    database,
                    settings=settings,
                    runtime_config=runtime_config,
                    mcp_manager=mcp,
                    connection_registry=connections,
                    plugins=plugins,
                    runtime_health=runtime_health,
                )
            ),
            commands=ControlCommandService(writer),
            recover_interrupted=writer.recover_interrupted_controls,
        )
