"""Read event-bound attachments into the normal Main Agent's current input."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from qq_ai_bot.admin.models import VisionRuntimeConfig
from qq_ai_bot.domain.messages import AttachmentKind, ChatImage, InboundMessage
from qq_ai_bot.services.image_preprocessor import ImagePreprocessor
from qq_ai_bot.services.media_resolver import MediaResolver, OneBotMediaGateway
from qq_ai_bot.services.video_frames import _run as run_parser
from qq_ai_bot.services.video_frames import sample_video
from qq_ai_bot.services.vision_rate_limit import VisionRateLimiter
from qq_ai_bot.services.vision_service import VisionProcessingError
from qq_ai_bot.vision.models import MediaReference


@dataclass(frozen=True, slots=True)
class NativeInput:
    images: tuple[ChatImage, ...] = ()
    documents: str = field(default="", repr=False)


class AttachmentInputService:
    def __init__(
        self,
        resolver: MediaResolver,
        preprocessor: ImagePreprocessor,
        *,
        concurrency: int,
        pending_limit: int,
        timeout: float,
        max_bytes: int,
        images_enabled: bool = True,
    ) -> None:
        self._resolver = resolver
        self._preprocessor = preprocessor
        self._semaphore = asyncio.Semaphore(concurrency)
        self._pending_limit = pending_limit
        self._pending = 0
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._limiter = VisionRateLimiter()
        self.images_enabled = images_enabled
        self._document_semaphore = asyncio.Semaphore(1)

    async def prepare(
        self,
        message: InboundMessage,
        runtime: VisionRuntimeConfig,
        gateway: OneBotMediaGateway | None,
    ) -> NativeInput:
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
                documents: list[str] = []
                text_remaining = 20_000
                size = 0
                kinds = {AttachmentKind.IMAGE, AttachmentKind.VIDEO, AttachmentKind.FILE}
                current = tuple(a for a in message.attachments if a.kind in kinds)
                replied = tuple(a for a in message.reply_attachments if a.kind in kinds)
                for attachment in (current or replied)[: runtime.max_images_per_turn]:
                    reference = MediaReference(
                        file=attachment.file,
                        url=attachment.url,
                        source="reply" if attachment.source == "reply" else "current",
                    )
                    remaining = runtime.max_frames_per_turn - len(images)
                    suffix = Path(
                        attachment.filename or urlsplit(reference.url or "").path
                    ).suffix.lower()
                    if attachment.kind is AttachmentKind.FILE:
                        with TemporaryDirectory(prefix="yuki-attachment-") as directory:
                            path = Path(directory) / "input.bin"
                            limit = (
                                runtime.video_max_download_bytes
                                if suffix in {".mp4", ".mov"}
                                else 20 * 1024 * 1024
                            )
                            await self._resolver.download_attachment(
                                reference, path, max_download_bytes=limit
                            )
                            with path.open("rb") as stream:
                                header = stream.read(12)
                            if header[4:8] != b"ftyp" and path.stat().st_size > 20 * 1024 * 1024:
                                raise VisionProcessingError("too_large", "非视频文件超过下载预算")
                            if header[4:8] == b"ftyp":
                                if not self.images_enabled:
                                    raise VisionProcessingError(
                                        "image_capability_unavailable", "主模型不支持图片"
                                    )
                                if remaining <= 0:
                                    raise VisionProcessingError(
                                        "frame_budget", "本轮图片帧预算不足"
                                    )
                                images.extend(
                                    await sample_video(
                                        path,
                                        source=reference.source,
                                        maximum=min(remaining, runtime.video_max_frames),
                                        max_duration_seconds=runtime.video_max_duration_seconds,
                                        sample_interval_seconds=runtime.video_sample_interval_seconds,
                                    )
                                )
                            elif header.startswith((b"\xff\xd8\xff", b"\x89PNG", b"GIF8")) or (
                                header.startswith(b"RIFF") and header[8:12] == b"WEBP"
                            ):
                                if not self.images_enabled:
                                    raise VisionProcessingError(
                                        "image_capability_unavailable", "主模型不支持图片"
                                    )
                                if remaining <= 0:
                                    raise VisionProcessingError(
                                        "frame_budget", "本轮图片帧预算不足"
                                    )
                                from qq_ai_bot.vision.models import DownloadedMedia

                                data = path.read_bytes()
                                prepared = await asyncio.to_thread(
                                    self._preprocessor.prepare,
                                    DownloadedMedia(
                                        content=data,
                                        content_type=None,
                                        content_hash="",
                                        byte_size=len(data),
                                    ),
                                    source=reference.source,
                                    max_frames=remaining,
                                )
                                images.extend(
                                    ChatImage(data_url=f.data_url, source=reference.source)
                                    for f in prepared.frames
                                )
                            else:
                                if text_remaining <= 0:
                                    documents.append("[后续附件未读取：本轮文本预算已用完]")
                                    continue
                                async with self._document_semaphore:
                                    raw = await run_parser(
                                        sys.executable,
                                        "-m",
                                        "qq_ai_bot.services.document_reader",
                                        str(path),
                                        suffix,
                                        str(text_remaining),
                                        "20",
                                        env={
                                            **{
                                                k: os.environ[k]
                                                for k in ("PATH", "SYSTEMROOT")
                                                if k in os.environ
                                            },
                                            "PYTHONPATH": os.pathsep.join(sys.path),
                                            "PYTHONIOENCODING": "utf-8",
                                        },
                                    )
                                result = json.loads(raw)
                                if result.get("error"):
                                    raise VisionProcessingError(
                                        str(result["error"]),
                                        "文件不支持、加密、损坏或超过解析限制",
                                    )
                                text = str(result["text"])
                                text_remaining -= len(text)
                                metadata = {
                                    k: result[k]
                                    for k in ("kind", "truncated", "units_read", "total_units")
                                }
                                metadata["name"] = Path(attachment.filename or "").name[:120]
                                documents.append(
                                    f"[附件{len(documents) + 1} source={reference.source} "
                                    f"{json.dumps(metadata)}]\n{text}"
                                )
                        size = sum(len(f.data_url) for f in images)
                        if size > self._max_bytes:
                            raise VisionProcessingError("too_large", "附件帧超过本轮预算")
                        continue
                    if remaining <= 0:
                        documents.append("[视觉附件未读取：本轮帧预算已用完]")
                        continue
                    if attachment.kind is AttachmentKind.VIDEO:
                        if not self.images_enabled:
                            raise VisionProcessingError(
                                "image_capability_unavailable", "主模型不支持图片"
                            )
                        # Never reinterpret a video ID as get_image, or open gateway-local paths.
                        location = reference.url or reference.file or ""
                        if not location.startswith(("https://", "http://", "base64://")):
                            raise VisionProcessingError("video_unavailable", "视频缺少可下载地址")
                        with TemporaryDirectory(prefix="yuki-video-download-") as directory:
                            path = Path(directory) / "input.mp4"
                            await self._resolver.download_attachment(
                                reference, path, max_download_bytes=runtime.video_max_download_bytes
                            )
                            video_frames = await sample_video(
                                path,
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
                    if not self.images_enabled:
                        documents.append("[图片未读取：当前主模型不支持图片输入]")
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
                if len(current or replied) > runtime.max_images_per_turn:
                    documents.append("[其余附件未读取：超过本轮附件数量上限]")
                if not images and not documents:
                    raise VisionProcessingError("no_images", "没有可读取图片")
                return NativeInput(tuple(images), "\n".join(documents))
        finally:
            self._pending -= 1
