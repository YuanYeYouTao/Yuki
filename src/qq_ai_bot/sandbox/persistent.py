"""Trusted lifecycle owner for one persistent gVisor environment.

The Bot has a narrow JSON socket, never Docker access. Job launch intent and
execd session identity are durable before launch. Reconnection observes existing
supervisor records; it never replays a dispatched command.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import shlex
import shutil
import time
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4, uuid5

from qq_ai_bot.sandbox.environment_tools import EXECUTION_TOOLS
from qq_ai_bot.sandbox.execd import Execd
from qq_ai_bot.sandbox.manager import TERMINAL, Manager, identifier
from qq_ai_bot.workspace.files import FileWorkspace
from qq_ai_bot.workspace.store import WorkspaceError, WorkspaceStore

LABEL = "io.yuki.sandbox=persistent-v1"
CONTAINER = "yuki-environment"
SEGMENT = 4 * 1024 * 1024
PACKAGE_BUDGET = 1024 * 1024 * 1024
LOG_BUDGET = 128 * 1024 * 1024
LOGGER = logging.getLogger(__name__)


class PersistentManager(Manager):
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
        testing: bool = False,
        name: str = "yuki-environment",
    ) -> None:
        super().__init__(root, store, image, network, proxy)
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
        self.launch_lock = asyncio.Lock()
        self.last_prune = 0.0
        self.layer_bytes = 0
        self.last_layer_check = 0.0
        self.base_image_size = int(self.setting("base_image_size", "0"))

    def setting(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM environment_state WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO environment_state VALUES (?,?)", (key, value))

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
        changed = super().finish(identity, state, result)
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
        if not self.base_image_size:
            code, size = await self.command(
                "docker", "image", "inspect", "--format", "{{.Size}}", self.image
            )
            if code:
                raise RuntimeError("environment_image_missing")
            self.base_image_size = int(size)
            self.set_setting("base_image_size", str(self.base_image_size))
        if not self.testing:
            code, runtimes = await self.command("docker", "info", "--format", "{{json .Runtimes}}")
            if code or self.runtime not in json.loads(runtimes):
                raise RuntimeError("gvisor_unavailable")
            code, network = await self.command("docker", "network", "inspect", self.network)
            if code or not json.loads(network)[0].get("Internal"):
                raise RuntimeError("private_network_required")
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
                "10001:10001",
                "--memory",
                "512m",
                "--memory-swap",
                "640m",
                "--cpus",
                "1",
                "--pids-limit",
                "128",
                "--restart",
                "unless-stopped",
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
                "--env",
                f"EXECD_ACCESS_TOKEN={token}",
            ]
            for variable in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                args.extend(("--env", f"{variable}={self.proxy}"))
            args.append(image)
            code, _ = await self.command(*args)
            if code:
                raise RuntimeError("environment_create_failed")
            current = await self.inspect_container()
        assert current is not None
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
            if not row["dispatched"]:
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
            if row["kind"] == "environment_packages" and record["status"] == "succeeded":
                await self.checkpoint(identity, result)
            if row["kind"] == "run_python" and record["status"] == "succeeded":
                try:
                    result["artifacts"] = await asyncio.to_thread(self.export_python, identity)
                except (WorkspaceError, OSError) as exc:
                    result["artifact_error"] = (
                        str(exc) if isinstance(exc, WorkspaceError) else type(exc).__name__
                    )
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
            # apt is launched by docker exec; its durable heartbeat is the only receipt.
            if (
                row["kind"] == "environment_packages"
                and time.time() - float(record.get("heartbeat", row["created"])) < 15
            ):
                return
            self.finish(identity, "failed", {"error": "execution_interrupted", "interrupted": True})
            return
        if identity in self.connections:
            return
        assert self.execd is not None
        state = await self.execd.request("GET", f"/pty/{row['session_id']}")
        if state.get("running"):
            await self.attach(
                identity, row["session_id"], tty=bool(json.loads(row["payload"]).get("tty"))
            )
        elif time.time() - row["created"] > 10:
            self.finish(identity, "failed", {"error": "execution_interrupted", "interrupted": True})

    def prepare_spec(self, identity: str, args: dict[str, Any], kind: str) -> dict[str, Any]:
        root = self.runtime_root / identifier(identity)
        root.mkdir(mode=0o755)
        root.chmod(0o755)
        state = root / "state"
        state.mkdir(mode=0o700)
        if os.geteuid() == 0:
            os.chown(
                state,
                0 if kind == "environment_packages" else 10001,
                0 if kind == "environment_packages" else 10001,
            )
        spec = {
            "cwd": args.get("cwd", "/workspace"),
            "command": args.get("command", ""),
            "tty": bool(args.get("tty")),
            "timeout_seconds": args.get("timeout_seconds", 1800),
        }
        if kind == "service":
            spec["timeout_seconds"] = 0
        if kind == "run_python":
            (root / "code.py").write_text(args["code"], encoding="utf-8")
            (root / "code.py").chmod(0o444)
            spec.update(cwd="/work", argv=["python", f"/var/lib/yuki-runtime/{identity}/code.py"])
            # Compatibility imports remain files in the same persistent environment.
            inputs = FileWorkspace(self.home / "inputs")
            for artifact_id in args.get("input_artifact_ids", []):
                _, content = self.store.read_bytes(identifier(artifact_id))
                try:
                    old = inputs.read(artifact_id)["version"]
                except FileNotFoundError:
                    old = None
                inputs.write(artifact_id, content, expected_version=old)
            outputs = FileWorkspace(self.home / "work" / "outputs")
            baseline = {
                item["path"]: outputs.read(item["path"])["version"]
                for item in outputs.listing(limit=100)["items"]
                if item["kind"] == "file"
            }
            (root / "outputs-before.json").write_text(json.dumps(baseline))
        if kind == "environment_packages":
            action = args["action"]
            packages = " ".join(shlex.quote(item) for item in args.get("packages", []))
            command = (
                "dpkg --configure -a && apt-get -f install -y"
                if action == "repair"
                else f"apt-get update -qq && apt-get {action} -y --no-install-recommends {packages}"
            )
            spec.update(
                capture=True,
                tty=False,
                timeout_seconds=1800,
                command="export DEBIAN_FRONTEND=noninteractive; "
                + command
                + " && apt-get clean && rm -rf /var/lib/apt/lists/*",
            )
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
        session = None
        if row["kind"] != "environment_packages":
            response = await self.execd.request(
                "POST",
                "/pty",
                {
                    "cwd": spec["cwd"],
                    "command": f"exec python /opt/yuki-runtime/supervisor.py {identity}",
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
        if session:
            await self.attach(identity, session, tty=bool(spec["tty"]))
        else:
            code, _ = await self.command(
                "docker",
                "exec",
                "-d",
                "-u",
                "0:0",
                self.name,
                "python",
                "/opt/yuki-runtime/supervisor.py",
                identity,
            )
            if code:
                raise RuntimeError("package_dispatch_unknown")

    def export_python(self, identity: str) -> list[dict[str, Any]]:
        root = self.runtime_root / identity
        completed = root / "published.json"
        if completed.exists():
            return cast(list[dict[str, Any]], json.loads(completed.read_text()))
        baseline = json.loads((root / "outputs-before.json").read_text())
        output = FileWorkspace(self.home / "work" / "outputs")
        artifacts: list[dict[str, Any]] = []
        page = output.listing(limit=100)
        for item in page["items"]:
            if item["kind"] != "file":
                continue
            path = item["path"]
            with output.open_file(path) as fd:
                version, _ = output.fingerprint(fd)
                if baseline.get(path) == version:
                    continue
                if len(artifacts) >= 20:
                    raise WorkspaceError("output_limit_use_workspace_publish")
                artifacts.append(
                    self.store.snapshot(
                        fd, Path(path).name, artifact_id=str(uuid5(UUID(identity), path))
                    )
                )
        completed.write_text(json.dumps(artifacts))
        return artifacts

    async def checkpoint(self, identity: str, result: dict[str, Any]) -> None:
        marker = f"checkpoint:{identity}"
        if self.setting(marker):
            result["checkpoint"] = self.setting(marker)
            return
        image = f"yuki-environment-checkpoint:{identity}"
        code, _ = await self.command(
            "docker", "commit", "--pause=false", self.name, image, deadline_seconds=120
        )
        if code:
            result["checkpoint_error"] = "checkpoint_failed_environment_retained"
            return
        old = self.setting("checkpoint_image")
        obsolete = self.setting("previous_checkpoint_image")
        self.set_setting("previous_checkpoint_image", old or self.image)
        self.set_setting("checkpoint_image", image)
        self.set_setting(marker, image)
        code, versions = await self.command(
            "docker",
            "exec",
            "-u",
            "0:0",
            self.name,
            "dpkg-query",
            "-W",
            "-f=${Package}\t${Version}\n",
        )
        if not code:
            (self.root / f"packages-{identity}.txt").write_bytes(versions)
        result["checkpoint"] = image
        if (
            obsolete
            and obsolete not in {old, self.image, image}
            and obsolete.startswith("yuki-environment-checkpoint:")
        ):
            await self.command("docker", "image", "rm", obsolete)

    def validate_execution(self, method: str, args: dict[str, Any]) -> None:
        if method == "run_python":
            if not isinstance(args.get("code"), str) or len(args["code"].encode()) > 65536:
                raise ValueError("invalid_code")
            inputs = args.get("input_artifact_ids", [])
            if not isinstance(inputs, list) or len(inputs) > 20:
                raise ValueError("invalid_inputs")
            for item in inputs:
                identifier(item)
            args.setdefault("timeout_seconds", 30)
        elif method == "environment_packages":
            if args.get("action") not in {"install", "remove", "repair"}:
                raise ValueError("invalid_package_action")
            packages = args.get("packages", [])
            if (
                not isinstance(packages, list)
                or len(packages) > 30
                or (not packages and args["action"] != "repair")
                or any(
                    not isinstance(p, str)
                    or not re.fullmatch(
                        r"[a-z0-9][a-z0-9+.-]*(?::amd64)?(?:=[A-Za-z0-9.+:~_-]+)?", p
                    )
                    for p in packages
                )
            ):
                raise ValueError("invalid_packages")
        else:
            if (
                not isinstance(args.get("command"), str)
                or not 1 <= len(args["command"].encode()) <= 65536
            ):
                raise ValueError("invalid_command")
            if type(args.get("tty", False)) is not bool:
                raise ValueError("invalid_tty")
        cwd = args.get("cwd", "/workspace")
        if not isinstance(cwd, str) or not cwd.startswith("/") or "\x00" in cwd or len(cwd) > 4096:
            raise ValueError("invalid_cwd")
        timeout = args.get("timeout_seconds", 1800)
        if type(timeout) is not int or not 0 <= timeout <= 86400:
            raise ValueError("invalid_timeout")

    async def submit(self, method: str, args: dict[str, Any], request_id: str) -> dict[str, Any]:
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
        if not self.testing and self.available_memory() < 256 * 1024 * 1024:
            return {
                "error": "host_memory_pressure",
                "available_bytes": self.available_memory(),
                "retryable": False,
            }
        active = self.active()
        category = "tty" if method == "terminal_exec" and args.get("tty") else "execution"
        if category == "tty":
            used = sum(
                row["kind"] == "terminal_exec" and bool(json.loads(row["payload"]).get("tty"))
                for row in active
            )
            limit = 4
        else:
            used = sum(
                row["kind"] != "service" and not json.loads(row["payload"]).get("tty")
                for row in active
            )
            limit = 1
        if method != "service" and used >= limit:
            return {
                "error": "environment_busy",
                "active_run_ids": [row["id"] for row in active],
                "retryable": False,
            }
        if (
            method == "environment_packages"
            and args["action"] == "install"
            and (
                self.layer_bytes >= PACKAGE_BUDGET
                or (not self.testing and shutil.disk_usage(self.root).free < 2 * 1024**3)
            )
        ):
            return {
                "error": "package_budget_exhausted",
                "used_bytes": self.layer_bytes,
                "budget_bytes": PACKAGE_BUDGET,
            }
        if self.queue.full() or (method != "service" and not self.completions.reserve_available()):
            return {"error": "completion_backlog_full", "retryable": False}
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
            if not isinstance(text, str) or len(text.encode()) > 8192:
                raise ValueError("invalid_terminal_input")
            await connection.send(json.dumps({"type": "stdin", "data": text}))
        else:
            raise ValueError("invalid_terminal_action")
        return {"run_id": identity, "status": "running", "input_accepted": True, "pending": True}

    def file_operation(self, method: str, args: dict[str, Any]) -> dict[str, Any]:
        path = args.get("path", "")
        version = args.get("expected_version")
        if method == "workspace_list":
            return self.files.listing(
                path, cursor=args.get("cursor", ""), limit=args.get("limit", 50)
            )
        if method == "workspace_read":
            return self.files.read(path, offset=args.get("offset", 0))
        if method == "workspace_write":
            return self.files.write(path, args["text"].encode(), expected_version=version)
        if method == "workspace_mkdir":
            return self.files.mkdir(path)
        if method == "workspace_move":
            return self.files.move(path, args["destination"], expected_version=version)
        if method == "workspace_delete":
            return self.files.delete(path, expected_version=version)
        if method == "workspace_patch":
            return self.files.patch(
                path, args["old_text"], args["new_text"], expected_version=str(version)
            )
        if method == "workspace_search":
            return self.files.search(args["query"], path=path)
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
        read = method in {"workspace_list", "workspace_read", "workspace_search"}
        async with self.file_lock:
            args = dict(args)
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
                        self.files.read(name)
                    except FileNotFoundError:
                        args["path"] = name
                    else:
                        args["path"] = f"imported/{artifact_id}/{name}"
            digest = hashlib.sha256(json.dumps([method, args], sort_keys=True).encode()).hexdigest()
            if not read:
                if not isinstance(request_id, str) or not 1 <= len(request_id) <= 256:
                    raise ValueError("invalid_request_id")
                row = self.db.execute(
                    "SELECT * FROM environment_mutations WHERE request_id=?", (request_id,)
                ).fetchone()
                if row:
                    return (
                        json.loads(row["result"])
                        if row["digest"] == digest
                        else {"error": "idempotency_conflict"}
                    )
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
            "package_layer_bytes": self.layer_bytes,
            "package_budget_bytes": PACKAGE_BUDGET,
            "checkpoint": self.setting("checkpoint_image", self.image),
            "host_available_bytes": self.available_memory(),
            "limits": {
                "memory_mib": 512,
                "swap_mib": 128,
                "cpus": 1,
                "pids": 128,
                "executions": 1,
                "services": 2,
                "terminals": 4,
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
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name):
            raise ValueError("invalid_service_name")
        row = self.db.execute("SELECT * FROM environment_services WHERE name=?", (name,)).fetchone()
        if action == "register":
            self.validate_execution("service", args)
            restart = args.get("restart", "on-failure")
            if restart not in {"always", "on-failure", "never"}:
                raise ValueError("invalid_restart_policy")
            if row and row["enabled"]:
                return {"error": "stop_service_before_update"}
            if (
                not row
                and self.db.execute("SELECT count(*) FROM environment_services").fetchone()[0] >= 2
            ):
                return {"error": "service_limit"}
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
                record = self.status_record(row["run_id"])
                if (
                    time.time() - float(record.get("started_at", time.time())) > 60
                    and row["attempts"] > 1
                ):
                    with self.db:
                        self.db.execute(
                            "UPDATE environment_services SET attempts=1 WHERE name=?",
                            (row["name"],),
                        )
                continue
            if row["next_start"] > time.time():
                continue
            spec = json.loads(row["spec"])
            if row["run_id"] and (
                spec["restart"] == "never"
                or (spec["restart"] == "on-failure" and previous.get("status") == "succeeded")
            ):
                continue
            if row["attempts"] >= 5:
                continue
            result = await self.submit("service", spec, f"service:{row['name']}:{uuid4()}")
            if result.get("run_id"):
                with self.db:
                    self.db.execute(
                        "UPDATE environment_services SET run_id=?,at"
                        "tempts=attempts+1,next_start=? WHERE name=?",
                        (
                            result["run_id"],
                            time.time() + min(300, 2 ** row["attempts"] * 5),
                            row["name"],
                        ),
                    )

    async def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        method, args = request.get("method"), request.get("args")
        if not isinstance(method, str) or not isinstance(args, dict):
            return {"error": "invalid_arguments"}
        try:
            if method in EXECUTION_TOOLS:
                async with self.launch_lock:
                    return await self.submit(method, dict(args), request.get("request_id", ""))
            if method.startswith("workspace_"):
                return await self.file_request(method, args, request.get("request_id", ""))
            if method == "environment_status":
                return self.environment_status()
            if method == "environment_service":
                return await self.service(args)
            if method == "terminal_read":
                completion = self.get(identifier(args.get("run_id")))
                return {
                    **completion,
                    **self.output(identifier(args.get("run_id")), args.get("cursor", 0)),
                    **({"completion": completion} if completion.get("status") in TERMINAL else {}),
                }
            if method in {"terminal_write", "terminal_control", "cancel_code_run"}:
                action = (
                    "input"
                    if method == "terminal_write"
                    else "cancel"
                    if method == "cancel_code_run"
                    else args.get("action", "")
                )
                return await self.control(str(args.get("run_id")), action, args.get("text", ""))
            return await super().handle(request)
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
                current = await self.inspect_container(size=True)
                self.last_layer_check = time.time()
                if current:
                    code, image_size = await self.command(
                        "docker", "image", "inspect", "--format", "{{.Size}}", current["Image"]
                    )
                    self.layer_bytes = (
                        int(current.get("SizeRw", 0))
                        + max(0, int(image_size) - self.base_image_size)
                        if not code
                        else PACKAGE_BUDGET
                    )
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
                if self.layer_bytes > PACKAGE_BUDGET or (
                    not self.testing and shutil.disk_usage(self.root).free < 1024**3
                ):
                    for row in self.active():
                        if (
                            row["kind"] == "environment_packages"
                            and json.loads(row["payload"]).get("action") == "install"
                        ):
                            await self.control(row["id"], "cancel")
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
                        self.files.read(path)
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
                    alias_record = self.files.read(alias)
                except FileNotFoundError:
                    alias_record = self.files.write(alias, content)
                if alias_record["version"] != metadata["sha256"]:
                    raise WorkspaceError("legacy_alias_checksum_failed")
                records.append(
                    {**metadata, "path": f"/workspace/{path}", "legacy_path": f"/workspace/{alias}"}
                )
            cursor = page["next_cursor"]
            if not cursor:
                break
        try:
            version = self.files.read("manifest.json")["version"]
        except FileNotFoundError:
            version = None
        self.files.write("manifest.json", json.dumps(records, ensure_ascii=False).encode(), version)
        return {"migrated": len(records), "verified": True}
