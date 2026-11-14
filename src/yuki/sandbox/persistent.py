"""Trusted lifecycle owner for one persistent environment container.

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
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from yuki.sandbox import network_guard
from yuki.sandbox.completions import CompletionOutbox
from yuki.sandbox.computer_control import ComputerControl
from yuki.sandbox.computer_runtime import ComputerRuntime
from yuki.sandbox.computer_transport import ComputerTransport
from yuki.sandbox.environment_programs import MODE_PACKAGES, wanted
from yuki.sandbox.environment_snapshots import EnvironmentSnapshots
from yuki.sandbox.environment_tools import EXECUTION_TOOLS
from yuki.sandbox.execd import Execd
from yuki.sandbox.legacy_acp import ACP_KINDS, LegacyACPResources
from yuki.sandbox.manager import TERMINAL, identifier
from yuki.sandbox.protocol import read_protocol_line
from yuki.workspace.files import FileWorkspace
from yuki.workspace.store import WorkspaceError, WorkspaceStore

LABEL = "io.yuki.sandbox=persistent-v1"
PROJECT_LABEL = "io.yuki.sandbox.project"
CONTAINER = "yuki-environment"
# Docker volumes that outlive containers and Compose projects (created by the installer).
VOLUMES = {
    "home": "yuki-sandbox-home",
    "runtime": "yuki-sandbox-runtime",
    "code": "yuki-sandbox-code",
    "media": "yuki-media-cache",
}
# Environment path -> repository path. The Bot image carries these files at the same
# relative paths under YUKI_ENVIRONMENT_SOURCE; the Manager copies them into the code
# volume, and the environment mounts each one read-only at its original path.
ENVIRONMENT_FILES = {
    "/opt/yuki-runtime/supervisor.py": "src/yuki/sandbox/environment_supervisor.py",
    "/opt/yuki-runtime/environment_programs.py": "src/yuki/sandbox/environment_programs.py",
    "/opt/yuki-runtime/sticker_index.py": "src/yuki/emoji/sticker_index.py",
    "/opt/yuki-runtime/media.py": "src/yuki/emoji/media.py",
    "/opt/yuki-runtime/desktop_service.py": "src/yuki/sandbox/desktop_service.py",
    "/opt/yuki-runtime/yuki_computer.py": "src/yuki/sandbox/yuki_computer.py",
    "/opt/yuki-runtime/yuki_agent_connector/cdp.py": (
        "packages/yuki-agent-connector/src/yuki_agent_connector/cdp.py"
    ),
    "/usr/local/bin/yuki-computer": "deploy/sandbox/yuki-computer.sh",
}
MIB = 1024 * 1024
SEGMENT = 4 * 1024 * 1024
LOG_BUDGET = 128 * 1024 * 1024
LOGGER = logging.getLogger(__name__)
# Environment-level autostart: /etc/rc.local belongs to her computer (rootFS, kept in
# snapshots). It runs once per environment generation; its real exit code is the last
# line of the log, and processes it starts with ``&`` stay with docker-init.
AUTOSTART = (
    "[ -e /etc/rc.local ] || exit 0\n"
    "/bin/sh /etc/rc.local > /var/log/rc.local.log 2>&1\n"
    'echo "exit $?" >> /var/log/rc.local.log\n'
    "exit 0\n"
)


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
        resolv_conf: str | None = None,
        testing: bool = False,
        name: str = "yuki-environment",
        media_cache: Path | None = None,
        code: Path | None = None,
        volumes: dict[str, str] = VOLUMES,
        project: str = "",
        profile: str = "",
        browser_mode: str = "none",
        packages: tuple[str, ...] = (),
        snapshots: Path | None = None,
        snapshot_keep: int = 3,
        snapshot_interval_hours: float = 0,
        display_idle_minutes: float = 0,
    ) -> None:
        if not re.fullmatch(r"yuki-environment(?:-[a-z0-9-]+)?", name):
            raise ValueError("invalid_environment_name")
        if browser_mode not in MODE_PACKAGES:
            raise ValueError("invalid_browser_mode")
        self.root, self.store, self.image = root.resolve(), store, image
        self.network, self.proxy = network, proxy
        # A file inside HOME; gVisor cannot use Docker's 127.0.0.11 resolver.
        self.resolv_conf = resolv_conf
        self.volumes, self.project, self.profile = volumes, project, profile
        self.browser_mode, self.packages = browser_mode, packages
        # Product settings from .env; none of them decides whether anything is accepted.
        self.display_idle_minutes = display_idle_minutes
        self.code_root = (code or self.root / "code").resolve()
        self.programs: dict[str, Any] = {}
        self.limits_error = ""
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self._waiters: dict[str, set[asyncio.Event]] = {}
        self.ready = False
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.home = home.resolve()
        self.runtime, self.testing = runtime, testing
        self.name = name
        self.files = FileWorkspace(self.home / "workspace")
        self.media_cache = FileWorkspace(media_cache.resolve()) if media_cache is not None else None
        self.media_cache_shared = False
        self.runtime_root = self.root / "environment-jobs"
        self.runtime_root.mkdir(mode=0o755, exist_ok=True)
        self.runtime_root.chmod(0o755)
        self.execd: Execd | None = None
        self.observed_container: dict[str, Any] = {}
        self.container_id = ""
        self.connections: dict[str, Any] = {}
        self.pumps: dict[str, asyncio.Task[None]] = {}
        self.file_lock = asyncio.Lock()
        self._file_tasks: set[asyncio.Task[Any]] = set()
        self._index_builds: dict[str, asyncio.Task[dict[str, Any]]] = {}
        self._file_requests: dict[str, tuple[str, asyncio.Task[dict[str, Any]]]] = {}
        self.last_prune = 0.0
        self.last_layer_check = 0.0
        self.legacy_acp = LegacyACPResources(self)
        self.desktop_manifest: dict[str, Any] = {"desktop": False, "tools": []}
        self.db = sqlite3.connect(self.root / "jobs.sqlite3")
        try:
            self.db.row_factory = sqlite3.Row
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, "
                "request_id TEXT UNIQUE NOT NULL, payload_hash TEXT NOT NULL, "
                "payload TEXT NOT NULL, state TEXT NOT NULL, result TEXT NOT NULL, "
                "created REAL NOT NULL)"
            )
            self.completions = CompletionOutbox(self.db)
            self.db.commit()
            self.db.executescript(
                "CREATE TABLE IF NOT EXISTS environment_jobs "
                "(id TEXT PRIMARY KEY, kind TEXT NOT NULL, "
                "session_id TEXT, container_id TEXT, dispatched INTEGER NOT NULL DEFAULT 0);"
                "CREATE TABLE IF NOT EXISTS environment_state"
                " (key TEXT PRIMARY KEY,value TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS environment_servic"
                "es (name TEXT PRIMARY KEY, spec TEXT NOT NULL,"
                "enabled INTEGER NOT NULL DEFAULT 0, run_id TEXT, "
                "attempts INTEGER NOT NULL DEFAULT 0,"
                "next_start REAL NOT NULL DEFAULT 0);"
                "CREATE TABLE IF NOT EXISTS environment_mutations (request_id TEXT PRIMARY KEY,"
                "digest TEXT NOT NULL, result TEXT NOT NULL);"
            )
            self.db.commit()
            self.computer = ComputerControl(self)
            self.desktop = ComputerRuntime(self)
            self.computer_transport = ComputerTransport(self)
            self.snapshots = EnvironmentSnapshots(
                self,
                (snapshots or self.root / "snapshots").resolve(),
                keep=snapshot_keep,
                interval_hours=snapshot_interval_hours,
            )
        except BaseException:
            self.db.close()
            raise

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
                # An open stream is someone looking at the screen: no idle sleep meanwhile.
                self.desktop.streams += 1
                self.desktop.touch()
                try:
                    await self.computer_transport.display_stream(reader, writer)
                finally:
                    self.desktop.streams -= 1
                    self.desktop.touch()
                return
            if request.get("method") == "files_read":
                await self.computer_transport.files_read(request["args"]["path"], writer)
                return
            if request.get("method") == "workspace_write":
                await self.workspace_write(request, reader, writer)
                return
            if request.get("method") == "control_watch":
                await self.computer.watch(reader, writer)
                return
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
            "swap_mib": int(self.setting("resource_swap_mib", "128")),
            "pids": int(self.setting("resource_pids", "128")),
            "tmp_mib": int(self.setting("resource_tmp_mib", "128")),
        }
        if shm := self.setting("resource_shm_mib"):
            limits["shm_mib"] = int(shm)
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
        if self.legacy_acp.kind(identity) in ACP_KINDS:
            return self.legacy_acp.view(identity)
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
        labels = value.get("Config", {}).get("Labels") or {}
        # One sandbox per Docker engine: another deployment's environment is never adopted.
        if labels.get("io.yuki.sandbox") != "persistent-v1" or (
            labels.get(PROJECT_LABEL, "") != self.project
        ):
            raise RuntimeError("environment_name_in_use")
        return cast(dict[str, Any], value)

    def claim(self) -> None:
        """Own the shared state volume before writing anything to it or to the code volume.

        The volumes have fixed names, so a second Compose project on the same Docker engine
        mounts the same state. It is refused here, before it can change settings or files.
        """
        if self.setting("project", self.project) != self.project:
            raise RuntimeError("environment_name_in_use")
        self.set_setting("project", self.project)

    def mount(
        self, volume: str, destination: str, *, subpath: str = "", readonly: bool = False
    ) -> str:
        value = f"type=volume,src={self.volumes[volume]},dst={destination}"
        if subpath:
            value += f",volume-subpath={subpath}"
        return value + (",readonly" if readonly else "")

    def publish_environment_files(self) -> None:
        source = Path(
            os.environ.get("YUKI_ENVIRONMENT_SOURCE") or Path(__file__).resolve().parents[3]
        )
        for destination, relative in ENVIRONMENT_FILES.items():
            target = self.code_root / destination.lstrip("/")
            target.parent.mkdir(parents=True, exist_ok=True)
            data = (source / relative).read_bytes()
            # Rewrite in place: a single-file mount follows the inode it was created with,
            # so a running environment sees the new release without being recreated.
            with open(target, "ab") as stream:
                stream.seek(0)
                stream.truncate()
                stream.write(data)
            target.chmod(0o755)

    async def check_runtime(self) -> None:
        code, raw = await self.command("docker", "info", "--format", "{{json .Runtimes}}")
        runtimes = json.loads(raw) if not code else {}
        if self.runtime not in runtimes:
            raise RuntimeError(
                "gvisor_unavailable" if self.runtime == "runsc" else "runtime_unavailable"
            )
        # Without it gVisor keeps rootFS writes in its own overlay, outside the Docker
        # layer, and a committed checkpoint would lose installed software.
        if self.runtime == "runsc" and "--overlay2=none" not in runtimes["runsc"].get(
            "runtimeArgs", ()
        ):
            raise RuntimeError("gvisor_overlay_unsupported")

    async def ensure_network(self) -> None:
        code, _ = await self.command("docker", "network", "inspect", self.network)
        if code:
            code, _ = await self.command(
                "docker",
                "network",
                "create",
                "--driver",
                "bridge",
                "--opt",
                f"com.docker.network.bridge.name={network_guard.BRIDGE}",
                self.network,
            )
            if code:
                raise RuntimeError("environment_network_missing")
        # The Manager must share the host network namespace, or the rules below would
        # filter only its own namespace.
        if not await asyncio.to_thread(Path(f"/sys/class/net/{network_guard.BRIDGE}").exists):
            raise RuntimeError("network_guard_missing")
        await network_guard.apply(self.command)

    async def read_manifest(self) -> list[dict[str, str]]:
        code, raw = await self.command(
            "docker", "exec", self.name, "cat", "/opt/yuki-runtime/manifest.json"
        )
        return list(json.loads(raw)["tools"]) if not code else []

    async def apply_programs(self) -> None:
        """Install what the configured mode needs; report capabilities from what is installed."""
        tools = await self.read_manifest()
        missing = [
            package
            for package in wanted(self.browser_mode, self.packages)
            if package not in {tool["name"] for tool in tools}
        ]
        if missing:
            code, output = await self.command(
                "docker",
                "exec",
                "--user",
                "0:0",
                self.name,
                "/usr/local/bin/python",
                "/opt/yuki-runtime/environment_programs.py",
                self.browser_mode,
                *self.packages,
                deadline_seconds=3600,
            )
            self.programs = {"missing": missing, "installed": not code}
            if code:
                self.programs["output"] = output.decode(errors="replace")[-4096:]
            tools = await self.read_manifest()
        installed = {tool["name"] for tool in tools}
        mode = self.browser_mode
        if mode == "headed" and not set(MODE_PACKAGES["headed"]) <= installed:
            mode = "headless"
        if mode == "headless" and not set(MODE_PACKAGES["headless"]) <= installed:
            mode = "none"
        self.desktop_manifest = {"tools": tools, "desktop": mode == "headed", "browser_mode": mode}
        await asyncio.to_thread(
            FileWorkspace(self.runtime_root).write, "computer/browser-mode", mode.encode()
        )

    async def apply_limits(self, current: dict[str, Any]) -> dict[str, Any]:
        """Apply CPU, memory and process limits to the running container in place."""
        limits = self.resource_limits()
        config = current.get("HostConfig", {})
        wanted_limits = {
            "Memory": int(limits["memory_mib"]) * MIB,
            "MemorySwap": (int(limits["memory_mib"]) + int(limits["swap_mib"])) * MIB,
            "NanoCpus": round(float(limits["cpus"]) * 1e9),
            "PidsLimit": int(limits["pids"]),
            "CpuShares": int(limits["cpu_shares"]),
        }
        self.limits_error = ""
        if any(config.get(key) != value for key, value in wanted_limits.items()):
            code, output = await self.command(
                "docker",
                "update",
                "--memory",
                f"{limits['memory_mib']}m",
                "--memory-swap",
                f"{int(limits['memory_mib']) + int(limits['swap_mib'])}m",
                "--cpus",
                str(limits["cpus"]),
                "--cpu-shares",
                str(limits["cpu_shares"]),
                "--pids-limit",
                str(limits["pids"]),
                self.name,
            )
            if code:
                self.limits_error = output.decode(errors="replace")[-2048:]
            current = await self.inspect_container() or current
        return current

    def recreate_required(self) -> list[str]:
        """Limits Docker only sets at creation, where the container differs from configuration."""
        limits, observed = self.resource_limits(), self.observed_container
        differs = []
        if observed.get("tmpfs") != (f"rw,nosuid,nodev,size={limits['tmp_mib']}m,mode=1777"):
            differs.append("tmp_mib")
        if "shm_mib" in limits and observed.get("shm_bytes") != int(limits["shm_mib"]) * MIB:
            differs.append("shm_mib")
        return differs

    async def recover(self) -> None:
        self.ready = False
        for relative in ("", "workspace", ".local", ".cache", "inputs", "work", "work/outputs"):
            directory = self.home / relative
            if directory.is_symlink():
                raise RuntimeError("unsafe_environment_directory")
            directory.mkdir(mode=0o755, parents=True, exist_ok=True)
            if os.geteuid() == 0:
                os.chown(directory, 10001, 10001)
        await asyncio.to_thread(self.store.cleanup)
        await asyncio.to_thread(self.publish_environment_files)
        if not self.testing:
            await self.check_runtime()
            await self.ensure_network()
        current = await self.snapshots.before_recover(await self.inspect_container())
        token = self.setting("execd_token")
        if not token:
            if current is not None:
                # The environment holds a token this Manager no longer has: never mint a
                # new one over it, and never adopt it without its receipts.
                raise RuntimeError("manager_state_missing")
            token = secrets.token_urlsafe(32)
            self.set_setting("execd_token", token)
        created = current is None
        if current is None:
            image = self.setting("checkpoint_image", self.image)
            limits = self.resource_limits()
            args = [
                "docker",
                "create",
                "--name",
                self.name,
                "--hostname",
                self.name,
                "--label",
                LABEL,
                "--label",
                f"{PROJECT_LABEL}={self.project}",
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
                self.mount("home", "/home/yuki"),
                "--mount",
                self.mount("runtime", "/var/lib/yuki-runtime"),
                "--env",
                f"EXECD_ACCESS_TOKEN={token}",
            ]
            for destination in ENVIRONMENT_FILES:
                args.extend(
                    (
                        "--mount",
                        self.mount(
                            "code", destination, subpath=destination.lstrip("/"), readonly=True
                        ),
                    )
                )
            if "shm_mib" in limits:
                args.extend(("--shm-size", f"{limits['shm_mib']}m"))
            if self.resolv_conf is not None:
                args.extend(
                    ("--mount", self.mount("home", "/etc/resolv.conf", subpath=self.resolv_conf))
                )
            if self.media_cache is not None:
                args.extend(
                    (
                        "--mount",
                        self.mount("media", "/home/yuki/inputs/media-cache", readonly=True),
                    )
                )
            # Rootfs snapshots retain their old container configuration. Override
            # its former user/proxy defaults without discarding installed files.
            for value in ("HOME=/home/yuki", "USER=root", "LOGNAME=root"):
                args.extend(("--env", value))
            for variable in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                args.extend(("--env", f"{variable}={self.proxy}"))
            args.append(image)
            # docker create pulls a missing image first; that can take minutes.
            code, _ = await self.command(*args, deadline_seconds=1800)
            if code:
                raise RuntimeError("environment_create_failed")
            self.snapshots.created_from(image)
            current = await self.inspect_container()
        assert current is not None
        self.media_cache_shared = self.media_cache is not None and any(
            mount.get("Destination") == "/home/yuki/inputs/media-cache"
            and mount.get("Name") == self.volumes["media"]
            and mount.get("RW") is False
            for mount in current.get("Mounts", ())
        )
        # The Manager starts the environment after its own checks, not Docker.
        if current.get("HostConfig", {}).get("RestartPolicy", {}).get("Name") != "no":
            await self.command("docker", "update", "--restart=no", self.name)
        if not current["State"]["Running"]:
            code, _ = await self.command("docker", "start", self.name)
            if code:
                raise RuntimeError("environment_start_failed")
            current = await self.inspect_container()
            assert current is not None
        if not self.testing:
            current = await self.apply_limits(current)
        config = current.get("HostConfig", {})
        self.observed_container = {
            "image": current.get("Image"),
            "image_reference": current.get("Config", {}).get("Image"),
            "runtime": config.get("Runtime"),
            "memory_bytes": config.get("Memory"),
            "memory_and_swap_bytes": config.get("MemorySwap"),
            "nano_cpus": config.get("NanoCpus"),
            "pids": config.get("PidsLimit"),
            "tmpfs": (config.get("Tmpfs") or {}).get("/tmp"),
            "shm_bytes": config.get("ShmSize"),
        }
        self.container_id = current["Id"] + ":" + current["State"]["StartedAt"]
        if not self.testing:
            await self.apply_programs()
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
        mode = self.desktop_manifest.get("browser_mode", "none")
        if not self.testing and self.setting("desktop_mode", mode) != mode:
            # A running desktop service keeps the mode it started with. Cancelling the
            # run lets its restart policy start a new one in the new mode.
            row = self.db.execute(
                "SELECT run_id FROM environment_services WHERE name='yuki-desktop'"
            ).fetchone()
            if row and row["run_id"] and self.get(row["run_id"]).get("pending"):
                await self.control(row["run_id"], "cancel")
        self.set_setting("desktop_mode", mode)
        self.ready = True
        await self.snapshots.after_recover(created=created)

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
            "env": {
                key: value
                for key, value in args.get("env", {}).items()
                if key in {"YUKI_WORK_ID", "YUKI_STEP_LABEL"} and isinstance(value, str)
            },
        }
        if kind == "service":
            spec["timeout_seconds"] = 0
            spec["env"] = {}
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

    async def submit(
        self, method: str, args: dict[str, Any], request_id: str, *, service_name: str | None = None
    ) -> dict[str, Any]:
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
            if service_name is not None:
                self.db.execute(
                    "UPDATE environment_services SET run_id=?,attempts=attempts+1,next_start=? "
                    "WHERE name=?",
                    (identity, time.time() + 5, service_name),
                )
        self.queue.put_nowait(identity)
        return self.get(identity) if method == "service" else await self.wait_result(identity)

    def cancel_process(self, identity: str) -> None:
        directory = FileWorkspace(self.runtime_root)
        directory.write(f"{identity}/state/cancel", b"cancel")

    async def control(self, identity: str, action: str, text: str = "") -> dict[str, Any]:
        identity = identifier(identity)
        if self.legacy_acp.kind(identity) == "acp_prompt":
            if action not in {"cancel", "close", "interrupt"}:
                raise ValueError("acp_prompt_requires_permission_reply")
            return self.legacy_acp.control(
                "cancel", {"run_id": identity}, f"prompt-cancel:{identity}"
            )
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
                result: dict[str, Any] = {}
                if method == "workspace_media_read":
                    before = os.fstat(fd)
                    if before.st_size > limit:
                        raise WorkspaceError("workspace_media_too_large")
                    chunks = []
                    remaining = limit + 1
                    while remaining:
                        chunk = os.read(fd, min(65536, remaining))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        remaining -= len(chunk)
                    data = b"".join(chunks)
                    info = os.fstat(fd)
                    if len(data) > limit:
                        raise WorkspaceError("workspace_media_too_large")
                    if len(data) != info.st_size or (
                        before.st_size,
                        before.st_mtime_ns,
                        before.st_ctime_ns,
                    ) != (info.st_size, info.st_mtime_ns, info.st_ctime_ns):
                        raise WorkspaceError("file_changed_during_read")
                    current = hashlib.sha256(data).hexdigest()
                else:
                    current, info = files.fingerprint(fd)
                if version is not None and current != version:
                    raise WorkspaceError("version_conflict")
                if method == "workspace_media_read":
                    result["base64"] = base64.b64encode(data).decode("ascii")
                result.update(
                    path=prefix + "/".join(files.parts(path)),
                    version=current,
                    size=info.st_size,
                )
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
        if method == "workspace_copy":
            files, relative, _prefix = self._media_source(path)
            with files.open_file(relative) as fd:
                return self.files.copy(args["destination"], fd, expected_version=version)
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
        if method == "workspace_copy_result":
            existing = self._file_requests.get(request_id)
            if existing is not None and not existing[1].done():
                await self._wait_file(existing[1])
            async with self.file_lock:
                row = self.db.execute(
                    "SELECT result FROM environment_mutations WHERE request_id=?", (request_id,)
                ).fetchone()
                if row is None:
                    return {"found": False}
                stored = json.loads(row["result"])
                if (stored.get("source_key"), stored.get("destination")) != (
                    args["source_key"],
                    args["destination"],
                ):
                    return {"error": "idempotency_conflict"}
                result = stored["result"]
                if result.get("error") == "file_mutation_unknown_read_current_version":
                    result["uncertain"] = True
                return {
                    "found": True,
                    "result": {**result, "external_untrusted": True},
                    "external_untrusted": True,
                }
        if method not in {"workspace_publish", "workspace_copy"} or not isinstance(request_id, str):
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

    async def workspace_write(
        self,
        request: dict[str, Any],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Write ``size`` streamed bytes of a known sha256 into one workspace file.

        The request ID is the original tool call. Its receipt is durable as unknown
        before the first byte is read, so a restart mid-transfer stays unknown and
        an earlier receipt is answered without reading the stream again.
        """
        args, request_id = request["args"], request["request_id"]
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("invalid_request_id")
        existing = self._file_requests.get(request_id)
        if existing is not None and not existing[1].done():
            await self._wait_file(existing[1])
        digest = hashlib.sha256(
            json.dumps(["workspace_write", args], sort_keys=True).encode()
        ).hexdigest()
        async with self.file_lock:
            row = self.db.execute(
                "SELECT * FROM environment_mutations WHERE request_id=?", (request_id,)
            ).fetchone()
            if row is None:
                with self.db:
                    self.db.execute(
                        "INSERT INTO environment_mutations VALUES (?,?,?)",
                        (
                            request_id,
                            digest,
                            json.dumps(
                                {
                                    "source_key": args["source_key"],
                                    "destination": args["destination"],
                                    "result": {
                                        "error": "file_mutation_unknown_read_current_version",
                                        "retryable": False,
                                        "uncertain": True,
                                    },
                                }
                            ),
                        ),
                    )
        if row is not None:
            result = (
                {"error": "idempotency_conflict"}
                if row["digest"] != digest
                else json.loads(row["result"])["result"]
            )
            if result.get("error") == "file_mutation_unknown_read_current_version":
                result["uncertain"] = True
            writer.write(json.dumps({**result, "external_untrusted": True}).encode() + b"\n")
            await writer.drain()
            return
        writer.write(b'{"ready":true}\n')
        await writer.drain()
        task = self._start_file(self._write_stream(args, request_id, reader))
        self._file_requests[request_id] = digest, task
        task.add_done_callback(lambda _: self._file_requests.pop(request_id, None))
        result = await self._wait_file(task)
        writer.write(json.dumps(result).encode() + b"\n")
        await writer.drain()

    async def _write_stream(
        self, args: dict[str, Any], request_id: str, reader: asyncio.StreamReader
    ) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        unread = [int(args["size"])]

        def chunks() -> Iterator[bytes]:
            while unread[0]:
                data = asyncio.run_coroutine_threadsafe(
                    reader.read(min(65536, unread[0])), loop
                ).result()
                if not data:
                    raise WorkspaceError("source_stream_incomplete")
                unread[0] -= len(data)
                yield data

        try:
            # The target hash is the declared source hash; a mismatch never replaces it.
            result = await asyncio.to_thread(
                self.files._write,
                args["destination"],
                chunks(),
                expected_version=args["sha256"],
            )
        except (WorkspaceError, OSError, ValueError) as exc:
            result = {"error": str(exc) if isinstance(exc, WorkspaceError) else type(exc).__name__}
            if isinstance(exc, OSError) and exc.errno in {28, 122}:
                result = {"error": "workspace_full", **self.disk_status()}
        async with self.file_lock:
            with self.db:
                self.db.execute(
                    "UPDATE environment_mutations SET result=? WHERE request_id=?",
                    (
                        json.dumps(
                            {
                                "source_key": args["source_key"],
                                "destination": args["destination"],
                                "result": result,
                            }
                        ),
                        request_id,
                    ),
                )
        # After an early failure the sender still pushes its declared bytes; take
        # them so it reads this answer instead of a broken pipe.
        while unread[0] and (data := await reader.read(min(65536, unread[0]))):
            unread[0] -= len(data)
        return {**result, "external_untrusted": True}

    async def _file_request(
        self, method: str, args: dict[str, Any], request_id: str
    ) -> dict[str, Any]:
        if method not in {
            "workspace_media_read",
            "workspace_media_validate",
            "workspace_publish",
            "workspace_copy",
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
                if not isinstance(request_id, str) or not request_id:
                    raise ValueError("invalid_request_id")
                row = self.db.execute(
                    "SELECT * FROM environment_mutations WHERE request_id=?", (request_id,)
                ).fetchone()
                if row:
                    if row["digest"] != digest:
                        return {"error": "idempotency_conflict"}
                    result = json.loads(row["result"])
                    if method == "workspace_copy":
                        result = result["result"]
                    if result.get("error") == "file_mutation_unknown_read_current_version":
                        result["uncertain"] = True
                    return {**result, "external_untrusted": True}
            if not read:
                pending = {
                    "error": "file_mutation_unknown_read_current_version",
                    "retryable": False,
                    "uncertain": True,
                }
                if method == "workspace_copy":
                    pending = {
                        "source_key": args["source_key"],
                        "destination": args["destination"],
                        "result": pending,
                    }
                with self.db:
                    self.db.execute(
                        "INSERT INTO environment_mutations VALUES (?,?,?)",
                        (
                            request_id,
                            digest,
                            json.dumps(pending),
                        ),
                    )
        try:
            result = await asyncio.to_thread(self.file_operation, method, args)
        except (WorkspaceError, OSError, ValueError, KeyError) as exc:
            result = {"error": str(exc) if isinstance(exc, WorkspaceError) else type(exc).__name__}
            if isinstance(exc, OSError) and exc.errno in {28, 122}:
                result = {"error": "workspace_full", **self.disk_status()}
        if not read:
            stored = result
            if method == "workspace_copy":
                stored = {
                    "source_key": args["source_key"],
                    "destination": args["destination"],
                    "result": result,
                }
            async with self.file_lock:
                with self.db:
                    self.db.execute(
                        "UPDATE environment_mutations SET result=? WHERE request_id=?",
                        (json.dumps(stored), request_id),
                    )
        return {**result, "external_untrusted": True}

    def disk_status(self) -> dict[str, Any]:
        # HOME is a Docker volume: these are the totals of the disk it shares with Docker
        # and the Bot's data, not a quota of its own.
        usage = shutil.disk_usage(self.home)
        return {
            "shared_disk": True,
            "capacity_bytes": usage.total,
            "used_bytes": usage.used,
            "available_bytes": usage.free,
        }

    def environment_status(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "generation": self.container_id or None,
            **self.desktop_manifest,
            "home": "/home/yuki",
            "workspace": "/workspace",
            "media_cache": {
                "path": "/inputs/media-cache",
                "configured": self.media_cache is not None,
                "shared": self.media_cache_shared,
            },
            "storage": self.disk_status(),
            "runtime_storage_bytes": shutil.disk_usage(self.runtime_root).total,
            "profile": self.profile or None,
            "runtime": self.runtime,
            "configured_browser_mode": self.browser_mode,
            "programs": self.programs,
            "configured_image": self.image,
            "container_at_recovery": self.observed_container,
            "limits_update_error": self.limits_error or None,
            "recreate_required": self.recreate_required() if self.observed_container else [],
            "browser": self.desktop.browser_status(),
            "checkpoint": self.setting("checkpoint_image", self.image),
            **self.snapshots.status(),
            **self.desktop.idle_status(),
            "host_available_bytes": self.available_memory(),
            "limits": {
                **self.resource_limits(),
            },
            "active_runs": [
                dict(row)
                for row in self.db.execute(
                    "SELECT jobs.id AS run_id,jobs.state AS status,kind,created FROM jobs "
                    "JOIN environment_jobs USING(id) WHERE jobs.state IN ('queued','running') "
                    "AND kind='terminal_exec' ORDER BY created"
                )
            ],
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

    async def autostart(self) -> dict[str, Any]:
        """Run /etc/rc.local once per environment generation: the key names the generation."""
        return await self.submit(
            "service", {"command": AUTOSTART, "cwd": "/"}, f"autostart:{self.container_id}"
        )

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
            await self.submit("service", spec, f"service:{uuid4()}", service_name=row["name"])

    async def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        method, args = request.get("method"), request.get("args")
        if not isinstance(method, str) or not isinstance(args, dict):
            return {"error": "invalid_arguments"}
        try:
            if method == "files_list":
                return await asyncio.to_thread(self.computer_transport.files_list, args)
            if method in {"display_status", "display_wake", "display_sleep", "tabs_list"}:
                return await self.desktop.handle(method, args, request.get("request_id", ""))
            if method in {"computer_action", "browser_action"}:
                return await self.desktop.action(method, args, request.get("request_id", ""))
            if method == "control_get":
                return self.computer.snapshot()
            if method == "control_set":
                return self.computer.set(request.get("request_id", ""), args)
            if method == "control_request_get":
                return self.computer.request_get(args.get("key", ""))
            if method == "sticker_index":
                return await self.sticker_index(args)
            if method.startswith("acp_"):
                return await self.legacy_acp.handle(method, args, request.get("request_id", ""))
            if method in EXECUTION_TOOLS:
                return await self.submit(method, dict(args), request.get("request_id", ""))
            if method in {
                "workspace_media_read",
                "workspace_media_validate",
                "workspace_publish",
                "workspace_copy",
                "workspace_copy_result",
            }:
                return await self.file_request(method, args, request.get("request_id", ""))
            if method == "environment_status":
                return self.environment_status()
            if method == "environment_snapshot":
                # The person's Desktop command: accepted here, progress via computer_revision.
                return self.snapshots.request_snapshot(
                    request.get("request_id", ""), "person", args.get("actor"), args.get("note")
                )
            if method == "environment_restore":
                return self.snapshots.request_restore(
                    request.get("request_id", ""), args.get("snapshot_id"), args.get("actor")
                )
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
                if self.legacy_acp.kind(identity) == "acp_prompt":
                    self.legacy_acp.refresh()
                    if wait_seconds:
                        await self.wait_result(identity, wait_seconds=wait_seconds)
                    return self.legacy_acp.query(identity, args.get("cursor"))
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
                # Snapshots and restores run here, so no terminal run is dispatched meanwhile.
                await self.snapshots.step()
                await self.desktop.idle_sleep()
            except Exception as exc:
                LOGGER.warning("environment_lifecycle_pending category=%s", type(exc).__name__)
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
                # control_watch subscribers learn display changes from this observation.
                self.desktop.observe_display()
            except (OSError, ValueError, WorkspaceError):
                LOGGER.warning("computer_wait_reconcile_pending")
            try:
                self.legacy_acp.refresh()
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
                if not self.testing:
                    await self.autostart()
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
