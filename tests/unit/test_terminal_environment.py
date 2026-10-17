"""Real Manager persistence and isolated native Supervisor behavior."""

import asyncio
import json
import os
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from qq_ai_bot.sandbox.persistent import PersistentManager
from qq_ai_bot.workspace.store import WorkspaceStore

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX environment runtime")
SUPERVISOR_PATH = (
    Path(__file__).resolve().parents[2] / "src/qq_ai_bot/sandbox/environment_supervisor.py"
)
NETWORK_GUARD_PATH = Path(__file__).resolve().parents[2] / "deploy/sandbox/network-guard.sh"


def manager_at(tmp_path):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    manager = PersistentManager(
        tmp_path / "manager",
        WorkspaceStore(tmp_path / "artifacts"),
        "image",
        "egress",
        "",
        home,
        testing=True,
    )
    manager.ready = True
    manager.container_id = "original-container"

    async def observe(identity):
        return manager.get(identity)

    manager.wait_result = observe
    return manager


async def test_manager_admits_real_jobs_despite_old_count_memory_and_outbox_limits(
    tmp_path, monkeypatch
):
    manager = manager_at(tmp_path)
    monkeypatch.setattr(manager, "available_memory", lambda: 0)
    try:
        for _ in range(513):
            identity = str(uuid4())
            manager.completions.record(identity, identity, {"status": "succeeded"})
        manager.db.commit()
        runs = []
        for tty in [False] * 3 + [True] * 6:
            result = await manager.submit(
                "terminal_exec", {"command": "printf retained", "tty": tty}, str(uuid4())
            )
            assert result["pending"] and result["status"] == "queued"
            runs.append(result["run_id"])
        assert len(set(runs)) == len(manager.active()) == 9
        assert manager.queue.qsize() == 9
        assert manager.db.execute("SELECT COUNT(*) FROM completion_outbox").fetchone()[0] == 513
        args = {"command": "printf original"}
        original = await manager.submit("terminal_exec", args, "original-request")
        manager.ready = False
        assert await manager.submit("terminal_exec", args, "original-request") == original
        assert (
            await manager.submit("terminal_exec", {"command": "different"}, "original-request")
        )["error"] == "idempotency_conflict"
    finally:
        await manager.close()


async def test_services_keep_real_policy_without_count_or_attempt_caps(tmp_path):
    manager = manager_at(tmp_path)
    try:
        for index in range(6):
            name = f"服务 {index}"
            result = await manager.service(
                {"action": "register", "name": name, "command": "sleep 1"}
            )
            assert result["registered"]
            with manager.db:
                manager.db.execute(
                    "UPDATE environment_services SET enabled=1,attempts=20 WHERE name=?", (name,)
                )
        await manager.restore_services()
        assert len(manager.active()) == 6
        assert all(
            row[0] == 21 for row in manager.db.execute("SELECT attempts FROM environment_services")
        )
        original = manager.db.execute(
            "SELECT run_id FROM environment_services WHERE name='服务 0'"
        ).fetchone()[0]
        updated = await manager.service(
            {"action": "register", "name": "服务 0", "command": "sleep 2"}
        )
        assert updated["registered"]
        assert (
            manager.db.execute(
                "SELECT run_id FROM environment_services WHERE name='服务 0'"
            ).fetchone()[0]
            == original
        )
        await manager.service({"action": "stop", "name": "服务 0"})
        assert (
            manager.db.execute(
                "SELECT enabled FROM environment_services WHERE name='服务 0'"
            ).fetchone()[0]
            == 0
        )
    finally:
        await manager.close()


async def test_retired_methods_never_submit_but_legacy_jobs_keep_original_results(tmp_path):
    manager = manager_at(tmp_path)
    try:
        identity = str(uuid4())
        completion = {
            "artifact_id": str(uuid4()),
            "exit_code": 0,
            "output": "original Python output",
        }
        with manager.db:
            manager.db.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?,?)",
                (identity, "legacy-python", "proof", "{}", "succeeded", json.dumps(completion), 1),
            )
        result = await manager.handle({"method": "terminal_read", "args": {"run_id": identity}})
        assert result["completion"] == {
            **completion,
            "run_id": identity,
            "status": "succeeded",
            "pending": False,
            "external_untrusted": True,
        }
        for method in (
            "run_python",
            "environment_packages",
            "get_code_run",
            "cancel_code_run",
            "terminal_write",
            "workspace_read",
            "workspace_write",
            "workspace_list",
            "workspace_delete",
        ):
            assert await manager.handle(
                {"method": method, "args": {"run_id": identity}, "request_id": method}
            ) == {"error": "unknown_method"}
        assert manager.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    finally:
        await manager.close()


async def test_read_wait_cancellation_leaves_process_and_original_intent_alive(tmp_path):
    manager = manager_at(tmp_path)
    try:
        result = await manager.submit("terminal_exec", {"command": "sleep 100"}, "waiting-original")
        identity = result["run_id"]
        # Use the real observation waiter, not the fixture's nonblocking submit read.
        manager.wait_result = PersistentManager.wait_result.__get__(manager)
        waiting = asyncio.create_task(
            manager.handle(
                {"method": "terminal_read", "args": {"run_id": identity, "wait_seconds": 100}}
            )
        )
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert manager.get(identity)["pending"]
        assert not manager._waiters
        assert (
            manager.db.execute("SELECT request_id FROM jobs WHERE id=?", (identity,)).fetchone()[0]
            == "waiting-original"
        )
        manager.finish(identity, "failed", {"exit_code": 7, "output": "original partial work"})
        read = await manager.handle(
            {"method": "terminal_read", "args": {"run_id": identity, "wait_seconds": 100}}
        )
        assert read["completion"]["exit_code"] == 7 and not read["pending"]
    finally:
        await manager.close()


async def test_new_terminal_uses_one_execd_path_and_no_implicit_lifetime(tmp_path):
    manager = manager_at(tmp_path)
    manager.execd = SimpleNamespace(
        request=AsyncMock(return_value={"session_id": "existing-execd-session"})
    )
    manager.attach = AsyncMock()
    try:
        result = await manager.submit(
            "terminal_exec",
            {
                "command": "printf '中文'; python -V; node -v",
                "cwd": ".",
                "tty": True,
                "timeout_seconds": 172800,
            },
            "terminal-request",
        )
        await manager.launch(result["run_id"])
        spec = json.loads((manager.runtime_root / result["run_id"] / "spec.json").read_text())
        assert spec["timeout_seconds"] == 172800 and spec["cwd"] == "." and spec["tty"]
        manager.attach.assert_awaited_once_with(
            result["run_id"], "existing-execd-session", tty=True
        )
        assert manager.execd.request.await_count == 1
        default = manager.prepare_spec(str(uuid4()), {"command": "sleep 99999"}, "terminal_exec")
        assert default["timeout_seconds"] == 0
        assert manager.get(result["run_id"])["status"] == "running"
    finally:
        await manager.close()


async def test_recovery_preserves_installed_image_and_uses_admin_execd(tmp_path, monkeypatch):
    from qq_ai_bot.sandbox import persistent

    manager = manager_at(tmp_path)
    manager.set_setting("checkpoint_image", "saved-rootfs:original-installations")
    current = {
        "Id": "original-environment",
        "State": {"Running": True, "StartedAt": "original-start"},
        "HostConfig": {"RestartPolicy": {"Name": "no"}},
        "NetworkSettings": {"Networks": {"egress": {"IPAddress": "172.30.0.2"}}},
    }
    manager.inspect_container = AsyncMock(side_effect=[None, current])
    manager.command = AsyncMock(return_value=(0, b""))
    monkeypatch.setattr(
        persistent, "Execd", lambda *_: SimpleNamespace(request=AsyncMock(return_value={}))
    )
    try:
        await manager.recover()
        command = manager.command.await_args.args
        assert command[:2] == ("docker", "create")
        assert command[command.index("--user") + 1] == "0:0"
        assert command[command.index("--network") + 1] == "egress"
        assert command[-1] == "saved-rootfs:original-installations"
        assert f"type=bind,src={manager.home},dst=/home/yuki" in command
        assert f"type=bind,src={manager.runtime_root},dst=/var/lib/yuki-runtime" in command
        assert (
            f"type=bind,src={SUPERVISOR_PATH},dst=/opt/yuki-runtime/supervisor.py,readonly"
            in command
        )
        assert "HOME=/home/yuki" in command and "USER=root" in command and "LOGNAME=root" in command
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            assert f"{name}=" in command
        assert manager.container_id == "original-environment:original-start"

        # Updating the Manager alone must leave the existing environment intact.
        manager.inspect_container = AsyncMock(return_value=current)
        manager.command.reset_mock()
        await manager.recover()
        manager.command.assert_not_awaited()
        assert manager.container_id == "original-environment:original-start"
    finally:
        await manager.close()


@pytest.mark.parametrize("native", ["not_started", "unknown", "failed"])
async def test_recovery_observes_legacy_package_intent_without_new_dispatch(
    tmp_path, monkeypatch, native
):
    from qq_ai_bot.sandbox import persistent

    manager = manager_at(tmp_path)
    identity = str(uuid4())
    with manager.db:
        manager.db.execute(
            "INSERT INTO jobs VALUES (?,?,?,?,?,?,?)",
            (identity, "original-apt-request", "original-proof", "{}", "queued", "{}", 1),
        )
        manager.db.execute(
            "INSERT INTO environment_jobs VALUES (?,?,?,?,?)",
            (identity, "environment_packages", None, "old-container:old-start", 0),
        )
    state = manager.state_path(identity)
    state.mkdir(parents=True)
    if native != "not_started":
        (state / "started").touch()
    if native == "failed":
        (state / "status.json").write_text(json.dumps({"status": "failed", "exit_code": 7}))
    manager.inspect_container = AsyncMock(
        return_value={
            "Id": "old-container",
            "State": {"Running": True, "StartedAt": "old-start"},
            "HostConfig": {"RestartPolicy": {"Name": "no"}},
            "NetworkSettings": {"Networks": {"egress": {"IPAddress": "172.30.0.2"}}},
        }
    )
    manager.command = AsyncMock()
    monkeypatch.setattr(
        persistent, "Execd", lambda *_: SimpleNamespace(request=AsyncMock(return_value={}))
    )
    try:
        await manager.recover()
        manager.command.assert_not_awaited()
        assert manager.queue.empty()
        result = manager.get(identity)
        if native == "not_started":
            assert result["status"] == "cancelled"
            assert result["error"] == "retired_execution_not_started"
        elif native == "unknown":
            assert result["pending"] and result["status"] == "queued"
            assert not manager.completions.pending()["events"]
        else:
            assert result["status"] == "failed" and result["exit_code"] == 7
            receipt = manager.completions.pending()["events"][0]
            assert receipt["run_id"] == identity
            assert receipt["request_id"] == "original-apt-request"
    finally:
        await manager.close()


async def test_network_guard_allows_public_protocols_after_private_isolation(tmp_path):
    upstream_hook = Path("/etc/yuki-sandbox/upstream-firewall.sh")
    if await asyncio.to_thread(upstream_hook.exists):
        pytest.skip("isolated harness must not invoke an operator firewall hook")
    binary = tmp_path / "bin"
    binary.mkdir()
    capture = tmp_path / "iptables-arguments"
    for name, body in {
        "id": "printf '0\\n'",
        "sysctl": "printf '1\\n'",
        "iptables": ('printf "%s\\n" "$*" >> "$CAPTURE"\ncase "$*" in *" -C "*) exit 1;; esac'),
    }.items():
        path = binary / name
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)
    process = await asyncio.create_subprocess_exec(
        "/bin/sh",
        str(NETWORK_GUARD_PATH),
        env={**os.environ, "PATH": f"{binary}:/usr/bin:/bin", "CAPTURE": str(capture)},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    assert process.returncode == 0, (stdout, stderr)
    commands = capture.read_text().splitlines()
    public = [line for line in commands if " -A YUKI-EGRESS " in line]
    assert public[-1] == "-w 10 -A YUKI-EGRESS -j RETURN"
    assert all(" -d " in line and line.endswith(" -j DROP") for line in public[:-1])
    assert any(" -d 10.0.0.0/8 " in line for line in public)
    assert any(" -d 172.16.0.0/12 " in line for line in public)
    assert any(" -d 192.168.0.0/16 " in line for line in public)
    assert any(" -d 169.254.0.0/16 " in line for line in public)
    assert not any(" --dport " in line or " -p " in line for line in public)
    assert any("INPUT -i yuki-egress0 -j DROP" in line for line in commands)
    assert any(
        "INPUT -i yuki-egress0 -p tcp --sport 44772 -m conntrack --ctstate ESTABLISHED -j ACCEPT"
        in line
        for line in commands
    )


async def start_supervisor(tmp_path, command, *, tty=False, deadline=0):
    runtime = tmp_path / "runtime"
    identity = str(uuid4())
    job = runtime / identity
    (job / "state").mkdir(parents=True)
    (job / "spec.json").write_text(
        json.dumps(
            {"cwd": str(tmp_path), "command": command, "tty": tty, "timeout_seconds": deadline}
        )
    )
    wrapper = (
        "import sys, importlib.util; from pathlib import Path; "
        "spec=importlib.util.spec_from_file_location('isolated_supervisor', sys.argv[3]); "
        "s=importlib.util.module_from_spec(spec); spec.loader.exec_module(s); "
        "root, job = Path(sys.argv[1]), sys.argv[2]; "
        "s.Path = lambda value: root if value == '/var/lib/yuki-runtime' else Path(value); "
        "sys.argv=['supervisor', job]; "
        "raise SystemExit(s.main())"
    )
    environment = {**os.environ, "HOME": str(tmp_path), "PYTHONUNBUFFERED": "1"}
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        wrapper,
        str(runtime),
        identity,
        str(SUPERVISOR_PATH),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
    )
    return process, job


async def output_until(job, marker):
    async with asyncio.timeout(15):
        while True:
            output = b"".join(
                path.read_bytes() for path in sorted((job / "state").glob("output-*.log"))
            )
            if marker in output:
                return output
            await asyncio.sleep(0.02)


async def test_native_pty_keeps_shell_state_and_arbitrary_stdin(tmp_path):
    process, job = await start_supervisor(tmp_path, "exec bash --noprofile --norc", tty=True)
    try:
        assert process.stdin is not None
        process.stdin.write(
            b"export KEPT=retained; function yuki_test(){ printf 'FUNCTION_OK'; }; "
            b"mkdir -p nested; cd nested\n"
        )
        await process.stdin.drain()
        process.stdin.write(
            b'printf \'STATE=%s HOME=%s CWD=%s \' "$KEPT" "$HOME" "$PWD"; '
            b"yuki_test; printf '\\nREADY_MARKER\\n'\n"
        )
        await process.stdin.drain()
        output = await output_until(job, b"FUNCTION_OK\r\nREADY_MARKER")
        assert b"STATE=retained" in output and str(tmp_path / "nested").encode() in output
        process.stdin.write(b"exit\n")
        await process.stdin.drain()
        async with asyncio.timeout(15):
            assert await process.wait() == 0
        assert json.loads((job / "state/status.json").read_text())["status"] == "succeeded"
    finally:
        if process.returncode is None:
            (job / "state/cancel").touch()
            await process.wait()


async def test_native_shell_file_operations_and_detached_child(tmp_path):
    script = (
        "from pathlib import Path; import time; time.sleep(.3); "
        "Path('后台.bin').write_bytes(bytes(range(256)))"
    )
    command = (
        "mkdir -p '中文目录'; printf abc | tr a-z A-Z > '中文目录/原件'; "
        "ln -s 原件 '中文目录/链接'; mv '中文目录/原件' '中文目录/新件'; "
        f"nohup {shlex.quote(sys.executable)} -c {shlex.quote(script)} </dev/null >child.log 2>&1 &"
    )
    process, job = await start_supervisor(tmp_path, command)
    async with asyncio.timeout(15):
        assert await process.wait() == 0
        while True:
            if (tmp_path / "后台.bin").exists():
                break
            await asyncio.sleep(0.02)
    assert (tmp_path / "后台.bin").read_bytes() == bytes(range(256))
    assert (tmp_path / "中文目录/新件").read_text() == "ABC"
    assert (tmp_path / "中文目录/链接").is_symlink()
    assert json.loads((job / "state/status.json").read_text())["status"] == "succeeded"


async def test_native_explicit_timeout_keeps_written_file_and_real_exit(tmp_path):
    process, job = await start_supervisor(
        tmp_path, "printf preserved > result.txt; sleep 300", deadline=1
    )
    async with asyncio.timeout(15):
        assert await process.wait() != 0
    state = json.loads((job / "state/status.json").read_text())
    assert state["status"] == "failed" and state["error"] == "timeout"
    assert (tmp_path / "result.txt").read_text() == "preserved"
