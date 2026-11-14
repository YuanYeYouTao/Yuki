"""Desktop Supervisor integration and screen/browser actions through the environment driver."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import time
from typing import TYPE_CHECKING, Any, cast
from uuid import NAMESPACE_URL, uuid4, uuid5

from yuki.sandbox.computer_keys import release_key
from yuki.sandbox.environment_tools import LIFECYCLE_ACTIONS, READ_ACTIONS
from yuki.sandbox.manager import TERMINAL, identifier
from yuki.workspace.files import FileWorkspace
from yuki.workspace.store import WorkspaceError

if TYPE_CHECKING:
    from yuki.sandbox.persistent import PersistentManager

SERVICE = "yuki-desktop"
ACTION_METHODS = {"computer_action": "computer", "browser_action": "browser"}
DRIVER = "computer/driver"
_DRIVER_KEYS = uuid5(NAMESPACE_URL, "https://yuki.life/computer-driver/v1")
# The driver's own wait for an action to settle (navigation/load, dialog, new tab,
# download); on expiry it reports ``loading: true``. The Manager waits for the
# driver's answer a little longer. These bound a call; they are not success gates.
SETTLE_SECONDS = 15.0
RESULT_SECONDS = 45.0
WAKE_SECONDS = 60.0


class ComputerRuntime:
    def __init__(self, manager: PersistentManager) -> None:
        self.manager = manager
        self.last_display: tuple[str, str] | None = None
        # One environment, one screen and one browser: actions run one at a time.
        self.lock = asyncio.Lock()
        # A short-lived signal held in memory: a Manager restart counts from its start.
        self.last_activity = time.monotonic()
        self.streams = 0

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    def idle_status(self) -> dict[str, Any]:
        status = self._service_status()
        running = status.get("browser") == "running"
        return {
            "display": status["display"],
            "session": self.session() if running else None,
            "idle_sleep_minutes": self.manager.display_idle_minutes,
            "idle_minutes": round((time.monotonic() - self.last_activity) / 60, 1)
            if running
            else None,
            "display_streams": self.streams,
        }

    def session(self) -> str | None:
        """Her desktop session as the desktop service last reported it."""
        try:
            value = self._read_json("computer/desktop-status.json").get("session")
        except (OSError, ValueError, WorkspaceError):
            return None
        return value if isinstance(value, str) else None

    def browser_status(self) -> str:
        return self._service_status().get("browser", "unavailable")

    def display_status(self) -> dict[str, Any]:
        return {"display": self._service_status()["display"]}

    def _service_status(self) -> dict[str, str]:
        headless = self.manager.desktop_manifest.get("browser_mode") == "headless"
        if not self.manager.ready or (
            not self.manager.testing
            and not self.manager.desktop_manifest.get("desktop")
            and not headless
        ):
            return {"display": "unavailable"}
        row = self.manager.db.execute(
            "SELECT enabled,run_id FROM environment_services WHERE name=?", (SERVICE,)
        ).fetchone()
        if row is None or not row["run_id"]:
            return {"display": "unavailable" if headless else "stopped", "browser": "stopped"}
        run = self.manager.get(row["run_id"])
        if run.get("status") in TERMINAL:
            return {
                "display": "unavailable"
                if headless or (row["enabled"] and run["status"] == "failed")
                else "stopped",
                "browser": "unavailable" if run["status"] == "failed" else "stopped",
            }
        try:
            with FileWorkspace(self.manager.runtime_root).open_file(
                "computer/desktop-status.json"
            ) as descriptor:
                with open(descriptor, closefd=False) as stream:
                    status = json.load(stream)
            if status.get("run_id") == row["run_id"] and (
                status.get("browser") == "running" or status.get("display") == "running"
            ):
                return {"display": "unavailable" if headless else "running", "browser": "running"}
        except (OSError, ValueError, WorkspaceError):
            pass
        return {"display": "unavailable" if headless else "starting", "browser": "starting"}

    async def handle(self, method: str, args: dict[str, Any], key: str) -> dict[str, Any]:
        if method == "display_status":
            return self.display_status()
        if method == "tabs_list":
            if self.browser_status() != "running":
                return {"tabs": [], "available": False}
            try:
                tabs = self._read_json(DRIVER + "/tabs.json").get("tabs", [])
            except FileNotFoundError:
                tabs = []
            return {"tabs": tabs}
        if method not in {"display_wake", "display_sleep"}:
            return {"error": "unknown_method"}
        self.touch()
        if not (
            self.manager.testing
            or self.manager.desktop_manifest.get("desktop")
            or self.manager.desktop_manifest.get("browser_mode") == "headless"
        ):
            return {"error": "display_unavailable"}
        identifier(key)
        digest = hashlib.sha256(json.dumps([method, args], sort_keys=True).encode()).hexdigest()
        row = self.manager.db.execute(
            "SELECT digest,result FROM environment_mutations WHERE request_id=?", (key,)
        ).fetchone()
        if row:
            if row["digest"] != digest:
                return {"error": "idempotency_conflict"}
            saved = json.loads(row["result"])
            if not saved.get("pending"):
                return cast(dict[str, Any], saved)
        if method == "display_sleep" and not args.get("confirm_active_work_tabs"):
            control = self.manager.computer.control()
            if control["holder"] == "agent":
                return {"error": "precondition_failed", "control": control}
        if row is None:
            with self.manager.db:
                self.manager.db.execute(
                    "INSERT INTO environment_mutations VALUES (?,?,?)",
                    (key, digest, '{"pending":true}'),
                )
        if method == "display_wake":
            result = await self._start(key)
        else:
            result = await self._stop()
        result = (
            {"error": result["error"]}
            if result.get("error") and result["error"] != "service_not_found"
            else self.display_status()
        )
        with self.manager.db:
            self.manager.db.execute(
                "UPDATE environment_mutations SET result=? WHERE request_id=?",
                (json.dumps(result), key),
            )
        self.observe_display()
        return result

    async def _start(self, key: str) -> dict[str, Any]:
        """Start the desktop service run for one wake key (or report the running one)."""
        registered = self.manager.db.execute(
            "SELECT 1 FROM environment_services WHERE name=?", (SERVICE,)
        ).fetchone()
        if not registered:
            await self.manager.service(
                {
                    "action": "register",
                    "name": SERVICE,
                    "command": ("exec /usr/local/bin/python /opt/yuki-runtime/desktop_service.py"),
                    "restart": "on-failure",
                    "cwd": "/home/yuki",
                }
            )
        original = self.manager.db.execute(
            "SELECT id FROM jobs WHERE request_id=?", ("desktop:" + key,)
        ).fetchone()
        service = self.manager.db.execute(
            "SELECT spec,run_id FROM environment_services WHERE name=?", (SERVICE,)
        ).fetchone()
        assert service is not None
        running = self.manager.get(service["run_id"]) if service["run_id"] else {}
        if original:
            result = self.manager.get(original["id"])
        elif running.get("pending"):
            result = running
        else:
            with self.manager.db:
                self.manager.db.execute(
                    "UPDATE environment_services SET enabled=1,attempts=0,next_start=0 "
                    "WHERE name=?",
                    (SERVICE,),
                )
            result = await self.manager.submit(
                "service", json.loads(service["spec"]), "desktop:" + key, service_name=SERVICE
            )
        return result

    async def _stop(self) -> dict[str, Any]:
        """Stop the screen layer: Xvnc, her session, Chromium and the driver."""
        return await self.manager.service({"action": "stop", "name": SERVICE})

    def observe_display(self) -> None:
        status = self._service_status()
        display = (status["display"], status.get("browser", "unavailable"))
        if display != self.last_display:
            self.last_display = display
            with self.manager.db:
                self.manager.computer._bump()
            self.manager.computer.notify()

    async def action(self, method: str, args: dict[str, Any], key: str) -> dict[str, Any]:
        """One ``computer``/``browser`` call; ``key`` is the original tool call key.

        An input writes a pending receipt before it reaches the driver and its
        result afterwards. A retry of the key returns that result; a key with
        only a pending row reports ``unknown`` and is never driven again.
        Reads write nothing and are simply read again.
        """
        tool = ACTION_METHODS[method]
        if not isinstance(args.get("action"), str) or not isinstance(key, str) or not key:
            raise ValueError("invalid_arguments")
        if tool == "computer" and args["action"] in LIFECYCLE_ACTIONS:
            return await self.lifecycle(args, key)
        try:
            return await self._action(tool, method, args, key)
        finally:
            self.touch()

    async def _action(
        self, tool: str, method: str, args: dict[str, Any], key: str
    ) -> dict[str, Any]:
        read = args["action"] in READ_ACTIONS[tool]
        digest = hashlib.sha256(json.dumps([method, args], sort_keys=True).encode()).hexdigest()
        async with self.lock:
            if not read:
                row = self.manager.db.execute(
                    "SELECT digest,result FROM environment_mutations WHERE request_id=?", (key,)
                ).fetchone()
                if row is not None:
                    if row["digest"] != digest:
                        return {"error": "idempotency_conflict"}
                    saved = cast(dict[str, Any], json.loads(row["result"]))
                    if not saved.get("pending"):
                        return saved
                    # The Manager ended between the pending row and the result.
                    # Withdraw a request the driver never claimed; never run it.
                    self._unlink(f"{DRIVER}/requests/{self._driver_id(key)}.json")
                    return {
                        "error": "action_unknown",
                        "message": "动作可能已经执行，结果未知；先截图或读快照确认，不要重复输入。",
                        "status": "unknown",
                    }
            unavailable = await self._ready(tool, key)
            if unavailable is not None:
                return unavailable
            if read:
                return await self._drive(tool, args, str(uuid4()), None, read=True)
            with self.manager.db:
                self.manager.db.execute(
                    "INSERT INTO environment_mutations VALUES (?,?,?)",
                    (key, digest, '{"pending":true}'),
                )
            lease: str | None = None
            try:
                acquired = self.manager.computer.set(
                    str(uuid5(_DRIVER_KEYS, "acquire:" + key)),
                    {
                        "action": "agent_acquire",
                        "actor": {"request_id": key, "work_id": args.get("owner")},
                    },
                )
                if acquired.get("error"):
                    control = acquired.get("control", {})
                    result: dict[str, Any] = {
                        "error": "control_held",
                        "message": "本人正在操作这台电脑，输入没有执行；等本人交还后再继续。",
                        "control": {
                            name: control[name] for name in ("holder", "since") if name in control
                        },
                    }
                else:
                    lease = acquired["control"]["lease_id"]
                    try:
                        result = await self._drive(
                            tool, args, self._driver_id(key), lease, read=False
                        )
                    except (OSError, ValueError, KeyError, TypeError, WorkspaceError):
                        # The runtime volume belongs to the environment; an unreadable
                        # answer does not tell whether the input happened.
                        result = {
                            "error": "driver_result_unreadable",
                            "message": "动作结果无法读取，可能已经执行；先截图或读快照确认。",
                            "status": "unknown",
                        }
            finally:
                if lease is not None:
                    self.manager.computer.set(
                        release_key(lease),
                        {
                            "action": "agent_release",
                            "actor": {"request_id": key},
                            "lease_id": lease,
                        },
                    )
            receipt = {name: value for name, value in result.items() if name != "image"}
            with self.manager.db:
                self.manager.db.execute(
                    "UPDATE environment_mutations SET result=? WHERE request_id=?",
                    (json.dumps(receipt, ensure_ascii=False), key),
                )
            return result

    def _held(self) -> dict[str, Any] | None:
        control = self.manager.computer.control()
        if control["holder"] != "human":
            return None
        return {
            "error": "control_held",
            "message": "本人正在操作这台电脑，没有执行；等本人交还后再继续。",
            "control": {name: control[name] for name in ("holder", "since") if name in control},
        }

    async def lifecycle(self, args: dict[str, Any], key: str) -> dict[str, Any]:
        """Yuki's own wake, sleep, snapshot and restore, keyed by the original tool call.

        Snapshots and restores run in the Manager worker; the call observes the
        accepted request until it ends, and a retry of the key answers the original
        record (or its current progress) without starting another.
        """
        action = args["action"]
        snapshots = self.manager.snapshots
        if action in {"snapshot", "restore"}:
            existing = snapshots.find(key)
            if existing is None:
                if held := self._held():
                    return held
                actor = {"request_id": key, "work_id": args.get("owner")}
                existing = (
                    snapshots.request_snapshot(key, "yuki", actor, args.get("text"))
                    if action == "snapshot"
                    else snapshots.request_restore(key, args.get("snapshot"), actor)
                )
                if existing.get("error"):
                    return existing
            return await snapshots.wait(key)
        manifest = self.manager.desktop_manifest
        if not (
            self.manager.testing
            or manifest.get("desktop")
            or manifest.get("browser_mode") == "headless"
        ):
            return {"error": "browser_unavailable", "message": "这个环境没有浏览器和画面。"}
        digest = hashlib.sha256(json.dumps(["lifecycle", action]).encode()).hexdigest()
        async with self.lock:
            row = self.manager.db.execute(
                "SELECT digest,result FROM environment_mutations WHERE request_id=?", (key,)
            ).fetchone()
            if row is not None:
                if row["digest"] != digest:
                    return {"error": "idempotency_conflict"}
                saved = cast(dict[str, Any], json.loads(row["result"]))
                if not saved.get("pending"):
                    return saved
                return {
                    "error": "action_unknown",
                    "message": "动作结果未知；先用 get_environment 看画面状态，再决定。",
                    "status": "unknown",
                }
            if action == "sleep" and (held := self._held()):
                result = held
            else:
                with self.manager.db:
                    self.manager.db.execute(
                        "INSERT INTO environment_mutations VALUES (?,?,?)",
                        (key, digest, '{"pending":true}'),
                    )
                result = await (self._sleep() if action == "sleep" else self._wake(key))
            with self.manager.db:
                self.manager.db.execute(
                    "INSERT OR REPLACE INTO environment_mutations VALUES (?,?,?)",
                    (key, digest, json.dumps(result, ensure_ascii=False)),
                )
        self.observe_display()
        return result

    async def _wake(self, key: str) -> dict[str, Any]:
        self.touch()
        if self.browser_status() != "running":
            started = await self._start(str(uuid5(_DRIVER_KEYS, "wake:" + key)))
            if started.get("error"):
                return {"error": started["error"]}
            deadline = time.monotonic() + WAKE_SECONDS
            while self.browser_status() not in {"running", "unavailable"}:
                if time.monotonic() > deadline:
                    break
                await asyncio.sleep(0.1)
        status = self._service_status()
        return {"display": status["display"], "browser": status.get("browser", "unavailable")}

    def _tabs(self) -> list[Any]:
        if self.browser_status() != "running":
            return []
        try:
            return list(self._read_json(DRIVER + "/tabs.json").get("tabs", []))
        except (OSError, ValueError, WorkspaceError):
            return []

    async def _sleep(self) -> dict[str, Any]:
        """Stop the screen layer; the receipt keeps which pages were open, and their Work."""
        tabs = self._tabs()
        stopped = await self._stop()
        if stopped.get("error") and stopped["error"] != "service_not_found":
            return {"error": stopped["error"]}
        # The run ends once its processes have; report what actually stopped. The idle
        # sleep runs inside the worker, so observe the run's own receipt here.
        deadline = time.monotonic() + WAKE_SECONDS
        while self.browser_status() in {"running", "starting"} and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
            for job in self.manager.active():
                if job["kind"] == "service" and job["dispatched"]:
                    await self.manager.reconcile(job)
        status = self._service_status()
        return {
            "display": status["display"],
            "browser": status.get("browser", "unavailable"),
            "tabs": tabs,
        }

    async def idle_sleep(self) -> None:
        """Stop an unused screen layer after the configured idle minutes (0: never)."""
        minutes = self.manager.display_idle_minutes
        if minutes <= 0 or self.lock.locked() or not self.manager.ready:
            return
        if self.streams or self.manager.computer.control()["holder"] == "human":
            self.touch()
            return
        if self.browser_status() != "running":
            return
        if time.monotonic() - self.last_activity < minutes * 60:
            return
        row = self.manager.db.execute(
            "SELECT run_id FROM environment_services WHERE name=?", (SERVICE,)
        ).fetchone()
        key = f"idle:{row['run_id']}"
        digest = hashlib.sha256(json.dumps(["idle", key]).encode()).hexdigest()
        async with self.lock:
            if self.manager.db.execute(
                "SELECT 1 FROM environment_mutations WHERE request_id=?", (key,)
            ).fetchone():
                return
            with self.manager.db:
                self.manager.db.execute(
                    "INSERT INTO environment_mutations VALUES (?,?,?)",
                    (key, digest, '{"pending":true}'),
                )
            result = await self._sleep()
            with self.manager.db:
                self.manager.db.execute(
                    "UPDATE environment_mutations SET result=? WHERE request_id=?",
                    (json.dumps({**result, "idle_minutes": minutes}, ensure_ascii=False), key),
                )
        self.observe_display()

    async def _ready(self, tool: str, key: str) -> dict[str, Any] | None:
        """Start the desktop service when needed; report what is actually unavailable."""
        manifest = self.manager.desktop_manifest
        headless = manifest.get("browser_mode") == "headless"
        desktop = self.manager.testing or bool(manifest.get("desktop"))
        if tool == "computer" and (headless or not desktop):
            return {"error": "display_unavailable", "message": "这个环境没有桌面画面。"}
        if not (desktop or headless):
            return {"error": "browser_unavailable", "message": "这个环境没有安装浏览器。"}
        if self.browser_status() != "running":
            woken = await self.handle("display_wake", {}, str(uuid5(_DRIVER_KEYS, "wake:" + key)))
            started = time.monotonic()
            while not woken.get("error") and self.browser_status() not in {
                "running",
                "unavailable",
            }:
                if time.monotonic() - started > WAKE_SECONDS:
                    break
                await asyncio.sleep(0.1)
            if self.browser_status() != "running":
                return {
                    "error": "display_unavailable" if tool == "computer" else "browser_unavailable",
                    "message": "桌面服务没有启动。",
                }
        return None

    @staticmethod
    def _driver_id(key: str) -> str:
        return str(uuid5(_DRIVER_KEYS, "request:" + key))

    async def _drive(
        self, tool: str, args: dict[str, Any], identity: str, lease: str | None, *, read: bool
    ) -> dict[str, Any]:
        files = FileWorkspace(self.manager.runtime_root)
        results = self.manager.runtime_root / DRIVER / "results"
        # Requests are serialized: any result left here belongs to a call the
        # Manager stopped waiting for. The runtime volume is small.
        for leftover in results.glob("*") if results.is_dir() else ():
            self._unlink(f"{DRIVER}/results/{leftover.name}")
        request = {
            "id": identity,
            "tool": tool,
            "args": {
                name: value
                for name, value in args.items()
                if name not in {"owner", "max_dimension", "max_pixels"}
            },
            "lease_id": lease,
            "owner": args.get("owner"),
            "max_dimension": args.get("max_dimension"),
            "max_pixels": args.get("max_pixels"),
            "wait_seconds": SETTLE_SECONDS,
        }
        await asyncio.to_thread(
            files.write,
            f"{DRIVER}/requests/{identity}.json",
            json.dumps(request, ensure_ascii=False).encode(),
        )
        started = time.monotonic()
        while time.monotonic() - started < RESULT_SECONDS:
            try:
                answer = await asyncio.to_thread(
                    self._read_json, f"{DRIVER}/results/{identity}.json"
                )
                break
            except FileNotFoundError:
                await asyncio.sleep(0.05)
        else:
            if self._unlink(f"{DRIVER}/requests/{identity}.json"):
                return {"error": "timeout", "message": "浏览器与桌面驱动没有响应，动作没有执行。"}
            return {
                "error": "timeout",
                "message": "动作超时，可能已经执行；先截图或读快照确认，不要重复输入。",
                **({} if read else {"status": "unknown"}),
            }
        result = cast(dict[str, Any], answer["result"])
        image = answer.get("image")
        if isinstance(image, dict):
            name = f"{DRIVER}/results/{image['file']}"
            with files.open_file(name) as descriptor:
                data = await asyncio.to_thread(_read_all, descriptor)
            self._unlink(name)
            result["image"] = {
                "base64": base64.b64encode(data).decode("ascii"),
                "media_type": image["media_type"],
                "width": image["width"],
                "height": image["height"],
            }
        self._unlink(f"{DRIVER}/results/{identity}.json")
        return result

    def _read_json(self, path: str) -> dict[str, Any]:
        with FileWorkspace(self.manager.runtime_root).open_file(path) as descriptor:
            with open(descriptor, closefd=False) as stream:
                return cast(dict[str, Any], json.load(stream))

    def _unlink(self, path: str) -> bool:
        """Remove one runtime file through checked directory FDs; False when absent."""
        try:
            with FileWorkspace(self.manager.runtime_root).parent(path) as (parent, name):
                os.unlink(name, dir_fd=parent)
            return True
        except FileNotFoundError:
            return False


def _read_all(descriptor: int) -> bytes:
    chunks = []
    while data := os.read(descriptor, 1 << 20):
        chunks.append(data)
    return b"".join(chunks)
