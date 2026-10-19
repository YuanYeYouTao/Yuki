import asyncio
import shutil

import pytest

from yuki.asr.audio import prepare_audio
from yuki.asr.provider import ASRError

pytestmark = pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="Requires native FFmpeg"
)


async def live_webm(seconds: float) -> bytes:
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000",
        "-t",
        str(seconds),
        "-c:a",
        "libopus",
        "-f",
        "webm",
        "pipe:1",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    output, error = await process.communicate()
    assert process.returncode == 0, error.decode()
    return output


async def test_streamed_webm_without_duration_is_decoded_before_asr():
    audio = await prepare_audio(await live_webm(0.25), max_duration_seconds=1)
    assert audio.startswith((b"ID3", b"\xff"))


async def test_streamed_webm_over_configured_duration_is_not_silently_truncated():
    with pytest.raises(ASRError, match="audio_limit"):
        await prepare_audio(await live_webm(1.5), max_duration_seconds=1)
