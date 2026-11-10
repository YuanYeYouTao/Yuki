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
from yuki.sandbox.environment_tools import READ_ACTIONS
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
        self.last_display: str | None = None
        # One environment, one screen and one browser: actions run one at a time.
        self.lock = asyncio.Lock()

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
            registered = self.manager.db.execute(
                "SELECT 1 FROM environment_services WHERE name=?", (SERVICE,)
            ).fetchone()
            if not registered:
                await self.manager.service(
                    {
                        "action": "register",
                        "name": SERVICE,
                        "command": (
                            "exec /usr/local/bin/python /opt/yuki-runtime/desktop_service.py"
                        ),
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
        else:
            result = await self.manager.service({"action": "stop", "name": SERVICE})
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

    def observe_display(self) -> None:
        display = self.display_status()["display"]
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
