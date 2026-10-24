"""Manager-owned leases and original terminal waits, committed before file delivery."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import NAMESPACE_URL, uuid4, uuid5

from yuki.sandbox.manager import TERMINAL, identifier
from yuki.workspace.files import FileWorkspace
from yuki.workspace.store import WorkspaceError

if TYPE_CHECKING:
    from yuki.sandbox.persistent import PersistentManager

LOGGER = logging.getLogger(__name__)


def timestamp() -> str:
    return datetime.now(UTC).isoformat()


class ComputerControl:
    def __init__(self, manager: PersistentManager) -> None:
        self.manager = manager
        self.changed: set[asyncio.Event] = set()
        manager.db.executescript(
            "CREATE TABLE IF NOT EXISTS help_requests ("
            "wait_id TEXT PRIMARY KEY,kind TEXT NOT NULL,run_id TEXT UNIQUE NOT NULL,"
            "work_id TEXT,note TEXT,lease_id TEXT,since TEXT NOT NULL,"
            "result TEXT,cancelled_at TEXT);"
            "CREATE INDEX IF NOT EXISTS ix_help_requests_pending ON help_requests(lease_id) "
            "WHERE result IS NULL AND cancelled_at IS NULL;"
        )
        manager.db.commit()

    @property
    def root(self) -> Path:
        return self.manager.runtime_root / "computer"

    def control(self) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            json.loads(self.manager.setting("computer_control", '{"holder":"none"}')),
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "control": self.control(),
            "help_requests": [
                dict(row)
                for row in self.manager.db.execute(
                    "SELECT wait_id,kind,run_id,work_id,note,lease_id,since FROM help_requests "
                    "WHERE result IS NULL AND cancelled_at IS NULL ORDER BY since,wait_id"
                )
            ],
            "revision": int(self.manager.setting("computer_revision", "0")),
        }

    def _save_control(self, value: dict[str, Any]) -> None:
        self.manager.db.execute(
            "INSERT OR REPLACE INTO environment_state VALUES ('computer_control',?)",
            (json.dumps(value),),
        )

    def _bump(self) -> None:
        self.manager.db.execute(
            "INSERT INTO environment_state VALUES ('computer_revision','1') "
            "ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1"
        )

    def notify(self) -> None:
        for changed in self.changed:
            changed.set()

    def request_get(self, key: str) -> dict[str, Any]:
        row = self.manager.db.execute(
            "SELECT result FROM environment_mutations WHERE request_id=?", (key,)
        ).fetchone()
        self.deliver_results()
        return json.loads(row[0]) if row else {"error": "unknown_request"}

    def set(self, key: str, args: dict[str, Any]) -> dict[str, Any]:
        identifier(key)
        action, actor = args.get("action"), args.get("actor")
        if action not in {"take", "return", "transfer", "agent_acquire", "agent_release"}:
            raise ValueError("invalid_control_action")
        if not isinstance(actor, dict):
            raise ValueError("invalid_control_actor")
        if args.get("note") is not None and not isinstance(args["note"], str):
            raise ValueError("invalid_computer_note")
        if action in {"take", "transfer"} or (action == "return" and not actor.get("system")):
            for field in ("person_id", "device_id"):
                identifier(actor.get(field))
            if not isinstance(actor.get("device_label"), str):
                raise ValueError("invalid_control_actor")
        if action in {"return", "transfer", "agent_release"}:
            identifier(args.get("lease_id"))
        if action == "agent_acquire":
            identifier(actor.get("run_id"))
        payload = json.dumps(args, sort_keys=True, ensure_ascii=False, allow_nan=False)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        db = self.manager.db
        with db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                "SELECT digest,result FROM environment_mutations WHERE request_id=?", (key,)
            ).fetchone()
            if prior is not None:
                result = (
                    json.loads(prior["result"])
                    if prior["digest"] == digest
                    else {"error": "idempotency_conflict"}
                )
            else:
                result = self._apply(action, actor, args)
                db.execute(
                    "INSERT INTO environment_mutations VALUES (?,?,?)",
                    (key, digest, json.dumps(result)),
                )
        self.deliver_results()
        self.notify()
        return result

    def _apply(self, action: str, actor: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        control, now = self.control(), timestamp()
        if action in {"return", "transfer", "agent_release"} and (
            control.get("lease_id") != args["lease_id"]
            or control["holder"] == "none"
            or (action == "agent_release" and control["holder"] != "agent")
            or (action in {"return", "transfer"} and control["holder"] != "human")
        ):
            return {"error": "precondition_failed", "control": control}
        if action in {"take", "agent_acquire"} and control["holder"] == "human":
            return {"error": "control_held", "control": control}
        if action == "agent_acquire" and control["holder"] == "agent":
            return {"error": "control_held", "control": control}
        old_lease = control.get("lease_id")
        if action in {"take", "transfer", "agent_acquire"}:
            control = {
                "holder": "agent" if action == "agent_acquire" else "human",
                "lease_id": str(uuid4()),
                **actor,
                "since": now,
            }
        else:
            control = {"holder": "none"}
        self._save_control(control)
        by: str | dict[str, Any] = "system" if actor.get("system") else actor
        result = {"control": control, "by": by, "note": args.get("note"), "at": now}
        if args.get("reason"):
            result["reason"] = args["reason"]
        if action == "transfer":
            self.manager.db.execute(
                "UPDATE help_requests SET lease_id=? WHERE kind='wait_control' AND lease_id=? "
                "AND result IS NULL AND cancelled_at IS NULL",
                (control["lease_id"], old_lease),
            )
        if action == "return":
            for row in self.manager.db.execute(
                "SELECT wait_id,kind FROM help_requests WHERE result IS NULL "
                "AND cancelled_at IS NULL AND (kind='help' OR lease_id=?)",
                (old_lease,),
            ).fetchall():
                returned = {
                    "returned": True,
                    "lease_id": old_lease,
                    "by": by,
                    "note": args.get("note"),
                    "at": now,
                    **({"reason": args["reason"]} if args.get("reason") else {}),
                }
                if row["kind"] == "wait_control":
                    returned.update(
                        control_free=True,
                        ended="released" if actor.get("system") else "returned",
                    )
                self.manager.db.execute(
                    "UPDATE help_requests SET result=? WHERE wait_id=?",
                    (json.dumps(returned), row["wait_id"]),
                )
        self._bump()
        return result

    def register(self, args: dict[str, Any]) -> dict[str, Any]:
        run_id, kind = identifier(args.get("run_id")), args.get("kind")
        if kind not in {"help", "wait_control"}:
            raise ValueError("invalid_computer_wait")
        note = args.get("note") if kind == "help" else None
        if note is not None and not isinstance(note, str):
            raise ValueError("invalid_computer_note")
        db = self.manager.db
        with db:
            db.execute("BEGIN IMMEDIATE")
            job = db.execute(
                "SELECT jobs.state,payload,kind FROM jobs JOIN environment_jobs USING(id) "
                "WHERE jobs.id=?",
                (run_id,),
            ).fetchone()
            if job is None or job["kind"] != "terminal_exec" or job["state"] in TERMINAL:
                raise ValueError("computer_wait_run_unavailable")
            prior = db.execute("SELECT * FROM help_requests WHERE run_id=?", (run_id,)).fetchone()
            if prior:
                if prior["kind"] != kind or prior["note"] != note:
                    raise ValueError("idempotency_conflict")
                result = json.loads(prior["result"]) if prior["result"] else None
            else:
                control, now = self.control(), timestamp()
                result = (
                    {"control_free": True, "holder": control["holder"], "at": now}
                    if kind == "wait_control" and control["holder"] != "human"
                    else None
                )
                work_id = json.loads(job["payload"]).get("env", {}).get("YUKI_WORK_ID")
                db.execute(
                    "INSERT INTO help_requests VALUES (?,?,?,?,?,?,?,?,NULL)",
                    (
                        run_id,
                        kind,
                        run_id,
                        work_id,
                        note,
                        control.get("lease_id"),
                        now,
                        json.dumps(result) if result is not None else None,
                    ),
                )
                self._bump()
        self.deliver_results()
        self.notify()
        return {"wait_id": run_id, "result": result, "pending": result is None}

    def cancel(self, run_id: str) -> None:
        with self.manager.db:
            updated = self.manager.db.execute(
                "UPDATE help_requests SET cancelled_at=? WHERE run_id=? AND cancelled_at IS NULL",
                (timestamp(), run_id),
            ).rowcount
            if updated:
                self._bump()
        if updated:
            self.notify()

    def finish_run(self, run_id: str) -> None:
        # A saved return result remains a completed wait, rather than cancellation.
        row = self.manager.db.execute(
            "SELECT result FROM help_requests WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is not None and row[0] is None:
            self.cancel(run_id)
        control = self.control()
        if control["holder"] == "agent" and control.get("run_id") == run_id:
            self.set(
                str(uuid5(NAMESPACE_URL, "yuki:computer:agent-end:" + control["lease_id"])),
                {
                    "action": "agent_release",
                    "actor": {"run_id": run_id},
                    "lease_id": control["lease_id"],
                },
            )

    def _write(self, path: str, value: dict[str, Any]) -> None:
        # Runtime directories are writable by the environment. Use checked FDs,
        # including for ancestors and replacement, rather than following host links.
        files = FileWorkspace(self.manager.runtime_root)
        data = json.dumps(value, ensure_ascii=False).encode()
        try:
            with files.open_file(path) as descriptor:
                with open(descriptor, "rb", closefd=False) as stream:
                    if stream.read() == data:
                        return
        except FileNotFoundError:
            pass
        files.write(path, data)

    def deliver_results(self) -> None:
        """Repair only derived files. The original transaction has already committed."""
        self._write("computer/control.json", self.control())
        pending = self.manager.db.execute(
            "SELECT help_requests.run_id,help_requests.result FROM help_requests JOIN jobs "
            "ON jobs.id=help_requests.run_id WHERE help_requests.result IS NOT NULL "
            "AND cancelled_at IS NULL AND jobs.state IN ('queued','running')"
        ).fetchall()
        for row in pending:
            self._write("computer/results/" + row["run_id"] + ".json", json.loads(row["result"]))

    def collect_requests(self) -> None:
        directory = self.root / "requests"
        if not directory.exists():
            return
        files = FileWorkspace(directory)
        for path in directory.glob("*.json"):
            try:
                run_id = identifier(path.stem)
                with files.open_file(path.name) as descriptor:
                    with open(descriptor, closefd=False) as stream:
                        request = json.load(stream)
                self.register({**request, "run_id": run_id})
            except (OSError, WorkspaceError):
                # Keep the original request when delivery or file observation
                # failed; a committed registration is recovered by its run ID.
                LOGGER.warning("computer_wait_request_pending")
                continue
            except (ValueError, TypeError):
                LOGGER.warning("computer_wait_request_unavailable")
            with files.parent(path.name) as (parent, name):
                os.unlink(name, dir_fd=parent)
        self.deliver_results()

    async def watch(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.deliver_results()
        changed = asyncio.Event()
        self.changed.add(changed)
        disconnected = asyncio.create_task(reader.read(1))
        update: asyncio.Task[bool] | None = None
        try:
            while True:
                changed.clear()
                writer.write(json.dumps(self.snapshot()).encode() + b"\n")
                await writer.drain()
                update = asyncio.create_task(changed.wait())
                done, _ = await asyncio.wait(
                    (disconnected, update), return_when=asyncio.FIRST_COMPLETED
                )
                if disconnected in done:
                    return
        finally:
            self.changed.discard(changed)
            disconnected.cancel()
            if update is not None:
                update.cancel()
            await asyncio.gather(
                disconnected, *([update] if update is not None else []), return_exceptions=True
            )
