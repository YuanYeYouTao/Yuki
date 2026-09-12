"""Shared scratch data expires without destroying valid or newer revisions."""

import os
from pathlib import Path

import pytest

from qq_ai_bot.workspace.store import WorkspaceError, WorkspaceStore


def test_workspace_expiry_revision_quota_and_file_integrity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [1000.0]
    monkeypatch.setattr("qq_ai_bot.workspace.store.time.time", lambda: clock[0])
    store = WorkspaceStore(tmp_path / "scratch", ttl=100, capacity=12, max_file=10, max_objects=2)
    first = store.write("note.txt", b"hello")
    identity = first["artifact_id"]
    clock[0] += 20
    assert store.read(identity)["text"] == "hello"
    same = store.write("renamed.txt", b"hello", artifact_id=identity, expected_revision=1)
    assert same["expires_at"] == first["expires_at"] and same["revision"] == 2
    with pytest.raises(WorkspaceError, match="version_conflict"):
        store.write("note.txt", b"new", artifact_id=identity, expected_revision=1)
    second = store.write("more.txt", b"1234567")
    with pytest.raises(WorkspaceError, match="workspace_full"):
        store.write("full.txt", b"x")
    assert len(store.list()["items"]) == 2
    restarted = WorkspaceStore(store.root, ttl=100, capacity=12, max_file=10, max_objects=2)
    assert restarted.read(identity)["expires_at"] == first["expires_at"]
    for name in ("../escape", "/etc/passwd", "a\\b", "C:secret"):
        with pytest.raises(WorkspaceError, match="invalid_artifact_name"):
            store.write(name, b"x")
    with pytest.raises(WorkspaceError, match="invalid_artifact_id"):
        store.read("../manifest.sqlite3")
    clock[0] = 1101
    with pytest.raises(WorkspaceError, match="artifact_expired"):
        store.read(identity)
    assert store.cleanup() == 1
    assert store.read(second["artifact_id"])["text"] == "1234567"
    store.delete(second["artifact_id"], 1)
    assert store.list()["items"] == []
    linked = store.write("link.txt", b"link")
    blob = next(store.root.glob("*.blob"))
    os.link(blob, tmp_path / "hardlink")
    with pytest.raises(WorkspaceError, match="unsafe_artifact"):
        store.read(linked["artifact_id"])


@pytest.mark.asyncio
async def test_published_image_inspection_is_bounded_and_charges_model(tmp_path: Path) -> None:
    import io
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from PIL import Image

    from qq_ai_bot.runtime.work_activation import current_work_control
    from qq_ai_bot.services.image_preprocessor import ImagePreprocessor
    from qq_ai_bot.vision.models import VisualObservation
    from qq_ai_bot.workspace.inspect import WorkspaceInspector

    store = WorkspaceStore(tmp_path / "artifacts")
    content = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(content, format="PNG")
    source = tmp_path / "红色.png"
    source.write_bytes(content.getvalue())
    with source.open("rb") as stream:
        published = store.snapshot(stream.fileno(), source.name)
    identity = published["artifact_id"]
    with pytest.raises(WorkspaceError, match="artifact_too_large"):
        store.read_bytes(identity, max_bytes=1)
    assert store.read_bytes(identity)[1] == content.getvalue()
    provider = SimpleNamespace(
        analyze=AsyncMock(return_value=VisualObservation(items=(), overall_description="红色图片"))
    )
    inspector = WorkspaceInspector(store, ImagePreprocessor(), provider)
    control = SimpleNamespace(validate=AsyncMock(), reserve_request=AsyncMock())
    token = current_work_control.set(control)
    try:
        result = await inspector(identity, "图片是什么颜色？")
        assert result["observation"]["overall_description"] == "红色图片"
        control.reserve_request.assert_awaited_once_with(auxiliary=True)
        mutable = store.write("draft.png", content.getvalue())
        with pytest.raises(WorkspaceError, match="inspection_requires_published_artifact"):
            await inspector(mutable["artifact_id"], "检查草稿")
        provider.analyze.assert_awaited_once()
    finally:
        current_work_control.reset(token)
