"""Bounded retrieval cache, without network transactions."""

import json
import sqlite3
import time
from contextlib import closing
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from qq_ai_bot.web.models import WebSearchResponse, WebSearchSource


class BridgeState:
    def __init__(self, path: Path) -> None:
        self.path = path

    def access(self, key: str, value: WebSearchResponse | None = None) -> WebSearchResponse | None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            self.path.chmod(0o600)
            db.execute(
                "CREATE TABLE IF NOT EXISTS cache "
                "(key TEXT PRIMARY KEY, payload TEXT NOT NULL, expires REAL NOT NULL)"
            )
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM cache WHERE expires <= ?", (time.time(),))
            if value is not None:
                payload = json.dumps(asdict(value), ensure_ascii=False, default=str)
                if len(payload.encode()) <= 32768:
                    db.execute(
                        "INSERT OR REPLACE INTO cache VALUES (?,?,?)",
                        (key, payload, time.time() + 600),
                    )
                    db.execute(
                        "DELETE FROM cache WHERE key NOT IN "
                        "(SELECT key FROM cache ORDER BY expires DESC LIMIT 128)"
                    )
                return None
            row = db.execute("SELECT payload FROM cache WHERE key=?", (key,)).fetchone()
            if row is None:
                return None
            result = json.loads(row[0])
            for source in result["sources"]:
                if source["published_at"]:
                    source["published_at"] = datetime.fromisoformat(source["published_at"])
            result["sources"] = tuple(WebSearchSource(**source) for source in result["sources"])
            return WebSearchResponse(**result)
