"""Shared control services assembled from the running Bot's dependencies."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from yuki_participation.autonomy_parameters import AutonomyParameters

from qq_ai_bot.admin.config_files import ConfigFileService
from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.application.control_access import ControlOperatorAccess
from qq_ai_bot.automation.service import AutomationService
from qq_ai_bot.config import Settings
from qq_ai_bot.control_plane.command_service import ControlCommandService
from qq_ai_bot.control_plane.query_service import ControlQueryService
from qq_ai_bot.control_plane.query_types import ComponentHealthView
from qq_ai_bot.conversation.media_service import ConversationMediaService
from qq_ai_bot.execution_trace.recorder import TraceRecorder
from qq_ai_bot.gateway.registry import GatewayConnectionRegistry
from qq_ai_bot.mcp.manager import MCPManager
from qq_ai_bot.memory.embedding.runtime import MemoryEmbeddingRuntime
from qq_ai_bot.memory.maintenance import MemoryMaintenanceWorker
from qq_ai_bot.memory.rebuild.service import MemoryRebuildService
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.model_runtime.profiles import ModelProfileCatalog
from qq_ai_bot.persistence.control_command import ControlCommandAdapter
from qq_ai_bot.persistence.control_query import ControlQueryAdapter
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.plugin_host.manager import PluginManager
from qq_ai_bot.workspace.service import WorkspaceService
from qq_ai_bot.workspace.store import WorkspaceStore


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
        workspace_service: WorkspaceService | None = None,
        rebuild_service: MemoryRebuildService | None = None,
        database: Database,
        runtime_config: RuntimeConfigService,
        connections: GatewayConnectionRegistry,
        mcp: MCPManager,
        automation: AutomationService,
        memories: MemoryFactService,
        maintenance: MemoryMaintenanceWorker,
        embeddings: MemoryEmbeddingRuntime,
        plugins: PluginManager,
        workspace: WorkspaceStore | None = None,
        conversation_media: ConversationMediaService | None = None,
        model_catalog: ModelProfileCatalog | None = None,
        participation_snapshot: Callable[[], Awaitable[dict[str, object]]] | None = None,
        autonomy_parameters: Callable[[], AutonomyParameters] | None = None,
        runtime_health: Callable[[], Awaitable[tuple[ComponentHealthView, ...]]] | None = None,
        trace_recorder: TraceRecorder | None = None,
    ) -> ControlPlaneBundle:
        config_files = ConfigFileService(
            settings, model_catalog, autonomy_parameters=autonomy_parameters
        )
        writer = ControlCommandAdapter(
            database,
            settings=settings,
            config_files=config_files,
            runtime_config=runtime_config,
            mcp_manager=mcp,
            automation=automation,
            memories=memories,
            maintenance=maintenance,
            embeddings=embeddings,
            plugins=plugins,
            workspace_service=workspace_service,
            rebuild_service=rebuild_service,
        )
        return ControlPlaneBundle(
            access=ControlOperatorAccess(database, settings.control_operators_file),
            queries=ControlQueryService(
                ControlQueryAdapter(
                    database,
                    settings=settings,
                    config_files=config_files,
                    runtime_config=runtime_config,
                    mcp_manager=mcp,
                    connection_registry=connections,
                    plugins=plugins,
                    workspace=workspace,
                    workspace_service=workspace_service,
                    automation=automation,
                    conversation_media=conversation_media,
                    model_catalog=model_catalog,
                    participation_snapshot=participation_snapshot,
                    runtime_health=runtime_health,
                    trace_recorder=trace_recorder,
                )
            ),
            commands=ControlCommandService(writer),
            recover_interrupted=writer.recover_interrupted_controls,
        )
