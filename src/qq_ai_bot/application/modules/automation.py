"""Application wiring for schedules and the shared Main Agent."""

from __future__ import annotations

from dataclasses import dataclass

from qq_ai_bot.admin.audit import AdminAuditService
from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.application.lifecycle import LifecycleRegistry
from qq_ai_bot.automation.repository import AutomationRepository
from qq_ai_bot.automation.service import AutomationService
from qq_ai_bot.automation.tools import AutomationToolService
from qq_ai_bot.automation.worker import AutomationWorker
from qq_ai_bot.config import Settings
from qq_ai_bot.conversation.rollup.repository import ConversationScopeRepository
from qq_ai_bot.identity.routing import PresenceRouter
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.services.chat import ChatService
from qq_ai_bot.services.turn_coordinator import ConversationTurnCoordinator
from qq_ai_bot.time.service import TimeContextService


@dataclass(frozen=True, slots=True)
class AutomationBundle:
    repository: AutomationRepository
    service: AutomationService
    tools: AutomationToolService
    worker: AutomationWorker


class AutomationModule:
    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        chat: ChatService,
        runtime_config: RuntimeConfigService,
        time_service: TimeContextService,
        admin_audit: AdminAuditService,
        presence_router: PresenceRouter,
        conversation_scopes: ConversationScopeRepository,
        turns: ConversationTurnCoordinator,
        work_repository: WorkRepository,
    ) -> None:
        self._settings = settings
        self._database = database
        self._chat = chat
        self._config = runtime_config
        self._time = time_service
        self._audit = admin_audit
        self._router = presence_router
        self._scopes = conversation_scopes
        self._turns = turns
        self._works = work_repository

    def build(self) -> AutomationBundle:
        repository = AutomationRepository(self._database)
        service = AutomationService(
            settings=self._settings,
            repository=repository,
            time_service=self._time,
            audit=self._audit,
        )
        return AutomationBundle(
            repository,
            service,
            AutomationToolService(service),
            AutomationWorker(
                settings=self._settings,
                repository=repository,
                time_service=self._time,
                generate=self._chat.generate_origin_turn,
                runtime_config=self._config,
                conversation_scopes=self._scopes,
                turns=self._turns,
                router=self._router,
                work_repository=self._works,
                bindings=self._chat.runtime.bindings,
            ),
        )

    @staticmethod
    def register_lifecycle(bundle: AutomationBundle, lifecycle: LifecycleRegistry) -> None:
        lifecycle.register(
            "automation_worker", start=bundle.worker.start, close=bundle.worker.close
        )
