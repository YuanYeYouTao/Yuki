"""Model runtime module and immutable bundle."""

from __future__ import annotations

from dataclasses import dataclass

from yuki.application.lifecycle import LifecycleRegistry
from yuki.execution_trace.recorder import TraceRecorder
from yuki.llm.base import LLMProvider
from yuki.model_runtime import (
    ModelClientPool,
    ModelInvocationRepository,
    ModelProfileCatalog,
    ModelRouter,
    ModelTask,
    TaskModelExecutor,
    load_model_profile_catalog,
)
from yuki.model_runtime.profiles import model_profile_environment
from yuki.model_runtime.secrets import model_secret_environment, read_model_secrets
from yuki.persistence.database import Database
from yuki.persistence.diagnostic_writer import DiagnosticWriter
from yuki.settings_domains import ModelRuntimeSettings, WebSettings


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
        web_settings: WebSettings,
    ) -> None:
        self._settings = settings
        self._database = database
        self._lifecycle = lifecycle
        self._web_settings = web_settings

    def build(self) -> ModelRuntimeBundle:
        settings = self._settings
        profiles = load_model_profile_catalog(
            settings.model_profiles_file,
            environment=model_profile_environment(settings),
            web_mode=self._web_settings.mode,
            search_backend=self._web_settings.web_search_backend,
        )
        clients = ModelClientPool(
            secret_overrides={
                **model_secret_environment(settings),
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
            self_reflection_timeout_seconds=settings.memory_self_reflection_timeout_seconds,
            self_reflection_quiet_seconds=settings.memory_self_reflection_quiet_seconds,
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
