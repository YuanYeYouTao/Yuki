"""Automation application module."""

from __future__ import annotations

from dataclasses import dataclass

from qq_ai_bot.admin.audit import AdminAuditService
from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.application.lifecycle import LifecycleRegistry
from qq_ai_bot.automation.executor import AutomationExecutor
from qq_ai_bot.automation.gateway import AutomationGateway, OneBotAutomationGateway
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
from qq_ai_bot.config import Settings
from qq_ai_bot.emoji.repository import EmojiRepository
from qq_ai_bot.emoji.storage import EmojiStorage
from qq_ai_bot.identity.routing import PresenceRouter
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.repositories import (
    EventLedgerRepository,
    RelationshipRepository,
)
from qq_ai_bot.services.main_agent_contract import MainAgentContract
from qq_ai_bot.services.main_agent_turns import MainAgentTurnService
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


class AutomationModule:
    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        main_turns: MainAgentTurnService,
        main_contract: MainAgentContract,
        runtime_config: RuntimeConfigService,
        time_service: TimeContextService,
        ledger: EventLedgerRepository,
        memories: MemoryFactService,
        relationships: RelationshipRepository,
        admin_audit: AdminAuditService,
        web_provider: WebSearchProvider | None,
        emoji_repository: EmojiRepository,
        emoji_storage: EmojiStorage,
        speech: SpeechService,
        presence_router: PresenceRouter,
    ) -> None:
        self._settings = settings
        self._database = database
        self._main_turns = main_turns
        self._main_contract = main_contract
        self._runtime_config = runtime_config
        self._time_service = time_service
        self._ledger = ledger
        self._memories = memories
        self._relationships = relationships
        self._admin_audit = admin_audit
        self._web_provider = web_provider
        self._emoji_repository = emoji_repository
        self._emoji_storage = emoji_storage
        self._speech = speech
        self._presence_router = presence_router

    def build(self) -> AutomationBundle:
        repository = AutomationRepository(self._database)

        def gateway_factory(context: CapabilityExecutionContext) -> AutomationGateway:
            return OneBotAutomationGateway(
                bot_user_id=context.bot_user_id,
                automation_id=context.automation_id,
                automation_run_id=context.automation_run_id,
                router=self._presence_router,
            )

        handlers = AutomationCapabilityHandlers(
            settings=self._settings,
            main_turns=self._main_turns,
            main_contract=self._main_contract,
            runtime_config=self._runtime_config,
            time_service=self._time_service,
            ledger=self._ledger,
            memories=self._memories,
            relationships=self._relationships,
            web_provider=self._web_provider,
            gateway_factory=gateway_factory,
        )
        registry = build_capability_registry(handlers.mapping())
        service = AutomationService(
            settings=self._settings,
            repository=repository,
            registry=registry,
            time_service=self._time_service,
            audit=self._admin_audit,
        )
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
        )

    @staticmethod
    def register_lifecycle(
        bundle: AutomationBundle,
        lifecycle: LifecycleRegistry,
    ) -> None:
        lifecycle.register(
            "automation_worker",
            start=bundle.worker.start,
            close=bundle.worker.close,
        )
