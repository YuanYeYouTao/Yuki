"""A saved original is named from its header. Description reads that file later."""

from __future__ import annotations

import asyncio
import hashlib
import threading
from pathlib import Path

from yuki.emoji.library import EmojiLibrary
from yuki.emoji.models import EmojiAsset


def _asset(digest: str) -> EmojiAsset:
    return EmojiAsset(
        id="11111111-1111-1111-1111-111111111111",
        platform="qq",
        sha256=digest,
        relative_path=f"stickers/qq/files/{digest}.bin",
        mime_type="application/octet-stream",
        image_format="BIN",
        byte_size=4,
    )


class _Sandbox:
    def __init__(self, digest: str) -> None:
        self.digest = digest
        self.calls: list[str] = []

    async def execute(self, _tool, payload, request_id):
        del request_id
        self.calls.append(payload["command"])
        if payload["command"] == "lookup":
            return {"asset": None}
        return {"created": True, "asset": _asset(self.digest).model_dump(mode="json")}


class _Workspace:
    def __init__(self, digest: str) -> None:
        self.sandbox = _Sandbox(digest)

    async def copy_from_cache(self, **_kwargs):
        return {"path": "/workspace/stickers/qq/files/copied.bin"}


async def test_save_without_vision_does_not_read_the_original(tmp_path, monkeypatch):
    path = tmp_path / "original.bin"
    path.write_bytes(b"bytes")
    digest = hashlib.sha256(b"bytes").hexdigest()

    def refuse_full_read(self, *_args, **_kwargs):
        raise AssertionError("the whole original was read before a description existed")

    monkeypatch.setattr(Path, "read_bytes", refuse_full_read)
    library = EmojiLibrary(_Workspace(digest))
    asset = await library.store_cached(
        path,
        shared_path="cache/original.bin",
        sha256=digest,
        platform="qq",
        source_key="source",
        request_id="request",
    )
    assert asset.sha256 == digest
    assert library._tasks == set()
    assert library.workspace.sandbox.calls == ["lookup", "catalog"]


async def test_description_rejects_a_changed_original_and_keeps_the_asset(tmp_path):
    path = tmp_path / "original.bin"
    path.write_bytes(b"changed")
    digest = hashlib.sha256(b"original").hexdigest()
    analyzed = False

    class Provider:
        async def analyze(self, *_args, **_kwargs):
            nonlocal analyzed
            analyzed = True

    async def runtime():
        raise AssertionError("vision was prepared after the source check failed")

    library = EmojiLibrary(
        _Workspace(digest),
        preprocessor=object(),
        provider=Provider(),
        vision_runtime=runtime,
    )
    asset = _asset(digest)
    assert await library.describe(asset, source_path=path, expected_sha=digest) is asset
    assert analyzed is False


async def test_cancelled_description_waits_for_the_file_read(tmp_path):
    path = tmp_path / "original.bin"
    payload = b"visible"
    path.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    started, release = threading.Event(), threading.Event()
    real_open = Path.open

    def blocking_open(self, *args, **kwargs):
        if self == path:
            started.set()
            assert release.wait(3)
        return real_open(self, *args, **kwargs)

    Path.open = blocking_open
    library = EmojiLibrary(
        _Workspace(digest),
        preprocessor=object(),
        provider=object(),
        vision_runtime=lambda: None,
    )
    task = asyncio.create_task(
        library.describe(_asset(digest), source_path=path, expected_sha=digest)
    )
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        done, _pending = await asyncio.wait({task}, timeout=0.05)
        assert done == set()
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled()
    finally:
        Path.open = real_open
        release.set()
