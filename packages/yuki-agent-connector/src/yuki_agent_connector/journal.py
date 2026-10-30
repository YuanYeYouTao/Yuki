"""Original dispatch evidence, native receipt projections and unacknowledged events."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, cast

from .config import private_path

OBSERVED_ACTIONS = {"watch", "prompt", "queue", "exec"}
TERMINAL = {"completed", "succeeded", "failed", "cancelled", "detached"}


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def original_command(command: dict[str, Any]) -> dict[str, Any]:
    if (
        command.get("resource") == "file"
        and command["action"] == "write"
        and "data_base64" in command["arguments"]
    ):
        import base64

        args = dict(command["arguments"])
        data = base64.b64decode(args.pop("data_base64"), validate=True)
        args["data_sha256"] = hashlib.sha256(data).hexdigest()
        return {**command, "arguments": args}
    # A missing scope belongs to the old single-execution watch contract.
    return (
        {"observation_scope": "execution", **command} if command["action"] == "watch" else command
    )


def fold_receipt(previous: dict[str, Any], facts: dict[str, Any], sequence: int) -> dict[str, Any]:
    if previous.get("status") in TERMINAL:
        return {**previous, "state_sequence": sequence}
    result = {**previous, **facts, "state_sequence": sequence}
    if previous.get("admitted") is True:
        result["admitted"] = True
    if result.get("status") in TERMINAL:
        result["pending"] = False
    if facts.get("status") and facts["status"] != "unknown":
        result.pop("reason", None)
        result.pop("error_type", None)
    return result


class Journal:
    def __init__(self, path: Path, *, namespace: str) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        private_path(path.parent, directory=True)
        self.db = sqlite3.connect(path)
        private_path(path)
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS installation (namespace TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS operations (
                id TEXT PRIMARY KEY, command TEXT NOT NULL, response TEXT,
                observing INTEGER NOT NULL DEFAULT 0, receipt TEXT
            );
            CREATE INDEX IF NOT EXISTS observing_operations ON operations(observing)
                WHERE observing = 1;
            CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL UNIQUE, event TEXT NOT NULL
            );
        """)
        existing = self.db.execute("SELECT namespace FROM installation").fetchone()
        owner = existing[0] if existing else namespace
        if existing and owner != namespace and owner.startswith("["):
            legacy = json.loads(owner)
            if isinstance(legacy, list) and len(legacy) == 2 and isinstance(legacy[0], str):
                owner = legacy[1]
        if owner != namespace:
            self.db.close()
            raise ValueError("Journal belongs to another device; use a separate data directory")
        with self.db:
            if existing and existing[0] != namespace:
                self.db.execute("UPDATE installation SET namespace=?", (namespace,))
            else:
                self.db.execute("INSERT OR IGNORE INTO installation VALUES (?)", (namespace,))
            if self.db.execute("PRAGMA user_version").fetchone()[0] == 0:
                self._upgrade_receipts()
                self.db.execute("PRAGMA user_version=1")

    @staticmethod
    def _initial(command: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": "unknown",
            "reason": "original_dispatch_not_confirmed",
            "state_sequence": 0,
        }
        if command["action"] in OBSERVED_ACTIONS:
            result["pending"] = True
        for key in ("session_id", "execution_id", "run_id"):
            if command.get(key) is not None:
                result[key] = command[key]
        return result

    @staticmethod
    def _response_state(response: dict[str, Any]) -> dict[str, Any]:
        if "error" in response:
            return {"status": "failed", "admitted": False, "error": response["error"]}
        return dict(response.get("result", {}))

    @staticmethod
    def _event_facts(event: dict[str, Any]) -> dict[str, Any]:
        payload = (
            event.get("result", {})
            if event.get("type") == "terminal_event"
            else event.get("payload", {})
        )
        facts = {
            key: payload[key]
            for key in ("status", "pending", "admitted", "reason", "error_type", "error")
            if key in payload
        }
        for key in ("session_id", "execution_id"):
            if event.get(key) is not None:
                facts[key] = event[key]
        if event["kind"] == "completed":
            facts.update(payload)
            facts.update(pending=False, admitted=True)
            if event.get("type") == "terminal_event":
                facts["completion"] = dict(payload)
        return facts

    def _upgrade_receipts(self) -> None:
        # Frozen upgrade only; ordinary startup does not scan history.
        for key, encoded, response, receipt, observing in self.db.execute(
            "SELECT id, command, response, receipt, observing FROM operations"
        ):
            command = original_command(json.loads(encoded))
            state = self._initial(command)
            if response:
                state = fold_receipt(state, self._response_state(json.loads(response)), 0)
            state["state_sequence"] = 0
            if receipt:
                state = fold_receipt(state, self._event_facts(json.loads(receipt)), 0)
            if command["action"] in OBSERVED_ACTIONS:
                observing = state.get("status") not in TERMINAL
            self.db.execute(
                "UPDATE operations SET command=?, receipt=?, observing=? WHERE id=?",
                (encode(command), encode(state), observing, key),
            )
        # An acknowledged detach is an observation exit, even if later progress was queued.
        for encoded in self.db.execute(
            "SELECT command FROM operations WHERE json_extract(command, '$.action')='detach' "
            "AND json_extract(response, '$.result.detached')=1 "
            "AND json_extract(response, '$.result.admitted')=1"
        ):
            self._detach(json.loads(encoded[0])["arguments"]["operation_id"])
        for sequence, key, encoded in self.db.execute(
            "SELECT sequence, id, event FROM events ORDER BY sequence"
        ):
            event = json.loads(encoded)
            self._project_event(event, sequence)
            self.db.execute("UPDATE events SET event=? WHERE id=?", (encode(event), key))
        for (key,) in self.db.execute(
            "SELECT id FROM operations WHERE json_extract(receipt, '$.detached')=1"
        ):
            self._detach(key, self._next_sequence(key))

    def begin(self, command: dict[str, Any]) -> dict[str, Any] | None:
        command = original_command(command)
        key = command["id"]
        previous = self.db.execute("SELECT command FROM operations WHERE id=?", (key,)).fetchone()
        if previous:
            if previous[0] != encode(command):
                raise ValueError("Original operation ID reused with different parameters")
            return {"type": "response", "id": key, "result": self.operation(key)}
        with self.db:
            self.db.execute(
                "INSERT INTO operations(id, command, observing, receipt) VALUES (?, ?, ?, ?)",
                (
                    key,
                    encode(command),
                    command["action"] in OBSERVED_ACTIONS,
                    encode(self._initial(command)),
                ),
            )
        return None

    def _next_sequence(self, key: str) -> int:
        inserted = self.db.execute(
            "INSERT INTO events(id, event) VALUES (?, '{}')", (f"response:{key}",)
        )
        sequence = cast(int, inserted.lastrowid)
        self.db.execute("DELETE FROM events WHERE sequence=?", (sequence,))
        return sequence

    def _detach(self, key: str, sequence: int = 0) -> None:
        state = fold_receipt(
            self.operation(key),
            {"status": "detached", "pending": False, "detached": True},
            sequence,
        )
        state["detached"] = True
        self.db.execute(
            "UPDATE operations SET observing=0, receipt=? WHERE id=?", (encode(state), key)
        )

    def complete(self, key: str, response: dict[str, Any]) -> None:
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            command = json.loads(
                self.db.execute("SELECT command FROM operations WHERE id=?", (key,)).fetchone()[0]
            )
            state, result = self.operation(key), self._response_state(response)
            # Reserve from the same durable order as events. Responses need an
            # order too, including admission arriving after an early milestone.
            sequence = self._next_sequence(key)
            if state.get("state_sequence", 0) and state.get("reason") != (
                "original_dispatch_not_confirmed"
            ):
                state = {**result, **state, "state_sequence": sequence}
            else:
                state = fold_receipt(state, result, sequence)
            if command.get("observation_scope") == "session":
                state.pop("execution_id", None)
            self.db.execute(
                "UPDATE operations SET response=?, receipt=?, observing=? WHERE id=?",
                (
                    encode(response),
                    encode(state),
                    command["action"] in OBSERVED_ACTIONS and state.get("status") not in TERMINAL,
                    key,
                ),
            )
            if (
                command["action"] == "detach"
                and result.get("admitted") is True
                and result.get("detached") is True
            ):
                self._detach(command["arguments"]["operation_id"], sequence)
            elif (
                command["action"] == "cancel"
                and result.get("pending") is False
                and (result.get("status") in TERMINAL)
            ):
                original = command.get("arguments", {}).get("operation_id")
                if original is not None:
                    terminal = fold_receipt(self.operation(original), result, sequence)
                    self.db.execute(
                        "UPDATE operations SET receipt=?, observing=0 WHERE id=?",
                        (encode(terminal), original),
                    )

    def operation(self, key: str) -> dict[str, Any]:
        row = self.db.execute("SELECT receipt FROM operations WHERE id=?", (key,)).fetchone()
        return json.loads(row[0]) if row else {"status": "not_found"}

    def annotate(self, key: str, facts: dict[str, Any]) -> dict[str, Any]:
        """Attach execution evidence without changing its native lifecycle."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            state = self.operation(key)
            if state.get("status") == "not_found":
                raise ValueError("The original operation is not recorded")
            state = {**state, **facts, "state_sequence": self._next_sequence(key)}
            self.db.execute("UPDATE operations SET receipt=? WHERE id=?", (encode(state), key))
            self.db.commit()
            return state
        except BaseException:
            self.db.rollback()
            raise

    def observation(self, key: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
        row = self.db.execute(
            "SELECT command, receipt FROM operations WHERE id=? AND observing=1", (key,)
        ).fetchone()
        return (json.loads(row[0]), json.loads(row[1])) if row else None

    def observations(
        self, instance_id: str | None = None
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        return [
            (json.loads(command), json.loads(receipt))
            for command, receipt in self.db.execute(
                "SELECT command, receipt FROM operations WHERE observing=1"
                + (
                    " AND json_extract(command, '$.instance_id')=?"
                    if instance_id is not None
                    else ""
                ),
                (instance_id,) if instance_id is not None else (),
            )
        ]

    def _project_event(self, event: dict[str, Any], sequence: int) -> None:
        event["sequence"] = sequence
        key = event.get("operation_id")
        if key is None:
            return
        row = self.db.execute(
            "SELECT command, receipt FROM operations WHERE id=?", (key,)
        ).fetchone()
        if row is None:
            return
        command, previous = json.loads(row[0]), json.loads(row[1])
        facts = self._event_facts(event)
        if command.get("observation_scope") == "session":
            facts.pop("execution_id", None)
        state = fold_receipt(previous, facts, sequence)
        self.db.execute(
            "UPDATE operations SET receipt=?, observing=? WHERE id=?",
            (
                encode(state),
                command["action"] in OBSERVED_ACTIONS and state.get("status") not in TERMINAL,
                key,
            ),
        )
        event["operation_state"] = state

    def append(self, event: dict[str, Any]) -> None:
        event = dict(event)
        snapshot_sequence = event.pop("snapshot_sequence", None)
        with self.db:
            if event.get("operation_id"):
                current = self.operation(event["operation_id"])
                if current.get("detached") or (
                    snapshot_sequence is not None
                    and snapshot_sequence != current.get("state_sequence", 0)
                    and not (
                        event["kind"] == "completed"
                        and event.get("payload", {}).get("status") in TERMINAL
                    )
                ):
                    return
            inserted = self.db.execute(
                "INSERT OR IGNORE INTO events(id, event) VALUES (?, ?)",
                (event["event_id"], encode(event)),
            )
            if not inserted.rowcount:
                return
            sequence = cast(int, inserted.lastrowid)
            self._project_event(event, sequence)
            self.db.execute("UPDATE events SET event=? WHERE sequence=?", (encode(event), sequence))

    def next_event(self, after_sequence: int = 0) -> tuple[int, str, str] | None:
        return cast(
            tuple[int, str, str] | None,
            self.db.execute(
                "SELECT sequence, id, event FROM events WHERE sequence>? ORDER BY sequence LIMIT 1",
                (after_sequence,),
            ).fetchone(),
        )

    def ack(self, key: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM events WHERE id=?", (key,))

    def close(self) -> None:
        self.db.close()
