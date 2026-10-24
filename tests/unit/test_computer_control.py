"""Real Manager SQLite, original terminal IDs, byte files and process failures."""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest

from yuki.sandbox.persistent import PersistentManager
from yuki.workspace.store import WorkspaceStore

CLI = Path(__file__).resolve().parents[2] / "src/yuki/sandbox/yuki_computer.py"


def manager_at(root):
    home = root / "home"
    home.mkdir(exist_ok=True)
    manager = PersistentManager(
        root / "manager",
        WorkspaceStore(root / "artifacts"),
        "image",
        "egress",
        "",
        home,
        testing=True,
    )
    manager.ready = True

    async def observe(identity):
        return manager.get(identity)

    manager.wait_result = observe
    return manager


def actor():
    return {"person_id": str(uuid4()), "device_id": str(uuid4()), "device_label": "裝置🙂"}


def set_control(manager, action, who=None, lease=None, key=None, **extra):
    args = {"action": action, "actor": who or actor(), **extra}
    if lease is not None:
        args["lease_id"] = lease
    return manager.computer.set(key or str(uuid4()), args)


async def run(manager, work=None):
    args = {"command": "yuki-computer wait-control"}
    if work:
        args["env"] = {"YUKI_WORK_ID": work, "YUKI_STEP_LABEL": ""}
    return (await manager.submit("terminal_exec", args, str(uuid4())))["run_id"]


def result(manager, run_id):
    return json.loads((manager.computer.root / "results" / (run_id + ".json")).read_text())


async def test_transfers_continue_original_wait_until_final_lease_returns_after_restart(tmp_path):
    manager = manager_at(tmp_path)
    work = str(uuid4())
    first = set_control(manager, "take")["control"]["lease_id"]
    waiting, help_run = await run(manager, work), await run(manager, str(uuid4()))
    manager.computer.register({"run_id": waiting, "kind": "wait_control"})
    manager.computer.register({"run_id": help_run, "kind": "help", "note": "登入"})
    second_actor = actor()
    second = set_control(manager, "transfer", second_actor, first)["control"]["lease_id"]
    assert second != first
    assert not (manager.computer.root / "results" / (waiting + ".json")).exists()
    await manager.close()
    manager = manager_at(tmp_path)
    try:
        assert manager.computer.snapshot()["control"]["device_id"] == second_actor["device_id"]
        third = set_control(manager, "transfer", lease=second)["control"]["lease_id"]
        assert set_control(manager, "return", lease=first)["error"] == "precondition_failed"
        assert manager.computer.control()["lease_id"] == third
        by = actor()
        returned = set_control(manager, "return", by, third, note="完成🙂")
        assert returned["control"] == {"holder": "none"}
        assert manager.computer.snapshot()["help_requests"] == []
        assert result(manager, waiting)["by"] == by
        assert result(manager, waiting)["control_free"] is True
        assert result(manager, waiting)["lease_id"] == third
        assert result(manager, help_run)["note"] == "完成🙂"
        saved = json.loads(
            manager.db.execute(
                "SELECT result FROM help_requests WHERE run_id=?", (waiting,)
            ).fetchone()[0]
        )
        assert result(manager, waiting) == saved
        assert (
            manager.db.execute(
                "SELECT work_id FROM help_requests WHERE run_id=?", (waiting,)
            ).fetchone()[0]
            == work
        )
        assert manager.get(waiting)["pending"]  # No fake completion or new execution.
    finally:
        await manager.close()


async def test_early_return_is_control_free_and_preexisting_result_is_read_before_register(
    tmp_path,
):
    manager = manager_at(tmp_path)
    try:
        lease = set_control(manager, "take")["control"]["lease_id"]
        set_control(manager, "return", lease=lease)
        waiting = await run(manager)
        manager.computer.register({"run_id": waiting, "kind": "wait_control"})
        value = result(manager, waiting)
        assert value["holder"] == "none" and value["control_free"] is True
        assert "by" not in value and "returned" not in value
        assert not manager.computer.snapshot()["help_requests"]
        # A later human lease does not replace the already committed original result.
        set_control(manager, "take")
        completed = await asyncio.create_subprocess_exec(
            sys.executable,
            str(CLI),
            "wait-control",
            env={
                **os.environ,
                "YUKI_JOB_ID": waiting,
                "YUKI_COMPUTER_RUNTIME": str(manager.computer.root),
            },
            stdout=asyncio.subprocess.PIPE,
        )
        output, _ = await asyncio.wait_for(completed.communicate(), 5)
        assert completed.returncode == 0 and json.loads(output) == value
        assert not (manager.computer.root / "requests" / (waiting + ".json")).exists()
    finally:
        await manager.close()


async def test_original_request_retry_conflict_and_agent_take_does_not_kill_run(tmp_path):
    manager = manager_at(tmp_path)
    try:
        original = await run(manager)
        agent = set_control(manager, "agent_acquire", {"run_id": original, "work_id": str(uuid4())})
        device, key = actor(), str(uuid4())
        first = set_control(manager, "take", device, key=key)
        assert first["control"]["holder"] == "human"
        assert manager.get(original)["pending"]
        assert (
            set_control(
                manager, "agent_release", {"run_id": original}, agent["control"]["lease_id"]
            )["error"]
            == "precondition_failed"
        )
        set_control(manager, "return", lease=first["control"]["lease_id"])
        assert set_control(manager, "take", device, key=key) == first
        assert manager.computer.control()["holder"] == "none"
        assert (
            set_control(manager, "take", device, key=key, note="changed")["error"]
            == "idempotency_conflict"
        )
    finally:
        await manager.close()


async def test_live_cli_registers_and_reads_actual_original_result_and_cancel_removes_wait(
    tmp_path,
):
    manager = manager_at(tmp_path)
    process = None
    try:
        lease = set_control(manager, "take")["control"]["lease_id"]
        identity = await run(manager)
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(CLI),
            "help-request",
            "--note",
            "需要協助",
            env={
                **os.environ,
                "YUKI_JOB_ID": identity,
                "YUKI_COMPUTER_RUNTIME": str(manager.computer.root),
            },
            stdout=asyncio.subprocess.PIPE,
        )
        for _ in range(100):
            if (manager.computer.root / "requests" / (identity + ".json")).exists():
                break
            await asyncio.sleep(0.01)
        manager.computer.collect_requests()
        assert manager.computer.snapshot()["help_requests"][0]["note"] == "需要協助"
        set_control(manager, "return", lease=lease)
        output, _ = await asyncio.wait_for(process.communicate(), 5)
        assert process.returncode == 0 and json.loads(output) == result(manager, identity)
        process = None
        cancelled = await run(manager)
        manager.computer.register({"run_id": cancelled, "kind": "help", "note": "cancel me"})
        await manager.control(cancelled, "cancel")
        assert not manager.computer.snapshot()["help_requests"]
        lease = set_control(manager, "take")["control"]["lease_id"]
        set_control(manager, "return", lease=lease)
        assert not (manager.computer.root / "results" / (cancelled + ".json")).exists()
    finally:
        if process and process.returncode is None:
            process.kill()
            await process.wait()
        await manager.close()


async def test_post_commit_process_death_replays_original_result_without_new_lease(tmp_path):
    manager = manager_at(tmp_path)
    lease = set_control(manager, "take")["control"]["lease_id"]
    identity = await run(manager)
    manager.computer.register({"run_id": identity, "kind": "wait_control"})
    key, who = str(uuid4()), actor()
    await manager.close()
    script = """
import os, json, sys
from pathlib import Path
from yuki.sandbox.persistent import PersistentManager
from yuki.workspace.store import WorkspaceStore
root=Path(sys.argv[1])
m=PersistentManager(root/'manager', WorkspaceStore(root/'artifacts'),
 'image', 'egress', '', root/'home', testing=True)
def die(*args): os._exit(71)
m.computer.deliver_results=die
m.computer.set(sys.argv[2],json.loads(sys.argv[3]))
"""
    args = {"action": "return", "lease_id": lease, "actor": who, "note": "durable"}
    child = await asyncio.create_subprocess_exec(
        sys.executable, "-c", script, str(tmp_path), key, json.dumps(args)
    )
    assert await child.wait() == 71
    manager = manager_at(tmp_path)
    try:
        assert manager.computer.control() == {"holder": "none"}
        assert not (manager.computer.root / "results" / (identity + ".json")).exists()
        replay = manager.computer.request_get(key)
        assert replay["by"] == who and replay["note"] == "durable"
        assert result(manager, identity)["by"] == who
        (manager.computer.root / "results" / (identity + ".json")).unlink()
        assert manager.computer.set(key, args) == replay
        assert result(manager, identity)["by"] == who
        assert (
            manager.db.execute(
                "SELECT COUNT(*) FROM environment_mutations WHERE request_id=?", (key,)
            ).fetchone()[0]
            == 1
        )
    finally:
        await manager.close()


async def test_independent_processes_return_same_lease_only_once(tmp_path):
    manager = manager_at(tmp_path)
    lease = set_control(manager, "take")["control"]["lease_id"]
    await manager.close()
    script = """
import json,sys
from pathlib import Path
from yuki.sandbox.persistent import PersistentManager
from yuki.workspace.store import WorkspaceStore
root=Path(sys.argv[1])
m=PersistentManager(root/'manager',WorkspaceStore(root/'artifacts'),'image','egress','',root/'home',testing=True)
# File delivery is tested separately; inspect serialization of the actual database writes.
m.computer.deliver_results=lambda:None
print(json.dumps(m.computer.set(sys.argv[2],json.loads(sys.argv[3]))))
"""
    args = {"action": "return", "lease_id": lease, "actor": actor()}
    children = await asyncio.gather(
        *(
            asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                script,
                str(tmp_path),
                str(uuid4()),
                json.dumps(args),
                stdout=asyncio.subprocess.PIPE,
            )
            for _ in range(2)
        )
    )
    output = await asyncio.gather(*(child.communicate() for child in children))
    values = [json.loads(item[0]) for item in output]
    assert (
        sum(
            value.get("control", {}).get("holder") == "none" and "error" not in value
            for value in values
        )
        == 1
    )
    assert sum(value.get("error") == "precondition_failed" for value in values) == 1
    assert all(child.returncode == 0 for child in children)


async def test_runtime_symlink_cannot_redirect_manager_file_writes(tmp_path):
    manager = manager_at(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    manager.computer.root.symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(OSError):
            set_control(manager, "take")
        assert list(outside.iterdir()) == []
        # The command committed; failure to deliver cannot authorize another take.
        assert manager.computer.control()["holder"] == "human"
    finally:
        await manager.close()


async def test_socket_watch_uses_real_persisted_changes_and_disconnect_drops_subscription(tmp_path):
    manager = manager_at(tmp_path)
    short = tempfile.TemporaryDirectory(prefix="yc-", dir="/tmp")
    socket = Path(short.name) / "m.sock"
    server = await asyncio.start_unix_server(manager.serve, str(socket))
    try:
        reader, writer = await asyncio.open_unix_connection(str(socket))
        writer.write(b'{"method":"control_watch","args":{}}\n')
        await writer.drain()
        assert json.loads(await asyncio.wait_for(reader.readline(), 2))["control"] == {
            "holder": "none"
        }
        take = await manager.handle(
            {
                "method": "control_set",
                "args": {"action": "take", "actor": actor()},
                "request_id": str(uuid4()),
            }
        )
        update = json.loads(await asyncio.wait_for(reader.readline(), 2))
        assert update["control"] == take["control"] and update["revision"] == 1
        writer.close()
        await writer.wait_closed()
        for _ in range(100):
            if not manager.computer.changed:
                break
            await asyncio.sleep(0.01)
        assert not manager.computer.changed
        assert manager.computer.control() == take["control"]
    finally:
        server.close()
        await server.wait_closed()
        short.cleanup()
        await manager.close()


async def test_system_release_finishes_others_wait_as_system_and_agent_end_cannot_release_human(
    tmp_path,
):
    manager = manager_at(tmp_path)
    try:
        identity = await run(manager, str(uuid4()))
        manager.computer.register(
            {"run_id": identity, "kind": "help", "note": "someone else's work"}
        )
        lease = set_control(manager, "take")["control"]["lease_id"]
        set_control(manager, "return", {"system": True}, lease, reason="person_forgotten")
        assert result(manager, identity)["by"] == "system"
        assert result(manager, identity)["reason"] == "person_forgotten"
        manager.finish(identity, "succeeded", {"exit_code": 0})
        assert (
            manager.db.execute(
                "SELECT cancelled_at FROM help_requests WHERE run_id=?", (identity,)
            ).fetchone()[0]
            is None
        )
        original = await run(manager)
        set_control(manager, "agent_acquire", {"run_id": original})
        manager.finish(original, "failed", {"exit_code": 1})
        assert manager.computer.control() == {"holder": "none"}
        second = await run(manager)
        set_control(manager, "agent_acquire", {"run_id": second})
        human = set_control(manager, "take")["control"]
        manager.finish(second, "cancelled", {"exit_code": None})
        assert manager.computer.control() == human
    finally:
        await manager.close()


async def test_terminal_wire_inherits_validated_original_work_without_leaking_person_source(
    database, tmp_path
):
    from tests.support.work_effect_results_helpers import owned_session

    from yuki.runtime.work_activation import current_work_control
    from yuki.sandbox.client import SandboxClient
    from yuki.sandbox.task_repository import SandboxTaskRepository

    control, _, _ = await owned_session(database, tmp_path)
    manager = manager_at(tmp_path)
    short = tempfile.TemporaryDirectory(prefix="yc-", dir="/tmp")
    socket = Path(short.name) / "m.sock"
    server = await asyncio.start_unix_server(manager.serve, str(socket))
    client = SandboxClient(socket, tasks=SandboxTaskRepository(database))
    token = current_work_control.set(control)
    try:
        original = {
            "work_id": control.current["id"],
            "conversation_id": control.lease.conversation_id,
            "generation": control.lease.generation,
        }
        response = await client.execute(
            "terminal_exec",
            {"command": "printf original"},
            request_id="original-terminal",
            source=original,
        )
        assert response["pending"]
        row = manager.db.execute(
            "SELECT payload FROM jobs WHERE id=?", (response["run_id"],)
        ).fetchone()
        payload = json.loads(row[0])
        assert payload == {
            "command": "printf original",
            "env": {"YUKI_WORK_ID": control.current["id"]},
        }
        manager.computer.register(
            {"run_id": response["run_id"], "kind": "help", "note": "actual source"}
        )
        assert manager.computer.snapshot()["help_requests"][0]["work_id"] == control.current["id"]
        # Re-observing the same intent retains the original Manager execution.
        retried = await client.execute(
            "terminal_exec",
            {"command": "printf original"},
            request_id="original-terminal",
            source=original,
        )
        assert retried["run_id"] == response["run_id"]
    finally:
        current_work_control.reset(token)
        server.close()
        await server.wait_closed()
        short.cleanup()
        await manager.close()


@pytest.mark.parametrize(
    "kind,with_work", [("terminal_exec", True), ("terminal_exec", False), ("service", True)]
)
async def test_supervisor_exposes_only_this_terminal_work_and_clears_inherited_service_labels(
    tmp_path, kind, with_work
):
    import shlex

    manager = manager_at(tmp_path)
    identity = str(uuid4())
    work = str(uuid4())
    code = (
        'import os,json; print(json.dumps([os.getenv("YUKI_WORK_ID"),'
        'os.getenv("YUKI_STEP_LABEL")]))'
    )
    args = {
        "command": f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}",
        "cwd": str(tmp_path),
    }
    if with_work:
        args["env"] = {"YUKI_WORK_ID": work, "YUKI_STEP_LABEL": ""}
    manager.prepare_spec(identity, args, kind)
    wrapper = """
import sys,importlib.util
from pathlib import Path
spec=importlib.util.spec_from_file_location('isolated_supervisor',sys.argv[3])
s=importlib.util.module_from_spec(spec);spec.loader.exec_module(s)
root,identity=Path(sys.argv[1]),sys.argv[2]
s.Path=lambda value:root if value=='/var/lib/yuki-runtime' else Path(value)
sys.argv=['supervisor',identity]
raise SystemExit(s.main())
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        wrapper,
        str(manager.runtime_root),
        identity,
        str(CLI.with_name("environment_supervisor.py")),
        env={
            **os.environ,
            "YUKI_WORK_ID": "stale-other-work",
            "YUKI_STEP_LABEL": "stale-other-label",
        },
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, error = await asyncio.wait_for(process.communicate(), 5)
        assert process.returncode == 0, error
        output = b"".join(
            path.read_bytes() for path in sorted(manager.state_path(identity).glob("output-*.log"))
        )
        assert json.loads(output) == (
            [work, ""] if kind == "terminal_exec" and with_work else [None, None]
        )
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        await manager.close()


async def test_registration_file_survives_delivery_failure_and_resumes_without_duplicate(
    tmp_path, monkeypatch
):
    manager = manager_at(tmp_path)
    try:
        identity = await run(manager)
        directory = manager.computer.root / "requests"
        directory.mkdir(parents=True)
        request = directory / (identity + ".json")
        request.write_text(json.dumps({"kind": "wait_control"}))
        write = manager.computer._write

        def fail(*args):
            raise OSError("disk unavailable after commit")

        monkeypatch.setattr(manager.computer, "_write", fail)
        with pytest.raises(OSError):
            manager.computer.collect_requests()
        assert request.exists()
        assert manager.db.execute("SELECT COUNT(*) FROM help_requests").fetchone()[0] == 1
        monkeypatch.setattr(manager.computer, "_write", write)
        manager.computer.collect_requests()
        assert not request.exists()
        assert result(manager, identity)["control_free"] is True
        assert manager.db.execute("SELECT COUNT(*) FROM help_requests").fetchone()[0] == 1
    finally:
        await manager.close()
