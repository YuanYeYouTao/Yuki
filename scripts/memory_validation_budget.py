"""Private validation network guard; never installed into the production runtime.

All verification clients must share one ledger outside the checkout. A reservation
is durable before transport dispatch, including failed requests. There are no
transport retries; provider retries, if enabled by a caller, reserve again.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import httpx

REQUEST_LIMIT = 48
PURPOSES = frozenset({"embedding", "reflection", "dream", "main_agent", "attribution"})


class ValidationBudgetExhausted(RuntimeError):
    """The next network request was refused before dispatch."""


def private_path(path: Path) -> Path:
    resolved = path.resolve()
    checkout = Path(__file__).resolve().parents[1]
    if resolved.is_relative_to(checkout):
        raise ValueError("validation artifacts must stay outside the checkout")
    return resolved


class RequestLedger:
    """One SQLite transaction arbitrates all clients and processes in this run."""

    def __init__(self, path: Path) -> None:
        self.path = private_path(path)
        if not self.path.parent.is_dir():
            raise ValueError("create a restricted private directory before validation")
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS requests ("
                "id INTEGER PRIMARY KEY, purpose TEXT NOT NULL, "
                "reserved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                "status TEXT NOT NULL DEFAULT 'reserved')"
            )

    def reserve(self, purpose: str) -> int:
        if purpose not in PURPOSES:
            raise ValueError("unknown validation purpose")
        with sqlite3.connect(self.path, timeout=30) as connection:
            connection.execute("BEGIN IMMEDIATE")
            used = connection.execute("SELECT count(*) FROM requests").fetchone()[0]
            if used >= REQUEST_LIMIT:
                raise ValidationBudgetExhausted("48-request validation budget exhausted")
            if (
                purpose == "main_agent"
                and connection.execute(
                    "SELECT count(*) FROM requests WHERE purpose='main_agent'"
                ).fetchone()[0]
                >= 16
            ):
                raise ValidationBudgetExhausted("16-request Main Agent validation budget exhausted")
            cursor = connection.execute("INSERT INTO requests(purpose) VALUES (?)", (purpose,))
            assert cursor.lastrowid is not None
            return cursor.lastrowid

    def finish(self, request_id: int, status: str) -> None:
        if status not in {"response", "transport_error", "cancelled"}:
            raise ValueError("invalid content-free request outcome")
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE requests SET status=? WHERE id=?", (status, request_id))

    def counts(self) -> dict[str, int]:
        with sqlite3.connect(self.path) as connection:
            return dict(
                connection.execute("SELECT purpose,count(*) FROM requests GROUP BY purpose")
            )


class BudgetTransport(httpx.AsyncBaseTransport):
    """Count at the HTTP dispatch boundary, without recording URLs or payloads."""

    def __init__(self, ledger: RequestLedger, *, purpose: str) -> None:
        if purpose not in PURPOSES:
            raise ValueError("unknown validation purpose")
        self._ledger = ledger
        self._purpose = purpose
        self._transport = httpx.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        request_id = self._ledger.reserve(self._purpose)
        try:
            response = await self._transport.handle_async_request(request)
        except BaseException as exc:
            self._ledger.finish(
                request_id, "transport_error" if isinstance(exc, Exception) else "cancelled"
            )
            raise
        self._ledger.finish(request_id, "response")
        return response

    async def aclose(self) -> None:
        await self._transport.aclose()
