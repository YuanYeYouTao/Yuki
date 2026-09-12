"""Admission-scoped voice recognition with a bounded queue and explicit failures."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from qq_ai_bot.asr.audio import prepare_audio
from qq_ai_bot.asr.provider import ASRError, ASRProvider
from qq_ai_bot.domain.audio import AudioTranscript, serialize_transcripts, transcript_context
from qq_ai_bot.domain.messages import AttachmentKind, InboundMessage
from qq_ai_bot.services.media_resolver import (
    MediaResolutionError,
    MediaResolver,
    OneBotMediaGateway,
)
from qq_ai_bot.settings_domains import ASRSettings
from qq_ai_bot.vision.models import MediaReference

logger = logging.getLogger(__name__)

_ERRORS = {
    "disabled": "语音识别未开启",
    "not_configured": "语音识别服务尚未配置或鉴权失败，请联系管理员",
    "busy": "语音识别正忙，请稍后重试",
    "timeout": "语音识别超时，请稍后重试",
    "audio_limit": "语音超过配置的大小或时长限制，请缩短后重发",
    "too_large": "语音超过配置的大小限制，请缩短后重发",
    "no_speech": "没有识别出清晰的语音，请重新录制或发送文字",
    "rate_limited": "语音识别请求过于频繁，请稍后重试",
    "decoder_unavailable": "语音解码器未就绪，请联系管理员",
    "invalid_audio": "无法解码这条语音，请重新录制或发送文字",
    "resource_unavailable": "无法读取这条语音，请重新发送",
    "get_record_failed": "语音下载或转码失败，请重新发送",
}


@dataclass(frozen=True, slots=True)
class AudioInput:
    transcript: str = field(default="", repr=False)
    error: str = ""

    @property
    def failure_message(self) -> str:
        return _ERRORS.get(self.error, "语音识别暂时失败，请稍后重试或发送文字") + "。"

    @property
    def context(self) -> str:
        text = transcript_context(self.transcript)
        if self.error:
            return (
                text + "\n[部分语音未识别：" + self.failure_message + " 不得猜测未识别内容。]"
            ).strip()
        return text


class ASRService:
    def __init__(
        self,
        *,
        settings: ASRSettings,
        provider: ASRProvider | None,
        resolver: MediaResolver,
    ) -> None:
        self._settings = settings
        self._provider = provider
        self._resolver = resolver
        self._slots = asyncio.Semaphore(settings.asr_global_concurrency)
        self._pending = 0

    @staticmethod
    def has_audio(message: InboundMessage) -> bool:
        return any(
            a.kind is AttachmentKind.AUDIO
            for a in (*message.attachments, *message.reply_attachments)
        )

    def health(self) -> dict[str, object]:
        return {
            "enabled": self._settings.asr_enabled,
            "configured": self._provider is not None,
            "pending": self._pending,
        }

    async def prepare(
        self, message: InboundMessage, gateway: OneBotMediaGateway | None
    ) -> AudioInput:
        if not self.has_audio(message):
            return AudioInput()
        if not self._settings.asr_enabled:
            return AudioInput(error="disabled")
        if self._provider is None:
            return AudioInput(error="not_configured")
        if self._pending >= self._settings.asr_queue_max_pending:
            return AudioInput(error="busy")
        self._pending += 1
        parts: list[AudioTranscript] = []
        try:
            # The deadline includes queueing, download, conversion and provider calls.
            async with asyncio.timeout(self._settings.asr_timeout_seconds):
                async with self._slots:
                    return await self._prepare(message, gateway, parts)
        except TimeoutError:
            return AudioInput(transcript=serialize_transcripts(tuple(parts)), error="timeout")
        finally:
            self._pending -= 1

    async def _prepare(
        self,
        message: InboundMessage,
        gateway: OneBotMediaGateway | None,
        parts: list[AudioTranscript],
    ) -> AudioInput:
        assert self._provider is not None
        attachments = [
            a
            for a in (*message.attachments, *message.reply_attachments)
            if a.kind is AttachmentKind.AUDIO
        ]
        error = "audio_limit" if len(attachments) > 3 else ""
        for attachment in attachments[:3]:
            try:
                if (
                    attachment.file_size is not None
                    and attachment.file_size > self._settings.asr_max_download_bytes
                ):
                    raise ASRError("audio_limit")
                media = await self._resolver.resolve_audio(
                    MediaReference(
                        file=attachment.file,
                        url=attachment.url,
                        source="reply" if attachment.source == "reply" else "current",
                        segment_index=attachment.segment_index,
                    ),
                    gateway=gateway,
                )
                audio = await prepare_audio(
                    media.content,
                    max_duration_seconds=self._settings.asr_max_duration_seconds,
                )
                text = await self._provider.transcribe(audio)
                if not text.strip() or len(text) > 12000:
                    raise ASRError("no_speech")
                # This is user/media data. It never re-enters deterministic command parsing.
                parts.append(
                    AudioTranscript(
                        "reply" if attachment.source == "reply" else "current",
                        attachment.segment_index,
                        text.strip(),
                    )
                )
            except (ASRError, MediaResolutionError) as exc:
                error = exc.code
                logger.info("asr_failed category=%s", error)
            except OSError:
                error = "resource_unavailable"
                logger.info("asr_failed category=resource_unavailable")
        return AudioInput(transcript=serialize_transcripts(tuple(parts)), error=error)

    async def close(self) -> None:
        try:
            if self._provider is not None:
                await self._provider.close()
        finally:
            await self._resolver.close()
