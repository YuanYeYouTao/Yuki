"""Visual-media application module."""

from __future__ import annotations

from dataclasses import dataclass

from yuki.application.lifecycle import LifecycleRegistry
from yuki.model_runtime.connections import VisionConnection
from yuki.persistence.repositories import MediaAnalysisRepository
from yuki.services.image_preprocessor import ImagePreprocessor
from yuki.services.media_resolver import MediaResolver
from yuki.services.vision_rate_limit import VisionRateLimiter
from yuki.services.vision_service import VISION_PROMPT_VERSION, VisionService
from yuki.settings_domains import VisionSettings
from yuki.vision.base import VisionProvider
from yuki.vision.fake import FakeVisionProvider
from yuki.vision.qwen import QwenVisionProvider


@dataclass(frozen=True, slots=True)
class MediaBundle:
    provider: VisionProvider | None
    resolver: MediaResolver
    image_preprocessor: ImagePreprocessor
    vision: VisionService | None


class MediaModule:
    """Build visual ingestion once and register its external resources."""

    def __init__(
        self,
        *,
        settings: VisionSettings,
        analyses: MediaAnalysisRepository,
        lifecycle: LifecycleRegistry,
        provider: VisionProvider | None = None,
        connection: VisionConnection | None = None,
        api_key: str = "",
    ) -> None:
        self._settings = settings
        self._analyses = analyses
        self._lifecycle = lifecycle
        self._provider = provider
        self._connection = connection
        self._api_key = api_key

    def build(self) -> MediaBundle:
        settings = self._settings
        provider = self._provider or self._build_provider(
            settings, connection=self._connection, api_key=self._api_key
        )
        resolver = MediaResolver(
            max_download_bytes=settings.vision_max_download_bytes,
            timeout_seconds=settings.vision_media_download_timeout_seconds,
            allow_private_urls=settings.vision_allow_private_urls,
        )
        preprocessor = ImagePreprocessor(
            max_dimension=settings.vision_max_dimension,
            max_pixels=settings.vision_max_pixels,
            max_prepared_bytes=settings.vision_max_prepared_bytes,
            gif_max_frames=settings.vision_gif_max_frames,
        )
        vision: VisionService | None = None
        if provider is not None:
            vision = VisionService(
                provider=provider,
                resolver=resolver,
                preprocessor=preprocessor,
                analyses=self._analyses,
                rate_limiter=VisionRateLimiter(),
                max_prepared_bytes=settings.vision_max_prepared_bytes,
                global_concurrency=settings.vision_global_concurrency,
                queue_max_pending=settings.vision_queue_max_pending,
                queue_timeout_seconds=settings.vision_queue_timeout_seconds,
                prompt_version=(
                    f"{VISION_PROMPT_VERSION}-{settings.vision_max_dimension:x}-"
                    f"{settings.vision_max_pixels:x}-{settings.vision_max_prepared_bytes:x}"
                ),
            )
            self._lifecycle.register("vision", close=vision.close)
        else:
            self._lifecycle.register("media_resolver", close=resolver.close)
        return MediaBundle(provider, resolver, preprocessor, vision)

    @staticmethod
    def _build_provider(
        settings: VisionSettings, *, connection: VisionConnection | None = None, api_key: str = ""
    ) -> VisionProvider | None:
        if connection is not None:
            settings = settings.model_copy(
                update={
                    "vision_enabled": connection.enabled,
                    "vision_provider": "qwen",
                    "vision_base_url": connection.base_url,
                    "vision_api_key": api_key,
                    "vision_model": connection.model,
                }
            )
        if not settings.vision_enabled:
            return None
        if settings.vision_provider.casefold() == "fake":
            return FakeVisionProvider(model=settings.vision_model)
        if settings.vision_provider.casefold() != "qwen":
            raise ValueError("VISION_PROVIDER must be qwen or fake")
        return QwenVisionProvider(
            base_url=settings.vision_base_url,
            api_key=settings.vision_api_key,
            model=settings.vision_model,
            timeout_seconds=settings.vision_timeout_seconds,
            global_concurrency=settings.vision_global_concurrency,
            max_output_tokens=settings.vision_max_output_tokens,
        )
