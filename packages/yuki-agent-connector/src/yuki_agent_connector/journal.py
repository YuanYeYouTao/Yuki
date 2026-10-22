"""Only original dispatch facts and unacknowledged significant events.

No Work state machine, native transcript mirror, token journal or scheduled jobs.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .config import private_path


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class Journal:
    def __init__(self, path: Path, *, namespace: str) -> None:
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
        if existing and existing[0] != namespace:
            self.db.close()
            raise ValueError(
                "Journal belongs to another server/device; use a separate data directory"
            )
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO installation VALUES (?)", (namespace,))

    def begin(self, command: dict[str, Any]) -> dict[str, Any] | None:
        key = command["id"]
        previous = self.db.execute(
            "SELECT command, response FROM operations WHERE id = ?", (key,)
        ).fetchone()
        if previous:
            if previous[0] != encode(command):
                raise ValueError("Original operation ID reused with different parameters")
            return (
                json.loads(previous[1])
                if previous[1]
                else {
                    "type": "response",
                    "id": key,
                    "result": {"status": "unknown", "reason": "original_dispatch_not_confirmed"},
                }
            )
        with self.db:
            self.db.execute(
                "INSERT INTO operations(id, command) VALUES (?, ?)", (key, encode(command))
            )
        return None

    def complete(self, key: str, response: dict[str, Any]) -> None:
        command = json.loads(
            self.db.execute("SELECT command FROM operations WHERE id = ?", (key,)).fetchone()[0]
        )
        action = command["action"]
        result = response.get("result", {})
        encoded = encode(response)
        with self.db:
            self.db.execute("UPDATE operations SET response = ? WHERE id = ?", (encoded, key))
            if action in {"watch", "prompt", "queue"} and result.get("session_id"):
                self.db.execute(
                    "UPDATE operations SET observing = CASE WHEN "
                    "json_extract(receipt, '$.kind') = 'completed' THEN 0 ELSE 1 END WHERE id = ?",
                    (key,),
                )
            elif (
                action == "detach"
                and result.get("admitted") is True
                and result.get("detached") is True
            ):
                self.db.execute(
                    "UPDATE operations SET observing = 0 WHERE id = ?",
                    (command["arguments"]["operation_id"],),
                )

    def operation(self, key: str) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT response, receipt FROM operations WHERE id = ?", (key,)
        ).fetchone()
        if not row:
            return {"status": "not_found"}
        original = json.loads(row[0]) if row[0] else {}
        result: dict[str, Any] = original.get("result", {"status": "unknown"})
        if "error" in original:
            result = {"status": "failed", "admitted": False, "error": original["error"]}
        if row[1]:
            receipt = json.loads(row[1])
            result.update(
                {
                    key: receipt[key]
                    for key in ("session_id", "execution_id")
                    if receipt.get(key) is not None
                }
            )
            payload = receipt["payload"]
            if receipt["kind"] == "completed":
                result.update(payload)
                result["pending"] = False
                result["admitted"] = True
            elif payload.get("admitted") is True:
                result.update(admitted=True, status=payload.get("status", "running"), pending=True)
        return result

    def observations(self) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        observations = []
        for command, response, receipt in self.db.execute(
            "SELECT command, response, receipt FROM operations WHERE observing = 1"
        ):
            result = json.loads(response).get("result", {}) if response else {}
            if receipt:
                original = json.loads(receipt)
                result.update(
                    {
                        key: original[key]
                        for key in ("session_id", "execution_id")
                        if original.get(key) is not None
                    }
                )
            observations.append((json.loads(command), result))
        return observations

    def append(self, event: dict[str, Any]) -> None:
        encoded = encode(event)
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO events(id, event) VALUES (?, ?)",
                (event["event_id"], encoded),
            )
            if event["kind"] == "completed" and event.get("operation_id"):
                self.db.execute(
                    "UPDATE operations SET receipt = ?, observing = 0 WHERE id = ?",
                    (encoded, event["operation_id"]),
                )
            elif (
                event.get("operation_id")
                and event.get("session_id")
                and event.get("payload", {}).get("admitted") is True
            ):
                self.db.execute(
                    "UPDATE operations SET receipt = COALESCE(receipt, ?), observing = CASE "
                    "WHEN json_extract(receipt, '$.kind') = 'completed' THEN 0 ELSE 1 END "
                    "WHERE id = ? AND json_extract(command, '$.action') "
                    "IN ('watch', 'prompt', 'queue')",
                    (encoded, event["operation_id"]),
                )

    def pending(self) -> list[dict[str, Any]]:
        return [
            json.loads(row[0])
            for row in self.db.execute("SELECT event FROM events ORDER BY sequence")
        ]

    def ack(self, key: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM events WHERE id = ?", (key,))

    def close(self) -> None:
        self.db.close()
