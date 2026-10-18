"""Bounded local pixels for the original Agent, without auxiliary model calls."""

from __future__ import annotations

import io
from functools import partial
from pathlib import Path
from typing import Any

from yuki.admin.models import VisionRuntimeConfig
from yuki.domain.messages import ChatImage
from yuki.emoji.media import media_facts
from yuki.services.image_preprocessor import ImagePreprocessingError, ImagePreprocessor
from yuki.services.media_resolver import _media_io
from yuki.services.video_frames import sample_video
from yuki.vision.models import DownloadedMedia

MAX_IMAGE_BYTES = 20 * 1024 * 1024


def native_sticker_facts(source: bytes | Path) -> dict[str, Any] | None:
    """Read actual native animation metadata without requiring image pixels or a filename."""
    stream = io.BytesIO(source) if isinstance(source, bytes) else source.open("rb")
    with stream:
        if stream.read(2) != b"\x1f\x8b":
            return None
        stream.seek(0)
        facts = media_facts(stream, inspect_pixels=False)
    return facts if facts is not None and facts["image_format"] == "TGS" else None


def image_extension(header: bytes) -> str | None:
    """Classify real image bytes independently of a platform's media label."""
    if header.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return ".webp"
    return None


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
                content_hash="",
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
        path: Path | DownloadedMedia,
        *,
        source: str,
        runtime: VisionRuntimeConfig | None = None,
        max_frames: int | None = None,
    ) -> tuple[ChatImage, ...]:
        def image_bytes() -> bytes | None:
            if isinstance(path, DownloadedMedia):
                return path.content if image_extension(path.content[:12]) is not None else None
            with path.open("rb") as stream:
                header = stream.read(12)
                if image_extension(header) is None:
                    return None
                return header + stream.read(MAX_IMAGE_BYTES + 1 - len(header))

        image = await _media_io(image_bytes)
        if image is not None:
            return await _media_io(
                partial(self.prepare_image, image, source=source, max_frames=max_frames)
            )
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
