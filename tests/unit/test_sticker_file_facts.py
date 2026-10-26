"""Sticker facts follow the file the publish bridge can open."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from PIL import Image

from yuki.emoji.sticker_index import StickerIndex


def _png(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (2, 2), "red").save(path, "PNG")


def test_regular_file_facts_use_the_open_descriptor(tmp_path):
    workspace = tmp_path / "workspace"
    original = workspace / "stickers" / "qq" / "files" / "red.png"
    _png(original)
    facts = StickerIndex(workspace).facts(original, inspect_pixels=False)
    assert facts["relative_path"] == "stickers/qq/files/red.png"
    assert facts["image_format"] == "PNG"
    assert facts["byte_size"] == original.stat().st_size


def test_internal_symlink_normalizes_to_the_regular_file(tmp_path):
    workspace = tmp_path / "workspace"
    original = workspace / "stickers" / "qq" / "files" / "red.png"
    alias = workspace / "stickers" / "qq" / "files" / "alias.png"
    _png(original)
    try:
        alias.symlink_to(original)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")
    facts = StickerIndex(workspace).facts(alias, inspect_pixels=False)
    assert facts["relative_path"] == "stickers/qq/files/red.png"
    assert Path(facts["relative_path"]).name != "alias.png"


def test_symlink_outside_this_platform_and_hardlink_stay_invalid(tmp_path):
    workspace = tmp_path / "workspace"
    original = workspace / "stickers" / "qq" / "files" / "red.png"
    other = workspace / "stickers" / "tg" / "files" / "other.png"
    outside = tmp_path / "outside.png"
    _png(original)
    _png(other)
    _png(outside)
    index = StickerIndex(workspace)
    linked = workspace / "stickers" / "qq" / "files" / "linked.png"
    try:
        os.link(original, linked)
    except OSError as exc:
        pytest.skip(f"hardlinks are unavailable: {exc}")
    if linked.stat().st_nlink == 1:
        pytest.skip("this OS does not report the extra hard link")
    with pytest.raises(ValueError, match="unsupported_sticker_file"):
        index.facts(linked, inspect_pixels=False)
    assert not (workspace / "stickers" / "qq" / "files").joinpath("linked-copy.png").exists()

    try:
        crossed = workspace / "stickers" / "qq" / "files" / "crossed.png"
        crossed.symlink_to(other)
        escaped = workspace / "stickers" / "qq" / "files" / "escaped.png"
        escaped.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")
    with pytest.raises(ValueError, match="unsupported_sticker_link"):
        index.facts(crossed, inspect_pixels=False)
    with pytest.raises(ValueError, match="unsupported_sticker_link"):
        index.facts(escaped, inspect_pixels=False)
