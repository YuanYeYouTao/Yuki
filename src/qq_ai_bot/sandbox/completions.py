"""Durable manager-to-Bot completion delivery over the trusted local socket."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

MAX_UNACKNOWLEDGED = 512
PAGE_BYTES = 240000


class CompletionOutbox:
    def __init__(self, database: sqlite3.Connection) -> None:
        self.db = database
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS completion_outbox ("
            "sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
            "run_id TEXT UNIQUE NOT NULL, payload TEXT NOT NULL)"
        )

    def reserve_available(self) -> bool:
        # Running/queued jobs reserve space for their eventual terminal event.
        count = self.db.execute(
            "SELECT (SELECT COUNT(*) FROM completion_outbox) + "
            "(SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running'))"
        ).fetchone()[0]
        return bool(count < MAX_UNACKNOWLEDGED)

    def record(self, run_id: str, request_id: str, result: dict[str, Any]) -> None:
        # Caller owns the transaction containing the terminal jobs update.
        self.db.execute(
            "INSERT INTO completion_outbox (run_id,payload) VALUES (?,?)",
            (run_id, json.dumps({"run_id": run_id, "request_id": request_id, "result": result})),
        )

    def pending(self, limit: object = 20, after: object = 0) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 20 or type(after) is not int or after < 0:
            return {"error": "invalid_arguments"}
        rows = self.db.execute(
            "SELECT sequence,payload FROM completion_outbox WHERE sequence > ? "
            "ORDER BY sequence LIMIT ?",
            (after, limit + 1),
        ).fetchall()
        events: list[dict[str, Any]] = []
        cursor = after
        for row in rows[:limit]:
            event = json.loads(row[1])
            candidate = {"events": [*events, event], "has_more": True, "next_cursor": row[0]}
            if len(json.dumps(candidate).encode()) > PAGE_BYTES:
                if not events:
                    return {"error": "completion_too_large"}
                break
            events.append(event)
            cursor = row[0]
        return {"events": events, "has_more": len(rows) > len(events), "next_cursor": cursor}

    def acknowledge(self, run_id: str) -> dict[str, Any]:
        # Safe to repeat after an acknowledgement response was lost. Consumers
        # must persist their own receipt before acknowledgement, never on read.
        with self.db:
            self.db.execute("DELETE FROM completion_outbox WHERE run_id=?", (run_id,))
        return {"acknowledged": True, "run_id": run_id}
