"""Bounded retrieval cache, without network transactions."""

import json
import logging
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
        try:
            return self._access(key, value)
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError) as exc:
            logging.getLogger(__name__).warning(
                "search_cache_unavailable category=%s", type(exc).__name__
            )
            return None

    def _access(self, key: str, value: WebSearchResponse | None) -> WebSearchResponse | None:
        document = asdict(value) if value else None
        payload = json.dumps(document, ensure_ascii=False, default=str) if document else None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            self.path.chmod(0o600)
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='cache'").fetchone() is None:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS cache "
                    "(key TEXT PRIMARY KEY, payload TEXT NOT NULL, expires REAL NOT NULL)"
                )
            if value is not None:
                assert payload is not None
                db.execute("BEGIN IMMEDIATE")
                db.execute("DELETE FROM cache WHERE expires <= ?", (time.time(),))
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
            row = db.execute(
                "SELECT payload FROM cache WHERE key=? AND expires>?", (key, time.time())
            ).fetchone()
            if row is None:
                return None
        result = json.loads(row[0])
        for source in result["sources"]:
            if source["published_at"]:
                source["published_at"] = datetime.fromisoformat(source["published_at"])
        result["sources"] = tuple(WebSearchSource(**source) for source in result["sources"])
        return WebSearchResponse(**result)
