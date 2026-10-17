"""Conversation, context, and memory-worker application module."""

from __future__ import annotations

from dataclasses import dataclass

from yuki.admin.config_service import RuntimeConfigService
from yuki.admin.permission_catalog import PermissionCatalogService
from yuki.application.lifecycle import LifecycleRegistry
from yuki.application.modules.model_runtime import ModelRuntimeBundle
from yuki.application.modules.persistence import PersistenceBundle
from yuki.capabilities import ToolArtifactWriter
from yuki.config import Settings
from yuki.conversation.features import AdmissionFeatureBuilder
from yuki.memory.dream.repository import DreamRepository
from yuki.memory.dream.service import DreamService
from yuki.memory.dream.worker import DreamWorker
from yuki.memory.embedding.runtime import MemoryEmbeddingRuntime
from yuki.memory.maintenance import MemoryMaintenanceWorker
from yuki.memory.mutation.service import MemoryMutationService
from yuki.memory.rebuild.service import MemoryRebuildService
from yuki.memory.rebuild.worker import MemoryRebuildWorker
from yuki.memory.runtime.partition_lookup import DatabaseMemoryPartitionLookup
from yuki.memory.self_reflection.repository import SelfReflectionRepository
from yuki.memory.self_reflection.service import SelfReflectionService
from yuki.memory.self_reflection.worker import SelfReflectionWorker
from yuki.memory.worker import MemoryWorker
from yuki.services.agent_tools import AgentToolService
from yuki.services.chat import ChatService, ToolInvocationRecorder
from yuki.services.concurrency import ConcurrencyManager
from yuki.services.effect_gate import ConversationEffectGate
from yuki.services.processing_status import ProcessingStatusService
from yuki.services.prompt_composer import PromptComposer
from yuki.services.prompt_registry import PromptRegistry
from yuki.services.rate_limit import SlidingWindowRateLimiter
from yuki.services.turn_coordinator import ConversationTurnCoordinator
from yuki.web.base import WebSearchProvider


@dataclass(frozen=True, slots=True)
class ConversationBundle:
    prompt_registry: PromptRegistry
    admission_features: AdmissionFeatureBuilder
    rate_limiter: SlidingWindowRateLimiter
    agent_tools: AgentToolService
    chat: ChatService
    memory_mutations: MemoryMutationService
    memory_worker: MemoryWorker
    memory_rebuild_service: MemoryRebuildService
    memory_rebuild_worker: MemoryRebuildWorker
    memory_maintenance_worker: MemoryMaintenanceWorker
    memory_self_reflection_worker: SelfReflectionWorker
    memory_dream_worker: DreamWorker


class ConversationModule:
    def __init__(
        self,
        *,
        settings: Settings,
        persistence: PersistenceBundle,
        model_runtime: ModelRuntimeBundle,
        runtime_config: RuntimeConfigService,
        permission_catalog: PermissionCatalogService,
        concurrency: ConcurrencyManager,
        turns: ConversationTurnCoordinator,
        effect_gate: ConversationEffectGate,
        web_provider: WebSearchProvider | None,
        memory_embeddings: MemoryEmbeddingRuntime,
        tool_artifacts: ToolArtifactWriter | None = None,
        tool_invocations: ToolInvocationRecorder | None = None,
        processing_status: ProcessingStatusService | None = None,
    ) -> None:
        self._settings = settings
        self._persistence = persistence
        self._model_runtime = model_runtime
        self._runtime_config = runtime_config
        self._permission_catalog = permission_catalog
        self._concurrency = concurrency
        self._turns = turns
        self._effect_gate = effect_gate
        self._web_provider = web_provider
        self._memory_embeddings = memory_embeddings
        self._tool_artifacts = tool_artifacts
        self._tool_invocations = tool_invocations
        self._processing_status = processing_status

    def build(self) -> ConversationBundle:
        settings = self._settings
        persistence = self._persistence
        models = self._model_runtime.executor
        prompt_registry = PromptRegistry()
        admission_features = AdmissionFeatureBuilder(
            ledger=persistence.ledger,
        )
        rate_limiter = SlidingWindowRateLimiter(
            per_user=settings.per_user_requests_per_minute,
            per_group=settings.per_group_requests_per_minute,
        )
        memory_worker = MemoryWorker(
            settings=settings,
            jobs=persistence.memory_jobs,
            facts=persistence.memories,
            ledger=persistence.ledger,
            people=persistence.people,
            model_executor=models,
            concurrency=self._concurrency,
        )
        memory_mutations = memory_worker.mutations
        agent_tools = AgentToolService(
            settings=settings,
            ledger=persistence.ledger,
            memories=persistence.memories,
            memory_context=persistence.memory_context,
            memory_mutations=memory_mutations,
            web_provider=self._web_provider,
            runtime_config=self._runtime_config,
            permission_catalog=self._permission_catalog,
        )
        chat = ChatService(
            settings=settings,
            model_executor=models,
            concurrency=self._concurrency,
            ledger=persistence.ledger,
            people=persistence.people,
            memories=persistence.memories,
            memory_context=persistence.memory_context,
            memory_partition_lookup=DatabaseMemoryPartitionLookup(persistence.database),
            tools=agent_tools,
            runtime_config=self._runtime_config,
            prompt_composer=PromptComposer(settings, prompt_registry),
            turn_coordinator=self._turns,
            tool_artifacts=self._tool_artifacts,
            tool_invocations=self._tool_invocations,
            conversation_scopes=persistence.conversation_scopes,
            effect_gate=self._effect_gate,
            processing_status=self._processing_status,
        )
        memory_rebuild_service = MemoryRebuildService(
            settings=settings,
            repository=persistence.memory_rebuilds,
            ledger=persistence.ledger,
            extractor=memory_worker.extractor,
            processor=memory_worker.processor,
        )
        memory_rebuild_worker = MemoryRebuildWorker(
            memory_rebuild_service,
            interval_seconds=settings.memory_rebuild_worker_interval_seconds,
        )
        memory_maintenance_worker = MemoryMaintenanceWorker(
            settings=settings,
            facts=persistence.memories,
            runtime_config=self._runtime_config,
            metrics=persistence.memory_metrics,
        )
        self_reflection_repository = SelfReflectionRepository(persistence.database)
        memory_self_reflection_worker = SelfReflectionWorker(
            settings=settings,
            repository=self_reflection_repository,
            service=SelfReflectionService(
                settings=settings,
                repository=self_reflection_repository,
                mutations=memory_mutations,
                models=models,
            ),
        )
        memory_dream_repository = DreamRepository(persistence.database)
        memory_dream_worker = DreamWorker(
            settings=settings,
            repository=memory_dream_repository,
            service=DreamService(
                settings=settings,
                repository=memory_dream_repository,
                facts=persistence.memories,
                mutations=memory_mutations,
                embeddings=self._memory_embeddings,
                models=models,
                concurrency=self._concurrency,
            ),
        )
        return ConversationBundle(
            prompt_registry,
            admission_features,
            rate_limiter,
            agent_tools,
            chat,
            memory_mutations,
            memory_worker,
            memory_rebuild_service,
            memory_rebuild_worker,
            memory_maintenance_worker,
            memory_self_reflection_worker,
            memory_dream_worker,
        )

    @staticmethod
    def register_workers(bundle: ConversationBundle, lifecycle: LifecycleRegistry) -> None:
        lifecycle.register(
            "memory_worker",
            start=bundle.memory_worker.start,
            close=bundle.memory_worker.close,
        )
        lifecycle.register(
            "memory_rebuild_worker",
            start=bundle.memory_rebuild_worker.start,
            close=bundle.memory_rebuild_worker.close,
        )
        lifecycle.register(
            "memory_maintenance_worker",
            start=bundle.memory_maintenance_worker.start,
            close=bundle.memory_maintenance_worker.close,
        )
        lifecycle.register(
            "memory_self_reflection_worker",
            start=bundle.memory_self_reflection_worker.start,
            close=bundle.memory_self_reflection_worker.close,
        )
        lifecycle.register(
            "memory_dream_worker",
            start=bundle.memory_dream_worker.start,
            close=bundle.memory_dream_worker.close,
            health=bundle.memory_dream_worker.health,
        )
