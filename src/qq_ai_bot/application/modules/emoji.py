"""Persistent emoji-library application module."""

from __future__ import annotations

from dataclasses import dataclass

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.application.lifecycle import LifecycleRegistry
from qq_ai_bot.emoji.classifier import EmojiClassifier
from qq_ai_bot.emoji.collector import EmojiCollector
from qq_ai_bot.emoji.delivery import EmojiDeliveryService
from qq_ai_bot.emoji.detector import EmojiCandidateDetector
from qq_ai_bot.emoji.grid import EmojiGridBuilder
from qq_ai_bot.emoji.lifecycle import EmojiLifecycleService
from qq_ai_bot.emoji.replacement import EmojiReplacementService
from qq_ai_bot.emoji.repository import EmojiRepository
from qq_ai_bot.emoji.retriever import EmojiRetriever
from qq_ai_bot.emoji.selector import EmojiSelector
from qq_ai_bot.emoji.storage import EmojiStorage
from qq_ai_bot.emoji.worker import EmojiWorker
from qq_ai_bot.model_runtime.executor import ModelExecutor
from qq_ai_bot.model_runtime.models import ModelTask
from qq_ai_bot.persistence.repositories import MediaAnalysisRepository
from qq_ai_bot.services.image_preprocessor import ImagePreprocessor
from qq_ai_bot.services.media_resolver import MediaResolver
from qq_ai_bot.settings_domains import ConversationSettings, EmojiSettings
from qq_ai_bot.vision.base import VisionProvider


@dataclass(frozen=True, slots=True)
class EmojiBundle:
    storage: EmojiStorage
    lifecycle: EmojiLifecycleService
    collector: EmojiCollector
    selector: EmojiSelector
    delivery: EmojiDeliveryService
    worker: EmojiWorker | None


class EmojiModule:
    def __init__(
        self,
        *,
        settings: EmojiSettings,
        conversation_settings: ConversationSettings,
        repository: EmojiRepository,
        analyses: MediaAnalysisRepository,
        resolver: MediaResolver,
        preprocessor: ImagePreprocessor,
        vision_provider: VisionProvider | None,
        models: ModelExecutor,
        runtime_config: RuntimeConfigService,
        lifecycle: LifecycleRegistry,
        bot_display_name: str = "Yuki",
    ) -> None:
        self._settings = settings
        self._conversation_settings = conversation_settings
        self._repository = repository
        self._analyses = analyses
        self._resolver = resolver
        self._preprocessor = preprocessor
        self._vision_provider = vision_provider
        self._models = models
        self._runtime_config = runtime_config
        self._lifecycle = lifecycle
        self._bot_display_name = bot_display_name

    def build(self) -> EmojiBundle:
        settings = self._settings
        storage = EmojiStorage(
            settings.emoji_storage_root,
            preview_max_dimension=settings.emoji_preview_max_dimension,
        )
        emoji_lifecycle = EmojiLifecycleService(
            self._repository,
            replacement=EmojiReplacementService(
                model_executor=self._models,
                model=self._models.model_name(ModelTask.EMOJI_REPLACEMENT),
                max_prompt_characters=self._conversation_settings.max_context_characters,
            ),
        )
        collector = EmojiCollector(
            detector=EmojiCandidateDetector(),
            resolver=self._resolver,
            storage=storage,
            repository=self._repository,
        )
        retriever = EmojiRetriever(self._repository, storage)
        selector = EmojiSelector(
            retriever=retriever,
            grid_builder=EmojiGridBuilder(storage),
            preprocessor=self._preprocessor,
            provider=self._vision_provider,
        )
        delivery = EmojiDeliveryService(
            selector=selector,
            repository=self._repository,
            storage=storage,
            bot_display_name=self._bot_display_name,
        )
        worker: EmojiWorker | None = None
        if self._vision_provider is not None:
            worker = EmojiWorker(
                repository=self._repository,
                classifier=EmojiClassifier(
                    provider=self._vision_provider,
                    preprocessor=self._preprocessor,
                    storage=storage,
                    analyses=self._analyses,
                ),
                lifecycle=emoji_lifecycle,
                storage=storage,
                runtime_config=self._runtime_config,
            )
        self._lifecycle.register("emoji_collector", close=collector.close)
        return EmojiBundle(storage, emoji_lifecycle, collector, selector, delivery, worker)

    @staticmethod
    def register_worker(bundle: EmojiBundle, lifecycle: LifecycleRegistry) -> None:
        if bundle.worker is not None:
            lifecycle.register(
                "emoji_worker",
                start=bundle.worker.start,
                close=bundle.worker.close,
            )
