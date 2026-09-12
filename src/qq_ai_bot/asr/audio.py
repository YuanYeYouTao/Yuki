"""Normalize bounded audio bytes without letting ffmpeg open remote resources."""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory

from qq_ai_bot.asr.provider import ASRError

_FORMATS = "wav,mp3,flac,ogg,amr,aac,mov,matroska,webm,aiff"


async def _run(*args: str) -> bytes:
    try:
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
    except OSError as exc:
        raise ASRError("decoder_unavailable") from exc
    try:
        async with asyncio.timeout(15):
            output, _ = await process.communicate()
        if process.returncode:
            raise ASRError("invalid_audio")
        return output
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def prepare_audio(content: bytes, *, max_duration_seconds: int) -> bytes:
    """Transcode to mono MP3; QQ SILK is converted by get_record before this step."""
    with TemporaryDirectory(prefix="yuki-asr-") as directory:
        source = Path(directory) / "input.audio"
        output = Path(directory) / "output.mp3"
        source.write_bytes(content)
        input_options = (
            "-v",
            "error",
            "-max_alloc",
            "33554432",
            "-protocol_whitelist",
            "file",
            "-format_whitelist",
            _FORMATS,
        )
        try:
            probe = json.loads(
                await _run(
                    "ffprobe",
                    *input_options,
                    "-select_streams",
                    "a:0",
                    "-show_entries",
                    "stream=codec_type:format=duration",
                    "-of",
                    "json",
                    str(source),
                )
            )
            duration = float(probe.get("format", {}).get("duration", 0))
            if not probe.get("streams"):
                raise ASRError("invalid_audio")
            if not math.isfinite(duration) or not 0 < duration <= max_duration_seconds:
                raise ASRError("audio_limit")
        except (ValueError, TypeError, AttributeError) as exc:
            raise ASRError("invalid_audio") from exc
        await _run(
            "ffmpeg",
            "-nostdin",
            *input_options,
            "-threads",
            "1",
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-vn",
            "-sn",
            "-dn",
            "-map_metadata",
            "-1",
            "-t",
            str(max_duration_seconds + 1),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "64k",
            "-threads",
            "1",
            "-fs",
            "7000000",
            str(output),
        )
        if not output.is_file() or not 0 < output.stat().st_size < 7_000_000:
            raise ASRError("audio_limit")
        return output.read_bytes()
