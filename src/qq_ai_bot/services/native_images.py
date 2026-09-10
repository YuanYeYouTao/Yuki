"""Prepare actual event images for the normal Main Agent, without a vision model."""

from __future__ import annotations

import asyncio

from qq_ai_bot.admin.models import VisionRuntimeConfig
from qq_ai_bot.domain.messages import ChatImage, InboundMessage
from qq_ai_bot.services.image_preprocessor import ImagePreprocessor
from qq_ai_bot.services.media_resolver import MediaResolver, OneBotMediaGateway
from qq_ai_bot.services.vision_rate_limit import VisionRateLimiter
from qq_ai_bot.services.vision_service import VisionProcessingError, VisionService


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
                for reference in VisionService.select_references(
                    message, maximum=runtime.max_images_per_turn
                ):
                    remaining = runtime.max_frames_per_turn - len(images)
                    if remaining <= 0:
                        break
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
