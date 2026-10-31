"""Build incoming ASR from its explicit protocol and complete connection."""

from yuki.application.lifecycle import LifecycleRegistry
from yuki.asr.provider import HTTPASRProvider
from yuki.asr.service import ASRService
from yuki.config import Settings
from yuki.model_runtime.connections import ASRConnection
from yuki.services.media_resolver import MediaResolver


def build_asr(
    settings: Settings,
    lifecycle: LifecycleRegistry,
    *,
    connection: ASRConnection | None = None,
    api_key: str = "",
) -> ASRService:
    selected = settings.asr
    if connection is None:
        base_url, api_key = settings.asr_credentials
    else:
        base_url = connection.base_url
        selected = selected.model_copy(
            update={
                "asr_enabled": connection.enabled,
                "asr_base_url": base_url,
                "asr_api_key": api_key,
                "asr_model": connection.model,
                "asr_protocol": connection.protocol,
            }
        )
    provider = (
        HTTPASRProvider(
            base_url=base_url,
            api_key=api_key,
            model=selected.asr_model,
            protocol=selected.asr_protocol,
            timeout_seconds=settings.asr_timeout_seconds,
        )
        if selected.asr_enabled and base_url
        else None
    )
    service = ASRService(
        settings=selected,
        provider=provider,
        resolver=MediaResolver(
            max_download_bytes=settings.asr_max_download_bytes,
            timeout_seconds=min(20, settings.asr_timeout_seconds),
        ),
    )
    lifecycle.register("asr", close=service.close)
    return service
