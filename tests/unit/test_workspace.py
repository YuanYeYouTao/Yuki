"""Shared scratch data expires without destroying valid or newer revisions."""

import os
from pathlib import Path

import pytest
from tests.support.workspace_snapshots import snapshot_bytes

from qq_ai_bot.workspace.store import WorkspaceError, WorkspaceStore


def test_workspace_expiry_revision_quota_and_file_integrity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [1000.0]
    monkeypatch.setattr("qq_ai_bot.workspace.store.time.time", lambda: clock[0])
    store = WorkspaceStore(tmp_path / "scratch", ttl=100, capacity=12, max_file=10, max_objects=2)
    first = snapshot_bytes(store, "note.txt", b"hello")
    identity = first["artifact_id"]
    clock[0] += 20
    assert store.read(identity)["text"] == "hello"
    same = snapshot_bytes(store, "note.txt", b"hello", artifact_id=identity)
    assert same == first
    with pytest.raises(WorkspaceError, match="snapshot_identity_conflict"):
        snapshot_bytes(store, "note.txt", b"new", artifact_id=identity)
    clock[0] += 10
    second = snapshot_bytes(store, "more.txt", b"1234567")
    with pytest.raises(WorkspaceError, match="workspace_full"):
        snapshot_bytes(store, "full.txt", b"x")
    assert len(store.list()["items"]) == 2
    newest = store.list(limit=1, number=1)
    older = store.list(limit=1, number=2)
    assert newest["total"] == older["total"] == 2
    assert newest["items"][0]["artifact_id"] == second["artifact_id"]
    assert older["items"][0]["artifact_id"] == identity
    assert store.list(limit=1, number=3)["items"] == []
    restarted = WorkspaceStore(store.root, ttl=100, capacity=12, max_file=10, max_objects=2)
    assert restarted.read(identity)["expires_at"] == first["expires_at"]
    for name in ("../escape", "/etc/passwd", "a\\b", "C:secret"):
        with pytest.raises(WorkspaceError, match="invalid_artifact_name"):
            snapshot_bytes(store, name, b"x")
    with pytest.raises(WorkspaceError, match="invalid_artifact_id"):
        store.read("../manifest.sqlite3")
    clock[0] = 1101
    with pytest.raises(WorkspaceError, match="artifact_expired"):
        store.read(identity)
    assert store.cleanup() == 1
    assert store.read(second["artifact_id"])["text"] == "1234567"
    store.delete(second["artifact_id"], 1)
    assert store.list()["items"] == []
    linked = snapshot_bytes(store, "link.txt", b"link")
    blob = next(store.root.glob("*.blob"))
    os.link(blob, tmp_path / "hardlink")
    with pytest.raises(WorkspaceError, match="unsafe_artifact"):
        store.read(linked["artifact_id"])


@pytest.mark.asyncio
async def test_published_image_inspection_is_bounded_without_auxiliary_model(
    tmp_path: Path,
) -> None:
    import io
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from PIL import Image

    from qq_ai_bot.runtime.work_activation import current_work_control
    from qq_ai_bot.services.image_preprocessor import ImagePreprocessor
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
    inspector = WorkspaceInspector(store, ImagePreprocessor())
    control = SimpleNamespace(validate=AsyncMock(), reserve_request=AsyncMock())
    token = current_work_control.set(control)
    try:
        result = await inspector(identity, "图片是什么颜色？")
        assert result.images and result.images[0].source == "workspace"
        assert result.images[0].artifact_id == identity
        assert result["status"] == "prepared_for_main_agent"
        assert "data:image" not in str(result)
        await inspector.validate_artifact(result.images[0])
        control.reserve_request.assert_not_awaited()
    finally:
        current_work_control.reset(token)
