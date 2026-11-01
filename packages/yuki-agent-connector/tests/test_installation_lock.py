from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from yuki_agent_connector.config import Config
from yuki_agent_connector.journal import Journal

OWNER = """
import json, os, sys
from pathlib import Path
from yuki_agent_connector.connector import installation_lock
with installation_lock(Path(sys.argv[1])):
    print(json.dumps({'owner_pid': os.getpid()}), flush=True)
    if sys.argv[2] == 'wait':
        sys.stdin.buffer.read()
"""


def native_probe(path: Path) -> None:
    with path.open("r+b", buffering=0) as stream:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@pytest.mark.parametrize("stale_pid", [False, True])
@pytest.mark.parametrize("abrupt", [False, True])
def test_actual_installation_owner_rejects_second_process_and_reopens_after_exit(
    tmp_path: Path, stale_pid: bool, abrupt: bool
) -> None:
    path = tmp_path / "connector.lock"
    path.write_bytes(b"\0previous-pid" if stale_pid else b"")
    config_path = tmp_path / "config.json"
    Config("ws://127.0.0.1/agents/v1/connect", "original-device", "fixture-token").save(config_path)
    original_config = config_path.read_bytes()
    journal = Journal(tmp_path / "journal.sqlite3", namespace="original-device")
    journal.begin(
        {
            "id": "original-operation",
            "instance_id": "original-instance",
            "action": "watch",
            "execution_id": "original-execution",
        }
    )
    journal.complete(
        "original-operation",
        {"result": {"pending": True, "status": "unknown", "execution_id": "original-execution"}},
    )
    original_receipt = journal.operation("original-operation")
    journal.close()
    environment = dict(os.environ)
    executable = sys.executable
    if sys.platform == "win32":
        executable = sys._base_executable
        environment["PYTHONPATH"] = os.pathsep.join(
            [
                str(Path(__file__).resolve().parents[1] / "src"),
                str(Path(sys.prefix) / "Lib" / "site-packages"),
            ]
        )
    options = {
        "creationflags": subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        "env": environment,
    }
    owner = subprocess.Popen(
        [executable, "-c", OWNER, str(path), "wait"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **options,
    )
    observed: dict[str, object] = {"stale_pid": stale_pid, "abrupt": abrupt}
    try:
        assert owner.stdout is not None
        ready = json.loads(owner.stdout.readline())
        observed.update(ready)
        assert ready["owner_pid"] == owner.pid
        with path.open("rb", buffering=0) as stream:
            stream.seek(1)
            actual_pid = stream.read().decode("ascii")
        observed["pid_while_owned"] = actual_pid
        assert actual_pid == str(owner.pid)
        second = subprocess.run(
            [executable, "-c", OWNER, str(path), "probe"],
            capture_output=True,
            text=True,
            timeout=5,
            **options,
        )
        observed["second_owner_exit"] = second.returncode
        observed["second_owner_stdout"] = second.stdout
        observed["second_owner_stderr"] = second.stderr
        assert second.returncode != 0 and not second.stdout, observed
        if abrupt:
            owner.kill()
        stdout, stderr = owner.communicate(timeout=5)
        observed["owner_exit"] = owner.returncode
        observed["owner_final_stdout"] = stdout.decode()
        observed["owner_stderr"] = stderr.decode()
        if not abrupt:
            assert owner.returncode == 0, observed
        native_probe(path)
        observed["native_byte_lock_released"] = True
        reopened = subprocess.run(
            [executable, "-c", OWNER, str(path), "probe"],
            capture_output=True,
            text=True,
            timeout=5,
            **options,
        )
        observed["reopened_exit"] = reopened.returncode
        observed["reopened_stdout"] = reopened.stdout
        observed["reopened_stderr"] = reopened.stderr
        assert reopened.returncode == 0, observed
        assert json.loads(reopened.stdout)["owner_pid"] != owner.pid
        assert path.read_bytes() == b"\0"
        assert config_path.read_bytes() == original_config
        journal = Journal(tmp_path / "journal.sqlite3", namespace="original-device")
        try:
            assert journal.operation("original-operation") == original_receipt
        finally:
            journal.close()
        observed["configuration_and_original_unknown_receipt_preserved"] = True
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.communicate(timeout=5)
        (tmp_path / "installation-lock-artifact.json").write_text(
            json.dumps(observed, indent=2), encoding="utf-8"
        )
