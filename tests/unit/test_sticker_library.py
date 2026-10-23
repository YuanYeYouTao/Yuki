"""File-owned metadata, actual SQLite cache and ordinary workspace snapshots."""

from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import io
import json
import os
import shutil
import sqlite3
import struct
import subprocess
import sys
import threading
import zlib
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PIL import Image
from tests.support.service_dependencies import workspace_service
from tests.support.workspace_snapshots import artifact_ids

from yuki.domain.messages import AttachmentKind, MessageAttachment
from yuki.emoji.library import EmojiLibrary
from yuki.emoji.sticker_index import StickerIndex
from yuki.services.image_preprocessor import ImagePreprocessor
from yuki.services.media_resolver import _media_io
from yuki.vision.fake import FakeVisionProvider
from yuki.workspace.files import FileWorkspace
from yuki.workspace.store import WorkspaceError, WorkspaceStore

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="Persistent environment uses Linux FDs/flock"
)


def picture(color="red") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (12, 8), color).save(output, format="PNG")
    return output.getvalue()


class LocalDirectoryBridge:
    """Actual standalone engine, files, content hashes and snapshot SQLite."""

    def __init__(self, root, store, *, cache_root=None):
        self.root, self.store = root, store
        self.cache = cache_root or root.parent / "cache"
        root.mkdir(parents=True, exist_ok=True)
        self.calls = []

    async def execute(self, method, args, *, request_id):
        self.calls.append((method, args))
        if method == "sticker_index":
            return await _media_io(StickerIndex(self.root).execute, args)
        if method == "workspace_copy":
            source = self.cache / str(args["path"]).removeprefix("/inputs/media-cache/")
            with source.open("rb") as stream:
                return FileWorkspace(self.root).copy(
                    args["destination"],
                    stream.fileno(),
                    expected_version=args["expected_version"],
                )
        if method in {"workspace_media_read", "workspace_media_validate"}:
            target = self.root / str(args["path"]).removeprefix("/workspace/")
            if not target.exists():
                return {"error": "FileNotFoundError"}
            content = target.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            if args.get("expected_version") not in {None, digest}:
                return {"error": "version_conflict"}
            return {"version": digest, "base64": base64.b64encode(content).decode()}
        if method == "workspace_publish":
            path = self.root / str(args["path"]).removeprefix("/workspace/")
            with path.open("rb") as stream:
                return self.store.snapshot(
                    stream.fileno(),
                    args.get("name") or path.name,
                    artifact_id=args["artifact_id"],
                    source_key=args["source_key"],
                    expected_version=args.get("expected_version"),
                )
        raise AssertionError(method)


@pytest.fixture
async def library(tmp_path):
    store = WorkspaceStore(tmp_path / "artifacts")
    workspace = workspace_service(store)
    bridge = LocalDirectoryBridge(tmp_path / "files", store)
    workspace.sandbox = bridge
    value = EmojiLibrary(workspace)
    yield value, bridge
    await value.close()


def cached_sticker_source(value, content):
    """Prepare one stable original before any collection starts."""
    digest = hashlib.sha256(content).hexdigest()
    relative = f"v1/{digest}"
    original = value.workspace.sandbox.cache / relative
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_bytes(content)
    return {
        "cached_path": original,
        "shared_path": "/inputs/media-cache/" + relative,
        "sha256": digest,
        "source_key": "test-original:" + digest,
    }


async def seed_cached_sticker(value, content, **arguments):
    """Seed the real shared original; collection itself only copies its cache path."""
    return await value.store_cached(
        **cached_sticker_source(value, content),
        request_id=arguments.pop("request_id", f"test-sticker:{uuid4()}"),
        **arguments,
    )


def index_path(bridge, asset):
    return bridge.root / f"stickers/{asset.platform}/index/{asset.id}.json"


def edit(bridge, asset, **changes):
    path = index_path(bridge, asset)
    record = json.loads(path.read_text())
    record.update(changes)
    path.write_text(json.dumps(record, ensure_ascii=False))
    return path


async def test_file_unique_source_and_platform_catalogs(library):
    value, bridge = library
    qq = await seed_cached_sticker(value, picture(), platform="qq", tags=("大笑",))
    tg = await seed_cached_sticker(value, picture(), platform="telegram", tags=("反讽",))
    third = await seed_cached_sticker(value, picture(), platform="matrix", tags=("😂",))
    assert len({qq.id, tg.id, third.id}) == 3 and qq.sha256 == tg.sha256
    assert (await value.select("大笑", platform="qq")).id == qq.id
    assert await value.select("反讽", platform="qq") is None
    assert (await value.select("😂", platform="matrix")).id == third.id
    assert json.loads(index_path(bridge, qq).read_text())["tags"] == ["大笑"]


async def test_duplicate_original_avoids_copy_and_renamed_file_stays_canonical(library):
    value, bridge = library
    asset = await seed_cached_sticker(value, picture(), platform="qq", tags=("大笑",))
    old = bridge.root / asset.relative_path
    renamed = old.with_name("my picture.png")
    old.rename(renamed)
    await value.rebuild()
    bridge.calls.clear()
    second = await seed_cached_sticker(value, picture(), platform="qq")
    assert second.id == asset.id and second.relative_path.endswith("my picture.png")
    assert not any(method == "workspace_copy" for method, _ in bridge.calls)
    assert len(list(renamed.parent.iterdir())) == 1


async def test_native_repeat_updates_only_real_sticker_fact(library):
    value, bridge = library
    first = await seed_cached_sticker(value, picture(), platform="qq", tags=("manual_tag",))
    assert not first.is_sticker
    provider = attach_caption_provider(value)
    bridge.calls.clear()
    native = await seed_cached_sticker(value, picture(), platform="qq", is_sticker=True)
    assert native.id == first.id and native.is_sticker and native.tags == ("manual_tag",)
    assert json.loads(index_path(bridge, native).read_text())["is_sticker"] is True
    assert not provider.requests
    assert not any(method == "workspace_copy" for method, _ in bridge.calls)


async def test_duplicate_sticker_keeps_first_caption_without_reloading_original(
    library, monkeypatch
):
    value, bridge = library
    provider = attach_caption_provider(value)
    content = picture()
    first = await seed_cached_sticker(value, content, platform="qq", tags=("manual_tag",))
    await asyncio.gather(*tuple(value._tasks))
    edit(bridge, first, tags=["manual_tag"])
    description = json.loads(index_path(bridge, first).read_text())["description"]
    original = bridge.cache / "v1" / first.sha256
    read_bytes = Path.read_bytes

    def read_other_file(path):
        assert path != original, "an already cataloged sticker must not reload the cache"
        return read_bytes(path)

    def unexpected_inspection(_content):
        raise AssertionError("an already cataloged sticker must not inspect the cache")

    monkeypatch.setattr(Path, "read_bytes", read_other_file)
    monkeypatch.setattr("yuki.emoji.library.inspect_content", unexpected_inspection)
    bridge.calls.clear()
    native = await seed_cached_sticker(value, content, platform="qq", is_sticker=True)
    repeated = await seed_cached_sticker(value, content, platform="qq", is_sticker=True)
    assert native.id == repeated.id == first.id
    assert native.is_sticker and repeated.is_sticker
    assert native.tags == repeated.tags == ("manual_tag",)
    cataloged = json.loads(index_path(bridge, native).read_text())
    assert cataloged["is_sticker"] is True and cataloged["description"] == description
    assert len(provider.requests) == 1
    assert not any(method == "workspace_copy" for method, _ in bridge.calls)


async def test_new_sticker_rejects_changed_cache_before_copy(library):
    value, bridge = library
    digest = hashlib.sha256(picture()).hexdigest()
    source = bridge.cache / "v1" / digest
    source.parent.mkdir(parents=True)
    source.write_bytes(picture("blue"))
    with pytest.raises(WorkspaceError, match="attachment_source_changed"):
        await value.store_cached(
            source,
            shared_path="/inputs/media-cache/v1/" + digest,
            sha256=digest,
            platform="qq",
            source_key="attachment:101:0",
            request_id="actual-original-changed",
            is_sticker=True,
        )
    assert not any(method == "workspace_copy" for method, _ in bridge.calls)
    assert not (bridge.root / "stickers/qq/files").exists()


async def test_terminal_tags_update_and_actual_facts_rebuild(library):
    value, bridge = library
    asset = await seed_cached_sticker(value, picture(), platform="qq", tags=("旧tag",))
    path = edit(
        bridge,
        asset,
        tags=["反讽", "幸灾乐祸"],
        description="故作开心",
        id="wrong-id",
        mime_type="application/fake",
        relative_path="not-the-real-file",
    )
    await value.rebuild()
    selected = await value.select("反讽", platform="qq")
    assert selected.id == asset.id and selected.relative_path == asset.relative_path
    assert selected.mime_type == "image/png" and selected.description == ""
    assert json.loads(path.read_text())["description"] == "故作开心"
    edit(bridge, selected, tags=["新tag"])
    await _media_io(StickerIndex(bridge.root).update, str(path.relative_to(bridge.root)))
    assert (await value.select("新tag", platform="qq")).id == asset.id
    assert await value.select("反讽", platform="qq") is None


async def test_delete_metadata_never_restores_cached_tags(library):
    value, bridge = library
    asset = await seed_cached_sticker(value, picture(), platform="qq", tags=("cachedold",))
    await value.rebuild()
    index_path(bridge, asset).unlink()
    assert await value.select("cachedold", platform="qq") is None
    await value.rebuild()
    assert json.loads(index_path(bridge, asset).read_text())["tags"] == []
    assert await value.select("cachedold", platform="qq") is None


async def test_missing_file_cannot_be_selected_but_published_original_survives(library):
    value, bridge = library
    asset = await seed_cached_sticker(value, picture(), platform="qq", tags=("大笑",))
    assert artifact_ids(value.workspace.store) == []
    published = await value.publish(asset, request_id="accepted-before-delete")
    (bridge.root / asset.relative_path).unlink()
    assert await value.select("大笑", platform="qq") is None
    metadata = await value.publish(asset, request_id="accepted-before-delete")
    assert metadata["artifact_id"] == published["artifact_id"]
    assert value.workspace.store.read_bytes(metadata["artifact_id"])[1] == picture()


async def test_collecting_a_new_original_normally_overwrites_its_destination(library):
    value, bridge = library
    first = await seed_cached_sticker(value, picture(), platform="qq")
    old = bridge.root / first.relative_path
    old.write_bytes(picture("blue"))
    actual = await seed_cached_sticker(
        value, picture(), platform="qq", request_id="new-real-original"
    )
    assert old.read_bytes() == picture()
    assert actual.relative_path == first.relative_path
    assert (bridge.root / actual.relative_path).read_bytes() == picture()
    result = await value.rebuild()
    assert result["assets"] == 1


async def test_explicit_copied_json_carries_tags_but_plain_image_copy_does_not(library):
    value, bridge = library
    original = await seed_cached_sticker(value, picture(), platform="qq", tags=("原库标签",))
    for platform in ("telegram", "matrix"):
        target = bridge.root / f"stickers/{platform}/files" / Path(original.relative_path).name
        target.parent.mkdir(parents=True)
        shutil.copyfile(bridge.root / original.relative_path, target)
    target_index = bridge.root / "stickers/telegram/index/copied.json"
    target_index.parent.mkdir()
    shutil.copyfile(index_path(bridge, original), target_index)
    await value.rebuild()
    copied = await value.select("原库标签", platform="telegram")
    assert copied is not None and copied.id != original.id
    assert copied.relative_path.startswith("stickers/telegram/files/")
    assert await value.select("原库标签", platform="matrix") is None


@pytest.mark.parametrize("damage", ["delete", "corrupt", "partial"])
async def test_cold_cache_waits_for_actual_rebuild(library, damage):
    value, bridge = library
    asset = await seed_cached_sticker(value, picture(), platform="qq", tags=("😂",))
    await value.rebuild()
    cache = bridge.root / "stickers/qq/search.sqlite3"
    if damage == "delete":
        cache.unlink()
    elif damage == "corrupt":
        cache.write_bytes(b"not SQLite")
    else:
        with closing(sqlite3.connect(cache)) as db:
            db.execute("PRAGMA user_version=0")
            db.execute("DELETE FROM terms")
    assert (await value.select("😂", platform="qq")).id == asset.id


async def test_corrupt_later_index_page_rebuilds_from_files(library):
    value, bridge = library
    asset = await seed_cached_sticker(
        value,
        picture(),
        platform="qq",
        tags=tuple(f"word{number:04}" for number in range(4000)),
    )
    await value.rebuild()
    cache = bridge.root / "stickers/qq/search.sqlite3"
    with closing(sqlite3.connect(cache)) as db:
        page_size = db.execute("PRAGMA page_size").fetchone()[0]
        pages = db.execute(
            "SELECT pageno FROM dbstat WHERE name='sqlite_autoindex_terms_1' "
            "AND pagetype='leaf' ORDER BY pageno"
        ).fetchall()
    assert len(pages) > 1
    with cache.open("r+b") as file:
        file.seek((pages[-1][0] - 1) * page_size)
        file.write(b"\xff")
    # The header and initial leaf are still readable; failure occurs during the query.
    assert StickerIndex(bridge.root).ready("qq")
    assert (await value.select("word3999", platform="qq")).id == asset.id


async def test_program_sql_error_does_not_erase_a_healthy_cache(library):
    value, bridge = library
    await seed_cached_sticker(value, picture(), platform="qq", tags=("valid",))
    await value.rebuild()
    engine = StickerIndex(bridge.root)
    cache = bridge.root / "stickers/qq/search.sqlite3"

    def erroneous_query(*args, **kwargs):
        with closing(sqlite3.connect(cache)) as db:
            return db.execute("SELECT missing_program_column FROM terms").fetchall()

    engine._search_ready = erroneous_query
    with pytest.raises(sqlite3.OperationalError, match="missing_program_column"):
        engine.search("qq", "valid")
    assert cache.exists() and engine.ready("qq")


async def test_changed_cache_schema_rebuilds_from_canonical_files(library):
    value, bridge = library
    asset = await seed_cached_sticker(value, picture(), platform="qq", tags=("valid",))
    await value.rebuild()
    cache = bridge.root / "stickers/qq/search.sqlite3"
    with closing(sqlite3.connect(cache)) as db:
        db.execute("ALTER TABLE terms RENAME COLUMN asset_id TO other_column")
    assert (await value.select("valid", platform="qq")).id == asset.id


async def test_cache_deleted_again_after_completed_build_uses_actual_cold_result(library):
    value, bridge = library
    asset = await seed_cached_sticker(value, picture(), platform="qq", tags=("valid",))
    execute = bridge.execute
    deleted = False

    async def remove_once(method, arguments, *, request_id):
        nonlocal deleted
        result = await execute(method, arguments, request_id=request_id)
        if method == "sticker_index" and arguments["command"] == "rebuild" and not deleted:
            deleted = True
            (bridge.root / "stickers/qq/search.sqlite3").unlink()
        return result

    bridge.execute = remove_once
    assert (await value.select("valid", platform="qq")).id == asset.id
    assert deleted


async def test_bad_json_does_not_disable_other_assets(library):
    value, bridge = library
    bad = await seed_cached_sticker(value, picture(), platform="qq", tags=("broken_tag",))
    good = await seed_cached_sticker(value, picture("green"), platform="qq", tags=("valid_tag",))
    index_path(bridge, bad).write_text("{")
    result = await value.rebuild()
    assert result["errors"] == 1
    assert (await value.select("valid_tag", platform="qq")).id == good.id
    assert await value.select("broken_tag", platform="qq") is None


def attach_caption_provider(value):
    provider = FakeVisionProvider()
    value.provider, value.preprocessor = provider, ImagePreprocessor()

    async def runtime():
        return SimpleNamespace(
            gif_max_frames=4,
            max_frames_per_turn=4,
            video_max_frames=3,
            video_max_duration_seconds=600,
            video_sample_interval_seconds=5,
            thinking_enabled=False,
            thinking_budget=1024,
        )

    value.vision_runtime = runtime
    return provider


async def test_concurrent_first_create_has_one_optional_caption(library):
    value, bridge = library
    provider = attach_caption_provider(value)
    source = cached_sticker_source(value, picture())
    first, second = await asyncio.gather(
        value.store_cached(**source, request_id="first-collection", platform="qq"),
        value.store_cached(**source, request_id="second-collection", platform="qq"),
    )
    await asyncio.gather(*tuple(value._tasks))
    assert first.id == second.id and len(provider.requests) == 1
    assert json.loads(index_path(bridge, first).read_text())["description"]
    await seed_cached_sticker(value, picture(), platform="qq")
    await value.rebuild()
    assert len(provider.requests) == 1


async def test_close_cancels_optional_caption_without_retries(library):
    value, bridge = library
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def analyze(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    attach_caption_provider(value)
    value.provider = SimpleNamespace(analyze=analyze)
    asset = await seed_cached_sticker(value, picture(), platform="qq")
    await entered.wait()
    await value.close()
    assert cancelled.is_set()
    assert (bridge.root / asset.relative_path).read_bytes() == picture()
    await seed_cached_sticker(value, picture(), platform="qq")


async def test_tgs_no_preview_stays_original_and_does_not_call_vision(library):
    value, bridge = library
    provider = attach_caption_provider(value)
    content = gzip.compress(json.dumps({"v": "5", "layers": [], "w": 64, "h": 64}).encode())
    asset = await seed_cached_sticker(
        value, content, platform="telegram", tags=("😂",), is_sticker=True
    )
    await asyncio.gather(*tuple(value._tasks))
    assert not provider.requests
    assert (bridge.root / asset.relative_path).read_bytes() == content
    assert (await value.select("😂", platform="telegram")).is_sticker


async def test_tgs_real_preview_caption_never_replaces_original(library):
    value, bridge = library
    provider = attach_caption_provider(value)
    preview = picture()
    preview_path = bridge.root / "admitted-preview"
    preview_path.write_bytes(preview)

    async def source():
        return preview_path, hashlib.sha256(preview).hexdigest()

    content = gzip.compress(json.dumps({"v": "5", "layers": [], "w": 64}).encode())
    asset = await seed_cached_sticker(
        value,
        content,
        platform="telegram",
        is_sticker=True,
        preview=MessageAttachment(
            AttachmentKind.IMAGE, "real", prepared_media=SimpleNamespace(source=source)
        ),
    )
    await asyncio.gather(*tuple(value._tasks))
    assert len(provider.requests) == 1 and "静态平台预览" in provider.requests[0][1]
    assert (bridge.root / asset.relative_path).read_bytes() == content


async def test_large_valid_png_saves_and_indexes_without_pixel_decode(library):
    value, bridge = library

    def chunk(kind, content):
        return (
            struct.pack(">I", len(content))
            + kind
            + content
            + struct.pack(">I", zlib.crc32(kind + content))
        )

    encoder = zlib.compressobj(9)
    compressed = b"".join(encoder.compress(b"\0" * 18001) for _ in range(10000)) + encoder.flush()
    content = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 18000, 10000, 8, 0, 0, 0, 0))
        + chunk(b"IDAT", compressed)
        + chunk(b"IEND", b"")
    )
    asset = await seed_cached_sticker(value, content, platform="qq", tags=("大图",))
    assert asset.image_format == "PNG"
    assert (bridge.root / asset.relative_path).read_bytes() == content
    assert (await value.select("大图", platform="qq")).id == asset.id


def test_actual_standalone_helper_runs_without_bot_package(tmp_path):
    from yuki.emoji import sticker_index

    isolated = tmp_path / "runtime"
    isolated.mkdir()
    shutil.copyfile(sticker_index.__file__, isolated / "sticker_index.py")
    shutil.copyfile(Path(sticker_index.__file__).with_name("media.py"), isolated / "media.py")
    workspace = tmp_path / "workspace"
    raw = workspace / "stickers/other/files/manual.png"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(picture())
    result = subprocess.run(
        [
            sys.executable,
            str(isolated / "sticker_index.py"),
            "--workspace",
            str(workspace),
            "rebuild",
        ],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": ""},
        cwd=isolated,
    )
    assert json.loads(result.stdout)["assets"] == 1


def test_actual_manager_imports_with_only_its_deployment_source_closure(tmp_path):
    import yuki

    source = Path(yuki.__file__).parent
    installed = tmp_path / "installed" / "yuki"
    installed.mkdir(parents=True)
    shutil.copyfile(source / "__init__.py", installed / "__init__.py")
    for directory in ("sandbox", "workspace"):
        shutil.copytree(source / directory, installed / directory)
    code = (
        "import sys; "
        f"sys.path.insert(0, {str(installed.parent)!r}); "
        "from yuki.sandbox.persistent import PersistentManager; "
        "assert not any(name.split('.')[0] in {'PIL','sqlalchemy','pydantic','nonebot'} "
        "for name in sys.modules); print('manager source closure imported')"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", code],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "manager source closure imported"


async def test_actual_manager_helper_and_file_close_drain(tmp_path):
    from yuki.sandbox.persistent import PersistentManager

    store = WorkspaceStore(tmp_path / "store")
    owner = PersistentManager(
        tmp_path / "manager", store, "test", "", "", tmp_path / "home", testing=True
    )
    raw = owner.files.root / "stickers/qq/files/real.png"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(picture())
    assert (await owner.sticker_index({"command": "rebuild"}))["assets"] == 1
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def blocked(*args):
        entered.set()
        release.wait()
        finished.set()
        return {"size": 0}

    owner.file_operation = blocked
    task = asyncio.create_task(owner.file_request("workspace_media_read", {}, "drain"))
    await asyncio.to_thread(entered.wait)
    task.cancel()
    closing = asyncio.create_task(owner.close())
    await asyncio.sleep(0)
    assert not closing.done() and not task.done() and not finished.is_set()
    release.set()
    await asyncio.gather(task, closing, return_exceptions=True)
    assert finished.is_set()


async def test_rebuild_preserves_completed_update_and_concurrent_collection(library):
    value, bridge = library
    original = await seed_cached_sticker(value, picture(), platform="qq", tags=("oldterm",))
    await value.rebuild()
    engine = StickerIndex(bridge.root)
    entered, release = threading.Event(), threading.Event()
    original_facts = engine.facts

    def delayed(path, **kwargs):
        result = original_facts(path, **kwargs)
        entered.set()
        release.wait()
        return result

    engine.facts = delayed
    rebuilding = asyncio.create_task(_media_io(engine.rebuild))
    await asyncio.to_thread(entered.wait)
    path = edit(bridge, original, tags=["newterm"])
    await _media_io(StickerIndex(bridge.root).update, str(path.relative_to(bridge.root)))
    new = await seed_cached_sticker(value, picture("green"), platform="qq", tags=("concurrent",))
    release.set()
    await rebuilding
    assert (await value.select("newterm", platform="qq")).id == original.id
    assert await value.select("oldterm", platform="qq") is None
    assert (await value.select("concurrent", platform="qq")).id == new.id


async def test_stale_candidate_cleanup_keeps_completed_new_metadata_update(library):
    value, bridge = library
    asset = await seed_cached_sticker(value, picture(), platform="qq", tags=("oldterm",))
    await value.rebuild()
    edit(bridge, asset, tags=["temporary"])
    engine = StickerIndex(bridge.root)
    entered, release = threading.Event(), threading.Event()
    original = engine.facts

    def blocked(path, **kwargs):
        facts = original(path, **kwargs)
        entered.set()
        release.wait()
        return facts

    engine.facts = blocked
    old_query = asyncio.create_task(_media_io(engine.search, "qq", "oldterm"))
    await asyncio.to_thread(entered.wait)
    path = edit(bridge, asset, tags=["newterm"])
    await _media_io(StickerIndex(bridge.root).update, str(path.relative_to(bridge.root)))
    assert (await value.select("newterm", platform="qq")).id == asset.id
    release.set()
    assert (await old_query)["items"] == []
    assert (await value.select("newterm", platform="qq")).id == asset.id


async def test_deleted_candidates_beyond_a_page_do_not_hide_real_file(library):
    value, bridge = library
    assets = [
        await seed_cached_sticker(
            value, picture((index, 10, 20)), platform="qq", tags=("samequery",)
        )
        for index in range(23)
    ]
    await value.rebuild()
    ordered = sorted(assets, key=lambda asset: asset.id)
    for asset in ordered[:-1]:
        (bridge.root / asset.relative_path).unlink()
    assert (await value.select("samequery", platform="qq")).id == ordered[-1].id


@pytest.mark.parametrize("format", ["WEBP", "GIF"])
async def test_animation_facts_survive_fast_lookup_and_selection(library, format):
    value, bridge = library
    output = io.BytesIO()
    Image.new("RGB", (12, 8), "red").save(
        output,
        format=format,
        save_all=True,
        append_images=[Image.new("RGB", (12, 8), "blue")],
        duration=200,
        loop=0,
    )
    content = output.getvalue()
    first = await seed_cached_sticker(
        value, content, platform="telegram", tags=("animation",), is_sticker=True
    )
    duplicate = await seed_cached_sticker(value, content, platform="telegram")
    selected = await value.select("animation", platform="telegram")
    assert first.animated and duplicate.animated and selected.animated
    assert first.frame_count == duplicate.frame_count == selected.frame_count == 2
    assert (bridge.root / selected.relative_path).read_bytes() == content


async def test_real_processes_choose_one_complete_atomic_metadata_winner(tmp_path):
    from yuki.emoji import sticker_index

    raw = tmp_path / "stickers/qq/files/shared.png"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(picture())
    request = json.dumps(
        {
            "command": "catalog",
            "platform": "qq",
            "path": "stickers/qq/files/shared.png",
            "tags": ["complete"],
        }
    ).encode()

    async def create():
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            sticker_index.__file__,
            "--workspace",
            str(tmp_path),
            "--request",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        output, error = await process.communicate(request)
        assert process.returncode == 0, error.decode()
        return json.loads(output)

    results = await asyncio.gather(*(create() for _ in range(8)))
    assert sum(item["created"] for item in results) == 1
    assert all(item["asset"]["tags"] == ["complete"] for item in results)
    assert len(list((tmp_path / "stickers/qq/index").glob("*.json"))) == 1


async def test_rebuild_registration_and_repeated_cancel_keep_actual_owner(tmp_path):
    from yuki.sandbox.persistent import PersistentManager

    owner = PersistentManager(
        tmp_path / "manager",
        WorkspaceStore(tmp_path / "store"),
        "test",
        "",
        "",
        tmp_path / "home",
        testing=True,
    )
    release, finished = asyncio.Event(), asyncio.Event()

    async def blocked(args):
        await release.wait()
        finished.set()
        return {"assets": 0}

    owner._sticker_helper = blocked
    task = asyncio.create_task(owner.sticker_index({"command": "rebuild", "platform": "qq"}))
    await asyncio.sleep(0)
    closing = asyncio.create_task(owner.close())
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not closing.done() and not task.done()
    release.set()
    await asyncio.gather(task, closing, return_exceptions=True)
    assert finished.is_set() and task.cancelled()


async def test_cancelling_close_still_drains_the_actual_file_thread(tmp_path):
    from yuki.sandbox.persistent import PersistentManager

    owner = PersistentManager(
        tmp_path / "manager",
        WorkspaceStore(tmp_path / "store"),
        "test",
        "",
        "",
        tmp_path / "home",
        testing=True,
    )
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def blocked(*args):
        entered.set()
        release.wait()
        finished.set()
        return {"size": 0}

    owner.file_operation = blocked
    caller = asyncio.create_task(owner.file_request("workspace_media_read", {}, "close-cancel"))
    await asyncio.to_thread(entered.wait)
    closing = asyncio.create_task(owner.close())
    await asyncio.sleep(0)
    closing.cancel()
    await asyncio.sleep(0)
    closing.cancel()
    await asyncio.sleep(0)
    assert not closing.done() and not caller.done() and not finished.is_set()
    release.set()
    await asyncio.gather(caller, closing, return_exceptions=True)
    assert closing.cancelled() and finished.is_set()


async def test_long_index_rpc_is_not_a_seven_second_observation(tmp_path):
    from yuki.sandbox.client import SandboxClient
    from yuki.sandbox.persistent import PersistentManager

    owner = PersistentManager(
        tmp_path / "manager",
        WorkspaceStore(tmp_path / "store"),
        "test",
        "",
        "",
        tmp_path / "home",
        testing=True,
    )
    release = asyncio.Event()
    handlers = set()

    async def blocked(args):
        await release.wait()
        return {"assets": 0}

    owner._sticker_helper = blocked

    async def accept(reader, writer):
        current = asyncio.current_task()
        handlers.add(current)
        try:
            await owner.serve(reader, writer)
        finally:
            handlers.discard(current)

    socket = tmp_path / "manager.sock"
    server = await asyncio.start_unix_server(accept, str(socket))
    task = asyncio.create_task(
        SandboxClient(socket).execute(
            "sticker_index",
            {"command": "rebuild", "platform": "qq"},
            request_id="cold",
        )
    )
    try:
        await asyncio.sleep(7.05)
        assert not task.done()
        release.set()
        assert await task == {"assets": 0}
    finally:
        release.set()
        await asyncio.gather(task, *tuple(handlers), return_exceptions=True)
        server.close()
        await server.wait_closed()
        await owner.close()
