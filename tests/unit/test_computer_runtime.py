"""Actual CLI/CDP and durable service lifecycle; no Docker daemon required."""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from uuid import uuid4

import pytest

pytest.importorskip("fcntl", reason="The persistent environment runs on Linux")
from tests.unit.test_computer_control import CLI, manager_at, run, set_control

from yuki.sandbox import yuki_computer
from yuki.sandbox.yuki_computer import Browser, ComputerError


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


async def test_real_cli_input_is_blocked_by_human_and_read_only_snapshot_does_not_acquire(
    tmp_path, monkeypatch, capsys
):
    manager = manager_at(tmp_path)
    process = None
    try:
        identity = await run(manager)
        set_control(manager, "take")
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(CLI),
            "input",
            "type",
            "should not execute xdotool",
            env={
                **os.environ,
                "YUKI_JOB_ID": identity,
                "YUKI_COMPUTER_RUNTIME": str(manager.computer.root),
            },
            stdout=asyncio.subprocess.PIPE,
        )
        while not list((manager.computer.root / "commands").glob("*.json")):
            assert process.returncode is None
            await asyncio.sleep(0.01)
        await manager.desktop.collect_commands()
        output, _ = await asyncio.wait_for(process.communicate(), 5)
        assert process.returncode == 75 and json.loads(output)["error"] == "control_held"
        process = None
        monkeypatch.setenv("YUKI_COMPUTER_RUNTIME", str(manager.computer.root))
        monkeypatch.setattr(
            sys, "argv", ["yuki-computer", "browser", "snapshot", "--tab", "real-target"]
        )
        monkeypatch.setattr(Browser, "target", lambda self, value: value)
        monkeypatch.setattr(
            Browser, "snapshot", lambda self, target: {"text": "tree", "target_id": target}
        )
        monkeypatch.setattr(
            yuki_computer,
            "input_lease",
            lambda root: pytest.fail("read-only snapshot tried to acquire input"),
        )
        assert yuki_computer.main() == 0
        assert json.loads(capsys.readouterr().out)["target_id"] == "real-target"
    finally:
        if process and process.returncode is None:
            process.kill()
            await process.wait()
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


@pytest.fixture
def actual_browser(tmp_path, monkeypatch):
    binary = os.environ.get("YUKI_TEST_CHROMIUM")
    if not binary:
        pytest.skip("Set YUKI_TEST_CHROMIUM to an actual isolated Chromium executable")
    profile = tmp_path / "chromium"
    process = subprocess.Popen(
        [
            binary,
            "--headless",
            "--no-sandbox",
            "--no-first-run",
            "--remote-debugging-port=0",
            "--user-data-dir=" + str(profile),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 15
        while not (profile / "DevToolsActivePort").exists():
            assert process.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.05)
        port = (profile / "DevToolsActivePort").read_text().splitlines()[0]
        monkeypatch.setattr(yuki_computer, "CDP_ADDRESS", "http://127.0.0.1:" + port)
        monkeypatch.setenv("HOME", str(tmp_path))
        yield Browser()
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_real_chromium_target_ownership_snapshot_click_unicode_and_restore_unknown(
    actual_browser, monkeypatch, tmp_path
):
    browser = actual_browser
    first_work, second_work = str(uuid4()), str(uuid4())
    page = (
        "data:text/html,<title>same</title><input aria-label='editor'>"
        "<button onclick='document.title=\"clicked\"'>click me</button>"
    )
    monkeypatch.setenv("YUKI_WORK_ID", first_work)
    first = browser.open(page, "same")["target_id"]
    monkeypatch.setenv("YUKI_WORK_ID", second_work)
    second = browser.open(page, "same")["target_id"]
    assert browser.target("same") == second
    monkeypatch.setenv("YUKI_WORK_ID", first_work)
    assert browser.target("same") == first
    tree = browser.snapshot(first)["text"]
    # Follow actual AX node IDs returned by Chromium, not guessed DOM indexes.
    click_node = int(
        next(line.split("]")[0][1:] for line in tree.splitlines() if "button: click me" in line)
    )
    editor = int(
        next(line.split("]")[0][1:] for line in tree.splitlines() if "textbox: editor" in line)
    )
    browser.click(first, click_node)
    assert next(tab for tab in browser.tabs() if tab["target_id"] == first)["title"] == "clicked"
    browser.call("DOM.focus", {"backendNodeId": editor}, first)
    browser.call("Input.insertText", {"text": "真文字🙂"}, first)
    value = browser.call(
        "Runtime.evaluate",
        {"expression": "document.querySelector('input').value", "returnByValue": True},
        first,
    )
    assert value["result"]["value"] == "真文字🙂"
    # A user-created/restored target has no original ownership record.
    unknown = browser.call("Target.createTarget", {"url": page})["targetId"]
    tabs = {tab["target_id"]: tab for tab in browser.tabs()}
    assert tabs[first]["work_id"] == first_work and tabs[second]["work_id"] == second_work
    assert "work_id" not in tabs[unknown]
    browser.close(first)
    assert first not in json.loads((browser.root / "tabs.json").read_text())
    monkeypatch.setenv("YUKI_WORK_ID", str(uuid4()))
    with pytest.raises(ComputerError, match="tab_not_found"):
        browser.target("same")


async def test_actual_cli_types_and_screenshots_through_manager_lease_and_releases_it(
    actual_browser, tmp_path, monkeypatch
):
    browser = actual_browser
    manager = manager_at(tmp_path)
    work = str(uuid4())
    monkeypatch.setenv("YUKI_WORK_ID", work)
    target = browser.open("data:text/html,<input aria-label='editor'>", "edit")["target_id"]
    tree = browser.snapshot(target)["text"]
    node = int(
        next(line.split("]")[0][1:] for line in tree.splitlines() if "textbox: editor" in line)
    )
    identity = await run(manager, work)
    manager.computer.deliver_results()
    environment = {
        **os.environ,
        "YUKI_JOB_ID": identity,
        "YUKI_COMPUTER_RUNTIME": str(manager.computer.root),
    }
    wrapper = (
        "import sys; from yuki.sandbox import yuki_computer as c; "
        "c.CDP_ADDRESS=sys.argv[1]; sys.argv=['yuki-computer',*sys.argv[2:]]; "
        "raise SystemExit(c.main())"
    )
    process = None

    async def invoke(*args):
        nonlocal process
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            wrapper,
            yuki_computer.CDP_ADDRESS,
            *args,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        async with asyncio.timeout(10):
            while process.returncode is None:
                await manager.desktop.collect_commands()
                await asyncio.sleep(0.01)
            output, error = await process.communicate()
        assert process.returncode == 0, (output, error)
        return json.loads(output)

    try:
        await invoke("browser", "type", "實際 CLI🙂", "--tab", target, "--node", str(node))
        value = browser.call(
            "Runtime.evaluate",
            {"expression": "document.querySelector('input').value", "returnByValue": True},
            target,
        )
        assert value["result"]["value"] == "實際 CLI🙂"
        assert manager.computer.control() == {"holder": "none"}
        recorded = json.loads((manager.state_path(identity) / "step-label.json").read_text())
        assert recorded == {"step_label": "browser"}
        # A screenshot remains a read operation while a human holds control.
        human = set_control(manager, "take")["control"]
        destination = tmp_path / "actual.png"
        await invoke("screenshot", "--tab", target, "--out", str(destination))
        assert destination.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        await invoke("browser", "snapshot", "--tab", target)
        assert manager.computer.control() == human
    finally:
        if process and process.returncode is None:
            process.kill()
            await process.wait()
        await manager.close()


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


async def test_real_headed_service_renders_unicode_exposes_rfb_and_stops_children(
    tmp_path, monkeypatch
):
    if not all(shutil.which(binary) for binary in ("Xvnc", "openbox", "chromium")):
        pytest.skip("Requires the actual headed environment packages")
    root = tmp_path / "runtime"
    root.mkdir()
    script = CLI.with_name("desktop_service.py")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script),
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "YUKI_COMPUTER_RUNTIME": str(root),
            "YUKI_JOB_ID": str(uuid4()),
        },
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
            monkeypatch.setenv("HOME", str(tmp_path))
            browser = Browser()
            page = await asyncio.to_thread(
                browser.open, "data:text/html;charset=utf-8,<h1>Yuki 真實有頭畫面</h1>", None
            )
            while True:
                snapshot = await asyncio.to_thread(browser.snapshot, page["target_id"])
                if "Yuki 真實有頭畫面" in snapshot["text"]:
                    break
                await asyncio.sleep(0.1)
            png = await asyncio.to_thread(
                browser.call, "Page.captureScreenshot", {}, page["target_id"]
            )
            import base64

            assert base64.b64decode(png["data"]).startswith(b"\x89PNG\r\n\x1a\n")
    finally:
        if process.returncode is None:
            process.terminate()
        await asyncio.wait_for(process.wait(), 15)
    assert process.returncode == 0
