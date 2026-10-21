"""Bounded Pillow preprocessing for static and animated vision inputs."""

from __future__ import annotations

import base64
import hashlib
import io
import math
import warnings
from typing import Final

from PIL import Image, ImageOps, UnidentifiedImageError

from yuki.vision.models import (
    DownloadedMedia,
    MediaSource,
    PreparedFrame,
    PreparedVisualInput,
)

_ALLOWED_FORMATS: Final = frozenset({"JPEG", "PNG", "WEBP", "GIF"})
_MAX_ASPECT_RATIO: Final = 200.0
_MAX_SOURCE_FRAMES: Final = 1000


class ImagePreprocessingError(RuntimeError):
    """A sanitized error for corrupt, unsafe, or unsupported images."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class ImagePreprocessor:
    """Decode, orient, resize, sample, and encode an in-memory image."""

    def __init__(
        self,
        *,
        max_dimension: int = 2048,
        max_pixels: int = 4_194_304,
        max_prepared_bytes: int = 6_291_456,
        gif_max_frames: int = 4,
    ) -> None:
        values = (max_dimension, max_pixels, max_prepared_bytes, gif_max_frames)
        if any(value <= 0 for value in values):
            raise ValueError("image preprocessing limits must be positive")
        self._max_dimension = max_dimension
        self._max_pixels = max_pixels
        self._max_prepared_bytes = max_prepared_bytes
        self._gif_max_frames = min(gif_max_frames, 8)

    def prepare(
        self,
        downloaded: DownloadedMedia,
        *,
        source: MediaSource,
        summary_hint: str | None = None,
        max_frames: int | None = None,
    ) -> PreparedVisualInput:
        """Prepare at most ``max_frames`` representative frames in time order."""

        if not downloaded.content or downloaded.byte_size != len(downloaded.content):
            raise ImagePreprocessingError("invalid_media", "图片内容为空或大小不一致")
        expected_hash = hashlib.sha256(downloaded.content).hexdigest()
        if downloaded.content_hash and downloaded.content_hash != expected_hash:
            raise ImagePreprocessingError("invalid_media", "图片内容校验失败")

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                image = Image.open(io.BytesIO(downloaded.content))
                image_format = (image.format or "").upper()
                if image_format not in _ALLOWED_FORMATS:
                    raise ImagePreprocessingError("unsupported_format", "不支持该图片格式")
                width, height = image.size
                self._validate_source_geometry(width, height)
                frame_count = int(getattr(image, "n_frames", 1) or 1)
                if frame_count < 1 or frame_count > _MAX_SOURCE_FRAMES:
                    raise ImagePreprocessingError("too_many_frames", "动画帧数异常")
                requested_frames = self._gif_max_frames if max_frames is None else max_frames
                frame_limit = min(max(1, requested_frames), self._gif_max_frames)
                indices = _sample_indices(frame_count, frame_limit)
                frames = self._prepare_frames(image, indices, frame_count)
        except ImagePreprocessingError:
            raise
        except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise ImagePreprocessingError("decompression_bomb", "图片像素规模不安全") from exc
        except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
            raise ImagePreprocessingError("corrupt_image", "图片损坏或无法解析") from exc

        return PreparedVisualInput(
            media_hash=expected_hash,
            frames=frames,
            animated=frame_count > 1,
            source=source,
            summary_hint=_clean_hint(summary_hint),
        )

    def _prepare_frames(
        self,
        image: Image.Image,
        indices: tuple[int, ...],
        frame_count: int,
    ) -> tuple[PreparedFrame, ...]:
        prepared: list[PreparedFrame] = []
        total_bytes = 0
        for frame_index in indices:
            try:
                image.seek(frame_index)
                frame = ImageOps.exif_transpose(image.copy())
                frame.load()
            except (EOFError, OSError, SyntaxError, ValueError) as exc:
                raise ImagePreprocessingError("frame_decode_failed", "动画帧解析失败") from exc
            self._validate_source_geometry(*frame.size)
            frame = self._resize(frame)
            encoded, mime_type = _encode_frame(frame)
            remaining = self._max_prepared_bytes - total_bytes
            if len(encoded) > remaining:
                encoded, mime_type, frame = _shrink_to_budget(frame, remaining)
            if not encoded or len(encoded) > remaining:
                raise ImagePreprocessingError("prepared_too_large", "预处理后的图片仍然过大")
            total_bytes += len(encoded)
            encoded_hash = hashlib.sha256(encoded).hexdigest()
            prepared.append(
                PreparedFrame(
                    content_hash=encoded_hash,
                    mime_type=mime_type,
                    width=frame.width,
                    height=frame.height,
                    frame_index=frame_index,
                    frame_count=frame_count,
                    data_url=(
                        f"data:{mime_type};base64,{base64.b64encode(encoded).decode('ascii')}"
                    ),
                )
            )
        return tuple(prepared)

    def _validate_source_geometry(self, width: int, height: int) -> None:
        if width <= 0 or height <= 0:
            raise ImagePreprocessingError("invalid_dimensions", "图片尺寸无效")
        pixels = width * height
        # Permit ordinary high-resolution photos to be resized, but reject headers
        # that would require an unsafe amount of memory to decode.
        if pixels > self._max_pixels * 8:
            raise ImagePreprocessingError("decompression_bomb", "图片像素规模不安全")
        if max(width / height, height / width) > _MAX_ASPECT_RATIO:
            raise ImagePreprocessingError("extreme_aspect_ratio", "图片宽高比异常")

    def _resize(self, frame: Image.Image) -> Image.Image:
        width, height = frame.size
        dimension_scale = min(1.0, self._max_dimension / max(width, height))
        pixel_scale = min(1.0, math.sqrt(self._max_pixels / (width * height)))
        scale = min(dimension_scale, pixel_scale)
        if scale < 1.0:
            size = (max(1, int(width * scale)), max(1, int(height * scale)))
            frame = frame.resize(size, Image.Resampling.LANCZOS)
        return frame


def _sample_indices(frame_count: int, limit: int) -> tuple[int, ...]:
    if frame_count <= limit:
        return tuple(range(frame_count))
    if limit == 1:
        return (0,)
    values = {round(index * (frame_count - 1) / (limit - 1)) for index in range(limit)}
    return tuple(sorted(values))


def _encode_frame(frame: Image.Image, *, quality: int = 88) -> tuple[bytes, str]:
    output = io.BytesIO()
    if "A" in frame.getbands() or frame.mode in {"LA", "PA"}:
        rgba = frame.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        composited = Image.alpha_composite(background, rgba).convert("RGB")
    else:
        composited = frame.convert("RGB")
    composited.save(output, format="JPEG", quality=quality, optimize=True)
    return output.getvalue(), "image/jpeg"


def _shrink_to_budget(
    frame: Image.Image,
    budget: int,
) -> tuple[bytes, str, Image.Image]:
    if budget <= 0:
        return b"", "image/jpeg", frame
    working = frame
    for quality in (75, 60, 45):
        encoded, mime_type = _encode_frame(working, quality=quality)
        if len(encoded) <= budget:
            return encoded, mime_type, working
    for _attempt in range(4):
        encoded, mime_type = _encode_frame(working, quality=45)
        if len(encoded) <= budget:
            return encoded, mime_type, working
        scale = min(0.85, math.sqrt(budget / max(len(encoded), 1)) * 0.9)
        new_size = (max(1, int(working.width * scale)), max(1, int(working.height * scale)))
        if new_size == working.size:
            break
        working = working.resize(new_size, Image.Resampling.LANCZOS)
    encoded, mime_type = _encode_frame(working, quality=40)
    return encoded, mime_type, working


def _clean_hint(value: str | None) -> str | None:
    if value is None:
        return None
    clean = " ".join(value.split())[:300]
    return clean or None
