"""Trusted lifecycle owner for one persistent gVisor environment.

The Bot has a narrow JSON socket, never Docker access. Job launch intent and
execd session identity are durable before launch. Reconnection observes existing
supervisor records; it never replays a dispatched command.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import math
import os
import re
import secrets
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from yuki.sandbox.completions import CompletionOutbox
from yuki.sandbox.environment_tools import EXECUTION_TOOLS
from yuki.sandbox.execd import Execd
from yuki.sandbox.manager import TERMINAL, identifier
from yuki.workspace.files import FileWorkspace
from yuki.workspace.store import WorkspaceError, WorkspaceStore

LABEL = "io.yuki.sandbox=persistent-v1"
CONTAINER = "yuki-environment"
SEGMENT = 4 * 1024 * 1024
LOG_BUDGET = 128 * 1024 * 1024
LOGGER = logging.getLogger(__name__)


class PersistentManager:
    def __init__(
        self,
        root: Path,
        store: WorkspaceStore,
        image: str,
        network: str,
        proxy: str,
        home: Path,
        *,
        runtime: str = "runsc",
        resolv_conf: Path | None = None,
        testing: bool = False,
        name: str = "yuki-environment",
    ) -> None:
        self.root, self.store, self.image = root.resolve(), store, image
        self.network, self.proxy = network, proxy
        self.resolv_conf = resolv_conf.resolve() if resolv_conf is not None else None
        self.queue: asyncio.Queue[str] = asyncio.Queue()
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
        self.home = home.resolve()
        self.runtime, self.testing = runtime, testing
        if not re.fullmatch(r"yuki-environment(?:-[a-z0-9-]+)?", name):
            raise ValueError("invalid_environment_name")
        self.name = name
        self.files = FileWorkspace(self.home / "workspace")
        self.runtime_root = self.root / "environment-jobs"
        self.runtime_root.mkdir(mode=0o755, exist_ok=True)
        self.runtime_root.chmod(0o755)
        self.db.executescript(
            "CREATE TABLE IF NOT EXISTS environment_jobs (id TEXT PRIMARY KEY, kind TEXT NOT NULL, "
            "session_id TEXT, container_id TEXT, dispatched INTEGER NOT NULL DEFAULT 0);"
            "CREATE TABLE IF NOT EXISTS environment_state"
            " (key TEXT PRIMARY KEY,value TEXT NOT NULL);"
            "CREATE TABLE IF NOT EXISTS environment_servic"
            "es (name TEXT PRIMARY KEY, spec TEXT NOT NULL,"
            "enabled INTEGER NOT NULL DEFAULT 0, run_id TEXT, attempts INTEGER NOT NULL DEFAULT 0,"
            "next_start REAL NOT NULL DEFAULT 0);"
            "CREATE TABLE IF NOT EXISTS workspace_migrations (artifact_id TEXT PRIMARY KEY,"
            "path TEXT NOT NULL, sha256 TEXT NOT NULL);"
            "CREATE TABLE IF NOT EXISTS environment_mutations (request_id TEXT PRIMARY KEY,"
            "digest TEXT NOT NULL, result TEXT NOT NULL);"
        )
        self.db.commit()
        self.execd: Execd | None = None
        self.container_id = ""
        self.connections: dict[str, Any] = {}
        self.pumps: dict[str, asyncio.Task[None]] = {}
        self.file_lock = asyncio.Lock()
        self.last_prune = 0.0
        self.last_layer_check = 0.0

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

    def _finish_receipt(self, identity: str, state: str, result: dict[str, Any]) -> bool:
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

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            async with asyncio.timeout(30):
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

    def setting(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM environment_state WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO environment_state VALUES (?,?)", (key, value))

    def resource_limits(self) -> dict[str, int | float]:
        limits = {
            "memory_mib": int(self.setting("resource_memory_mib", "512")),
            "cpus": float(self.setting("resource_cpus", "1")),
            "cpu_shares": int(self.setting("resource_cpu_shares", "1024")),
        }
        return limits

    @staticmethod
    def available_memory() -> int:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
        return 0

    def state_path(self, identity: str) -> Path:
        return self.runtime_root / identifier(identity) / "state"

    def read_state_file(
        self, identity: str, name: str, *, offset: int = 0, limit: int = 32768
    ) -> bytes:
        # User-controlled runtime content is always opened relative to checked FDs.
        root = FileWorkspace(self.runtime_root)
        with root.open_file(f"{identifier(identity)}/state/{name}") as fd:
            os.lseek(fd, offset, os.SEEK_SET)
            return os.read(fd, limit)

    def status_record(self, identity: str) -> dict[str, Any]:
        try:
            value = json.loads(self.read_state_file(identity, "status.json", limit=4096))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, WorkspaceError):
            return {}

    def output(self, identity: str, cursor: int = 0) -> dict[str, Any]:
        if type(cursor) is not int or cursor < 0:
            raise ValueError("invalid_cursor")
        record = self.status_record(identity)
        end = record.get("output_offset", 0)
        if type(end) is not int or end < 0 or end > 2**60:
            end = 0
        first = max(0, (max(0, end - 1) // SEGMENT - 1) * SEGMENT)
        position = min(end, max(cursor, first))
        start = position
        data = bytearray()
        missing = cursor < first
        while position < end and len(data) < 32768:
            segment, offset = divmod(position, SEGMENT)
            try:
                part = self.read_state_file(
                    identity,
                    f"output-{segment}.log",
                    offset=offset,
                    limit=min(32768 - len(data), end - position),
                )
            except (OSError, WorkspaceError):
                missing = True
                position = min(end, (segment + 1) * SEGMENT)
                continue
            if not part:
                break
            data.extend(part)
            position += len(part)
        return {
            "output": bytes(data).decode(errors="replace"),
            "cursor": start,
            "next_cursor": position,
            "output_offset": end,
            "output_lost": missing,
            "truncated": position < end,
        }

    def get(self, identity: str) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM jobs WHERE id=?", (identifier(identity),)).fetchone()
        if row is None:
            return {"error": "run_not_found"}
        result = {
            "run_id": identity,
            **json.loads(row["result"]),
            "status": row["state"],
            "pending": row["state"] in {"queued", "running"},
            "external_untrusted": True,
        }
        if (
            row["state"] not in TERMINAL
            and self.db.execute("SELECT 1 FROM environment_jobs WHERE id=?", (identity,)).fetchone()
        ):
            result.update(self.output(identity))
        return result

    def finish(self, identity: str, state: str, result: dict[str, Any]) -> bool:
        meta = self.db.execute(
            "SELECT kind FROM environment_jobs WHERE id=?", (identity,)
        ).fetchone()
        if meta and meta[0] == "service":
            with self.db:
                changed = self.db.execute(
                    "UPDATE jobs SET state=?,result=? WHERE id=? AND state IN ('queued','running')",
                    (state, json.dumps(result), identity),
                ).rowcount
            return bool(changed)
        changed = self._finish_receipt(identity, state, result)
        if changed and state in TERMINAL:
            with self.db:
                # Hash/source receipts survive; finished executable payloads need
                # not grow the Manager database for the lifetime of the home.
                self.db.execute("UPDATE jobs SET payload='{}' WHERE id=?", (identity,))
        return changed

    async def inspect_container(self, *, size: bool = False) -> dict[str, Any] | None:
        code, raw = await self.command(
            "docker", "inspect", *(("--size",) if size else ()), self.name
        )
        if code:
            return None
        value = json.loads(raw)[0]
        if value.get("Config", {}).get("Labels", {}).get("io.yuki.sandbox") != "persistent-v1":
            raise RuntimeError("environment_name_in_use")
        return cast(dict[str, Any], value)

    async def recover(self) -> None:
        self.ready = False
        if not self.testing and not await asyncio.to_thread(os.path.ismount, self.home):
            raise RuntimeError("persistent_home_not_mounted")
        if not self.testing and not await asyncio.to_thread(os.path.ismount, self.runtime_root):
            raise RuntimeError("bounded_runtime_not_mounted")
        for relative in ("", "workspace", ".local", ".cache", "inputs", "work", "work/outputs"):
            directory = self.home / relative
            if directory.is_symlink():
                raise RuntimeError("unsafe_environment_directory")
            directory.mkdir(mode=0o755, parents=True, exist_ok=True)
            if os.geteuid() == 0:
                os.chown(directory, 10001, 10001)
        await asyncio.to_thread(self.store.cleanup)
        if not self.testing:
            code, runtimes = await self.command("docker", "info", "--format", "{{json .Runtimes}}")
            if code or self.runtime not in json.loads(runtimes):
                raise RuntimeError("gvisor_unavailable")
            code, _ = await self.command("docker", "network", "inspect", self.network)
            if code:
                raise RuntimeError("environment_network_missing")
            for interface in ("yuki-sandbox0", "yuki-egress0"):
                code, _ = await self.command(
                    "iptables", "-C", "INPUT", "-i", interface, "-j", "DROP"
                )
                if code:
                    raise RuntimeError("network_guard_missing")
        token = self.setting("execd_token")
        if not token:
            token = secrets.token_urlsafe(32)
            self.set_setting("execd_token", token)
        current = await self.inspect_container()
        if current is None:
            image = self.setting("checkpoint_image", self.image)
            limits = self.resource_limits()
            args = [
                "docker",
                "create",
                "--name",
                self.name,
                "--label",
                LABEL,
                "--runtime",
                self.runtime,
                "--network",
                self.network,
                "--user",
                "0:0",
                "--memory",
                f"{limits['memory_mib']}m",
                "--memory-swap",
                f"{limits['memory_mib'] + 128}m",
                "--cpus",
                str(limits["cpus"]),
                "--cpu-shares",
                str(limits["cpu_shares"]),
                "--pids-limit",
                "128",
                "--restart",
                "no",
                "--security-opt",
                "no-new-privileges",
                "--init",
                "--log-driver",
                "local",
                "--log-opt",
                "max-size=2m",
                "--log-opt",
                "max-file=2",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,size=128m,mode=1777",
                "--mount",
                f"type=bind,src={self.home},dst=/home/yuki",
                "--mount",
                f"type=bind,src={self.runtime_root},dst=/var/lib/yuki-runtime",
                "--mount",
                f"type=bind,src={Path(__file__).with_name('environment_supervisor.py').resolve()},"
                "dst=/opt/yuki-runtime/supervisor.py,readonly",
                "--env",
                f"EXECD_ACCESS_TOKEN={token}",
            ]
            if self.resolv_conf is not None:
                args.extend(("--mount", f"type=bind,src={self.resolv_conf},dst=/etc/resolv.conf"))
            # Rootfs snapshots retain their old container configuration. Override
            # its former user/proxy defaults without discarding installed files.
            for value in ("HOME=/home/yuki", "USER=root", "LOGNAME=root"):
                args.extend(("--env", value))
            for variable in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                args.extend(("--env", f"{variable}={self.proxy}"))
            args.append(image)
            code, _ = await self.command(*args)
            if code:
                raise RuntimeError("environment_create_failed")
            current = await self.inspect_container()
        assert current is not None
        # Docker must not auto-start this bind-mounted container before the home
        # filesystem is mounted at boot. The ordered Manager owns recovery.
        if current.get("HostConfig", {}).get("RestartPolicy", {}).get("Name") != "no":
            await self.command("docker", "update", "--restart=no", self.name)
        if not current["State"]["Running"]:
            code, _ = await self.command("docker", "start", self.name)
            if code:
                raise RuntimeError("environment_start_failed")
            current = await self.inspect_container()
            assert current is not None
        self.container_id = current["Id"] + ":" + current["State"]["StartedAt"]
        address = current["NetworkSettings"]["Networks"][self.network]["IPAddress"]
        self.execd = Execd(address, token)
        for attempt in range(20):
            try:
                await self.execd.request("GET", "/ping")
                break
            except (OSError, ValueError):
                if attempt == 19:
                    raise RuntimeError("execd_unavailable") from None
                await asyncio.sleep(0.5)
        # Legacy queued jobs cannot safely be replayed into a different environment.
        for row in self.db.execute(
            "SELECT id FROM jobs WHERE state IN ('queued','running') "
            "AND id NOT IN (SELECT id FROM environment_jobs)"
        ).fetchall():
            self.finish(row[0], "failed", {"error": "environment_migrated", "interrupted": True})
        for row in self.active():
            if row["kind"] not in EXECUTION_TOOLS | {"service"}:
                # Retired jobs never gain another dispatch from an upgrade.
                # A native start/terminal receipt still belongs to the old run.
                try:
                    self.read_state_file(row["id"], "started", limit=1)
                    started = True
                except FileNotFoundError:
                    started = False
                except WorkspaceError:
                    started = True  # No proof of non-execution; preserve the original unknown.
                if not row["dispatched"] and not started and not self.status_record(row["id"]):
                    self.finish(row["id"], "cancelled", {"error": "retired_execution_not_started"})
                else:
                    await self.reconcile(row)
            elif not row["dispatched"]:
                self.queue.put_nowait(row["id"])
            else:
                await self.reconcile(row)
        self.ready = True

    def active(self) -> list[Any]:
        return self.db.execute(
            "SELECT jobs.*,environment_jobs.kind,session_id,container_id,dispatched "
            "FROM jobs JOIN environment_jobs USING(id) "
            "WHERE jobs.state IN ('queued','running')"
        ).fetchall()

    async def attach(self, identity: str, session: str, *, tty: bool) -> None:
        assert self.execd is not None
        connection = await self.execd.connect(session, tty=tty)
        self.connections[identity] = connection

        async def drain() -> None:
            try:
                async for _message in connection:
                    pass  # Durable output lives in the supervisor's bounded segments.
            except Exception:
                pass
            finally:
                if self.connections.get(identity) is connection:
                    self.connections.pop(identity, None)
                await connection.close()

        self.pumps[identity] = asyncio.create_task(drain(), name=f"execd-{identity}")

    async def reconcile(self, row: Any) -> None:
        identity = row["id"]
        record = self.status_record(identity)
        if record.get("status") in TERMINAL:
            result = {
                key: record[key] for key in ("exit_code", "error", "finished_at") if key in record
            }
            result.update(self.output(identity))
            self.finish(identity, record["status"], result)
            connection = self.connections.pop(identity, None)
            if connection:
                await connection.close()
            if row["session_id"] and self.execd:
                try:
                    await self.execd.request("DELETE", f"/pty/{row['session_id']}")
                except (OSError, ValueError):
                    pass
            return
        if row["container_id"] != self.container_id:
            self.finish(identity, "failed", {"error": "environment_recreated", "interrupted": True})
            return
        if not row["session_id"]:
            return
        if identity in self.connections:
            return
        await self.attach(
            identity, row["session_id"], tty=bool(json.loads(row["payload"]).get("tty"))
        )

    def prepare_spec(self, identity: str, args: dict[str, Any], kind: str) -> dict[str, Any]:
        root = self.runtime_root / identifier(identity)
        root.mkdir(mode=0o755)
        root.chmod(0o755)
        state = root / "state"
        state.mkdir(mode=0o700)
        if os.geteuid() == 0:
            os.chown(
                state,
                0,
                0,
            )
        spec = {
            "cwd": args.get("cwd", "/workspace"),
            "command": args.get("command", ""),
            "tty": bool(args.get("tty")),
            "timeout_seconds": args.get("timeout_seconds", 0),
        }
        if kind == "service":
            spec["timeout_seconds"] = 0
        (root / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
        (root / "spec.json").chmod(0o444)
        return spec

    async def launch(self, identity: str) -> None:
        row = self.db.execute(
            "SELECT payload,kind,dispatched FROM jobs JOIN environment_jobs USING(id) WHERE id=?",
            (identity,),
        ).fetchone()
        if row is None or row["dispatched"] or self.get(identity)["status"] in TERMINAL:
            return
        args = json.loads(row["payload"])
        spec = await asyncio.to_thread(self.prepare_spec, identity, args, row["kind"])
        assert self.execd is not None
        response = await self.execd.request(
            "POST",
            "/pty",
            {
                "cwd": spec["cwd"],
                "command": "exec /usr/local/bin/python -I /opt/yuki-runtime/supervisor.py "
                + identity,
            },
        )
        session = response["session_id"]
        # Commit dispatch before the only action that can actually execute code.
        with self.db:
            self.db.execute(
                "UPDATE environment_jobs SET session_id=?,container_id=?,dispatched=1 WHERE id=?",
                (session, self.container_id, identity),
            )
            self.db.execute("UPDATE jobs SET state='running' WHERE id=?", (identity,))
        await self.attach(identity, session, tty=bool(spec["tty"]))

    def validate_execution(self, method: str, args: dict[str, Any]) -> None:
        if not isinstance(args.get("command"), str):
            raise ValueError("invalid_command")
        if type(args.get("tty", False)) is not bool:
            raise ValueError("invalid_tty")
        if not isinstance(args.get("cwd", "/workspace"), str):
            raise ValueError("invalid_cwd")
        timeout = args.get("timeout_seconds", 0)
        if type(timeout) is not int or timeout < 0:
            raise ValueError("invalid_timeout")

    async def submit(self, method: str, args: dict[str, Any], request_id: str) -> dict[str, Any]:
        if method not in EXECUTION_TOOLS | {"service"}:
            return {"error": "unknown_method"}
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 256:
            return {"error": "invalid_request_id"}
        self.validate_execution(method, args)
        payload = json.dumps(args, sort_keys=True)
        digest = hashlib.sha256((method + ":" + payload).encode()).hexdigest()
        prior = self.db.execute(
            "SELECT id,payload_hash FROM jobs WHERE request_id=?", (request_id,)
        ).fetchone()
        if prior:
            return (
                self.get(prior["id"])
                if prior["payload_hash"] == digest
                else {"error": "idempotency_conflict"}
            )
        if not self.ready:
            return {"error": "environment_unavailable", "retryable": False}
        identity = str(uuid4())
        with self.db:
            self.db.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?,?)",
                (identity, request_id, digest, payload, "queued", "{}", time.time()),
            )
            self.db.execute(
                "INSERT INTO environment_jobs (id,kind) VALUES (?,?)", (identity, method)
            )
        self.queue.put_nowait(identity)
        return self.get(identity) if method == "service" else await self.wait_result(identity)

    async def control(self, identity: str, action: str, text: str = "") -> dict[str, Any]:
        identity = identifier(identity)
        result = self.get(identity)
        if result.get("error") or result.get("status") in TERMINAL:
            return result
        if action in {"cancel", "close"}:
            meta = self.db.execute(
                "SELECT dispatched FROM environment_jobs WHERE id=?", (identity,)
            ).fetchone()
            if meta and not meta[0]:
                self.finish(
                    identity, "cancelled", {"exit_code": None, "error": "cancelled_before_launch"}
                )
            else:
                directory = FileWorkspace(self.runtime_root)
                try:
                    directory.write(f"{identity}/state/cancel", b"cancel", expected_version=None)
                except WorkspaceError as exc:
                    if str(exc) != "version_conflict":
                        raise
            return self.get(identity)
        connection = self.connections.get(identity)
        if connection is None:
            return {"error": "terminal_reconnecting", "run_id": identity, "retryable": False}
        if action == "interrupt":
            await connection.send(json.dumps({"type": "signal", "signal": "SIGINT"}))
        elif action == "input":
            if not isinstance(text, str):
                raise ValueError("invalid_terminal_input")
            await connection.send(json.dumps({"type": "stdin", "data": text}))
        else:
            raise ValueError("invalid_terminal_action")
        return {"run_id": identity, "status": "running", "input_accepted": True, "pending": True}

    def file_operation(self, method: str, args: dict[str, Any]) -> dict[str, Any]:
        path = args.get("path", "")
        version = args.get("expected_version")
        if method in {"workspace_media_read", "workspace_media_validate"}:
            # Private Host/Manager operations, never tool declarations. Read
            # from a safely opened workspace FD, outside any SQLite writer.
            limit = 20 * 1024 * 1024
            with self.files.open_file(path) as fd:
                before = os.fstat(fd)
                if before.st_size > limit:
                    raise WorkspaceError("workspace_media_too_large")
                current, info = self.files.fingerprint(fd)
                if version is not None and current != version:
                    raise WorkspaceError("version_conflict")
                result = {
                    "path": "/workspace/" + "/".join(self.files.parts(path)),
                    "version": current,
                    "size": info.st_size,
                }
                if method == "workspace_media_read":
                    os.lseek(fd, 0, os.SEEK_SET)
                    chunks = []
                    remaining = limit + 1
                    while remaining:
                        chunk = os.read(fd, min(65536, remaining))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        remaining -= len(chunk)
                    data = b"".join(chunks)
                    after = os.fstat(fd)
                    if (
                        len(data) != info.st_size
                        or hashlib.sha256(data).hexdigest() != current
                        or (info.st_mtime_ns, info.st_ctime_ns)
                        != (after.st_mtime_ns, after.st_ctime_ns)
                    ):
                        raise WorkspaceError("file_changed_during_read")
                    result["base64"] = base64.b64encode(data).decode("ascii")
                return result
        if method == "workspace_publish":
            with self.files.open_file(path) as fd:
                current, _ = self.files.fingerprint(fd)
                if version is not None and version != current:
                    raise WorkspaceError("version_conflict")
                return {
                    **self.store.snapshot(fd, args.get("name") or Path(path).name),
                    "path": path,
                    "version": current,
                }
        if method == "workspace_checkout":
            metadata, content = self.store.read_bytes(args["artifact_id"])
            return self.files.write(path, content, expected_version=version) | {
                "artifact_id": metadata["artifact_id"]
            }
        raise ValueError("unknown_workspace_method")

    async def file_request(
        self, method: str, args: dict[str, Any], request_id: str
    ) -> dict[str, Any]:
        if method not in {
            "workspace_media_read",
            "workspace_media_validate",
            "workspace_publish",
            "workspace_checkout",
        }:
            return {"error": "unknown_method"}
        read = method in {"workspace_media_read", "workspace_media_validate"}
        async with self.file_lock:
            args = dict(args)
            digest = hashlib.sha256(json.dumps([method, args], sort_keys=True).encode()).hexdigest()
            if not read:
                if not isinstance(request_id, str) or not 1 <= len(request_id) <= 256:
                    raise ValueError("invalid_request_id")
                row = self.db.execute(
                    "SELECT * FROM environment_mutations WHERE request_id=?", (request_id,)
                ).fetchone()
                if row:
                    if row["digest"] != digest and method == "workspace_checkout":
                        # Old receipts hashed the resolved path. Read those original
                        # results even when an interrupted write changed file existence.
                        artifact_id = identifier(args.get("artifact_id"))
                        name = args.get("name", artifact_id)
                        legacy_paths = [name, f"imported/{artifact_id}/{name}"]
                        mapped = self.db.execute(
                            "SELECT path FROM workspace_migrations WHERE artifact_id=?",
                            (artifact_id,),
                        ).fetchone()
                        if mapped:
                            legacy_paths.append(mapped[0])
                        for path in legacy_paths:
                            legacy_digest = hashlib.sha256(
                                json.dumps([method, args | {"path": path}], sort_keys=True).encode()
                            ).hexdigest()
                            if row["digest"] == legacy_digest:
                                digest = legacy_digest
                                break
                    return (
                        {**json.loads(row["result"]), "external_untrusted": True}
                        if row["digest"] == digest
                        else {"error": "idempotency_conflict"}
                    )
            if method == "workspace_checkout":
                artifact_id = identifier(args.get("artifact_id"))
                mapped = self.db.execute(
                    "SELECT path FROM workspace_migrations WHERE artifact_id=?", (artifact_id,)
                ).fetchone()
                if mapped:
                    args["path"] = mapped[0]
                else:
                    name = args.get("name", artifact_id)
                    self.files.parts(name)
                    try:
                        with self.files.open_file(name):
                            pass
                    except FileNotFoundError:
                        args["path"] = name
                    else:
                        args["path"] = f"imported/{artifact_id}/{name}"
            if not read:
                with self.db:
                    self.db.execute(
                        "INSERT INTO environment_mutations VALUES (?,?,?)",
                        (
                            request_id,
                            digest,
                            json.dumps(
                                {
                                    "error": "file_mutation_unknown_read_current_version",
                                    "retryable": False,
                                }
                            ),
                        ),
                    )
            try:
                result = await asyncio.to_thread(self.file_operation, method, args)
            except (WorkspaceError, OSError, ValueError, KeyError) as exc:
                result = {
                    "error": str(exc) if isinstance(exc, WorkspaceError) else type(exc).__name__
                }
                if isinstance(exc, OSError) and exc.errno in {28, 122}:
                    result = {"error": "workspace_full", **self.disk_status()}
            if not read:
                with self.db:
                    self.db.execute(
                        "UPDATE environment_mutations SET result=? WHERE request_id=?",
                        (json.dumps(result), request_id),
                    )
                    if method == "workspace_checkout" and not result.get("error"):
                        self.db.execute(
                            "INSERT OR REPLACE INTO workspace_migrations VALUES (?,?,?)",
                            (args["artifact_id"], args["path"], result["version"]),
                        )
            return {**result, "external_untrusted": True}

    def disk_status(self) -> dict[str, Any]:
        usage = shutil.disk_usage(self.home)
        return {
            "capacity_bytes": usage.total,
            "used_bytes": usage.used,
            "available_bytes": usage.free,
        }

    def environment_status(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "home": "/home/yuki",
            "workspace": "/workspace",
            "storage": self.disk_status(),
            "checkpoint": self.setting("checkpoint_image", self.image),
            "host_available_bytes": self.available_memory(),
            "limits": {
                **self.resource_limits(),
                "swap_mib": 128,
                "pids": 128,
            },
            "runs": [
                dict(row)
                for row in self.db.execute(
                    "SELECT jobs.id AS run_id,jobs.state AS status,kind,created FROM jobs "
                    "JOIN environment_jobs USING(id) ORDER BY created DESC LIMIT 24"
                )
            ],
            "services": [
                dict(row)
                for row in self.db.execute(
                    "SELECT name,enabled,run_id,attempts FROM environment_services"
                )
            ],
        }

    async def service(self, args: dict[str, Any]) -> dict[str, Any]:
        action, name = args.get("action"), args.get("name", "")
        if action == "status" and not name:
            return {"services": self.environment_status()["services"]}
        if not isinstance(name, str) or not name:
            raise ValueError("invalid_service_name")
        row = self.db.execute("SELECT * FROM environment_services WHERE name=?", (name,)).fetchone()
        if action == "register":
            self.validate_execution("service", args)
            restart = args.get("restart", "on-failure")
            if restart not in {"always", "on-failure", "never"}:
                raise ValueError("invalid_restart_policy")
            spec = {
                "command": args["command"],
                "cwd": args.get("cwd", "/workspace"),
                "restart": restart,
                "timeout_seconds": 0,
            }
            with self.db:
                self.db.execute(
                    "INSERT INTO environment_services(name,spec) VALUES (?,?) "
                    "ON CONFLICT(name) DO UPDATE SET spec=excluded.spec",
                    (name, json.dumps(spec)),
                )
            return {"name": name, "registered": True, "started": False}
        if row is None:
            return {"error": "service_not_found"}
        if action == "logs":
            return (
                self.output(row["run_id"], args.get("cursor", 0))
                if row["run_id"]
                else {"output": "", "next_cursor": 0}
            )
        if action == "status":
            return {
                "name": name,
                "enabled": bool(row["enabled"]),
                "spec": json.loads(row["spec"]),
                "run": self.get(row["run_id"]) if row["run_id"] else None,
            }
        if action == "start":
            with self.db:
                self.db.execute(
                    "UPDATE environment_services SET enabled"
                    "=1,attempts=0,next_start=0 WHERE name=?",
                    (name,),
                )
            await self.restore_services()
            return await self.service({"action": "status", "name": name})
        if action in {"stop", "delete"}:
            with self.db:
                self.db.execute("UPDATE environment_services SET enabled=0 WHERE name=?", (name,))
            if row["run_id"]:
                await self.control(row["run_id"], "cancel")
            if action == "delete":
                with self.db:
                    self.db.execute("DELETE FROM environment_services WHERE name=?", (name,))
            return {"name": name, "enabled": False, "stopping": bool(row["run_id"])}
        raise ValueError("invalid_service_action")

    async def restore_services(self) -> None:
        for row in self.db.execute("SELECT * FROM environment_services WHERE enabled=1").fetchall():
            previous = self.get(row["run_id"]) if row["run_id"] else {}
            if previous.get("pending"):
                continue
            if row["next_start"] > time.time():
                continue
            spec = json.loads(row["spec"])
            if row["run_id"] and (
                spec["restart"] == "never"
                or (spec["restart"] == "on-failure" and previous.get("status") == "succeeded")
            ):
                continue
            result = await self.submit("service", spec, f"service:{uuid4()}")
            if result.get("run_id"):
                with self.db:
                    self.db.execute(
                        "UPDATE environment_services SET run_id=?,at"
                        "tempts=attempts+1,next_start=? WHERE name=?",
                        (
                            result["run_id"],
                            time.time() + 5,
                            row["name"],
                        ),
                    )

    async def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        method, args = request.get("method"), request.get("args")
        if not isinstance(method, str) or not isinstance(args, dict):
            return {"error": "invalid_arguments"}
        try:
            if method in EXECUTION_TOOLS:
                return await self.submit(method, dict(args), request.get("request_id", ""))
            if method in {
                "workspace_media_read",
                "workspace_media_validate",
                "workspace_publish",
                "workspace_checkout",
            }:
                return await self.file_request(method, args, request.get("request_id", ""))
            if method == "environment_status":
                return self.environment_status()
            if method == "environment_service":
                return await self.service(args)
            if method == "terminal_read":
                identity = identifier(args.get("run_id"))
                wait_seconds = args.get("wait_seconds", 0)
                if (
                    isinstance(wait_seconds, bool)
                    or not isinstance(wait_seconds, (int, float))
                    or not math.isfinite(wait_seconds)
                    or wait_seconds < 0
                ):
                    raise ValueError("invalid_wait_seconds")
                completion = (
                    await self.wait_result(identity, wait_seconds=wait_seconds)
                    if wait_seconds
                    else self.get(identity)
                )
                if completion.get("error"):
                    return completion
                return {
                    **completion,
                    **self.output(identity, args.get("cursor", 0)),
                    **({"completion": completion} if completion.get("status") in TERMINAL else {}),
                }
            if method == "terminal_control":
                return await self.control(
                    str(args.get("run_id")), args.get("action", ""), args.get("text", "")
                )
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
            return {"error": "unknown_method"}
        except (WorkspaceError, ValueError, KeyError, TypeError) as exc:
            return {
                "error": str(exc)
                if isinstance(exc, (WorkspaceError, ValueError))
                else "invalid_arguments",
                "retryable": False,
            }

    async def worker(self) -> None:
        while True:
            try:
                identity = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                identity = None
            if identity:
                try:
                    await self.launch(identity)
                except Exception as exc:
                    LOGGER.warning(
                        "environment_launch_uncertain run_id=%s category=%s",
                        identity,
                        type(exc).__name__,
                    )
                    # A dispatched job may have started; only reconciliation decides its outcome.
                    row = self.db.execute(
                        "SELECT dispatched FROM environment_jobs WHERE id=?", (identity,)
                    ).fetchone()
                    if row and not row[0]:
                        self.finish(identity, "failed", {"error": type(exc).__name__})
                finally:
                    self.queue.task_done()
            for row in self.active():
                if row["dispatched"]:
                    try:
                        await self.reconcile(row)
                    except Exception as exc:
                        LOGGER.warning(
                            "environment_reconcile_pending run_id=%s category=%s",
                            row["id"],
                            type(exc).__name__,
                        )
            for key, task in tuple(self.pumps.items()):
                if task.done():
                    self.pumps.pop(key, None)
            if time.time() - self.last_layer_check > 15:
                current = await self.inspect_container()
                self.last_layer_check = time.time()
                if (
                    not current
                    or not current["State"]["Running"]
                    or current["Id"] + ":" + current["State"]["StartedAt"] != self.container_id
                ):
                    self.ready = False
                    for row in self.active():
                        self.finish(
                            row["id"],
                            "failed",
                            {
                                "error": "environment_stopped",
                                "interrupted": True,
                                "oom": bool(current and current["State"].get("OOMKilled")),
                            },
                        )
                    try:
                        await self.recover()
                    except Exception:
                        LOGGER.warning("environment_recovery_pending")
            if self.ready:
                await self.restore_services()
            if time.time() - self.last_prune > 60:
                self.prune_logs()
                self.last_prune = time.time()
            await asyncio.sleep(0.5)

    def prune_logs(self) -> None:
        # Active jobs each retain <= 8 MiB. Reclaim only completed output, never receipts/files.
        logs = []
        for entry in self.runtime_root.glob("*/state/output-*.log"):
            if not entry.is_symlink() and entry.is_file():
                info = entry.stat()
                logs.append((info.st_mtime, info.st_size, entry))
        total = sum(size for _, size, _ in logs)
        active = {row["id"] for row in self.active()}
        for modified, size, path in sorted(logs):
            if total <= LOG_BUDGET - 96 * 1024**2 and modified > time.time() - 7 * 86400:
                break
            if path.parent.parent.name not in active:
                path.unlink(missing_ok=True)
                total -= size
        for row in self.db.execute(
            "SELECT jobs.id FROM jobs JOIN environment_jobs USING(id) "
            "WHERE state IN ('succeeded','failed','cancelled') AND created<? "
            "AND jobs.id NOT IN (SELECT run_id FROM completion_outbox)",
            (time.time() - 7 * 86400,),
        ).fetchall():
            directory = self.runtime_root / identifier(row[0])
            if directory.exists() and not directory.is_symlink():
                # POSIX shutil.rmtree uses directory FDs, avoiding symlink traversal.
                shutil.rmtree(directory)

    async def close(self) -> None:
        self.ready = False
        for connection in tuple(self.connections.values()):
            await connection.close()
        for task in self.pumps.values():
            task.cancel()
        await asyncio.gather(*self.pumps.values(), return_exceptions=True)
        self.db.close()

    def migrate_files(self) -> dict[str, Any]:
        """Idempotent offline migration; never overwrites a terminal's existing content."""
        self.files.root.mkdir(parents=True, exist_ok=True)
        self.store.cleanup()
        cursor = ""
        records = []
        while True:
            page = self.store.list(cursor=cursor, limit=100)
            for item in page["items"]:
                identity = item["artifact_id"]
                metadata, content = self.store.read_bytes(identity)
                previous = self.db.execute(
                    "SELECT * FROM workspace_migrations WHERE artifact_id=?", (identity,)
                ).fetchone()
                path = previous["path"] if previous else metadata["name"]
                if not previous and path in {"manifest.json", "by-id", "imported"}:
                    path = f"imported/{identity}/{path}"
                if not previous:
                    try:
                        with self.files.open_file(path):
                            pass
                    except FileNotFoundError:
                        pass
                    else:
                        path = f"imported/{identity}/{metadata['name']}"
                    written = self.files.write(path, content)
                    if written["version"] != metadata["sha256"]:
                        raise WorkspaceError("migration_checksum_failed")
                    with self.db:
                        self.db.execute(
                            "INSERT INTO workspace_migrations VALUES (?,?,?)",
                            (identity, path, metadata["sha256"]),
                        )
                alias = f"by-id/{identity}"
                try:
                    with self.files.open_file(alias) as fd:
                        alias_version, _ = self.files.fingerprint(fd)
                except FileNotFoundError:
                    alias_version = self.files.write(alias, content)["version"]
                if alias_version != metadata["sha256"]:
                    raise WorkspaceError("legacy_alias_checksum_failed")
                records.append(
                    {**metadata, "path": f"/workspace/{path}", "legacy_path": f"/workspace/{alias}"}
                )
            cursor = page["next_cursor"]
            if not cursor:
                break
        try:
            with self.files.open_file("manifest.json") as fd:
                version, _ = self.files.fingerprint(fd)
        except FileNotFoundError:
            version = None
        self.files.write("manifest.json", json.dumps(records, ensure_ascii=False).encode(), version)
        return {"migrated": len(records), "verified": True}
