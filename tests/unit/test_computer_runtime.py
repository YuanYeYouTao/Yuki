"""Actual CLI/CDP and durable service lifecycle; no Docker daemon required."""

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest
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

    async def interrupted(*args):
        result = await submit(*args)
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


def test_resource_configuration_uses_desktop_defaults_and_existing_overrides(tmp_path):
    manager = manager_at(tmp_path)
    try:
        assert manager.resource_limits() == {
            "memory_mib": 3072,
            "cpus": 2,
            "cpu_shares": 1024,
            "swap_mib": 512,
            "pids": 2048,
            "tmp_mib": 512,
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


def test_installer_executes_only_original_environment_upgrade_and_stops_for_wrong_label(tmp_path):
    binary = tmp_path / "bin"
    binary.mkdir()
    capture = tmp_path / "docker-calls.jsonl"
    docker = binary / "docker"
    docker.write_text(f"""#!{sys.executable}
import json,os,sys
with open(os.environ['CAPTURE'],'a') as stream: stream.write(json.dumps(sys.argv[1:])+'\\n')
args=sys.argv[1:]
if args[0]=='inspect':
 if '.Labels' in args[2]: print(os.environ.get('TEST_LABEL','persistent-v1'))
 elif '.State.Running' in args[2]: print('true')
""")
    docker.chmod(0o755)
    installer = Path(__file__).resolve().parents[2] / "deploy/sandbox/install-desktop.sh"
    environment = {
        **os.environ,
        "PATH": str(binary) + ":" + os.environ["PATH"],
        "CAPTURE": str(capture),
    }
    done = subprocess.run(
        ["sh", str(installer), "--container", "original-environment"],
        env=environment,
        capture_output=True,
    )
    assert done.returncode == 0, done.stderr
    calls = [json.loads(line) for line in capture.read_text().splitlines()]
    assert all(call[0] in {"inspect", "exec", "cp"} for call in calls)
    assert any(call[0] == "exec" and "apt-get install" in " ".join(call) for call in calls)
    assert sum(call[0] == "cp" for call in calls) == 4
    before = len(calls)
    rejected = subprocess.run(
        ["sh", str(installer), "--container", "original-environment"],
        env={**environment, "TEST_LABEL": "other-owner"},
        capture_output=True,
    )
    assert rejected.returncode != 0
    assert len(capture.read_text().splitlines()) == before + 1
