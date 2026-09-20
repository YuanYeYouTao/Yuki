"""Automation application module."""

from __future__ import annotations

from dataclasses import dataclass

from qq_ai_bot.admin.action_service import AdminActionService
from qq_ai_bot.admin.audit import AdminAuditService
from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.application.lifecycle import LifecycleRegistry
from qq_ai_bot.automation.executor import AutomationExecutor
from qq_ai_bot.automation.gateway import OneBotProactiveGateway, ProactiveGateway
from qq_ai_bot.automation.handlers import AutomationCapabilityHandlers
from qq_ai_bot.automation.registry import (
    AutomationCapabilityRegistry,
    CapabilityExecutionContext,
    build_capability_registry,
)
from qq_ai_bot.automation.repository import AutomationRepository
from qq_ai_bot.automation.service import AutomationService
from qq_ai_bot.automation.tools import AutomationToolService
from qq_ai_bot.automation.worker import AutomationWorker
from qq_ai_bot.capabilities.results import ToolArtifactWriter, ToolResultBudgeter
from qq_ai_bot.config import Settings
from qq_ai_bot.emoji.repository import EmojiRepository
from qq_ai_bot.emoji.selector import EmojiSelector
from qq_ai_bot.emoji.storage import EmojiStorage
from qq_ai_bot.identity.routing import PresenceRouter
from qq_ai_bot.mcp.automation import MCPAutomationBridge
from qq_ai_bot.mcp.manager import MCPManager
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.model_runtime.executor import ModelExecutor
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.repositories import (
    AgentActionRepository,
    EventLedgerRepository,
    RelationshipRepository,
)
from qq_ai_bot.services.concurrency import ConcurrencyManager
from qq_ai_bot.speech.service import SpeechService
from qq_ai_bot.time.service import TimeContextService
from qq_ai_bot.web.base import WebSearchProvider


@dataclass(frozen=True, slots=True)
class AutomationBundle:
    repository: AutomationRepository
    handlers: AutomationCapabilityHandlers
    registry: AutomationCapabilityRegistry
    service: AutomationService
    tools: AutomationToolService
    executor: AutomationExecutor
    worker: AutomationWorker
    mcp_bridge: MCPAutomationBridge


class AutomationModule:
    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        models: ModelExecutor,
        concurrency: ConcurrencyManager,
        runtime_config: RuntimeConfigService,
        time_service: TimeContextService,
        ledger: EventLedgerRepository,
        memories: MemoryFactService,
        relationships: RelationshipRepository,
        admin_actions: AdminActionService,
        admin_audit: AdminAuditService,
        agent_actions: AgentActionRepository,
        web_provider: WebSearchProvider | None,
        emoji_repository: EmojiRepository,
        emoji_selector: EmojiSelector,
        emoji_storage: EmojiStorage,
        speech: SpeechService,
        mcp_manager: MCPManager,
        mcp_artifacts: ToolArtifactWriter,
        presence_router: PresenceRouter,
    ) -> None:
        self._settings = settings
        self._database = database
        self._models = models
        self._concurrency = concurrency
        self._runtime_config = runtime_config
        self._time_service = time_service
        self._ledger = ledger
        self._memories = memories
        self._relationships = relationships
        self._admin_actions = admin_actions
        self._admin_audit = admin_audit
        self._agent_actions = agent_actions
        self._web_provider = web_provider
        self._emoji_repository = emoji_repository
        self._emoji_selector = emoji_selector
        self._emoji_storage = emoji_storage
        self._speech = speech
        self._mcp_manager = mcp_manager
        self._mcp_artifacts = mcp_artifacts
        self._presence_router = presence_router

    def build(self) -> AutomationBundle:
        repository = AutomationRepository(self._database)

        def gateway_factory(context: CapabilityExecutionContext) -> ProactiveGateway:
            return OneBotProactiveGateway(
                bot_user_id=context.bot_user_id,
                creator_user_id=context.creator_user_id,
                automation_id=context.automation_id,
                automation_run_id=context.automation_run_id,
                ledger=self._ledger,
                actions=self._agent_actions,
                router=self._presence_router,
                target_person_id=context.canonical_target_person_id,
                target_space_id=context.canonical_target_space_id,
            )

        handlers = AutomationCapabilityHandlers(
            settings=self._settings,
            model_executor=self._models,
            concurrency=self._concurrency,
            runtime_config=self._runtime_config,
            time_service=self._time_service,
            ledger=self._ledger,
            memories=self._memories,
            relationships=self._relationships,
            web_provider=self._web_provider,
            gateway_factory=gateway_factory,
            emoji_repository=self._emoji_repository,
            emoji_selector=self._emoji_selector,
            emoji_storage=self._emoji_storage,
            speech=self._speech,
        )
        registry = build_capability_registry(handlers.mapping())
        handlers.bind_registry(registry)
        mcp_bridge = MCPAutomationBridge(
            manager=self._mcp_manager,
            registry=registry,
            result_budgeter=ToolResultBudgeter(
                max_characters=(
                    self._settings.mcp_result_token_budget * 4
                    if self._settings.mcp_result_token_budget is not None
                    else self._settings.agent_tool_result_max_characters
                ),
                artifacts=self._mcp_artifacts,
                artifact_retention_seconds=self._settings.mcp_artifact_retention_seconds,
            ),
        )
        service = AutomationService(
            settings=self._settings,
            repository=repository,
            registry=registry,
            time_service=self._time_service,
            audit=self._admin_audit,
        )
        handlers.bind_automation_service(service)
        tools = AutomationToolService(service)
        executor = AutomationExecutor(
            settings=self._settings,
            registry=registry,
            repository=repository,
            time_service=self._time_service,
            gateway_factory=gateway_factory,
            router=self._presence_router,
        )
        worker = AutomationWorker(
            settings=self._settings,
            repository=repository,
            executor=executor,
            time_service=self._time_service,
        )
        return AutomationBundle(
            repository,
            handlers,
            registry,
            service,
            tools,
            executor,
            worker,
            mcp_bridge,
        )

    @staticmethod
    def register_lifecycle(
        bundle: AutomationBundle,
        lifecycle: LifecycleRegistry,
    ) -> None:
        lifecycle.register(
            "mcp_automation_bridge",
            start=bundle.mcp_bridge.start,
            close=bundle.mcp_bridge.close,
            health=bundle.mcp_bridge.health,
        )
        lifecycle.register(
            "automation_worker",
            start=bundle.worker.start,
            close=bundle.worker.close,
        )
