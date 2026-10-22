"""Background catalog facts cannot restore text read before a completed terminal edit."""

import asyncio
import io
import os
import threading

import pytest
from PIL import Image
from tests.unit.test_sticker_library import library as library
from tests.unit.test_sticker_library import store_content

from yuki.emoji.sticker_index import StickerIndex
from yuki.services.media_resolver import _media_io


@pytest.mark.skipif(os.name != "posix", reason="File index coordination uses Linux flock")
@pytest.mark.parametrize("move_original", (False, True))
async def test_duplicate_native_flag_keeps_metadata_edited_during_original_hash(
    tmp_path, monkeypatch, move_original
):
    files = tmp_path / "stickers/qq/files"
    files.mkdir(parents=True)
    output = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(output, format="PNG")
    first, duplicate = files / "first.png", files / "duplicate.png"
    first.write_bytes(output.getvalue())
    duplicate.write_bytes(output.getvalue())
    main = StickerIndex(tmp_path)
    asset = main.catalog("qq", "stickers/qq/files/first.png", tags=["oldterm"])["asset"]
    main.rebuild()
    background = StickerIndex(tmp_path)
    original_facts = background.facts
    entered, release = threading.Event(), threading.Event()

    def slow_canonical_hash(path, **kwargs):
        result = original_facts(path, **kwargs)
        if path == first:
            entered.set()
            release.wait()
        return result

    monkeypatch.setattr(background, "facts", slow_canonical_hash)
    task = asyncio.create_task(
        _media_io(
            lambda: background.catalog("qq", "stickers/qq/files/duplicate.png", is_sticker=True)
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        path = main.metadata_path("qq", asset["id"])
        edited = main.read_json(path)
        edited.update(tags=["newterm"], description="current description", ocr_text="new OCR")
        if move_original:
            moved = first.with_name("moved.png")
            first.rename(moved)
            edited["relative_path"] = str(moved.relative_to(tmp_path))
        main.write_json(path, edited)
        await _media_io(main.update, str(path.relative_to(tmp_path)))
        assert main.search("qq", "newterm")["items"]
        release.set()
        result = await task
        current = main.read_json(path)
        assert current["is_sticker"] and result["asset"]["is_sticker"]
        assert current["tags"] == result["asset"]["tags"] == ["newterm"]
        assert current["relative_path"] == edited["relative_path"]
        assert current["description"] == "current description" and current["ocr_text"] == "new OCR"
        assert main.search("qq", "newterm")["items"]
        assert main.search("qq", "oldterm")["items"] == []
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.skipif(os.name != "posix", reason="File index coordination uses Linux flock")
async def test_cache_deleted_between_ready_and_query_rebuilds_from_actual_files(
    library, monkeypatch
):
    value, bridge = library
    output = io.BytesIO()
    Image.new("RGB", (8, 8), "green").save(output, format="PNG")
    asset = await store_content(value, output.getvalue(), platform="qq", tags=("selected",))
    await value.rebuild()
    original_ready = StickerIndex.ready
    removed = False

    def remove_after_ready(engine, platform):
        nonlocal removed
        result = original_ready(engine, platform)
        if result and not removed:
            removed = True
            (bridge.root / f"stickers/{platform}/search.sqlite3").unlink()
        return result

    monkeypatch.setattr(StickerIndex, "ready", remove_after_ready)
    assert (await value.select("selected", platform="qq")).id == asset.id
    assert removed and StickerIndex(bridge.root).ready("qq")
