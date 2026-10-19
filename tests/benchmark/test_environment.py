import asyncio
import shutil

from benchmarks.environment import BenchmarkEnvironment

from yuki.workspace.store import WorkspaceStore


class LocalTask(BenchmarkEnvironment):
    """A disposable task directory for exercising the real native Supervisor."""

    def __init__(self, path):
        super().__init__()
        self.path = path
        self.root = str(path / "runtime")

    async def run(self, command, *, cwd=None, timeout_seconds=None):
        process = await asyncio.create_subprocess_exec(
            "bash",
            "-lc",
            command,
            cwd=cwd or self.path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        async with asyncio.timeout(timeout_seconds):
            stdout, stderr = await process.communicate()
        return {
            "return_code": process.returncode,
            "stdout": stdout.decode(),
            "stderr": stderr.decode(),
        }

    async def upload(self, source, destination):
        shutil.copyfile(source, destination)

    async def download(self, source, destination):
        shutil.copyfile(source, destination)


async def test_real_execution_is_not_replayed_for_original_request(tmp_path):
    environment = LocalTask(tmp_path)
    await environment.setup()
    try:
        args = {"command": "printf x >> executions; printf DONE"}
        result = await environment.execute("terminal_exec", args, request_id="one")
        replay = await environment.execute("terminal_exec", args, request_id="one")
        assert result["run_id"] == replay["run_id"]
        assert replay["status"] == "succeeded"
        assert (tmp_path / "executions").read_text() == "x"
        assert replay["completion"]["pending"] is False
    finally:
        await environment.cancel_runs()
        await environment.close()


async def test_real_interactive_shell_and_cancel_keep_original_run(tmp_path):
    environment = LocalTask(tmp_path)
    await environment.setup()
    try:
        result = await environment.execute(
            "terminal_exec",
            {"command": "exec bash --noprofile --norc", "tty": True},
            request_id="interactive",
        )
        identity = result["run_id"]
        assert result["pending"]
        await environment.execute(
            "terminal_control",
            {
                "run_id": identity,
                "action": "input",
                "text": "export SAVED=retained\nprintf 'VALUE=%s\\n' \"$SAVED\"\n",
            },
            request_id="input",
        )
        async with asyncio.timeout(10):
            while True:
                output = await environment.execute(
                    "terminal_read", {"run_id": identity}, request_id="read"
                )
                if "VALUE=retained" in output["output"]:
                    break
                await asyncio.sleep(0.05)
        await environment.execute(
            "terminal_control", {"run_id": identity, "action": "cancel"}, request_id="cancel"
        )
        final = await environment.execute(
            "terminal_read", {"run_id": identity, "wait_seconds": 5}, request_id="finish"
        )
        assert final["run_id"] == identity
        assert final["status"] == "cancelled"
        assert final["pending"] is False
    finally:
        await environment.cancel_runs()
        await environment.close()


async def test_native_file_versions_and_immutable_publication(tmp_path):
    environment = LocalTask(tmp_path)
    environment.workspace_store = WorkspaceStore(tmp_path / "artifacts")
    await environment.setup()
    (tmp_path / "result.txt").write_text("original")
    try:
        read = await environment.execute(
            "workspace_media_read", {"path": "result.txt"}, request_id="media"
        )
        publication = await environment.execute(
            "workspace_publish", {"path": "result.txt"}, request_id="publish"
        )
        (tmp_path / "result.txt").write_text("changed")
        stale = await environment.execute(
            "workspace_media_validate",
            {"path": "result.txt", "expected_version": read["version"]},
            request_id="validate",
        )
        assert stale["error"] == "version_conflict"
        _, data = environment.workspace_store.read_bytes(publication["artifact_id"])
        assert data == b"original"
    finally:
        await environment.cancel_runs()
        await environment.close()


async def test_long_stdin_is_delivered_completely(tmp_path):
    environment = LocalTask(tmp_path)
    await environment.setup()
    try:
        result = await environment.execute(
            "terminal_exec",
            {
                "command": 'python3 -c "import sys; from pathlib import Path; '
                "Path('received').write_bytes(sys.stdin.buffer.read(100000))\""
            },
            request_id="long-input",
        )
        await environment.execute(
            "terminal_control",
            {"run_id": result["run_id"], "action": "input", "text": "x" * 100000},
            request_id="write-long-input",
        )
        final = await environment.execute(
            "terminal_read",
            {"run_id": result["run_id"], "wait_seconds": 5},
            request_id="read-long-input",
        )
        assert final["status"] == "succeeded"
        assert (tmp_path / "received").read_bytes() == b"x" * 100000
    finally:
        await environment.cancel_runs()
        await environment.close()


async def test_checkout_conflict_does_not_overwrite_and_close_preserves_services(tmp_path):
    environment = LocalTask(tmp_path)
    environment.workspace_store = WorkspaceStore(tmp_path / "artifacts")
    await environment.setup()
    (tmp_path / "source.txt").write_text("source")
    (tmp_path / "target.txt").write_text("keep")
    try:
        artifact = await environment.execute(
            "workspace_publish",
            {"path": "source.txt"},
            request_id="publication",
        )
        conflict = await environment.execute(
            "workspace_checkout",
            {
                "artifact_id": artifact["artifact_id"],
                "name": "source.txt",
                "destination": "target.txt",
                "expected_version": "stale",
            },
            request_id="conflict",
        )
        assert conflict["error"] == "version_conflict"
        assert (tmp_path / "target.txt").read_text() == "keep"
        service = await environment.execute(
            "terminal_exec",
            {"command": "sleep 300"},
            request_id="live-service",
        )
        await environment.close()
        still_live = await environment.execute(
            "terminal_read",
            {"run_id": service["run_id"]},
            request_id="verify-service",
        )
        assert still_live["pending"]
    finally:
        await environment.cancel_runs()
        await environment.close()
