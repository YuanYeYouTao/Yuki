"""Persistence module with an explicit immutable repository bundle."""

from __future__ import annotations

from dataclasses import dataclass

from yuki.admin.config_service import RuntimeConfigService
from yuki.admin.models import WorkStorageRuntimeConfig
from yuki.application.lifecycle import LifecycleRegistry
from yuki.config import Settings
from yuki.conversation.scope_repository import ConversationScopeRepository
from yuki.emoji.repository import EmojiRepository
from yuki.identity.write_settings import configure_identity_write_settings
from yuki.memory.audit import MemoryAuditService
from yuki.memory.context import MemoryContextService
from yuki.memory.fts import SQLiteMemoryFTSIndex
from yuki.memory.metrics import MemoryLifecycleMetrics
from yuki.memory.query import MemoryQueryBuilder
from yuki.memory.rebuild.repository import MemoryRebuildRepository
from yuki.memory.repository import MemoryFactRepository, MemoryJobRepository
from yuki.memory.retrieval import MemoryRetriever
from yuki.memory.service import MemoryFactService
from yuki.memory.targets import MemoryTargetResolver
from yuki.persistence.database import Database
from yuki.persistence.repositories import (
    EmojiDescriptionRepository,
    EventLedgerRepository,
    GroupSettingsRepository,
    MediaAnalysisRepository,
    PeopleRepository,
    PrivateUserSettingsRepository,
    WebSearchSourceRepository,
)
from yuki.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork
from yuki.persistence.turn_observations import RuntimeTurnObservationRepository


@dataclass(frozen=True, slots=True)
class PersistenceBundle:
    database: Database
    runtime_config: RuntimeConfigService
    groups: GroupSettingsRepository
    private_users: PrivateUserSettingsRepository
    people: PeopleRepository
    ledger: EventLedgerRepository
    scoped_events: ScopedEventLedgerUnitOfWork
    conversation_scopes: ConversationScopeRepository
    memories: MemoryFactService
    memory_context: MemoryContextService
    memory_index: SQLiteMemoryFTSIndex
    memory_jobs: MemoryJobRepository
    memory_audit: MemoryAuditService
    memory_metrics: MemoryLifecycleMetrics
    memory_rebuilds: MemoryRebuildRepository
    web_sources: WebSearchSourceRepository
    media_analyses: MediaAnalysisRepository
    emoji_descriptions: EmojiDescriptionRepository
    emoji_repository: EmojiRepository
    turn_observations: RuntimeTurnObservationRepository


class PersistenceModule:
    def __init__(
        self,
        settings: Settings,
        *,
        lifecycle: LifecycleRegistry,
        database: Database | None = None,
        runtime_config: RuntimeConfigService | None = None,
    ) -> None:
        self._settings = settings
        self._lifecycle = lifecycle
        self._database = database
        self._runtime_config = runtime_config

    def build(self) -> PersistenceBundle:
        settings = self._settings
        configure_identity_write_settings(settings)
        database = self._database or Database(settings.database_url)
        self._lifecycle.register("database", close=database.close)
        runtime_config = self._runtime_config or RuntimeConfigService(
            settings=settings,
            database=database,
        )

        async def protocol_storage_policy() -> WorkStorageRuntimeConfig:
            # Physical capacity has one owner: the deployment, never a chat scope.
            return (await runtime_config.snapshot()).work_storage

        database.protocol_storage_policy = protocol_storage_policy
        memory_rebuilds = MemoryRebuildRepository(database)
        people = PeopleRepository(
            database,
            memory_rebuilds=memory_rebuilds,
        )
        memory_repository = MemoryFactRepository(database)
        memory_metrics = MemoryLifecycleMetrics()
        memories = MemoryFactService(memory_repository)
        memory_index = SQLiteMemoryFTSIndex(database)
        memory_context = MemoryContextService(
            query_builder=MemoryQueryBuilder(MemoryTargetResolver(people)),
            retriever=MemoryRetriever(repository=memory_repository, lexical_index=memory_index),
            facts=memories,
        )
        scoped_events = ScopedEventLedgerUnitOfWork(database)
        ledger = EventLedgerRepository(database)
        ledger.set_scoped_writer(scoped_events)
        return PersistenceBundle(
            database=database,
            runtime_config=runtime_config,
            groups=GroupSettingsRepository(database),
            private_users=PrivateUserSettingsRepository(database),
            people=people,
            ledger=ledger,
            scoped_events=scoped_events,
            conversation_scopes=ConversationScopeRepository(database),
            memories=memories,
            memory_context=memory_context,
            memory_index=memory_index,
            memory_jobs=MemoryJobRepository(database),
            memory_audit=MemoryAuditService(memory_repository, metrics=memory_metrics),
            memory_metrics=memory_metrics,
            memory_rebuilds=memory_rebuilds,
            web_sources=WebSearchSourceRepository(database),
            media_analyses=MediaAnalysisRepository(database),
            emoji_descriptions=EmojiDescriptionRepository(database),
            emoji_repository=EmojiRepository(database),
            turn_observations=RuntimeTurnObservationRepository(database),
        )
