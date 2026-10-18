"""Local MP4/MOV and WebM sampling; URLs and audio never reach the decoder/model."""

from __future__ import annotations

import base64
import json
import math
from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory

from yuki.domain.messages import ChatImage
from yuki.services.media_process import MediaProcessError, run_media_process
from yuki.services.media_resolver import _media_io
from yuki.services.vision_service import VisionProcessingError
from yuki.vision.models import DownloadedMedia


def is_video_header(header: bytes) -> bool:
    return header[4:8] == b"ftyp" or header.startswith(b"\x1aE\xdf\xa3")


async def _run(*args: str, env: dict[str, str] | None = None) -> bytes:
    try:
        return await run_media_process(*args, env=env)
    except MediaProcessError as exc:
        raise VisionProcessingError("invalid_video", "视频无法解析") from exc


async def sample_video(
    media: DownloadedMedia | Path,
    *,
    source: str,
    maximum: int,
    max_duration_seconds: int = 600,
    sample_interval_seconds: int = 5,
) -> tuple[ChatImage, ...]:
    """Adapt sampling to duration and the shared frame budget; never cache files."""
    if maximum <= 0:
        return ()
    if max_duration_seconds <= 0 or sample_interval_seconds <= 0:
        raise ValueError("video sampling settings must be positive")
    with TemporaryDirectory(prefix="yuki-video-") as directory:
        if isinstance(media, Path):
            path = media  # Caller owns this temporary download, never a model-supplied path.
        else:
            path = Path(directory) / "input.mp4"
            await _media_io(path.write_bytes, media.content)
        metadata = json.loads(
            await _run(
                "ffprobe",
                "-v",
                "error",
                "-max_alloc",
                "33554432",
                "-protocol_whitelist",
                "file",
                "-format_whitelist",
                "mov,matroska,webm",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height,avg_frame_rate,duration:format=duration",
                "-of",
                "json",
                str(path),
            )
        )
        streams = metadata.get("streams", [])
        duration = float(metadata.get("format", {}).get("duration", 0))
        if not streams or not math.isfinite(duration) or not 0 < duration <= max_duration_seconds:
            raise VisionProcessingError("video_limit", "视频超过配置的时长上限或不包含画面")
        width, height = int(streams[0].get("width", 0)), int(streams[0].get("height", 0))
        if min(width, height) <= 0 or max(width, height) > 4096:
            raise VisionProcessingError("video_limit", "视频分辨率超过限制")
        try:
            video_duration = float(streams[0].get("duration", duration))
        except (TypeError, ValueError):
            video_duration = duration
        if math.isfinite(video_duration) and video_duration > 0:
            # Audio can extend past the last video frame. Seeking against the
            # container duration then succeeds with no image output at EOF.
            duration = min(duration, video_duration)
        count = min(maximum, max(1, math.ceil(duration / sample_interval_seconds) + 1))
        try:
            frame_rate = float(Fraction(streams[0].get("avg_frame_rate", "0/1")))
        except (ValueError, ZeroDivisionError):
            frame_rate = 0.0
        frame_period = 1 / frame_rate if frame_rate > 0 else 1.0
        last_position = max(0.0, duration - max(frame_period, 0.05))
        frames: list[ChatImage] = []
        for index in range(count):
            # Leave one frame period before EOF (also works for low-FPS clips).
            position = last_position * index / max(1, count - 1)
            output = Path(directory) / f"frame-{index}.jpg"
            await _run(
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-max_alloc",
                "33554432",
                "-threads",
                "1",
                "-protocol_whitelist",
                "file",
                "-format_whitelist",
                "mov,matroska,webm",
                "-ss",
                str(position),
                "-i",
                str(path),
                "-map",
                "0:v:0",
                "-an",
                "-sn",
                "-dn",
                "-frames:v",
                "1",
                "-vf",
                "scale=768:768:force_original_aspect_ratio=decrease:out_range=full,format=yuvj420p",
                "-threads",
                "1",
                "-q:v",
                "3",
                "-fs",
                "2097152",
                str(output),
            )
            if not output.is_file() or not 0 < output.stat().st_size <= 2097152:
                raise VisionProcessingError("invalid_video", "视频抽帧失败")
            frames.append(
                ChatImage(
                    data_url="data:image/jpeg;base64,"
                    + base64.b64encode(output.read_bytes()).decode(),
                    source=source,
                    video_timestamp_seconds=position,
                )
            )
        return tuple(frames)
