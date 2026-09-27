"""Shared control services assembled from the running Bot's dependencies."""

from __future__ import annotations

from dataclasses import dataclass

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.automation.service import AutomationService
from qq_ai_bot.config import Settings
from qq_ai_bot.control_plane.command_service import ControlCommandService
from qq_ai_bot.control_plane.query_service import ControlQueryService
from qq_ai_bot.gateway.registry import GatewayConnectionRegistry
from qq_ai_bot.mcp.manager import MCPManager
from qq_ai_bot.memory.embedding.runtime import MemoryEmbeddingRuntime
from qq_ai_bot.memory.maintenance import MemoryMaintenanceWorker
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.persistence.control_command import ControlCommandAdapter
from qq_ai_bot.persistence.control_query import ControlQueryAdapter
from qq_ai_bot.persistence.database import Database


@dataclass(frozen=True, slots=True)
class ControlPlaneBundle:
    queries: ControlQueryService
    commands: ControlCommandService


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
    ) -> ControlPlaneBundle:
        return ControlPlaneBundle(
            queries=ControlQueryService(
                ControlQueryAdapter(
                    database,
                    settings=settings,
                    runtime_config=runtime_config,
                    mcp_manager=mcp,
                    connection_registry=connections,
                )
            ),
            commands=ControlCommandService(
                ControlCommandAdapter(
                    database,
                    settings=settings,
                    runtime_config=runtime_config,
                    mcp_manager=mcp,
                    automation=automation,
                    memories=memories,
                    maintenance=maintenance,
                    embeddings=embeddings,
                )
            ),
        )
