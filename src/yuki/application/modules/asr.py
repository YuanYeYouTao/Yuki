"""Build incoming ASR using the existing Qwen credentials by default."""

from yuki.application.lifecycle import LifecycleRegistry
from yuki.asr.provider import QwenASRProvider
from yuki.asr.service import ASRService
from yuki.config import Settings
from yuki.services.media_resolver import MediaResolver


def build_asr(settings: Settings, lifecycle: LifecycleRegistry) -> ASRService:
    base_url, api_key = settings.asr_credentials
    provider = (
        QwenASRProvider(
            base_url=base_url,
            api_key=api_key,
            model=settings.asr_model,
            timeout_seconds=settings.asr_timeout_seconds,
        )
        if settings.asr_enabled and base_url and api_key
        else None
    )
    service = ASRService(
        settings=settings.asr,
        provider=provider,
        resolver=MediaResolver(
            max_download_bytes=settings.asr_max_download_bytes,
            timeout_seconds=min(20, settings.asr_timeout_seconds),
        ),
    )
    lifecycle.register("asr", close=service.close)
    return service
