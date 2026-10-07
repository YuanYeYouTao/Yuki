"""Model runtime module and immutable bundle."""

from __future__ import annotations

from dataclasses import dataclass

from qq_ai_bot.application.lifecycle import LifecycleRegistry
from qq_ai_bot.execution_trace.recorder import TraceRecorder
from qq_ai_bot.llm.base import LLMProvider
from qq_ai_bot.model_runtime import (
    ModelClientPool,
    ModelInvocationRepository,
    ModelProfileCatalog,
    ModelRouter,
    ModelTask,
    TaskModelExecutor,
    load_model_profile_catalog,
)
from qq_ai_bot.model_runtime.profiles import model_profile_environment
from qq_ai_bot.model_runtime.secrets import read_model_secrets
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.diagnostic_writer import DiagnosticWriter
from qq_ai_bot.settings_domains import ModelRuntimeSettings


@dataclass(frozen=True, slots=True)
class ModelRuntimeBundle:
    profiles: ModelProfileCatalog
    clients: ModelClientPool
    invocations: ModelInvocationRepository
    router: ModelRouter
    executor: TaskModelExecutor
    chat_provider: LLMProvider


class ModelRuntimeModule:
    def __init__(
        self,
        settings: ModelRuntimeSettings,
        database: Database,
        *,
        lifecycle: LifecycleRegistry,
    ) -> None:
        self._settings = settings
        self._database = database
        self._lifecycle = lifecycle

    def build(self) -> ModelRuntimeBundle:
        settings = self._settings
        profiles = load_model_profile_catalog(
            settings.model_profiles_file,
            environment=model_profile_environment(settings),
        )
        clients = ModelClientPool(
            secret_overrides={
                "LLM_API_KEY": settings.llm_api_key,
                "LLM_FLASH_API_KEY": settings.llm_flash_api_key,
                **read_model_secrets(settings.model_profiles_file)[1],
            },
        )
        diagnostics = DiagnosticWriter()
        self._lifecycle.register(
            "diagnostic_writer",
            start=diagnostics.start,
            close=diagnostics.close,
            health=diagnostics.health,
        )
        invocations = ModelInvocationRepository(self._database, writer=diagnostics)
        router = ModelRouter(profiles)
        for profile in profiles.profiles.values():
            clients.get(profile)
        executor = TaskModelExecutor(
            router=router,
            pool=clients,
            invocations=invocations,
            traces=TraceRecorder(
                self._database,
                retention_days=settings.execution_trace_retention_days,
                max_payload_bytes=settings.execution_trace_max_payload_bytes,
                writer=diagnostics,
            ),
            max_concurrency=settings.global_llm_concurrency,
            compaction_timeout_seconds=settings.conversation_rollup_model_timeout_seconds,
            self_reflection_timeout_seconds=settings.memory_self_reflection_timeout_seconds,
        )
        self._lifecycle.register("model_runtime", close=executor.close)
        _route, chat_profile = router.route(ModelTask.CHAT_AGENT)
        return ModelRuntimeBundle(
            profiles,
            clients,
            invocations,
            router,
            executor,
            clients.get(chat_profile),
        )
