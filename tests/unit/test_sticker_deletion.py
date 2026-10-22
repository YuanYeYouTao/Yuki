"""File deletion stays authoritative across indexing and optional descriptions."""

import asyncio
import os
import sqlite3
from types import SimpleNamespace

import pytest
from tests.unit.test_sticker_library import (
    attach_caption_provider,
    index_path,
    picture,
    store_content,
)
from tests.unit.test_sticker_library import (
    library as library,
)

from yuki.emoji.sticker_index import StickerIndex
from yuki.services.media_resolver import _media_io

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Index uses Linux flock")


async def test_late_description_does_not_recreate_deleted_metadata(library):
    value, bridge = library
    provider = attach_caption_provider(value)
    entered, release = asyncio.Event(), asyncio.Event()

    async def analyze(*args, **kwargs):
        entered.set()
        await release.wait()
        return await provider.analyze(*args, **kwargs)

    value.provider = SimpleNamespace(analyze=analyze)
    asset = await store_content(value, picture(), platform="qq", is_sticker=True)
    await entered.wait()
    metadata = index_path(bridge, asset)
    metadata.unlink()
    release.set()
    await asyncio.gather(*tuple(value._tasks), return_exceptions=True)
    assert not metadata.exists()
    assert (bridge.root / asset.relative_path).exists()


@pytest.mark.parametrize("replace_original", (False, True))
@pytest.mark.parametrize("cache_ready", (False, True))
async def test_rebuild_removes_metadata_and_cache_when_original_is_gone(
    library, replace_original, cache_ready
):
    value, bridge = library
    asset = await store_content(value, picture(), platform="qq", tags=("removed",))
    if cache_ready:
        await value.rebuild()
    original = bridge.root / asset.relative_path
    if replace_original:
        original.write_bytes(picture("blue"))
    else:
        original.unlink()
    await value.rebuild()
    assert not index_path(bridge, asset).exists()
    with sqlite3.connect(bridge.root / "stickers/qq/search.sqlite3") as db:
        assert db.execute("SELECT 1 FROM assets WHERE id=?", (asset.id,)).fetchone() is None
        assert db.execute("SELECT 1 FROM terms WHERE asset_id=?", (asset.id,)).fetchone() is None


@pytest.mark.parametrize("duplicate", (False, True))
async def test_rebuild_preserves_tags_for_renamed_or_remaining_duplicate(library, duplicate):
    value, bridge = library
    asset = await store_content(value, picture(), platform="qq", tags=("retained",))
    original = bridge.root / asset.relative_path
    renamed = original.with_name("my chosen name.png")
    if duplicate:
        renamed.write_bytes(original.read_bytes())
        original.unlink()
    else:
        original.rename(renamed)
    await value.rebuild()
    selected = await value.select("retained", platform="qq")
    assert selected is not None and selected.id == asset.id
    assert selected.relative_path == renamed.relative_to(bridge.root).as_posix()


async def test_rebuild_keeps_metadata_edited_after_scan_started(library, monkeypatch):
    value, bridge = library
    removed = await store_content(value, picture(), platform="qq", tags=("old",))
    other = await store_content(value, picture("blue"), platform="qq")
    (bridge.root / removed.relative_path).unlink()
    engine = StickerIndex(bridge.root)
    metadata = index_path(bridge, removed)
    facts = engine.facts

    def edit_while_scanning(path, **kwargs):
        if path == bridge.root / other.relative_path:
            current = engine.read_json(metadata)
            current["tags"] = ["new edit"]
            engine.write_json(metadata, current)
        return facts(path, **kwargs)

    monkeypatch.setattr(engine, "facts", edit_while_scanning)
    await _media_io(engine.rebuild, "qq")
    assert engine.read_json(metadata)["tags"] == ["new edit"]


async def test_failed_original_read_does_not_discard_renamed_metadata(library, monkeypatch):
    value, bridge = library
    asset = await store_content(value, picture(), platform="qq", tags=("retained",))
    original = bridge.root / asset.relative_path
    renamed = original.with_name("renamed.png")
    original.rename(renamed)
    engine = StickerIndex(bridge.root)
    facts = engine.facts

    def unreadable(path, **kwargs):
        if path == renamed:
            raise PermissionError("actual read failure")
        return facts(path, **kwargs)

    monkeypatch.setattr(engine, "facts", unreadable)
    result = await _media_io(engine.rebuild, "qq")
    assert result["errors"] == 1
    assert engine.read_json(index_path(bridge, asset))["tags"] == ["retained"]


@pytest.mark.parametrize("rename_metadata", (False, True))
async def test_metadata_update_removes_previous_asset_from_cache(library, rename_metadata):
    value, bridge = library
    asset = await store_content(value, picture(), platform="qq", tags=("old",))
    await value.rebuild()
    original = bridge.root / asset.relative_path
    original.write_bytes(picture("blue"))
    engine = StickerIndex(bridge.root)
    metadata = index_path(bridge, asset)
    if rename_metadata:
        renamed = metadata.with_name("my sticker.json")
        metadata.rename(renamed)
        metadata = renamed
    updated = await _media_io(engine.update, metadata.relative_to(bridge.root).as_posix())
    assert updated["asset"]["id"] != asset.id
    assert not index_path(bridge, asset).exists()
    with sqlite3.connect(bridge.root / "stickers/qq/search.sqlite3") as db:
        assert db.execute("SELECT 1 FROM assets WHERE id=?", (asset.id,)).fetchone() is None
        assert db.execute("SELECT 1 FROM terms WHERE asset_id=?", (asset.id,)).fetchone() is None
