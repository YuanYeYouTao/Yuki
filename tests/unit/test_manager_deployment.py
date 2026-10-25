"""Deployment uses the selected Bot code and tests a live Manager, not a socket file."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from tests.support.workspace_snapshots import snapshot_bytes

from yuki.deployment_setup.environment_check import check_environment
from yuki.workspace.store import WorkspaceStore

ROOT = Path(__file__).resolve().parents[2]


def load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def sync():
    return load_module("sync_manager_deployment", ROOT / "deploy/sandbox/sync-manager.py")


def test_export_uses_bot_source_and_preserves_original_data_and_service_arguments(
    tmp_path, monkeypatch, sync
):
    revision = "a" * 40
    manager = tmp_path / "manager"
    old_source = manager / "src/yuki/sandbox/environment_supervisor.py"
    old_source.parent.mkdir(parents=True)
    old_source.write_text("original bound supervisor\n")
    override = tmp_path / "90-source-release.conf"
    override.write_text(
        '[Service]\nEnvironment="PYTHONPATH=/previous/src"\n'
        'Environment="YUKI_MANAGER_MEDIA_CACHE=/original/media"\n'
        "ExecStart=/original/python -m yuki.sandbox.manager --root /original/state\n"
    )
    workspace = tmp_path / "workspace"
    store = WorkspaceStore(workspace)
    original = snapshot_bytes(store, "original.txt", b"keep original receipt")
    with sqlite3.connect(workspace / "manifest.sqlite3") as db:
        with pytest.raises(sqlite3.OperationalError, match="no such column: expires_at"):
            db.execute("UPDATE artifacts SET expires_at=253402300799")

    commands = []

    def output(*args):
        commands.append(args)
        if args[1] == "inspect":
            return f"sha256:original-image\n{revision}"
        assert args[:3] == ("docker", "exec", "original-bot")
        return "/app/.venv/lib/python3.12/site-packages/yuki"

    def copy(args, *, check):
        commands.append(args)
        assert args[:2] == ["docker", "cp"] and check
        assert args[2] == ("original-bot:/app/.venv/lib/python3.12/site-packages/yuki/.")
        shutil.copytree(ROOT / "src/yuki", args[3], dirs_exist_ok=True)

    monkeypatch.setattr(sync, "command", output)
    monkeypatch.setattr(sync.subprocess, "run", copy)
    result = sync.sync_manager("original-bot", manager, override)

    source = manager / "releases" / revision / "src"
    assert result["source"] == str(source)
    assert old_source.read_text() == "original bound supervisor\n"
    updated = override.read_text()
    assert "PYTHONPATH=/previous/src" not in updated
    assert any(
        f"PYTHONPATH={source}" in shlex.split(line.partition("=")[2])
        for line in updated.splitlines()
        if line.startswith("Environment=")
    )
    assert 'Environment="YUKI_MANAGER_MEDIA_CACHE=/original/media"\n' in updated
    assert "ExecStart=/original/python -m yuki.sandbox.manager --root /original/state\n" in updated
    assert all(args[0] == "docker" for args in commands)
    exported = load_module("exported_workspace_store", source / "yuki/workspace/store.py")
    exported_store = exported.WorkspaceStore(workspace)
    exported_store.cleanup()
    metadata, data = exported_store.read_bytes(original["artifact_id"])
    assert metadata == original and data == b"keep original receipt"
    # An immutable same-image export can be repeated without replacing its source directory.
    assert sync.sync_manager("original-bot", manager, override) == result
    assert updated == override.read_text()


def test_existing_release_is_not_overwritten_when_its_source_differs(tmp_path, monkeypatch, sync):
    revision = "b" * 40
    manager = tmp_path / "manager"
    original = manager / "releases" / revision / "src/yuki/manager.py"
    original.parent.mkdir(parents=True)
    original.write_text("original bound code")
    override = tmp_path / "90-source-release.conf"
    override.write_text("[Service]\nEnvironment=PYTHONPATH=/old\n")
    monkeypatch.setattr(
        sync, "command", lambda *args: f"image\n{revision}" if args[1] == "inspect" else "/yuki"
    )

    def copy(args, *, check):
        (Path(args[-1]) / "manager.py").write_text("different source")

    monkeypatch.setattr(sync.subprocess, "run", copy)
    with pytest.raises(ValueError, match="Existing release differs"):
        sync.sync_manager("bot", manager, override)
    assert original.read_text() == "original bound code"
    assert override.read_text() == "[Service]\nEnvironment=PYTHONPATH=/old\n"


def test_source_pin_keeps_unrelated_environment_and_sections(sync):
    original = (
        '[Service]\nEnvironment="PYTHONPATH=/old" "PRIVATE_VALUE=keep this"\n'
        "\n[Unit]\nAfter=custom.service\n"
    )
    updated = sync.source_override(original, Path("/new/src"))
    assert '"PRIVATE_VALUE=keep this"\n' in updated
    assert updated.count("PYTHONPATH=") == 1
    assert updated.endswith("[Unit]\nAfter=custom.service\n")


def test_source_pin_preserves_systemd_escapes_in_other_environment_words(sync):
    original = '[Service]\nEnvironment="PYTHONPATH=/old" "OTHER=alpha\\x20beta"\n'
    updated = sync.source_override(original, Path("/new/src"))
    assert '"OTHER=alpha\\x20beta"\n' in updated
    assert '"OTHER=alpha\\\\x20beta"' not in updated
    assert updated.count("PYTHONPATH=") == 1


async def test_stale_socket_file_does_not_pass_manager_deployment_check(tmp_path, monkeypatch):
    monkeypatch.setattr("yuki.sandbox.deployment_check.os.geteuid", lambda: 10001, raising=False)
    path = tmp_path / "manager.sock"
    if hasattr(socket, "AF_UNIX"):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stale:
            stale.bind(str(path))
    else:
        path.touch()
    assert path.exists()
    result = await check_environment(path, tmp_path)
    assert result == {"ok": False, "error": "manager_connection_failed"}


@pytest.mark.skipif(sys.platform == "win32", reason="Unix socket server needs Linux")
async def test_live_manager_status_is_read_without_starting_a_job(tmp_path, monkeypatch):
    monkeypatch.setattr("yuki.sandbox.deployment_check.os.geteuid", lambda: 10001, raising=False)
    received = []

    async def respond(reader, writer):
        received.append(json.loads(await reader.readline()))
        writer.write(
            json.dumps(
                {"ready": True, "workspace": "/workspace", "private_task": "not-public"}
            ).encode()
            + b"\n"
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    path = tmp_path / "manager.sock"
    async with await asyncio.start_unix_server(respond, str(path)):
        result = await check_environment(path, tmp_path)
    assert result == {"ok": True, "uid": 10001, "manager": "connected", "workspace": "/workspace"}
    assert len(received) == 1 and received[0]["method"] == "environment_status"


def test_shared_checker_has_no_site_package_dependencies(tmp_path):
    environment = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    result = subprocess.run(
        [sys.executable, "-S", "-m", "yuki.sandbox.deployment_check", "--help"],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "--socket" in result.stdout and "--artifact-store" in result.stdout
