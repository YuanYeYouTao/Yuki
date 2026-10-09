"""Persistence module with an explicit immutable repository bundle."""

from __future__ import annotations

from dataclasses import dataclass, replace

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.admin.models import WorkStorageRuntimeConfig
from qq_ai_bot.application.lifecycle import LifecycleRegistry
from qq_ai_bot.config import Settings
from qq_ai_bot.conversation.rollup.metrics import ConversationRollupMetrics
from qq_ai_bot.conversation.rollup.models import RollupPolicyConfig
from qq_ai_bot.conversation.rollup.origins import parse_rollup_llm_origins
from qq_ai_bot.conversation.rollup.repository import (
    ConversationRollupRepository,
    ConversationScopeRepository,
)
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.emoji.repository import EmojiRepository
from qq_ai_bot.identity.write_settings import configure_identity_write_settings
from qq_ai_bot.memory.audit import MemoryAuditService
from qq_ai_bot.memory.context import MemoryContextService
from qq_ai_bot.memory.fts import SQLiteMemoryFTSIndex
from qq_ai_bot.memory.metrics import MemoryLifecycleMetrics
from qq_ai_bot.memory.query import MemoryQueryBuilder
from qq_ai_bot.memory.rebuild.repository import MemoryRebuildRepository
from qq_ai_bot.memory.repository import MemoryFactRepository, MemoryJobRepository
from qq_ai_bot.memory.retrieval import MemoryRetriever
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.memory.targets import MemoryTargetResolver
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.repositories import (
    AgentActionRepository,
    EmojiDescriptionRepository,
    EventLedgerRepository,
    GroupSettingsRepository,
    MediaAnalysisRepository,
    PeopleRepository,
    PrivateUserSettingsRepository,
    WebSearchSourceRepository,
)
from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork
from qq_ai_bot.persistence.turn_observations import RuntimeTurnObservationRepository


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
    conversation_rollups: ConversationRollupRepository
    conversation_rollup_metrics: ConversationRollupMetrics
    memories: MemoryFactService
    memory_context: MemoryContextService
    memory_index: SQLiteMemoryFTSIndex
    memory_jobs: MemoryJobRepository
    memory_audit: MemoryAuditService
    memory_metrics: MemoryLifecycleMetrics
    memory_rebuilds: MemoryRebuildRepository
    agent_actions: AgentActionRepository
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
        memories = MemoryFactService(memory_repository, metrics=memory_metrics)
        memory_index = SQLiteMemoryFTSIndex(database)
        memory_context = MemoryContextService(
            query_builder=MemoryQueryBuilder(MemoryTargetResolver(people)),
            retriever=MemoryRetriever(repository=memory_repository, lexical_index=memory_index),
            facts=memories,
            metrics=memory_metrics,
        )
        rollup_config = RollupPolicyConfig(
            context_token_budget=min(
                settings.context_window_tokens, settings.context_compaction_window_tokens
            ),
            trigger_ratio=settings.conversation_rollup_trigger_ratio,
            target_ratio=settings.conversation_rollup_target_ratio,
            batch_max_events=settings.conversation_rollup_batch_max_events,
            batch_max_characters=settings.conversation_rollup_batch_max_characters,
            summary_max_characters=settings.conversation_rollup_summary_max_characters,
            max_output_tokens=settings.conversation_rollup_max_output_tokens,
            bot_display_name=settings.bot_display_name,
            timezone=settings.default_timezone,
            llm_origins=parse_rollup_llm_origins(settings.conversation_rollup_llm_origins),
        )
        rollup_metrics = ConversationRollupMetrics()

        async def policy_for_scope(scope: ConversationScope) -> RollupPolicyConfig:
            snapshot = await runtime_config.snapshot(
                group_id=scope.group_id,
                user_id=scope.private_peer_user_id if scope.group_id is None else None,
            )
            return replace(
                rollup_config,
                context_token_budget=min(
                    snapshot.context.window_tokens, snapshot.context.compaction_window_tokens
                ),
                trigger_ratio=snapshot.context.compaction_trigger_ratio,
                target_ratio=snapshot.context.compaction_target_ratio,
                summary_max_characters=snapshot.context.rollup_summary_characters,
                max_output_tokens=snapshot.context.rollup_output_tokens,
            )

        scoped_events = ScopedEventLedgerUnitOfWork(
            database,
            config=rollup_config,
            metrics=rollup_metrics,
        )
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
            conversation_rollups=ConversationRollupRepository(
                database,
                rollup_config,
                metrics=rollup_metrics,
                policy_for_scope=policy_for_scope,
            ),
            conversation_rollup_metrics=rollup_metrics,
            memories=memories,
            memory_context=memory_context,
            memory_index=memory_index,
            memory_jobs=MemoryJobRepository(database),
            memory_audit=MemoryAuditService(memory_repository, metrics=memory_metrics),
            memory_metrics=memory_metrics,
            memory_rebuilds=memory_rebuilds,
            agent_actions=AgentActionRepository(database),
            web_sources=WebSearchSourceRepository(database),
            media_analyses=MediaAnalysisRepository(database),
            emoji_descriptions=EmojiDescriptionRepository(database),
            emoji_repository=EmojiRepository(database),
            turn_observations=RuntimeTurnObservationRepository(database),
        )
