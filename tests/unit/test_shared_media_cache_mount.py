"""One Bot-owned cache is a read-only environment view and an existing safe FD source."""

import base64
import hashlib
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from tests.unit.test_sticker_library import picture

from yuki.sandbox import persistent
from yuki.sandbox.persistent import PersistentManager
from yuki.services.image_preprocessor import ImagePreprocessor
from yuki.workspace.inspect import WorkspaceInspector
from yuki.workspace.service import WorkspaceService
from yuki.workspace.store import WorkspaceError, WorkspaceStore

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Environment cache FDs require Linux")


def manager_at(tmp_path):
    cache = tmp_path / "bot-cache"
    cache.mkdir()
    owner = PersistentManager(
        tmp_path / "manager",
        WorkspaceStore(tmp_path / "artifacts"),
        "base-image",
        "egress",
        "",
        tmp_path / "home",
        testing=True,
        media_cache=cache,
    )
    return owner, cache


async def test_cache_reads_and_publication_reuse_safe_fd_without_allowing_cache_checkout(tmp_path):
    owner, cache = manager_at(tmp_path)
    original = cache / "v1/conversation/101/0-original.png"
    original.parent.mkdir(parents=True)
    data = picture()
    original.write_bytes(data)
    version = hashlib.sha256(data).hexdigest()
    path = "/inputs/media-cache/v1/conversation/101/0-original.png"
    try:
        selected = owner.file_operation(
            "workspace_media_read", {"path": path, "expected_version": version}
        )
        assert selected["path"] == path and base64.b64decode(selected["base64"]) == data
        validated = owner.file_operation(
            "workspace_media_validate", {"path": path, "expected_version": version}
        )
        assert validated["version"] == version and "base64" not in validated
        workspace = WorkspaceService(owner.store)
        workspace.sandbox = SimpleNamespace(execute=owner.file_request)
        workspace.visual_inspector = WorkspaceInspector(owner.store, ImagePreprocessor())
        viewed = await workspace.execute(
            "workspace_inspect", {"path": path, "expected_version": version, "question": "查看缓存"}
        )
        assert viewed.images[0].workspace_path == path
        await workspace.validate_images(viewed.images)
        published = owner.file_operation(
            "workspace_publish", {"path": path, "expected_version": version}
        )
        assert owner.store.read_bytes(published["artifact_id"])[1] == data
        with pytest.raises(WorkspaceError, match="path_outside_workspace"):
            await owner.file_request(
                "workspace_checkout",
                {"artifact_id": published["artifact_id"], "destination": path},
                "cache-is-a-read-source",
            )
        outside = tmp_path / "host-only"
        outside.mkdir()
        (outside / "private.bin").write_bytes(b"synthetic host-only bytes")
        (cache / "escape").symlink_to(outside, target_is_directory=True)
        with pytest.raises(OSError):
            owner.file_operation(
                "workspace_media_read", {"path": "/inputs/media-cache/escape/private.bin"}
            )
        original.unlink()
        original.symlink_to(outside / "private.bin")
        with pytest.raises(OSError):
            owner.file_operation("workspace_publish", {"path": path})
        assert owner.store.read_bytes(published["artifact_id"])[1] == data
    finally:
        await owner.close()


async def test_new_environment_mounts_same_cache_read_only_without_recreating_old_environment(
    tmp_path, monkeypatch
):
    owner, cache = manager_at(tmp_path)
    current = {
        "Id": "preserved-rootfs",
        "State": {"Running": True, "StartedAt": "original-start"},
        "HostConfig": {"RestartPolicy": {"Name": "no"}},
        "NetworkSettings": {"Networks": {"egress": {"IPAddress": "172.30.0.2"}}},
        "Mounts": [
            {
                "Source": str(cache),
                "Destination": "/home/yuki/inputs/media-cache",
                "RW": False,
            }
        ],
    }
    owner.set_setting("checkpoint_image", "private-complete-rootfs")
    owner.inspect_container = AsyncMock(side_effect=[None, current])
    owner.command = AsyncMock(return_value=(0, b""))
    monkeypatch.setattr(
        persistent, "Execd", lambda *_: SimpleNamespace(request=AsyncMock(return_value={}))
    )
    try:
        await owner.recover()
        command = owner.command.await_args.args
        assert command[:2] == ("docker", "create") and command[-1] == "private-complete-rootfs"
        assert f"type=bind,src={cache},dst=/home/yuki/inputs/media-cache,readonly" in command
        assert owner.media_cache_shared and owner.ready
        assert owner.environment_status()["media_cache"] == {
            "path": "/inputs/media-cache",
            "configured": True,
            "shared": True,
        }
        # A changed configuration does not interrupt unrelated tools or replace an old container.
        current["Mounts"] = []
        owner.inspect_container = AsyncMock(return_value=current)
        owner.command.reset_mock()
        await owner.recover()
        owner.command.assert_not_awaited()
        assert owner.ready and not owner.media_cache_shared
        assert owner.container_id == "preserved-rootfs:original-start"
    finally:
        await owner.close()
