"""Administrator business-service application module."""

from __future__ import annotations

from dataclasses import dataclass

from yuki.admin.audit import AdminAuditService
from yuki.admin.config_service import RuntimeConfigService
from yuki.config import Settings
from yuki.emoji.admin import EmojiAdminService
from yuki.emoji.collector import EmojiCollector
from yuki.emoji.lifecycle import EmojiLifecycleService
from yuki.emoji.repository import EmojiRepository
from yuki.emoji.storage import EmojiStorage
from yuki.emoji.worker import EmojiWorker
from yuki.memory.audit import MemoryAuditService
from yuki.memory.context import MemoryContextService
from yuki.memory.dream.worker import DreamWorker
from yuki.memory.embedding.runtime import MemoryEmbeddingRuntime
from yuki.memory.fts import SQLiteMemoryFTSIndex
from yuki.memory.maintenance import MemoryMaintenanceWorker
from yuki.memory.mutation.service import MemoryMutationService
from yuki.memory.self_reflection.worker import SelfReflectionWorker
from yuki.memory.service import MemoryFactService
from yuki.persistence.database import Database
from yuki.persistence.repositories import (
    EventLedgerRepository,
    GroupSettingsRepository,
)
from yuki.services.admin.config_admin import ConfigAdminService
from yuki.services.admin.group_admin import GroupAdminService
from yuki.services.admin.memory_admin import MemoryAdminService
from yuki.services.admin.preference_admin import PreferenceAdminService


@dataclass(frozen=True, slots=True)
class AdminBundle:
    audit: AdminAuditService
    memories: MemoryAdminService
    preferences: PreferenceAdminService
    groups: GroupAdminService
    config: ConfigAdminService
    emoji: EmojiAdminService


class AdminModule:
    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        runtime_config: RuntimeConfigService,
        memories: MemoryFactService,
        memory_context: MemoryContextService,
        memory_index: SQLiteMemoryFTSIndex,
        memory_embeddings: MemoryEmbeddingRuntime,
        memory_audit: MemoryAuditService,
        memory_maintenance: MemoryMaintenanceWorker,
        groups: GroupSettingsRepository,
        emoji_repository: EmojiRepository,
        emoji_lifecycle: EmojiLifecycleService,
        emoji_storage: EmojiStorage,
        emoji_collector: EmojiCollector,
        emoji_worker: EmojiWorker | None,
        memory_mutations: MemoryMutationService,
        ledger: EventLedgerRepository,
        memory_self_reflection: SelfReflectionWorker,
        memory_dream: DreamWorker,
    ) -> None:
        self._settings = settings
        self._database = database
        self._runtime_config = runtime_config
        self._memories = memories
        self._memory_context = memory_context
        self._memory_index = memory_index
        self._memory_embeddings = memory_embeddings
        self._memory_audit = memory_audit
        self._memory_maintenance = memory_maintenance
        self._groups = groups
        self._emoji_repository = emoji_repository
        self._emoji_lifecycle = emoji_lifecycle
        self._emoji_storage = emoji_storage
        self._emoji_collector = emoji_collector
        self._emoji_worker = emoji_worker
        self._memory_mutations = memory_mutations
        self._ledger = ledger
        self._memory_self_reflection = memory_self_reflection
        self._memory_dream = memory_dream

    def build(self) -> AdminBundle:
        audit = AdminAuditService(self._database)
        memories = MemoryAdminService(
            settings=self._settings,
            memories=self._memories,
            audit=audit,
            memory_context=self._memory_context,
            memory_index=self._memory_index,
            memory_embeddings=self._memory_embeddings,
            runtime_config=self._runtime_config,
            fact_audit=self._memory_audit,
            maintenance=self._memory_maintenance,
            mutations=self._memory_mutations,
            ledger=self._ledger,
            self_reflection=self._memory_self_reflection,
            dream=self._memory_dream,
        )
        preferences = PreferenceAdminService(
            audit=audit,
            memory_mutations=memories,
        )
        groups = GroupAdminService(
            groups=self._groups,
            audit=audit,
        )
        config = ConfigAdminService(self._runtime_config)
        emoji = EmojiAdminService(
            repository=self._emoji_repository,
            lifecycle=self._emoji_lifecycle,
            storage=self._emoji_storage,
            collector=self._emoji_collector,
            config=config,
            worker=self._emoji_worker,
        )
        return AdminBundle(
            audit,
            memories,
            preferences,
            groups,
            config,
            emoji,
        )
