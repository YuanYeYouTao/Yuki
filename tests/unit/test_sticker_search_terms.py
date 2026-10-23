"""Mixed-script sticker goals use real words and upgrade disposable old terms."""

import os
import sqlite3
from contextlib import closing

import pytest
from tests.unit.test_sticker_library import edit, index_path, picture, seed_cached_sticker
from tests.unit.test_sticker_library import library as library

from yuki.emoji.sticker_index import terms


def test_mixed_script_terms_preserve_unicode_words_and_emoji():
    result = terms("Yuki开心大笑 Café开心 😂")
    assert {"yuki", "café", "开心", "大笑", "开", "笑", "😂"} <= result
    assert not {"y", "u", "k", "i", "c", "a", "f", "é", "ki", "i开"} & result


@pytest.mark.skipif(os.name != "posix", reason="The real index uses Linux flock")
async def test_mixed_goal_selects_yuki_instead_of_latin_fragments_in_description(library):
    value, bridge = library
    intended = await seed_cached_sticker(
        value, picture(), platform="qq", tags=("开心", "大笑", "Yuki", "哈哈")
    )
    other = await seed_cached_sticker(
        value, picture("green"), platform="qq", tags=("开心", "期待", "满足", "吃饭时间")
    )
    edit(
        bridge,
        other,
        description="Hololive旗下Suzume穿着lucky卫衣，手持叉子，面带微笑，期待美食。",
    )
    await value.rebuild()
    assert (await value.select("Yuki开心大笑", emotion="happy", platform="qq")).id == intended.id


@pytest.mark.skipif(os.name != "posix", reason="The real index uses Linux flock")
async def test_description_ocr_and_native_emoji_remain_searchable_without_tag_gate(library):
    value, bridge = library
    described = await seed_cached_sticker(value, picture(), platform="qq")
    edit(bridge, described, description="紫薇星", ocr_text="observatory")
    native = await seed_cached_sticker(value, picture("blue"), platform="qq", tags=("😂",))
    await value.rebuild()
    assert (await value.select("紫薇星", platform="qq")).id == described.id
    assert (await value.select("observatory", platform="qq")).id == described.id
    assert (await value.select("😂", platform="qq")).id == native.id


@pytest.mark.skipif(os.name != "posix", reason="The real index uses Linux flock")
async def test_v1_cache_rebuilds_terms_without_changing_canonical_files(library):
    value, bridge = library
    asset = await seed_cached_sticker(
        value, picture(), platform="qq", tags=("Yuki", "开心", "大笑")
    )
    await value.rebuild()
    metadata = index_path(bridge, asset)
    original_json = metadata.read_bytes()
    original_image = (bridge.root / asset.relative_path).read_bytes()
    cache = bridge.root / "stickers/qq/search.sqlite3"
    with closing(sqlite3.connect(cache)) as db, db:
        # A real v1 cache contains character fragments and lacks the complete word.
        db.execute("DELETE FROM terms")
        db.executemany("INSERT INTO terms VALUES(?,?)", ((term, asset.id) for term in ("i", "u")))
        db.execute("INSERT INTO assets VALUES('00000000-0000-0000-0000-000000000001')")
        db.execute("INSERT INTO terms VALUES('i','00000000-0000-0000-0000-000000000001')")
        db.execute("PRAGMA user_version=1")

    assert (await value.select("Yuki开心大笑", platform="qq")).id == asset.id
    assert metadata.read_bytes() == original_json
    assert (bridge.root / asset.relative_path).read_bytes() == original_image
