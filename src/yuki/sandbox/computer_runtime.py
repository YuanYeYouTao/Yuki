"""Desktop Supervisor integration and environment file-command delivery."""

from __future__ import annotations

import hashlib
import json
import os
from typing import TYPE_CHECKING, Any, cast

from yuki.sandbox.manager import TERMINAL, identifier
from yuki.workspace.files import FileWorkspace
from yuki.workspace.store import WorkspaceError

if TYPE_CHECKING:
    from yuki.sandbox.persistent import PersistentManager

SERVICE = "yuki-desktop"


class ComputerRuntime:
    def __init__(self, manager: PersistentManager) -> None:
        self.manager = manager
        self.last_display: str | None = None

    def display_status(self) -> dict[str, Any]:
        if not self.manager.ready:
            return {"display": "unavailable"}
        row = self.manager.db.execute(
            "SELECT enabled,run_id FROM environment_services WHERE name=?", (SERVICE,)
        ).fetchone()
        if row is None or not row["run_id"]:
            return {"display": "stopped"}
        run = self.manager.get(row["run_id"])
        if run.get("status") in TERMINAL:
            return {
                "display": "unavailable"
                if row["enabled"] and run["status"] == "failed"
                else "stopped"
            }
        try:
            with FileWorkspace(self.manager.runtime_root).open_file(
                "computer/desktop-status.json"
            ) as descriptor:
                with open(descriptor, closefd=False) as stream:
                    status = json.load(stream)
            if status.get("run_id") == row["run_id"] and status.get("display") == "running":
                return {"display": "running"}
        except (OSError, ValueError, WorkspaceError):
            pass
        return {"display": "starting"}

    async def handle(self, method: str, args: dict[str, Any], key: str) -> dict[str, Any]:
        if method == "display_status":
            return self.display_status()
        if method == "tabs_list":
            if self.display_status()["display"] != "running":
                return {"tabs": [], "available": False}
            code, raw = await self.manager.command(
                "docker", "exec", self.manager.name, "yuki-computer", "status", "--json"
            )
            return json.loads(raw) if not code else {"error": "display_unavailable"}
        if method not in {"display_wake", "display_sleep"}:
            return {"error": "unknown_method"}
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
        else:
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
            result = await self.manager.service({"action": "start", "name": SERVICE})
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

    @staticmethod
    def read_command(files: FileWorkspace, name: str) -> dict[str, Any]:
        with files.open_file(name) as descriptor:
            with open(descriptor, closefd=False) as stream:
                return cast(dict[str, Any], json.load(stream))

    async def collect_commands(self) -> None:
        directory = self.manager.computer.root / "commands"
        if not directory.exists():
            self.observe_display()
            return
        files = FileWorkspace(directory)
        for path in directory.glob("*.json"):
            try:
                key = identifier(path.stem)
                command = self.read_command(files, path.name)
                method, args = command["method"], command["args"]
                if method in {"agent_acquire", "agent_release"}:
                    run_id = identifier(command.get("run_id"))
                    row = self.manager.db.execute(
                        "SELECT state,payload FROM jobs WHERE id=?", (run_id,)
                    ).fetchone()
                    if row is None or row["state"] in TERMINAL:
                        result = {"error": "computer_run_unavailable"}
                    else:
                        work_id = json.loads(row["payload"]).get("env", {}).get("YUKI_WORK_ID")
                        result = self.manager.computer.set(
                            key,
                            {
                                "action": method,
                                "actor": {"run_id": run_id, "work_id": work_id},
                                **(
                                    {"lease_id": args["lease_id"]}
                                    if method == "agent_release"
                                    else {}
                                ),
                            },
                        )
                else:
                    result = await self.handle(method, args, key)
                self.manager.computer._write("computer/command-results/" + key + ".json", result)
            except (OSError, WorkspaceError):
                continue  # Keep the original operation when result delivery failed.
            except (ValueError, TypeError, KeyError):
                result = {"error": "invalid_computer_command"}
                self.manager.computer._write("computer/command-results/" + path.name, result)
            with files.parent(path.name) as (parent, name):
                os.unlink(name, dir_fd=parent)
        self.observe_display()
