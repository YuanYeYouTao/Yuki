"""In-process task-container transport; shell execution stays in the native Supervisor."""

from __future__ import annotations

import asyncio
import base64
import json
import shlex
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

ROOT = Path(__file__).resolve().parents[1]
TERMINAL = {"succeeded", "failed", "cancelled"}


class BenchmarkEnvironment:
    def __init__(self) -> None:
        self.root = "/tmp/yuki-benchmark"
        self.cwd = ""
        self.home = ""
        self.python = "python3"
        self.runs: dict[str, str] = {}
        self.workspace_store: Any = None

    async def run(self, command: str, *, cwd=None, timeout_seconds=None) -> dict:
        raise NotImplementedError

    async def upload(self, source: Path, destination: str) -> None:
        raise NotImplementedError

    async def download(self, source: str, destination: Path) -> None:
        raise NotImplementedError

    async def setup(self) -> None:
        probe = await self.run("pwd && printf '%s\\n' \"$HOME\"")
        if probe["return_code"]:
            raise RuntimeError("cannot inspect task working directory")
        self.cwd, self.home = probe["stdout"].strip().splitlines()[-2:]
        result = await self.run(f"mkdir -p {shlex.quote(self.root)}")
        if result["return_code"]:
            raise RuntimeError("cannot prepare task terminal runtime")
        probe = await self.run(
            "for p in python3 /opt/miniconda3/bin/python3 /usr/bin/python3; do "
            "if \"$p\" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; "
            'then command -v "$p"; exit 0; fi; done; exit 1'
        )
        if probe["return_code"] == 0:
            self.python = probe["stdout"].strip().splitlines()[-1]
        else:
            # Old task environments keep their own interpreter; only this stdlib helper uses ours.
            archive = await asyncio.to_thread(self._python_archive)
            await self.upload(archive, self.root + "/python.tar.gz")
            unpacked = await self.run(
                f"tar -xzf {shlex.quote(self.root + '/python.tar.gz')} -C {shlex.quote(self.root)}"
            )
            if unpacked["return_code"]:
                raise RuntimeError("cannot unpack local helper interpreter")
            self.python = self.root + "/python/bin/python3.12"
        await self.upload(
            ROOT / "src/yuki/sandbox/environment_supervisor.py", self.root + "/supervisor.py"
        )
        await self.upload(ROOT / "benchmarks/task_io.py", self.root + "/task_io.py")

    @staticmethod
    def _python_archive() -> Path:
        directory = Path.home() / ".cache/yuki-benchmark"
        directory.mkdir(parents=True, exist_ok=True)
        archive = directory / f"python-{sys.version.split()[0]}-{sys.platform}.tar.gz"
        if not archive.exists():
            pending = archive.with_suffix(f".{uuid4().hex}.pending")
            with tarfile.open(pending, "w:gz") as stream:
                stream.add(sys.base_prefix, arcname="python")
            pending.replace(archive)
        return archive

    async def _rpc(self, action: str, **arguments: Any) -> dict:
        payload = {"root": self.root, "action": action, "cwd": self.cwd, **arguments}
        remote = f"{self.root}/request-{uuid4().hex}.json"
        with tempfile.TemporaryDirectory(prefix="yuki-task-rpc-") as directory:
            path = Path(directory) / "request.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            await self.upload(path, remote)
        result = await self.run(
            f"{shlex.quote(self.python)} -I "
            f"{shlex.quote(self.root + '/task_io.py')} {shlex.quote(remote)}",
            timeout_seconds=arguments.get("wait_seconds", 0) + 30,
        )
        if result["return_code"]:
            raise RuntimeError(f"task helper failed: {result['stderr']}")
        return json.loads(result["stdout"])

    async def execute(self, name: str, args: dict, *, request_id: str, source=None) -> dict:
        if name == "terminal_exec":
            identity = str(uuid5(NAMESPACE_URL, request_id))
            self.runs[request_id] = identity
            result = await self._rpc(
                "start",
                run_id=identity,
                spec={
                    "command": args["command"],
                    "cwd": args.get("cwd") or self.cwd,
                    "tty": args.get("tty", False),
                    "timeout_seconds": args.get("timeout_seconds", 0),
                    "capture": False,
                },
            )
            # Match the native short observation; a pending result keeps its original run_id.
            until = time.monotonic() + 4.5
            while result.get("pending") and time.monotonic() < until:
                await asyncio.sleep(0.1)
                result = await self._rpc("read", run_id=identity)
            return self._completion(result)
        if name == "get_code_run_by_request":
            return self._completion(
                await self._rpc("read", run_id=str(uuid5(NAMESPACE_URL, args["request_id"])))
            )
        if name in {"terminal_read", "terminal_control"}:
            request = {"run_id": args["run_id"], "cursor": args.get("cursor", 0)}
            if name == "terminal_control":
                result = await self._rpc(
                    "control", **request, control=args["action"], text=args.get("text", "")
                )
            else:
                result = await self._rpc(
                    "read", **request, wait_seconds=args.get("wait_seconds", 0)
                )
            return self._completion(result)
        if name == "environment_status":
            return {
                "status": "ready",
                "workspace": self.cwd,
                "home": self.home,
                "runs": list(self.runs.values()),
            }
        if name in {"workspace_media_read", "workspace_media_validate"}:
            return await self._rpc("media_read" if name.endswith("read") else "file_info", **args)
        if name == "workspace_publish":
            metadata = await self._rpc("file_info", **args)
            if metadata.get("error"):
                return metadata
            if self.workspace_store is None:
                raise RuntimeError("benchmark artifact store is not bound")
            with tempfile.TemporaryDirectory(prefix="yuki-publish-") as directory:
                path = Path(directory) / "artifact"
                await self.download(metadata["path"], path)
                with path.open("rb") as stream:
                    artifact = self.workspace_store.snapshot(
                        stream.fileno(),
                        args.get("name") or Path(args["path"]).name,
                        artifact_id=args.get("artifact_id"),
                        source_key=args.get("source_key"),
                        expected_version=metadata["version"],
                    )
            return {**artifact, "path": metadata["path"], "version": metadata["version"]}
        if name == "workspace_checkout":
            metadata, content = self.workspace_store.read_bytes(args["artifact_id"])
            result = await self._rpc(
                "write_file",
                path=args.get("destination") or args["name"],
                base64=base64.b64encode(content).decode(),
                expected_version=args.get("expected_version"),
            )
            return {**result, "artifact_id": metadata["artifact_id"]}
        if name == "external_agent" and args.get("action") == "list":
            return {"agents": []}
        return {"error": "capability_unavailable", "tool": name}

    @staticmethod
    def _completion(result: dict) -> dict:
        if result.get("status") in TERMINAL:
            return {**result, "pending": False, "completion": {**result, "pending": False}}
        return result

    async def cancel_runs(self) -> None:
        for identity in self.runs.values():
            result = await self._rpc("read", run_id=identity)
            if result.get("pending"):
                await self._rpc("control", run_id=identity, control="cancel")
                await self._rpc("read", run_id=identity, wait_seconds=5)

    async def close(self) -> None:
        # The official verifier may still need live services; the harness owns the container.
        pass


class HarborEnvironment(BenchmarkEnvironment):
    def __init__(self, environment: Any) -> None:
        super().__init__()
        self.environment = environment

    async def run(self, command: str, *, cwd=None, timeout_seconds=None) -> dict:
        result = await self.environment.exec(command, cwd=cwd, timeout_sec=timeout_seconds)
        return {
            "return_code": result.return_code,
            "stdout": result.stdout or "",
            "stderr": result.stderr or "",
        }

    async def upload(self, source: Path, destination: str) -> None:
        await self.environment.upload_file(source, destination)

    async def download(self, source: str, destination: Path) -> None:
        await self.environment.download_file(source, destination)


class ContainerEnvironment(BenchmarkEnvironment):
    def __init__(self, container_id: str) -> None:
        super().__init__()
        self.container_id = container_id

    async def _docker(self, *arguments: str, timeout_seconds=None) -> dict:
        process = await asyncio.create_subprocess_exec(
            "docker",
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with asyncio.timeout(timeout_seconds):
                stdout, stderr = await process.communicate()
        except BaseException:
            process.kill()
            await process.wait()
            raise
        return {
            "return_code": process.returncode,
            "stdout": stdout.decode(errors="replace"),
            "stderr": stderr.decode(errors="replace"),
        }

    async def run(self, command: str, *, cwd=None, timeout_seconds=None) -> dict:
        options = ["exec"]
        if cwd:
            options.extend(["-w", cwd])
        return await self._docker(
            *options, self.container_id, "bash", "-lc", command, timeout_seconds=timeout_seconds
        )

    async def upload(self, source: Path, destination: str) -> None:
        result = await self._docker("cp", str(source), f"{self.container_id}:{destination}")
        if result["return_code"]:
            raise RuntimeError(result["stderr"])

    async def download(self, source: str, destination: Path) -> None:
        result = await self._docker("cp", f"{self.container_id}:{source}", str(destination))
        if result["return_code"]:
            raise RuntimeError(result["stderr"])
