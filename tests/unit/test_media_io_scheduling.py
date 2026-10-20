"""Slow media files must yield the shared loop; no latency threshold is tested."""

import asyncio
import base64
import hashlib
import io
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from yuki.asr import audio
from yuki.services import media_resolver, video_frames
from yuki.vision.models import DownloadedMedia


@pytest.fixture
def local_decoder(monkeypatch):
    commands = []
    write = Path.write_bytes
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "blue").save(buffer, "JPEG")
    jpeg = buffer.getvalue()
    decoded_audio = b"locally decoded original audio"

    async def run_audio(*args):
        commands.append(args)
        await asyncio.sleep(0)
        if args[0] == "ffprobe":
            return b'{"streams":[{"codec_type":"audio"}],"format":{"duration":"1"}}'
        write(Path(args[-1]), decoded_audio)
        return b""

    async def run_video(*args, **kwargs):
        commands.append(args)
        await asyncio.sleep(0)
        if args[0] == "ffprobe":
            return json.dumps(
                {
                    "streams": [{"width": 8, "height": 8, "avg_frame_rate": "1/1"}],
                    "format": {"duration": "1"},
                }
            ).encode()
        write(Path(args[-1]), jpeg)
        return b""

    monkeypatch.setattr(audio, "_run", run_audio)
    monkeypatch.setattr(video_frames, "_run", run_video)

    async def decode(kind):
        if kind == "asr":
            result = await audio.prepare_audio(b"original admitted audio", max_duration_seconds=30)
            assert result == decoded_audio
        else:
            original = b"original admitted local video"
            result = await video_frames.sample_video(
                DownloadedMedia(
                    content=original,
                    content_type="video/mp4",
                    content_hash=hashlib.sha256(original).hexdigest(),
                    byte_size=len(original),
                ),
                source="current",
                maximum=1,
            )
            assert len(result) == 1
            assert base64.b64decode(result[0].data_url.split(",", 1)[1]) == jpeg
            assert result[0].source == "current"

    return SimpleNamespace(decode=decode, commands=commands)


@pytest.mark.parametrize("kind", ["asr", "video"])
async def test_local_media_decoding_preserves_original_output_and_protocol(local_decoder, kind):
    await local_decoder.decode(kind)
    assert [args[0] for args in local_decoder.commands] == ["ffprobe", "ffmpeg"]
    for args in local_decoder.commands:
        assert args[args.index("-protocol_whitelist") + 1] == "file"
        assert Path(args[-1]).is_absolute()


@pytest.mark.parametrize(
    ("kind", "method", "filename"),
    [
        ("asr", "write_bytes", "input.audio"),
        ("asr", "read_bytes", "output.mp3"),
        ("video", "read_bytes", "frame-0.jpg"),
    ],
    ids=["asr-input-write", "asr-output-read", "video-frame-read"],
)
async def test_blocked_media_file_allows_loop_callback_before_io_finishes(
    monkeypatch, local_decoder, kind, method, filename
):
    loop = asyncio.get_running_loop()
    released = threading.Event()
    loop_progressed = threading.Event()
    observed_order = []
    original = getattr(Path, method)

    def loop_callback():
        loop_progressed.set()
        released.set()

    def blocked_file(path, *args):
        if path.name == filename:
            loop.call_soon_threadsafe(loop_callback)
            # This watchdog only prevents a faulty implementation from deadlocking the
            # test. Passing depends on callback/IO ordering, never elapsed milliseconds.
            watchdog = threading.Timer(5, released.set)
            watchdog.daemon = True
            watchdog.start()
            try:
                released.wait()
                observed_order.append(loop_progressed.is_set())
            finally:
                watchdog.cancel()
        return original(path, *args)

    monkeypatch.setattr(Path, method, blocked_file)
    await local_decoder.decode(kind)
    assert observed_order and all(observed_order)


@pytest.mark.parametrize("blocked_stage", ["directory", "input-write"])
async def test_cancelled_audio_releases_its_directory_after_the_file_owner_finishes(
    monkeypatch, local_decoder, blocked_stage
):
    loop = asyncio.get_running_loop()
    entered, released = asyncio.Event(), threading.Event()
    directories = []
    temporary = media_resolver.TemporaryDirectory
    write = Path.write_bytes

    def hold():
        loop.call_soon_threadsafe(entered.set)
        assert released.wait(5)

    def create(*args, **kwargs):
        directory = temporary(*args, **kwargs)
        directories.append(Path(directory.name))
        if blocked_stage == "directory":
            hold()
        return directory

    def write_input(path, content):
        if path.name == "input.audio":
            hold()
        return write(path, content)

    monkeypatch.setattr(media_resolver, "TemporaryDirectory", create)
    if blocked_stage == "input-write":
        monkeypatch.setattr(Path, "write_bytes", write_input)
    task = asyncio.create_task(local_decoder.decode("asr"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert directories and all(path.is_dir() for path in directories)
    finally:
        released.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert all(not path.exists() for path in directories)
    assert not local_decoder.commands
