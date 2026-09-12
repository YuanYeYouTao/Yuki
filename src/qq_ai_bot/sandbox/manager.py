"""Trusted Linux host daemon. Not mounted into, or executed by, the Bot container.

Only this process controls Docker. All paths, images and resource options are
operator configuration; untrusted requests contain code and artifact IDs only.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import shutil
import signal
import sqlite3
import stat
import time
from collections import Counter
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from qq_ai_bot.sandbox.completions import CompletionOutbox
from qq_ai_bot.workspace.store import WorkspaceError, WorkspaceStore

LABEL = "io.yuki.sandbox=python-v1"
TERMINAL = {"succeeded", "failed", "cancelled"}


def identifier(raw: object) -> str:
    result = str(UUID(str(raw)))
    if result != raw:
        raise ValueError("invalid_identifier")
    return result


class Manager:
    def __init__(
        self, root: Path, store: WorkspaceStore, image: str, network: str, proxy: str
    ) -> None:
        self.root, self.store, self.image = root.resolve(), store, image
        self.network, self.proxy = network, proxy
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=4)
        self.current: str | None = None
        self.cancelled: set[str] = set()
        self._waiters: dict[str, set[asyncio.Event]] = {}
        self.ready = False
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "jobs.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, "
            "request_id TEXT UNIQUE NOT NULL, payload_hash TEXT NOT NULL, "
            "payload TEXT NOT NULL, state TEXT NOT NULL, result TEXT NOT NULL, "
            "created REAL NOT NULL)"
        )
        self.completions = CompletionOutbox(self.db)
        self.db.commit()

    async def command(self, *args: str, deadline_seconds: float = 30) -> tuple[int, bytes]:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        kept = bytearray()
        assert proc.stdout is not None
        try:
            async with asyncio.timeout(deadline_seconds):
                while chunk := await proc.stdout.read(8192):
                    if len(kept) < 32769:
                        kept.extend(chunk[: 32769 - len(kept)])
                code = await proc.wait()
        except BaseException:
            proc.kill()
            await proc.wait()
            raise
        return code, bytes(kept)

    def finish(self, identity: str, state: str, result: dict[str, Any]) -> bool:
        if state not in {*TERMINAL, "running"}:
            raise ValueError("invalid_job_state")
        with self.db:
            changed = self.db.execute(
                "UPDATE jobs SET state=?, result=? WHERE id=? "
                "AND state IN ('queued','running') RETURNING request_id",
                (state, json.dumps(result), identity),
            ).fetchone()
            if changed is None:
                return False
            if state in TERMINAL:
                self.completions.record(
                    identity,
                    changed[0],
                    {
                        **result,
                        "run_id": identity,
                        "status": state,
                        "pending": False,
                        "external_untrusted": True,
                    },
                )
        for waiter in self._waiters.get(identity, ()):
            waiter.set()
        return True

    async def wait_result(self, identity: str, *, wait_seconds: float = 4.5) -> dict[str, Any]:
        """Wait within the socket deadline; observation cancellation leaves the job alive."""
        result = self.get(identity)
        if result.get("error") or result.get("status") in TERMINAL:
            return result
        changed = asyncio.Event()
        waiters = self._waiters.setdefault(identity, set())
        waiters.add(changed)
        try:
            async with asyncio.timeout(wait_seconds):
                while True:
                    changed.clear()
                    result = self.get(identity)
                    if result.get("error") or result.get("status") in TERMINAL:
                        return result
                    await changed.wait()
        except TimeoutError:
            return self.get(identity)
        finally:
            waiters.discard(changed)
            if not waiters:
                self._waiters.pop(identity, None)

    def get(self, identity: str) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM jobs WHERE id=?", (identifier(identity),)).fetchone()
        if row is None or time.time() - row["created"] >= 86400:
            return {"error": "run_not_found"}
        return {
            "run_id": identity,
            "status": row["state"],
            **json.loads(row["result"]),
            "external_untrusted": True,
            "pending": row["state"] in {"queued", "running"},
        }

    async def recover(self) -> None:
        # Select only our labelled containers; never enumerate or remove unrelated services.
        code, containers = await self.command("docker", "ps", "-aq", "--filter", f"label={LABEL}")
        if code:
            return
        for token in containers.decode().split():
            if len(token) == 12 and all(c in "0123456789abcdef" for c in token):
                await self.command("docker", "rm", "-f", token)
        for row in self.db.execute(
            "SELECT id FROM jobs WHERE state IN ('queued','running')"
        ).fetchall():
            await self.cleanup(row["id"])
            self.finish(row["id"], "failed", {"error": "manager_restarted"})
        code, info = await self.command("docker", "info", "--format", "{{json .Runtimes}}")
        if code or "runsc" not in json.loads(info):
            return
        code, network = await self.command("docker", "network", "inspect", self.network)
        if code or not json.loads(network)[0].get("Internal"):
            return
        code, _ = await self.command("docker", "image", "inspect", self.image)
        if code:
            return
        for chain, interface in (("INPUT", "yuki-sandbox0"), ("INPUT", "yuki-egress0")):
            code, _ = await self.command("iptables", "-C", chain, "-i", interface, "-j", "DROP")
            if code:
                return
        code, _ = await self.command(
            "iptables", "-C", "DOCKER-USER", "-i", "yuki-egress0", "-j", "YUKI-EGRESS"
        )
        self.ready = code == 0 and bool(self.proxy)

    async def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        method, args = request.get("method"), request.get("args")
        if not isinstance(args, dict):
            return {"error": "invalid_arguments"}
        if method == "list_code_completions":
            return self.completions.pending(args.get("limit", 20), args.get("after", 0))
        if method == "ack_code_completion":
            return self.completions.acknowledge(identifier(args.get("run_id")))
        if method == "get_code_run_by_request":
            original_id = args.get("request_id")
            if not isinstance(original_id, str) or not 1 <= len(original_id) <= 256:
                return {"error": "invalid_request_id"}
            prior = self.db.execute(
                "SELECT id FROM jobs WHERE request_id=?", (original_id,)
            ).fetchone()
            return self.get(prior["id"]) if prior else {"error": "unknown_request"}
        if method in {"get_code_run", "cancel_code_run"}:
            identity = identifier(args.get("run_id"))
            if method == "get_code_run":
                return await self.wait_result(identity)
            result = self.get(identity)
            if (
                method == "cancel_code_run"
                and result.get("status") not in TERMINAL
                and not result.get("error")
            ):
                self.cancelled.add(identity)
                if self.current == identity:
                    await self.command("docker", "rm", "-f", f"yuki-python-{identity}")
                self.finish(identity, "cancelled", {})
                result = self.get(identity)
            return result
        if method != "run_python":
            return {"error": "unknown_method"}
        if not self.ready:
            return {"error": "sandbox_unavailable", "retryable": False}
        code = args.get("code")
        inputs = args.get("input_artifact_ids", [])
        timeout = args.get("timeout_seconds", 30)
        if (
            not isinstance(code, str)
            or len(code.encode()) > 65536
            or not isinstance(inputs, list)
            or len(inputs) > 20
            or type(timeout) is not int
            or not 1 <= timeout <= 120
        ):
            return {"error": "invalid_arguments"}
        inputs = [identifier(item) for item in inputs]
        payload = json.dumps(
            {"code": code, "input_artifact_ids": inputs, "timeout_seconds": timeout}, sort_keys=True
        )
        digest = hashlib.sha256(payload.encode()).hexdigest()
        request_id = request.get("request_id")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 256:
            return {"error": "invalid_request_id"}
        prior = self.db.execute(
            "SELECT id, payload_hash FROM jobs WHERE request_id=?", (request_id,)
        ).fetchone()
        if prior:
            return (
                self.get(prior["id"])
                if prior["payload_hash"] == digest
                else {"error": "idempotency_conflict"}
            )
        if self.queue.full():
            return {"error": "sandbox_queue_full", "retryable": False}
        if not self.completions.reserve_available():
            return {"error": "completion_backlog_full", "retryable": False}
        identity = str(uuid4())
        self.db.execute(
            "INSERT INTO jobs VALUES (?,?,?,?,?,?,?)",
            (identity, request_id, digest, payload, "queued", "{}", time.time()),
        )
        self.db.commit()
        self.queue.put_nowait(identity)
        return await self.wait_result(identity)

    def docker_args(self, identity: str) -> tuple[str, ...]:
        directory = self.root / identifier(identity)
        return (
            "docker",
            "run",
            "--name",
            f"yuki-python-{identity}",
            "--label",
            LABEL,
            "--runtime",
            "runsc",
            "--network",
            self.network,
            "--user",
            "65532:65532",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--memory",
            "256m",
            "--memory-swap",
            "256m",
            "--cpus",
            "0.5",
            "--pids-limit",
            "64",
            "--log-driver",
            "none",
            "--init",
            "--workdir",
            "/work",
            "--mount",
            f"type=bind,src={directory / 'inputs'},dst=/inputs,readonly",
            "--mount",
            f"type=bind,src={directory / 'workspace'},dst=/workspace,readonly",
            "--mount",
            f"type=bind,src={directory / 'work'},dst=/work",
            "--env",
            f"HTTPS_PROXY={self.proxy}",
            "--env",
            f"HTTP_PROXY={self.proxy}",
            "--env",
            f"https_proxy={self.proxy}",
            "--env",
            f"http_proxy={self.proxy}",
            "--env",
            "HOME=/work",
            "--env",
            "TMPDIR=/work",
            "--env",
            "PYTHONUSERBASE=/work/.local",
            "--env",
            "PIP_TARGET=/work/packages",
            "--env",
            "PYTHONPATH=/work/packages",
            self.image,
            "python",
            "/inputs/code.py",
        )

    def stage_workspace(self, destination: Path) -> None:
        """Expose live artifacts with usable names, never the private index/blobs."""
        destination.mkdir(mode=0o755)
        destination.chmod(0o755)
        by_id = destination / "by-id"
        by_id.mkdir(mode=0o755)
        by_id.chmod(0o755)
        records: list[dict[str, Any]] = []
        cursor = ""
        total = 0
        while True:
            page = self.store.list(cursor=cursor, limit=100)
            for item in page["items"]:
                try:
                    metadata, data = self.store.read_bytes(item["artifact_id"])
                except WorkspaceError as exc:
                    if str(exc) in {"artifact_not_found", "artifact_expired"}:
                        continue
                    raise
                total += len(data)
                if total > self.store.capacity or len(records) >= self.store.max_objects:
                    raise ValueError("workspace_snapshot_limit")
                artifact_id = identifier(metadata["artifact_id"])
                path = by_id / artifact_id
                path.write_bytes(data)
                path.chmod(0o444)
                records.append({**metadata, "path": f"/workspace/by-id/{artifact_id}"})
            cursor = page["next_cursor"]
            if not cursor:
                break
        counts = Counter(item["name"] for item in records)
        for item in records:
            name = item["name"]
            if (
                counts[name] == 1
                and name not in {"by-id", "manifest.json", ".", ".."}
                and Path(name).name == name
                and not any(c in name for c in "\\/:\x00")
            ):
                (destination / name).hardlink_to(by_id / item["artifact_id"])
                item["named_path"] = f"/workspace/{name}"
        manifest = destination / "manifest.json"
        manifest.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
        manifest.chmod(0o444)

    async def execute(self, identity: str) -> None:
        row = self.db.execute("SELECT payload FROM jobs WHERE id=?", (identity,)).fetchone()
        args = json.loads(row["payload"])
        directory = self.root / identifier(identity)
        directory.mkdir(mode=0o700)
        inputs, work = directory / "inputs", directory / "work"
        inputs.mkdir(mode=0o755)
        await asyncio.to_thread(self.stage_workspace, directory / "workspace")
        # systemd's private umask must not remove traversal for the sandbox UID.
        inputs.chmod(0o755)
        work.mkdir()
        mounted, _ = await self.command(
            "mount",
            "-t",
            "tmpfs",
            "-o",
            "size=128m,nodev,nosuid,mode=1777",
            "yuki-python",
            str(work),
        )
        if mounted:
            raise RuntimeError("scratch_unavailable")
        (work / "outputs").mkdir(mode=0o777)
        (work / "outputs").chmod(0o777)
        (inputs / "code.py").write_text(args["code"], encoding="utf-8")
        total = 0
        for artifact_id in args["input_artifact_ids"]:
            _metadata, content = await asyncio.to_thread(self.store.read_bytes, artifact_id)
            total += len(content)
            if total > 100 * 1024 * 1024:
                raise ValueError("input_limit")
            (inputs / artifact_id).write_bytes(content)
        for item in inputs.iterdir():
            item.chmod(0o444)
        if identity in self.cancelled:
            return
        self.finish(identity, "running", {})
        exit_code, output = await self.command(
            *self.docker_args(identity), deadline_seconds=args["timeout_seconds"]
        )
        # Stop any surviving descendant before touching output files.
        await self.command("docker", "rm", "-f", f"yuki-python-{identity}")
        if identity in self.cancelled:
            return
        result: dict[str, Any] = {
            "exit_code": exit_code,
            "output": output[:32768].decode(errors="replace"),
            "truncated": len(output) > 32768,
        }
        if exit_code:
            self.finish(identity, "failed", result)
            return
        output_root = work / "outputs"
        if output_root.is_symlink() or not stat.S_ISDIR(output_root.lstat().st_mode):
            raise ValueError("unsafe_output")
        paths = list(output_root.iterdir())
        if len(paths) > 20:
            raise ValueError("output_limit")
        total = 0
        for path in paths:
            info = path.lstat()
            total += info.st_size
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or total > 100 * 1024 * 1024:
                raise ValueError("unsafe_output")
        files = [(path.name, await asyncio.to_thread(path.read_bytes)) for path in paths]
        if identity in self.cancelled:
            return
        # Publish and commit the final status without an intervening cancellation point.
        result["artifacts"] = self.store.publish_batch(files)
        self.finish(identity, "succeeded", result)

    async def cleanup(self, identity: str) -> None:
        directory = self.root / identifier(identity)
        if directory.parent != self.root or directory.is_symlink():
            raise ValueError("unsafe_job_directory")
        await self.command("docker", "rm", "-f", f"yuki-python-{identity}")
        if directory.exists():
            if await asyncio.to_thread(os.path.ismount, directory / "work"):
                status, _ = await self.command("umount", str(directory / "work"))
                if status:
                    raise RuntimeError("scratch_cleanup_failed")
            shutil.rmtree(directory)

    async def worker(self) -> None:
        while True:
            self.db.execute(
                "DELETE FROM jobs WHERE created<? AND state IN ('succeeded','failed','cancelled')",
                (time.time() - 86400,),
            )
            self.db.commit()
            try:
                identity = await asyncio.wait_for(self.queue.get(), timeout=60)
            except TimeoutError:
                self.db.execute(
                    "DELETE FROM jobs WHERE created<? "
                    "AND state IN ('succeeded','failed','cancelled')",
                    (time.time() - 86400,),
                )
                self.db.commit()
                continue
            self.current = identity
            try:
                if identity not in self.cancelled:
                    await self.execute(identity)
            except Exception as exc:
                self.finish(
                    identity,
                    "cancelled" if identity in self.cancelled else "failed",
                    {"error": type(exc).__name__},
                )
            finally:
                try:
                    await self.cleanup(identity)
                except Exception:
                    self.ready = False
                    logging.getLogger(__name__).exception(
                        "sandbox_cleanup_failed run_id=%s", identity
                    )
                    self.finish(identity, "failed", {"error": "cleanup_failed"})
                self.cancelled.discard(identity)
                self.current = None
                self.queue.task_done()

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            async with asyncio.timeout(8):
                raw = await reader.readline()
                request = json.loads(raw)
                if not isinstance(request, dict):
                    raise ValueError("invalid_request")
                result = await self.handle(request)
                writer.write(json.dumps(result).encode() + b"\n")
                await writer.drain()
        except (ValueError, OSError, TimeoutError):
            writer.write(b'{"error":"invalid_request"}\n')
        finally:
            writer.close()
            await writer.wait_closed()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--persistent-home", type=Path)
    parser.add_argument("--migrate-only", action="store_true")
    parser.add_argument("--network", default="yuki-sandbox-internal")
    parser.add_argument("--proxy", default="http://172.30.251.2:3128")
    args = parser.parse_args()
    from qq_ai_bot.sandbox.persistent import PersistentManager

    manager: Manager
    if args.persistent_home:
        manager = PersistentManager(
            args.root,
            WorkspaceStore(args.workspace),
            args.image,
            args.network,
            args.proxy,
            args.persistent_home,
        )
    else:
        manager = Manager(
            args.root, WorkspaceStore(args.workspace), args.image, args.network, args.proxy
        )
    if args.migrate_only:
        if not isinstance(manager, PersistentManager):
            raise ValueError("persistent_home_required")
        print(json.dumps(manager.migrate_files()))
        return
    await manager.recover()
    args.socket.parent.mkdir(parents=True, exist_ok=True)
    if args.socket.exists():
        if not stat.S_ISSOCK(args.socket.lstat().st_mode):
            raise ValueError("unsafe_socket")
        args.socket.unlink()
    serve_unix = getattr(asyncio, "start_unix_server", None)
    if serve_unix is None:
        raise RuntimeError("unix_socket_required")
    server = await serve_unix(manager.serve, str(args.socket), limit=262144)
    args.socket.chmod(0o660)
    worker = asyncio.create_task(manager.worker())
    task = asyncio.current_task()
    assert task is not None
    worker.add_done_callback(lambda completed: task.cancel() if not completed.cancelled() else None)
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        async with server:
            await server.serve_forever()
    except asyncio.CancelledError:
        pass
    finally:
        failure = worker.exception() if worker.done() and not worker.cancelled() else None
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        if isinstance(manager, PersistentManager):
            await manager.close()
        elif manager.current:
            await manager.cleanup(manager.current)
        if failure is not None:
            raise RuntimeError("sandbox_worker_failed") from failure


if __name__ == "__main__":
    asyncio.run(main())
