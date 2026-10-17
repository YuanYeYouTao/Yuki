"""Read event-bound attachments into the normal Main Agent's current input."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from yuki.admin.models import VisionRuntimeConfig
from yuki.domain.messages import AttachmentKind, ChatImage, InboundMessage, MessageAttachment
from yuki.gateway.media import PlatformMediaGateway
from yuki.services.image_preprocessor import ImagePreprocessingError, ImagePreprocessor
from yuki.services.media_process import MediaProcessError
from yuki.services.media_process import run_media_process as run_parser
from yuki.services.media_resolver import (
    MediaResolutionError,
    MediaResolver,
    read_local_media,
)
from yuki.services.native_media import NativeMediaPreparer
from yuki.services.vision_rate_limit import VisionRateLimiter
from yuki.services.vision_service import VisionProcessingError
from yuki.vision.models import MediaReference


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
        images_enabled: bool | Callable[[], bool] = True,
    ) -> None:
        self._resolver = resolver
        self._preparer = NativeMediaPreparer(preprocessor, max_bytes=max_bytes)
        self._semaphore = asyncio.Semaphore(concurrency)
        self._pending_limit = pending_limit
        self._pending = 0
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._limiter = VisionRateLimiter()
        self._images_enabled = images_enabled
        self._document_semaphore = asyncio.Semaphore(1)

    @property
    def images_enabled(self) -> bool:
        enabled = self._images_enabled
        return enabled() if callable(enabled) else enabled

    @images_enabled.setter
    def images_enabled(self, enabled: bool | Callable[[], bool]) -> None:
        self._images_enabled = enabled

    async def prepare(
        self,
        message: InboundMessage,
        runtime: VisionRuntimeConfig,
        gateway: PlatformMediaGateway | None,
    ) -> NativeInput:
        kinds = {AttachmentKind.IMAGE, AttachmentKind.VIDEO, AttachmentKind.FILE}
        selected = tuple(
            a for a in (*message.attachments, *message.reply_attachments) if a.kind in kinds
        )
        if not selected:
            return NativeInput()
        if self._pending >= self._pending_limit:
            raise VisionProcessingError("queue_full", "附件处理队列已满")
        self._pending += 1
        try:
            async with asyncio.timeout(self._timeout), self._semaphore:
                images: list[ChatImage] = []
                documents: list[str] = []
                text_remaining = 20_000
                size = 0
                visual_count = 0
                visual_allowed: bool | None = None

                async def admit_visual(index: int) -> bool:
                    nonlocal visual_count, visual_allowed
                    if visual_count >= runtime.max_images_per_turn:
                        documents.append(f"[视觉附件{index}未读取：超过本轮图片数量上限]")
                        return False
                    visual_count += 1
                    if visual_allowed is None:
                        visual_allowed = await self._limiter.allow(
                            user_id=message.person_id or message.presence_id or "",
                            group_id=message.space_id,
                            per_user_per_minute=runtime.per_user_requests_per_minute,
                            per_group_per_minute=runtime.per_group_requests_per_minute,
                        )
                    if not visual_allowed:
                        raise VisionProcessingError("rate_limited", "图片处理过于频繁")
                    return True

                async def consume(attachment: MessageAttachment, index: int) -> None:
                    nonlocal text_remaining, size
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
                                reference,
                                path,
                                max_download_bytes=limit,
                                local_source=attachment.local_source,
                                gateway=gateway,
                                kind="file",
                            )
                            with path.open("rb") as stream:
                                header = stream.read(12)
                            if header[4:8] != b"ftyp" and path.stat().st_size > 20 * 1024 * 1024:
                                raise VisionProcessingError("too_large", "非视频文件超过下载预算")
                            if header[4:8] == b"ftyp":
                                if not await admit_visual(index):
                                    return
                                if not self.images_enabled:
                                    raise VisionProcessingError(
                                        "image_capability_unavailable", "主模型不支持图片"
                                    )
                                if remaining <= 0:
                                    raise VisionProcessingError(
                                        "frame_budget", "本轮图片帧预算不足"
                                    )
                                images.extend(
                                    await self._preparer.prepare_video(
                                        path,
                                        source=reference.source,
                                        runtime=runtime,
                                        max_frames=remaining,
                                    )
                                )
                            elif header.startswith((b"\xff\xd8\xff", b"\x89PNG", b"GIF8")) or (
                                header.startswith(b"RIFF") and header[8:12] == b"WEBP"
                            ):
                                if not await admit_visual(index):
                                    return
                                if not self.images_enabled:
                                    raise VisionProcessingError(
                                        "image_capability_unavailable", "主模型不支持图片"
                                    )
                                if remaining <= 0:
                                    raise VisionProcessingError(
                                        "frame_budget", "本轮图片帧预算不足"
                                    )
                                from yuki.vision.models import DownloadedMedia

                                data = path.read_bytes()
                                prepared = await asyncio.to_thread(
                                    self._preparer.prepare_image,
                                    DownloadedMedia(
                                        content=data,
                                        content_type=None,
                                        content_hash="",
                                        byte_size=len(data),
                                    ),
                                    source=reference.source,
                                    max_frames=remaining,
                                )
                                images.extend(prepared)
                            else:
                                if text_remaining <= 0:
                                    documents.append("[后续附件未读取：本轮文本预算已用完]")
                                    return
                                async with self._document_semaphore:
                                    try:
                                        raw = await run_parser(
                                            sys.executable,
                                            "-m",
                                            "yuki.services.document_reader",
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
                                    except MediaProcessError as exc:
                                        raise VisionProcessingError(
                                            "document_reader_failed", "文档解析进程失败"
                                        ) from exc
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
                                    f"[附件{index} source={reference.source} "
                                    f"{json.dumps(metadata)}]\n{text}"
                                )
                        size = sum(len(f.data_url) for f in images)
                        if size > self._max_bytes:
                            raise VisionProcessingError("too_large", "附件帧超过本轮预算")
                        return
                    if not await admit_visual(index):
                        return
                    if remaining <= 0:
                        documents.append("[视觉附件未读取：本轮帧预算已用完]")
                        return
                    if attachment.kind is AttachmentKind.VIDEO:
                        if not self.images_enabled:
                            raise VisionProcessingError(
                                "image_capability_unavailable", "主模型不支持图片"
                            )
                        with TemporaryDirectory(prefix="yuki-video-download-") as directory:
                            path = Path(directory) / "input.mp4"
                            await self._resolver.download_attachment(
                                reference,
                                path,
                                max_download_bytes=runtime.video_max_download_bytes,
                                local_source=attachment.local_source,
                                gateway=gateway,
                                kind="video",
                            )
                            video_frames = await self._preparer.prepare_video(
                                path,
                                source=reference.source,
                                runtime=runtime,
                                max_frames=remaining,
                            )
                        size += sum(len(frame.data_url) for frame in video_frames)
                        if size > self._max_bytes:
                            raise VisionProcessingError("too_large", "视频帧超过本轮预算")
                        images.extend(video_frames)
                        return
                    if not self.images_enabled:
                        documents.append("[图片未读取：当前主模型不支持图片输入]")
                        return
                    downloaded = (
                        await asyncio.to_thread(
                            read_local_media, attachment.local_source, 20 * 1024 * 1024
                        )
                        if attachment.local_source is not None
                        else await self._resolver.resolve(reference, gateway)
                    )
                    prepared = await asyncio.to_thread(
                        self._preparer.prepare_image,
                        downloaded,
                        source=reference.source,
                        max_frames=remaining,
                    )
                    for frame in prepared:
                        size += len(frame.data_url)
                        if size > self._max_bytes:
                            raise VisionProcessingError("too_large", "处理后图片超过本轮预算")
                        images.append(frame)

                first_failure: (
                    VisionProcessingError | MediaResolutionError | ImagePreprocessingError | None
                ) = None
                had_readable_output = bool(documents)
                fatal_codes = {
                    "too_large",
                    "frame_budget",
                    "video_limit",
                    "private_url",
                    "decompression_bomb",
                    "archive_expansion_limit",
                    "unsafe_archive",
                    "prepared_too_large",
                }
                for index, attachment in enumerate(selected, start=1):
                    prior_images, prior_documents = len(images), len(documents)
                    try:
                        await consume(attachment, index)
                    except (
                        VisionProcessingError,
                        MediaResolutionError,
                        ImagePreprocessingError,
                    ) as exc:
                        if exc.code in fatal_codes:
                            raise
                        if first_failure is None:
                            first_failure = exc
                        source = "reply" if attachment.source == "reply" else "current"
                        documents.append(
                            f"[附件{index} source={source} kind={attachment.kind.value} "
                            f"未读取：{exc.code}；不要推断其内容]"
                        )
                    else:
                        had_readable_output |= (
                            len(images) > prior_images or len(documents) > prior_documents
                        )
                if first_failure is not None and not had_readable_output:
                    raise first_failure
                if not images and not documents:
                    raise VisionProcessingError("no_images", "没有可读取图片")
                return NativeInput(tuple(images), "\n".join(documents))
        finally:
            self._pending -= 1
