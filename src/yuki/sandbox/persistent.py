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
import sys
import time
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from yuki.sandbox.acp_manager import ACP_KINDS, ACPResources
from yuki.sandbox.acp_peer import read_protocol_line
from yuki.sandbox.completions import CompletionOutbox
from yuki.sandbox.computer_control import ComputerControl
from yuki.sandbox.computer_runtime import ComputerRuntime
from yuki.sandbox.computer_transport import ComputerTransport
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
        acp_config: Path | None = None,
        testing: bool = False,
        name: str = "yuki-environment",
        media_cache: Path | None = None,
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
        self.media_cache = FileWorkspace(media_cache.resolve()) if media_cache is not None else None
        self.media_cache_shared = False
        self.applied_limits: dict[str, int | float] | None = None
        self.environment_manifest: dict[str, Any] | None = None
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
        self._file_tasks: set[asyncio.Task[Any]] = set()
        self._index_builds: dict[str, asyncio.Task[dict[str, Any]]] = {}
        self._file_requests: dict[str, tuple[str, asyncio.Task[dict[str, Any]]]] = {}
        self.last_prune = 0.0
        self.last_layer_check = 0.0
        self.acp = ACPResources(self, acp_config)
        self.computer = ComputerControl(self)
        self.desktop = ComputerRuntime(self)
        self.computer_transport = ComputerTransport(self)
        if "start_key" not in {
            row[1] for row in self.db.execute("PRAGMA table_info(environment_services)")
        }:
            self.db.execute("ALTER TABLE environment_services ADD COLUMN start_key TEXT")
            self.db.commit()
        self.service_lock = asyncio.Lock()

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
                raw = await read_protocol_line(reader)
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise ValueError("invalid_request")
            if request.get("method") == "display_stream":
                await self.computer_transport.display_stream(reader, writer)
                return
            if request.get("method") == "files_read":
                await self.computer_transport.files_read(request["args"]["path"], writer)
                return
            if request.get("method") == "control_watch":
                await self.computer.watch(reader, writer)
                return
            result = await self.handle(request)
            writer.write(json.dumps(result).encode() + b"\n")
            await writer.drain()
        except (ValueError, OSError, TimeoutError, WorkspaceError):
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
            "memory_mib": int(self.setting("resource_memory_mib", "3072")),
            "cpus": float(self.setting("resource_cpus", "2")),
            "cpu_shares": int(self.setting("resource_cpu_shares", "1024")),
            "swap_mib": int(self.setting("resource_swap_mib", "512")),
            "pids": int(self.setting("resource_pids", "2048")),
            "tmp_mib": int(self.setting("resource_tmp_mib", "512")),
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
        if self.acp.kind(identity) in ACP_KINDS:
            return self.acp.view(identity)
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
            try:
                label = json.loads(self.read_state_file(identity, "step-label.json"))
                if label.get("step_label") in {"browser", "computer"}:
                    result["step_label"] = label["step_label"]
            except (OSError, ValueError, WorkspaceError):
                pass
        return result

    def finish(self, identity: str, state: str, result: dict[str, Any]) -> bool:
        meta = self.db.execute(
            "SELECT kind FROM environment_jobs WHERE id=?", (identity,)
        ).fetchone()
        if meta and meta[0] in {"service", "acp_peer", "acp_control"}:
            with self.db:
                changed = self.db.execute(
                    "UPDATE jobs SET state=?,result=? WHERE id=? AND state IN ('queued','running')",
                    (state, json.dumps(result), identity),
                ).rowcount
            return bool(changed)
        changed = self._finish_receipt(identity, state, result)
        if changed and state in TERMINAL:
            self.computer.finish_run(identity)
        if changed and state in TERMINAL and not (meta and meta[0] in ACP_KINDS):
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
                f"{limits['memory_mib'] + limits['swap_mib']}m",
                "--cpus",
                str(limits["cpus"]),
                "--cpu-shares",
                str(limits["cpu_shares"]),
                "--pids-limit",
                str(limits["pids"]),
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
                f"/tmp:rw,nosuid,nodev,size={limits['tmp_mib']}m,mode=1777",
                "--mount",
                f"type=bind,src={self.home},dst=/home/yuki",
                "--mount",
                f"type=bind,src={self.runtime_root},dst=/var/lib/yuki-runtime",
                "--mount",
                f"type=bind,src={Path(__file__).with_name('environment_supervisor.py').resolve()},"
                "dst=/opt/yuki-runtime/supervisor.py,readonly",
                "--mount",
                f"type=bind,src={Path(__file__).with_name('acp_peer.py').resolve()},"
                "dst=/opt/yuki-runtime/acp_peer.py,readonly",
                "--env",
                f"EXECD_ACCESS_TOKEN={token}",
            ]
            for source, target in (
                ("yuki_computer.py", "yuki_computer.py"),
                ("desktop_service.py", "desktop_service.py"),
            ):
                args.extend(
                    (
                        "--mount",
                        f"type=bind,src={Path(__file__).with_name(source).resolve()},dst=/opt/yuki-runtime/{target},readonly",
                    )
                )
            if self.resolv_conf is not None:
                args.extend(("--mount", f"type=bind,src={self.resolv_conf},dst=/etc/resolv.conf"))
            if self.media_cache is not None:
                args.extend(
                    (
                        "--mount",
                        f"type=bind,src={self.media_cache.root},"
                        "dst=/home/yuki/inputs/media-cache,readonly",
                    )
                )
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
        host = current.get("HostConfig")
        if host is not None:
            memory = host.get("Memory", 0)
            swap = host.get("MemorySwap", 0)
            self.applied_limits = {
                "memory_mib": memory // (1024 * 1024),
                "cpus": host.get("NanoCpus", 0) / 1_000_000_000,
                "swap_mib": (swap - memory) // (1024 * 1024) if swap > 0 else swap,
                "pids": host.get("PidsLimit", 0),
                "cpu_shares": host.get("CpuShares", 0),
            }
        self.media_cache_shared = self.media_cache is not None and any(
            mount.get("Destination") == "/home/yuki/inputs/media-cache"
            and mount.get("Source") == str(self.media_cache.root)
            and mount.get("RW") is False
            for mount in current.get("Mounts", ())
        )
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
            if row["kind"] not in EXECUTION_TOOLS | {"service", "acp_peer"}:
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
        self.computer.deliver_results()
        if not self.testing:
            code, manifest = await self.command(
                "docker", "exec", self.name, "cat", "/opt/yuki-runtime/manifest.json"
            )
            self.environment_manifest = json.loads(manifest) if not code else None
        self.ready = True

    def active(self) -> list[Any]:
        return self.db.execute(
            "SELECT jobs.*,environment_jobs.kind,session_id,container_id,dispatched "
            "FROM jobs JOIN environment_jobs USING(id) "
            "WHERE jobs.state IN ('queued','running') AND kind NOT IN ('acp_prompt','acp_control')"
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
            try:
                label = json.loads(self.read_state_file(identity, "step-label.json"))
                if label.get("step_label") in {"browser", "computer"}:
                    result["step_label"] = label["step_label"]
            except (OSError, ValueError, WorkspaceError):
                pass
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
        root.mkdir(mode=0o755, exist_ok=True)
        root.chmod(0o755)
        state = root / "state"
        state.mkdir(mode=0o700, exist_ok=True)
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
            "env": args.get("env", {}) if kind == "terminal_exec" else {},
        }
        if kind == "acp_peer":
            spec.update(
                acp_peer=args,
                argv=["/usr/local/bin/python", "-I", "/opt/yuki-runtime/acp_peer.py", identity],
            )
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
        try:
            spec = await asyncio.to_thread(self.prepare_spec, identity, args, row["kind"])
        finally:
            if row["kind"] == "acp_peer" and self.get(identity).get("purged"):
                directory = self.runtime_root / identity
                if directory.exists():
                    shutil.rmtree(directory)
        if row["kind"] == "acp_peer" and self.get(identity)["status"] in TERMINAL:
            return
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
        if row["kind"] == "acp_peer" and self.get(identity)["status"] in TERMINAL:
            # Creating an execd PTY has not dispatched the Supervisor. A close
            # received while creating it must never regain launch permission.
            await self.execd.request("DELETE", f"/pty/{session}")
            return
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

    def cancel_process(self, identity: str) -> None:
        directory = FileWorkspace(self.runtime_root)
        try:
            directory.write(f"{identity}/state/cancel", b"cancel", expected_version=None)
        except WorkspaceError as exc:
            if str(exc) != "version_conflict":
                raise

    async def control(self, identity: str, action: str, text: str = "") -> dict[str, Any]:
        identity = identifier(identity)
        if self.acp.kind(identity) == "acp_prompt":
            if action not in {"cancel", "close", "interrupt"}:
                raise ValueError("acp_prompt_requires_permission_reply")
            return self.acp.control("cancel", {"run_id": identity}, f"prompt-cancel:{identity}")
        result = self.get(identity)
        if result.get("error") or result.get("status") in TERMINAL:
            return result
        if action in {"cancel", "close"}:
            self.computer.cancel(identity)
            meta = self.db.execute(
                "SELECT dispatched FROM environment_jobs WHERE id=?", (identity,)
            ).fetchone()
            if meta and not meta[0]:
                self.finish(
                    identity, "cancelled", {"exit_code": None, "error": "cancelled_before_launch"}
                )
            else:
                self.cancel_process(identity)
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

    def _media_source(self, path: str) -> tuple[FileWorkspace, str, str]:
        prefix = "/inputs/media-cache/"
        if path.startswith(prefix):
            if self.media_cache is None:
                raise WorkspaceError("media_cache_unavailable")
            return self.media_cache, path.removeprefix(prefix), prefix
        return self.files, path, "/workspace/"

    def file_operation(self, method: str, args: dict[str, Any]) -> dict[str, Any]:
        path = args.get("path", "")
        version = args.get("expected_version")
        if method in {"workspace_media_read", "workspace_media_validate"}:
            # Private Host/Manager operations, never tool declarations. Read
            # from a safely opened workspace FD, outside any SQLite writer.
            limit = 20 * 1024 * 1024
            files, path, prefix = self._media_source(path)
            with files.open_file(path) as fd:
                before = os.fstat(fd)
                if method == "workspace_media_read" and before.st_size > limit:
                    raise WorkspaceError("workspace_media_too_large")
                current, info = files.fingerprint(fd)
                if version is not None and current != version:
                    raise WorkspaceError("version_conflict")
                result = {
                    "path": prefix + "/".join(files.parts(path)),
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
            files, relative, _prefix = self._media_source(path)
            with files.open_file(relative) as fd:
                snapshot = self.store.snapshot(
                    fd,
                    args.get("name") or Path(path).name,
                    artifact_id=args.get("artifact_id"),
                    source_key=args.get("source_key"),
                    expected_version=version,
                )
                return {
                    **snapshot,
                    "path": path,
                    "version": snapshot["sha256"],
                }
        if method == "workspace_checkout":
            metadata, content = self.store.read_bytes(args["artifact_id"])
            if args.get("destination"):
                try:
                    with self.files.open_file(path) as fd:
                        actual, _ = self.files.fingerprint(fd)
                    if actual == metadata["sha256"]:
                        return {
                            "path": "/workspace/" + "/".join(self.files.parts(path)),
                            "version": actual,
                            "size": metadata["size"],
                            "artifact_id": metadata["artifact_id"],
                        }
                except FileNotFoundError:
                    pass
            return self.files.write(path, content, expected_version=version) | {
                "artifact_id": metadata["artifact_id"]
            }
        raise ValueError("unknown_workspace_method")

    def _start_file(self, operation: Any) -> asyncio.Task[dict[str, Any]]:
        task = asyncio.create_task(operation)
        self._file_tasks.add(task)
        task.add_done_callback(self._file_tasks.discard)
        return task

    async def _wait_file(self, task: asyncio.Task[dict[str, Any]]) -> dict[str, Any]:
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                if task.cancelled():
                    raise
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError
        return result

    async def _owned_file(self, operation: Any) -> dict[str, Any]:
        return await self._wait_file(self._start_file(operation))

    async def sticker_index(self, args: dict[str, Any]) -> dict[str, Any]:
        if args.get("command") == "rebuild":
            platform = str(args.get("platform") or "*")
            existing = self._index_builds.get(platform)
            if existing is None or existing.done():
                existing = self._start_file(self._sticker_helper(args))
                self._index_builds[platform] = existing
            try:
                return await self._wait_file(existing)
            finally:
                if existing.done() and self._index_builds.get(platform) is existing:
                    self._index_builds.pop(platform, None)
        return await self._owned_file(self._sticker_helper(args))

    async def _sticker_helper(self, args: dict[str, Any]) -> dict[str, Any]:
        if self.testing:
            command = [
                sys.executable,
                str(Path(__file__).parents[1] / "emoji" / "sticker_index.py"),
                "--workspace",
                str(self.files.root),
                "--request",
            ]
        else:
            command = [
                "docker",
                "exec",
                "-i",
                self.name,
                "python",
                "/opt/yuki-runtime/sticker_index.py",
                "--workspace",
                "/workspace",
                "--request",
            ]
        # No command()/socket observation deadline and no 32 KiB stdout truncation.
        # Closing drains this actual helper; killing docker exec's client is not cancellation.
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        output, error = await process.communicate(json.dumps(args, ensure_ascii=False).encode())
        if process.returncode:
            LOGGER.warning("sticker_helper_failed detail=%s", error.decode(errors="replace"))
            return {"error": "sticker_helper_failed"}
        result = json.loads(output)
        if not isinstance(result, dict):
            raise ValueError("invalid_sticker_result")
        return result

    async def file_request(
        self, method: str, args: dict[str, Any], request_id: str
    ) -> dict[str, Any]:
        if method not in {"workspace_publish", "workspace_checkout"} or not isinstance(
            request_id, str
        ):
            return await self._owned_file(self._file_request(method, args, request_id))
        digest = hashlib.sha256(json.dumps([method, args], sort_keys=True).encode()).hexdigest()
        existing = self._file_requests.get(request_id)
        if existing is not None:
            if existing[0] != digest:
                return {"error": "idempotency_conflict"}
            return await self._wait_file(existing[1])
        task = self._start_file(self._file_request(method, args, request_id))
        self._file_requests[request_id] = digest, task
        task.add_done_callback(lambda _: self._file_requests.pop(request_id, None))
        return await self._wait_file(task)

    async def _file_request(
        self, method: str, args: dict[str, Any], request_id: str
    ) -> dict[str, Any]:
        if method not in {
            "workspace_media_read",
            "workspace_media_validate",
            "workspace_publish",
            "workspace_checkout",
        }:
            return {"error": "unknown_method"}
        read = method in {
            "workspace_media_read",
            "workspace_media_validate",
        }
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
                if args.get("destination") is not None:
                    self.files.parts(args["destination"])
                    args["path"] = args["destination"]
                else:
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
            result = {"error": str(exc) if isinstance(exc, WorkspaceError) else type(exc).__name__}
            if isinstance(exc, OSError) and exc.errno in {28, 122}:
                result = {"error": "workspace_full", **self.disk_status()}
        if not read:
            async with self.file_lock:
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
            "generation": self.container_id or None,
            "home": "/home/yuki",
            "workspace": "/workspace",
            "media_cache": {
                "path": "/inputs/media-cache",
                "configured": self.media_cache is not None,
                "shared": self.media_cache_shared,
            },
            "storage": self.disk_status(),
            "checkpoint": self.setting("checkpoint_image", self.image),
            "host_available_bytes": self.available_memory(),
            "limits": self.applied_limits
            if self.applied_limits is not None
            else self.resource_limits(),
            "manifest": self.environment_manifest,
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
            if row["run_id"] and not self.get(row["run_id"]).get("pending"):
                with self.db:
                    self.db.execute(
                        "UPDATE environment_services SET run_id=NULL,start_key=NULL WHERE name=?",
                        (name,),
                    )
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
            elif row["start_key"]:
                original = self.db.execute(
                    "SELECT id FROM jobs WHERE request_id=?", (row["start_key"],)
                ).fetchone()
                if original:
                    await self.control(original["id"], "cancel")
                    with self.db:
                        self.db.execute(
                            "UPDATE environment_services SET run_id=?,start_key=NULL WHERE name=?",
                            (original["id"], name),
                        )
            if action == "delete":
                with self.db:
                    self.db.execute("DELETE FROM environment_services WHERE name=?", (name,))
            return {"name": name, "enabled": False, "stopping": bool(row["run_id"])}
        raise ValueError("invalid_service_action")

    async def restore_services(self) -> None:
        async with self.service_lock:
            await self._restore_services()

    async def _restore_services(self) -> None:
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
            key = row["start_key"] or f"service:{uuid4()}"
            if not row["start_key"]:
                with self.db:
                    self.db.execute(
                        "UPDATE environment_services SET start_key=? WHERE name=?",
                        (key, row["name"]),
                    )
            original = self.db.execute("SELECT id FROM jobs WHERE request_id=?", (key,)).fetchone()
            result = (
                self.get(original["id"]) if original else await self.submit("service", spec, key)
            )
            if result.get("run_id"):
                with self.db:
                    self.db.execute(
                        "UPDATE environment_services SET run_id=?,start_key=NULL,at"
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
            if method == "files_list":
                return await asyncio.to_thread(self.computer_transport.files_list, args)
            if method in {"display_status", "display_wake", "display_sleep", "tabs_list"}:
                return await self.desktop.handle(method, args, request.get("request_id", ""))
            if method == "control_get":
                return self.computer.snapshot()
            if method == "control_set":
                return self.computer.set(request.get("request_id", ""), args)
            if method == "control_request_get":
                return self.computer.request_get(args.get("key", ""))
            if method == "sticker_index":
                return await self.sticker_index(args)
            if method.startswith("acp_"):
                return await self.acp.handle(method, args, request.get("request_id", ""))
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
                if self.acp.kind(identity) == "acp_prompt":
                    self.acp.refresh()
                    if wait_seconds:
                        await self.wait_result(identity, wait_seconds=wait_seconds)
                    return self.acp.query(identity, args.get("cursor"))
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
            try:
                self.computer.collect_requests()
                await self.desktop.collect_commands()
            except (OSError, ValueError, WorkspaceError):
                LOGGER.warning("computer_wait_reconcile_pending")
            try:
                self.acp.refresh()
            except (OSError, ValueError):
                LOGGER.warning("acp_receipt_reconcile_pending")
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
            "AND kind NOT IN ('acp_peer','acp_prompt','acp_control') "
            "AND jobs.id NOT IN (SELECT run_id FROM completion_outbox)",
            (time.time() - 7 * 86400,),
        ).fetchall():
            directory = self.runtime_root / identifier(row[0])
            if directory.exists() and not directory.is_symlink():
                # POSIX shutil.rmtree uses directory FDs, avoiding symlink traversal.
                shutil.rmtree(directory)

    async def close(self) -> None:
        self.ready = False
        cancelled = False
        try:
            for connection in tuple(self.connections.values()):
                await connection.close()
            for task in self.pumps.values():
                task.cancel()
            await asyncio.gather(*self.pumps.values(), return_exceptions=True)
        except asyncio.CancelledError:
            cancelled = True
        finally:
            while self._file_tasks:
                actual = asyncio.gather(*tuple(self._file_tasks), return_exceptions=True)
                while not actual.done():
                    try:
                        await asyncio.shield(actual)
                    except asyncio.CancelledError:
                        cancelled = True
            self.db.close()
        if cancelled:
            raise asyncio.CancelledError

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
