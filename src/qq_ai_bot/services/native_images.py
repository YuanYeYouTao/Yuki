"""Prepare actual event images for the normal Main Agent, without a vision model."""

from __future__ import annotations

import asyncio

from qq_ai_bot.admin.models import VisionRuntimeConfig
from qq_ai_bot.domain.messages import AttachmentKind, ChatImage, InboundMessage
from qq_ai_bot.services.image_preprocessor import ImagePreprocessor
from qq_ai_bot.services.media_resolver import MediaResolver, OneBotMediaGateway
from qq_ai_bot.services.video_frames import sample_video
from qq_ai_bot.services.vision_rate_limit import VisionRateLimiter
from qq_ai_bot.services.vision_service import VisionProcessingError
from qq_ai_bot.vision.models import MediaReference


class NativeImageService:
    def __init__(
        self,
        resolver: MediaResolver,
        preprocessor: ImagePreprocessor,
        *,
        concurrency: int,
        pending_limit: int,
        timeout: float,
        max_bytes: int,
    ) -> None:
        self._resolver = resolver
        self._preprocessor = preprocessor
        self._semaphore = asyncio.Semaphore(concurrency)
        self._pending_limit = pending_limit
        self._pending = 0
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._limiter = VisionRateLimiter()

    async def prepare(
        self,
        message: InboundMessage,
        runtime: VisionRuntimeConfig,
        gateway: OneBotMediaGateway | None,
    ) -> tuple[ChatImage, ...]:
        if self._pending >= self._pending_limit:
            raise VisionProcessingError("queue_full", "图片处理队列已满")
        if not await self._limiter.allow(
            user_id=message.sender.user_id,
            group_id=message.group_id,
            per_user_per_minute=runtime.per_user_requests_per_minute,
            per_group_per_minute=runtime.per_group_requests_per_minute,
        ):
            raise VisionProcessingError("rate_limited", "图片处理过于频繁")
        self._pending += 1
        try:
            async with asyncio.timeout(self._timeout), self._semaphore:
                images: list[ChatImage] = []
                size = 0
                kinds = {AttachmentKind.IMAGE, AttachmentKind.VIDEO}
                current = tuple(a for a in message.attachments if a.kind in kinds)
                replied = tuple(a for a in message.reply_attachments if a.kind in kinds)
                for attachment in (current or replied)[: runtime.max_images_per_turn]:
                    reference = MediaReference(
                        file=attachment.file,
                        url=attachment.url,
                        source="reply" if attachment.source == "reply" else "current",
                    )
                    remaining = runtime.max_frames_per_turn - len(images)
                    if remaining <= 0:
                        break
                    if attachment.kind is AttachmentKind.VIDEO:
                        # Never reinterpret a video ID as get_image, or open gateway-local paths.
                        location = reference.url or reference.file or ""
                        if not location.startswith(("https://", "http://", "base64://")):
                            raise VisionProcessingError("video_unavailable", "视频缺少可下载地址")
                        downloaded = await self._resolver.resolve(reference, None)
                        video_frames = await sample_video(
                            downloaded,
                            source=reference.source,
                            maximum=min(remaining, runtime.video_max_frames),
                            max_duration_seconds=runtime.video_max_duration_seconds,
                            sample_interval_seconds=runtime.video_sample_interval_seconds,
                        )
                        size += sum(len(frame.data_url) for frame in video_frames)
                        if size > self._max_bytes:
                            raise VisionProcessingError("too_large", "视频帧超过本轮预算")
                        images.extend(video_frames)
                        continue
                    downloaded = await self._resolver.resolve(reference, gateway)
                    prepared = await asyncio.to_thread(
                        self._preprocessor.prepare,
                        downloaded,
                        source=reference.source,
                        max_frames=remaining,
                    )
                    for frame in prepared.frames:
                        size += len(frame.data_url)
                        if size > self._max_bytes:
                            raise VisionProcessingError("too_large", "处理后图片超过本轮预算")
                        images.append(ChatImage(data_url=frame.data_url, source=reference.source))
                if not images:
                    raise VisionProcessingError("no_images", "没有可读取图片")
                return tuple(images)
        finally:
            self._pending -= 1
