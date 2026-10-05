"""Selected pixels stay on the original model path with actual file versions."""

import base64
import hashlib
import io
import os
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from qq_ai_bot.services.image_preprocessor import ImagePreprocessingError, ImagePreprocessor
from qq_ai_bot.services.native_media import NativeMediaPreparer
from qq_ai_bot.workspace.inspect import WorkspaceInspector
from qq_ai_bot.workspace.service import WorkspaceService
from qq_ai_bot.workspace.store import WorkspaceError, WorkspaceStore


def _image(*, animated=False):
    output = io.BytesIO()
    first = Image.new("RGB", (12, 12), "red")
    if animated:
        first.save(
            output,
            "GIF",
            save_all=True,
            append_images=[Image.new("RGB", (12, 12), "blue")],
            duration=100,
        )
    else:
        first.save(output, "PNG")
    return output.getvalue()


def test_shared_preparer_preserves_gif_pixels_and_budgets():
    preparer = NativeMediaPreparer(ImagePreprocessor())
    images = preparer.prepare_image(_image(animated=True), source="history")
    assert len(images) == 2 and images[0].data_url != images[1].data_url
    assert all(image.source == "history" for image in images)
    with pytest.raises(ImagePreprocessingError, match="预算"):
        NativeMediaPreparer(ImagePreprocessor(), max_bytes=1).prepare_image(
            _image(), source="workspace"
        )


@pytest.mark.asyncio
async def test_video_runtime_settings_and_actual_timestamps(tmp_path, monkeypatch):
    frame = NativeMediaPreparer(ImagePreprocessor()).prepare_image(_image(), source="history")[0]
    sample = AsyncMock(return_value=(replace(frame, video_timestamp_seconds=3.25),))
    monkeypatch.setattr("qq_ai_bot.services.native_media.sample_video", sample)
    runtime = SimpleNamespace(
        max_frames_per_turn=7,
        video_max_frames=5,
        video_max_duration_seconds=90,
        video_sample_interval_seconds=9,
    )
    result = await NativeMediaPreparer(ImagePreprocessor()).prepare_video(
        tmp_path / "v.mp4", source="history", runtime=runtime
    )
    assert result[0].video_timestamp_seconds == 3.25
    assert sample.await_args.kwargs == {
        "source": "history",
        "maximum": 5,
        "max_duration_seconds": 90,
        "sample_interval_seconds": 9,
    }


@pytest.mark.asyncio
async def test_selected_manager_file_pixels_and_dispatch_version_guard(tmp_path):
    data = _image()
    version = hashlib.sha256(data).hexdigest()
    sandbox = SimpleNamespace(
        execute=AsyncMock(
            return_value={
                "path": "/workspace/selected.png",
                "version": version,
                "base64": base64.b64encode(data).decode(),
                "size": len(data),
            }
        )
    )
    store = WorkspaceStore(tmp_path / "artifacts")
    service = WorkspaceService(store)
    service.sandbox = sandbox
    service.visual_inspector = WorkspaceInspector(store, ImagePreprocessor())
    result = await service.execute(
        "workspace_inspect",
        {"path": "selected.png", "expected_version": version, "question": "查看我选定的文件"},
    )
    assert result.images[0].workspace_path == "/workspace/selected.png"
    assert result.images[0].version == version
    assert "base64" not in result and "data:image" not in str(result)
    assert list(store.list()["items"]) == []  # Reading does not publish or mutate.
    sandbox.execute.return_value = {"version": version}
    await service.validate_images(result.images)
    assert sandbox.execute.await_args.args[0] == "workspace_media_validate"
    sandbox.execute.return_value = {"error": "version_conflict"}
    with pytest.raises(WorkspaceError, match="version_changed"):
        await service.validate_images(result.images)
    with pytest.raises(WorkspaceError, match="choose_path_or_artifact_id"):
        await service.execute(
            "workspace_inspect", {"path": "selected.png", "artifact_id": "x", "question": "q"}
        )


@pytest.mark.skipif(os.name != "posix", reason="Manager safe workspace FDs require POSIX")
def test_manager_media_read_has_fixed_bound_versions_and_no_publication(tmp_path):
    from qq_ai_bot.sandbox.persistent import PersistentManager
    from qq_ai_bot.workspace.files import FileWorkspace

    manager = object.__new__(PersistentManager)
    manager.files = FileWorkspace(tmp_path)
    data = _image()
    version = hashlib.sha256(data).hexdigest()
    (tmp_path / "selected.png").write_bytes(data)
    result = manager.file_operation(
        "workspace_media_read", {"path": "selected.png", "expected_version": version}
    )
    assert base64.b64decode(result["base64"]) == data
    assert result["version"] == version
    assert sorted(p.name for p in tmp_path.iterdir()) == ["selected.png"]
    validate = manager.file_operation(
        "workspace_media_validate", {"path": "selected.png", "expected_version": version}
    )
    assert "base64" not in validate
    (tmp_path / "selected.png").write_bytes(b"changed")
    with pytest.raises(WorkspaceError, match="version_conflict"):
        manager.file_operation(
            "workspace_media_validate", {"path": "selected.png", "expected_version": version}
        )
    (tmp_path / "escape").symlink_to("/etc")
    with pytest.raises(OSError):
        manager.file_operation("workspace_media_read", {"path": "escape/passwd"})
    with pytest.raises(WorkspaceError, match="outside"):
        manager.file_operation("workspace_media_read", {"path": "../outside"})
