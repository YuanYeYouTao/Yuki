"""An independent terminal can catalog one file and explicitly maintain its tags."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from tests.unit.test_sticker_library import picture

from yuki.emoji import sticker_index
from yuki.emoji.sticker_index import StickerIndex


@pytest.mark.skipif(os.name != "posix", reason="The persistent terminal uses Linux flock")
def test_single_file_update_preserves_text_and_replaces_only_explicit_tags(tmp_path):
    runtime = tmp_path / "independent-runtime"
    runtime.mkdir()
    source = Path(sticker_index.__file__)
    for name in ("sticker_index.py", "media.py"):
        shutil.copyfile(source.with_name(name), runtime / name)
    root = tmp_path / "workspace"
    original = root / "stickers/matrix/files/my reaction.png"
    original.parent.mkdir(parents=True)
    original.write_bytes(picture())

    def terminal(*arguments):
        result = subprocess.run(
            [
                sys.executable,
                str(runtime / "sticker_index.py"),
                "--workspace",
                str(root),
                *arguments,
            ],
            cwd=runtime,
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": ""},
        )
        return json.loads(result.stdout)

    relative = original.relative_to(root).as_posix()
    asset = terminal("update", relative, "--tag", "开心", "--tag", "😂")["asset"]
    assert terminal("search", "--platform", "matrix", "开心")["items"][0]["id"] == asset["id"]
    engine = StickerIndex(root)
    metadata = engine.metadata_path("matrix", asset["id"])
    current = engine.read_json(metadata)
    current.update(description="Main自己的描述", ocr_text="原图文字")
    engine.write_json(metadata, current)
    assert terminal("update", relative)["asset"]["tags"] == ["开心", "😂"]
    assert engine.read_json(metadata)["description"] == "Main自己的描述"
    assert engine.read_json(metadata)["ocr_text"] == "原图文字"
    replaced = terminal("update", relative, "--tag", "赞同")["asset"]
    assert replaced["id"] == asset["id"] and replaced["tags"] == ["赞同"]
    assert terminal("search", "--platform", "matrix", "开心")["items"] == []
    matches = terminal("search", "--platform", "matrix", "赞同")["items"]
    assert matches[0]["id"] == asset["id"]
    assert engine.read_json(metadata)["description"] == "Main自己的描述"
    assert engine.read_json(metadata)["ocr_text"] == "原图文字"
    # Hot search leaves a newly copied, unindexed file alone instead of scanning the library.
    (original.parent / "unindexed.png").write_bytes(picture("blue"))
    assert terminal("search", "--platform", "matrix", "赞同")["items"]
    assert len(list(metadata.parent.glob("*.json"))) == 1
    broken = metadata.parent / "broken.json"
    broken.write_text("{invalid JSON", encoding="utf-8")
    (root / "stickers/matrix/search.sqlite3").unlink()
    assert terminal("search", "--platform", "matrix", "赞同")["items"][0]["id"] == asset["id"]
    assert broken.read_text(encoding="utf-8") == "{invalid JSON"
