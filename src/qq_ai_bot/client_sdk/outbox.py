"""Client-owned durable requests; never a server task or effect ledger."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any
from uuid import UUID


class Outbox:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(sqlite3.connect(path)) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS owner (id INTEGER PRIMARY KEY CHECK(id=1), "
                "server TEXT NOT NULL, person TEXT NOT NULL, binding_id TEXT, conversation_id TEXT)"
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(owner)")}
            for column in ("binding_id", "conversation_id"):
                if column not in columns:
                    db.execute(f"ALTER TABLE owner ADD COLUMN {column} TEXT")
            db.execute(
                "CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, path TEXT NOT NULL, "
                "body TEXT NOT NULL, state TEXT NOT NULL, result TEXT, problem TEXT)"
            )
            db.commit()
        path.chmod(0o600)

    def owner(self) -> dict[str, str | None] | None:
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute(
                "SELECT server,person,binding_id,conversation_id FROM owner WHERE id=1"
            ).fetchone()
        return (
            dict(zip(("server", "person", "binding_id", "conversation_id"), row, strict=True))
            if row
            else None
        )

    def bind(
        self,
        server: str,
        person: str,
        *,
        binding_id: str | None = None,
        conversation_id: str | None = None,
    ) -> None:
        with closing(sqlite3.connect(self.path)) as db:
            db.execute(
                "INSERT OR IGNORE INTO owner(id,server,person,binding_id,conversation_id) "
                "VALUES (1,?,?,?,?)",
                (server, person, binding_id, conversation_id),
            )
            row = db.execute(
                "SELECT server,person,binding_id,conversation_id FROM owner WHERE id=1"
            ).fetchone()
            if row[:2] != (server, person) or any(
                stored is not None and expected is not None and stored != expected
                for stored, expected in zip(row[2:], (binding_id, conversation_id), strict=True)
            ):
                raise ValueError("outbox_owner_mismatch")
            db.execute(
                "UPDATE owner SET binding_id=coalesce(binding_id,?), "
                "conversation_id=coalesce(conversation_id,?) WHERE id=1",
                (binding_id, conversation_id),
            )
            db.commit()

    def prepare(self, identity: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
        if str(UUID(identity)) != identity or UUID(identity).version != 4:
            raise ValueError("invalid_request_id")
        encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with closing(sqlite3.connect(self.path)) as db:
            db.execute(
                "INSERT OR IGNORE INTO requests VALUES (?,?,?,'draft',NULL,NULL)",
                (identity, path, encoded),
            )
            stored = db.execute("SELECT path,body FROM requests WHERE id=?", (identity,)).fetchone()
            if stored != (path, encoded):
                raise ValueError("request_conflict")
            db.commit()
        return self.get(identity)

    def get(self, identity: str) -> dict[str, Any]:
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute(
                "SELECT id,path,body,state,result,problem FROM requests WHERE id=?", (identity,)
            ).fetchone()
        if row is None:
            raise KeyError(identity)
        return {
            "request_id": row[0],
            "path": row[1],
            "body": json.loads(row[2]),
            "state": row[3],
            "result": json.loads(row[4]) if row[4] else None,
            "problem": row[5],
        }

    def update(
        self, identity: str, state: str, result: Any = None, problem: str | None = None
    ) -> None:
        if state not in {"draft", "submitted", "accepted", "result"}:
            raise ValueError("invalid_outbox_state")
        with closing(sqlite3.connect(self.path)) as db:
            db.execute(
                "UPDATE requests SET state=?,result=?,problem=? WHERE id=?",
                (state, json.dumps(result) if result is not None else None, problem, identity),
            )
            db.commit()

    def pending(self) -> list[dict[str, Any]]:
        with closing(sqlite3.connect(self.path)) as db:
            ids = [
                row[0]
                for row in db.execute(
                    "SELECT id FROM requests WHERE state != 'result' ORDER BY rowid"
                )
            ]
        return [self.get(identity) for identity in ids]
