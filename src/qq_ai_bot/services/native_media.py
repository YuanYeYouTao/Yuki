"""Bounded local pixels for the original Agent, without auxiliary model calls."""

from __future__ import annotations

import hashlib
from pathlib import Path

from qq_ai_bot.admin.models import VisionRuntimeConfig
from qq_ai_bot.domain.messages import ChatImage
from qq_ai_bot.services.image_preprocessor import ImagePreprocessingError, ImagePreprocessor
from qq_ai_bot.services.video_frames import sample_video
from qq_ai_bot.vision.models import DownloadedMedia

MAX_IMAGE_BYTES = 20 * 1024 * 1024


class NativeMediaPreparer:
    def __init__(
        self, preprocessor: ImagePreprocessor, *, max_bytes: int = 6_291_456, max_frames: int = 16
    ) -> None:
        self.preprocessor, self.max_bytes, self.max_frames = preprocessor, max_bytes, max_frames

    def check_budget(
        self, images: tuple[ChatImage, ...], *, max_frames: int | None = None
    ) -> tuple[ChatImage, ...]:
        if (
            len(images) > (max_frames or self.max_frames)
            or sum(len(i.data_url) for i in images) > self.max_bytes
        ):
            raise ImagePreprocessingError("prepared_too_large", "媒体超过本次准备预算")
        return images

    def prepare_image(
        self, data: bytes | DownloadedMedia, *, source: str, max_frames: int | None = None
    ) -> tuple[ChatImage, ...]:
        downloaded = (
            data
            if isinstance(data, DownloadedMedia)
            else DownloadedMedia(
                content=data,
                content_type=None,
                content_hash=hashlib.sha256(data).hexdigest(),
                byte_size=len(data),
            )
        )
        if len(downloaded.content) > MAX_IMAGE_BYTES:
            raise ImagePreprocessingError("too_large", "图片超过读取预算")
        prepared = self.preprocessor.prepare(
            downloaded,
            # PreparedVisualInput's transport label is legacy; ChatImage keeps
            # the actual Host source dependency below.
            source="reply" if source == "reply" else "current",
            max_frames=max_frames or self.max_frames,
        )
        return self.check_budget(
            tuple(ChatImage(data_url=f.data_url, source=source) for f in prepared.frames),
            max_frames=max_frames,
        )

    async def prepare_video(
        self,
        path: Path,
        *,
        source: str,
        runtime: VisionRuntimeConfig | None = None,
        max_frames: int | None = None,
    ) -> tuple[ChatImage, ...]:
        maximum = (
            min(runtime.max_frames_per_turn, runtime.video_max_frames)
            if runtime
            else self.max_frames
        )
        if max_frames is not None:
            maximum = min(maximum, max_frames)
        return self.check_budget(
            await sample_video(
                path,
                source=source,
                maximum=maximum,
                max_duration_seconds=runtime.video_max_duration_seconds if runtime else 600,
                sample_interval_seconds=runtime.video_sample_interval_seconds if runtime else 5,
            ),
            max_frames=maximum,
        )
