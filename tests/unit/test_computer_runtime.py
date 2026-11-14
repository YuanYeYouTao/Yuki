"""Actual desktop service and durable service lifecycle; no Docker daemon required."""

import asyncio
import json
import shutil
import sys
from uuid import uuid4

import pytest

pytest.importorskip("fcntl", reason="The persistent environment runs on Linux")
from tests.unit.test_computer_control import CLI, manager_at
from tests.unit.test_computer_driver import control, environment, exchange


async def test_display_start_retry_and_explicit_wake_after_normal_end(tmp_path):
    manager = manager_at(tmp_path)
    try:
        key = str(uuid4())
        first = await manager.handle({"method": "display_wake", "args": {}, "request_id": key})
        assert first == {"display": "starting"}
        identity = manager.db.execute(
            "SELECT run_id FROM environment_services WHERE name='yuki-desktop'"
        ).fetchone()[0]
        assert (
            await manager.handle({"method": "display_wake", "args": {}, "request_id": key}) == first
        )
        assert manager.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
        # Explicit stop is not an instruction to resume on the next Manager scan.
        await manager.handle({"method": "display_sleep", "args": {}, "request_id": str(uuid4())})
        await manager.restore_services()
        assert manager.desktop.display_status() == {"display": "stopped"}
        assert manager.get(identity)["status"] == "cancelled"
        await manager.handle({"method": "display_wake", "args": {}, "request_id": str(uuid4())})
        second = manager.db.execute(
            "SELECT run_id FROM environment_services WHERE name='yuki-desktop'"
        ).fetchone()[0]
        assert second != identity
        # A clean service exit still permits an explicit wake with a fresh intent.
        manager.finish(second, "succeeded", {"exit_code": 0})
        await manager.handle({"method": "display_wake", "args": {}, "request_id": str(uuid4())})
        assert manager.desktop.display_status()["display"] == "starting"
        assert manager.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 3
    finally:
        await manager.close()


async def test_service_start_commit_before_binding_reuses_original_job_after_reopen(tmp_path):
    manager = manager_at(tmp_path)
    submit = manager.submit

    async def interrupted(*args, **kwargs):
        result = await submit(*args, **kwargs)
        assert result["run_id"]
        raise OSError("process interrupted before service binds the committed original")

    manager.submit = interrupted
    key = str(uuid4())
    with pytest.raises(OSError):
        await manager.handle({"method": "display_wake", "args": {}, "request_id": key})
    original = manager.db.execute("SELECT id FROM jobs").fetchone()[0]
    await manager.close()
    manager = manager_at(tmp_path)
    try:
        await manager.handle({"method": "display_wake", "args": {}, "request_id": key})
        assert (
            manager.db.execute("SELECT run_id FROM environment_services").fetchone()[0] == original
        )
        assert manager.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    finally:
        await manager.close()


def test_desktop_keeps_existing_resource_defaults_and_explicit_overrides(tmp_path):
    manager = manager_at(tmp_path)
    try:
        assert manager.resource_limits() == {
            "memory_mib": 512,
            "cpus": 1,
            "cpu_shares": 1024,
            "swap_mib": 128,
            "pids": 128,
            "tmp_mib": 128,
        }
        manager.set_setting("resource_memory_mib", "4096")
        manager.set_setting("resource_pids", "3000")
        assert manager.resource_limits()["memory_mib"] == 4096
        assert manager.resource_limits()["pids"] == 3000
    finally:
        manager.db.close()


async def test_unavailable_headed_image_never_reports_a_started_display(tmp_path):
    manager = manager_at(tmp_path)
    manager.testing = False
    try:
        assert manager.desktop.display_status() == {"display": "unavailable"}
        response = await manager.handle(
            {"method": "display_wake", "args": {}, "request_id": str(uuid4())}
        )
        assert response == {"error": "display_unavailable"}
        assert manager.db.execute("SELECT id FROM jobs").fetchall() == []
    finally:
        await manager.close()


async def test_real_headed_service_renders_unicode_exposes_rfb_and_stops_children(tmp_path):
    if not all(shutil.which(binary) for binary in ("Xvnc", "openbox-session", "chromium")):
        pytest.skip("Requires the actual headed environment packages")
    root = tmp_path / "runtime"
    root.mkdir()
    workspace = tmp_path / "workspace"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(CLI.with_name("desktop_service.py")),
        env=environment(
            HOME=str(tmp_path),
            YUKI_COMPUTER_RUNTIME=str(root),
            YUKI_DESKTOP_WORKSPACE=str(workspace),
            YUKI_JOB_ID=str(uuid4()),
        ),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(60):
            while not (root / "desktop-status.json").exists():
                assert process.returncode is None
                await asyncio.sleep(0.1)
            status = json.loads((root / "desktop-status.json").read_text())
            assert status["display"] == "running"
            reader, writer = await asyncio.open_connection("127.0.0.1", 5901)
            assert await reader.readexactly(12) == b"RFB 003.008\n"
            writer.close()
            await writer.wait_closed()
            # The service's own driver answers through the runtime files.
            lease = str(uuid4())
            control(root, "agent", lease)
            page = await asyncio.to_thread(
                exchange,
                root,
                "browser",
                {
                    "action": "open",
                    "url": "data:text/html;charset=utf-8,<h1>Yuki 真實有頭畫面</h1>",
                },
                lease=lease,
            )
            assert "Yuki 真實有頭畫面" in page["result"]["snapshot"]
            shot = await asyncio.to_thread(exchange, root, "browser", {"action": "screenshot"})
            assert shot["image"]["data"].startswith(b"\xff\xd8")
            assert (workspace / "downloads").is_dir()
    finally:
        if process.returncode is None:
            process.terminate()
        await asyncio.wait_for(process.wait(), 15)
    assert process.returncode == 0
    # The driver ran as one of the service's children and stopped with it.
    driver = await asyncio.create_subprocess_exec(
        "pgrep", "-f", str(workspace / "downloads"), stdout=asyncio.subprocess.PIPE
    )
    found, _ = await driver.communicate()
    assert driver.returncode == 1, found
