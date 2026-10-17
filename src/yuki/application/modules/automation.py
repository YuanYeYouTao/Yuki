"""Application wiring for schedules and the shared Main Agent."""

from __future__ import annotations

from dataclasses import dataclass

from yuki.admin.audit import AdminAuditService
from yuki.admin.config_service import RuntimeConfigService
from yuki.application.lifecycle import LifecycleRegistry
from yuki.automation.repository import AutomationRepository
from yuki.automation.service import AutomationService
from yuki.automation.tools import AutomationToolService
from yuki.automation.worker import AutomationWorker
from yuki.config import Settings
from yuki.conversation.scope_repository import ConversationScopeRepository
from yuki.persistence.database import Database
from yuki.runtime.work_repository import WorkRepository
from yuki.services.chat import ChatService
from yuki.services.turn_coordinator import ConversationTurnCoordinator
from yuki.time.service import TimeContextService


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
                work_repository=self._works,
                bindings=self._chat.runtime.bindings,
            ),
        )

    @staticmethod
    def register_lifecycle(bundle: AutomationBundle, lifecycle: LifecycleRegistry) -> None:
        lifecycle.register(
            "automation_worker", start=bundle.worker.start, close=bundle.worker.close
        )
