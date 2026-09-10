"""Bounded local MP4 sampling; URLs and audio never reach the decoder/model."""

from __future__ import annotations

import asyncio
import base64
import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory

from qq_ai_bot.domain.messages import ChatImage
from qq_ai_bot.services.vision_service import VisionProcessingError
from qq_ai_bot.vision.models import DownloadedMedia


async def _run(*args: str) -> bytes:
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    try:
        async with asyncio.timeout(15):
            output, _ = await process.communicate()
        if process.returncode:
            raise VisionProcessingError("invalid_video", "视频无法解析")
        return output
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def sample_video(
    media: DownloadedMedia, *, source: str, maximum: int
) -> tuple[ChatImage, ...]:
    """Sample at most four frames across a <=120s MP4; do not imply full viewing."""
    if maximum <= 0:
        return ()
    if len(media.content) > 32 * 1024 * 1024:
        raise VisionProcessingError("video_limit", "视频文件过大")
    if len(media.content) < 12 or media.content[4:8] != b"ftyp":
        raise VisionProcessingError("unsupported_video", "目前仅支持 MP4/MOV 视频画面")
    with TemporaryDirectory(prefix="yuki-video-") as directory:
        path = Path(directory) / "input.mp4"
        path.write_bytes(media.content)
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
                "mov",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height:format=duration",
                "-of",
                "json",
                str(path),
            )
        )
        streams = metadata.get("streams", [])
        duration = float(metadata.get("format", {}).get("duration", 0))
        if not streams or not math.isfinite(duration) or not 0 < duration <= 120:
            raise VisionProcessingError("video_limit", "视频需在 120 秒以内且包含画面")
        width, height = int(streams[0].get("width", 0)), int(streams[0].get("height", 0))
        if min(width, height) <= 0 or max(width, height) > 4096:
            raise VisionProcessingError("video_limit", "视频分辨率超过限制")
        count = min(maximum, 4)
        frames: list[ChatImage] = []
        for index in range(count):
            position = duration * index / count
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
                "mov",
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
                "scale=768:768:force_original_aspect_ratio=decrease",
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
