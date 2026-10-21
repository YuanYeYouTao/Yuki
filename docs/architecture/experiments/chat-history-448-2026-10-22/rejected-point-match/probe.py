import asyncio
import json
import sqlite3
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path.cwd()))
from sqlalchemy import event, insert, text
from tests.conftest import _seed_database
from tests.support.event_seed import seed_event

from yuki.domain.conversations import ScopeType
from yuki.persistence.database import Database
from yuki.persistence.event_repository import EventLedgerRepository
from yuki.persistence.history_search import namespace_terms, prepare_terms
from yuki.persistence.models import ChatEventModel

out = Path(sys.argv[1])


async def run(n):
    path = out / f"actual-{n}.db"
    db = Database(f"sqlite+aiosqlite:///{path.as_posix()}")
    await db.create_schema()
    await _seed_database(db)
    ledger = EventLedgerRepository(db)
    original, _ = await seed_event(
        ledger,
        bot_user_id="8000",
        scope_type=ScopeType.GROUP,
        group_id="2001",
        sender_user_id="10001",
        direction="inbound",
        platform_message_id="template",
        content="common marker",
    )
    async with db.sessions() as r:
        source = await r.get(ChatEventModel, original.id)
        template = {
            column.name: getattr(source, column.name) for column in ChatEventModel.__table__.columns
        }
    stamp = datetime(2026, 9, 1, tzinfo=UTC)
    tokens = namespace_terms(original.canonical_conversation_id, prepare_terms("common marker"))
    for low in range(0, n, 256):
        rows = []
        for i in range(low, min(low + 256, n)):
            values = dict(
                template,
                id=i + 2,
                canonical_event_id=str(uuid4()),
                platform_message_id=f"synthetic-{i}",
                occurred_at=stamp + timedelta(seconds=i),
            )
            rows.append(values)
        async with db.immediate_session() as w:
            await w.execute(insert(ChatEventModel), rows)
            await w.execute(
                text("INSERT INTO chat_history_fts(rowid,tokens) VALUES(:id,:tokens)"),
                [{"id": row["id"], "tokens": tokens} for row in rows],
            )
    async with db.immediate_session() as w:
        await w.execute(text("ANALYZE"))
    captured = []

    def capture(_conn, _cursor, statement, params, _ctx, _many):
        if statement.startswith("SELECT chat_events.id,"):
            captured.append((statement, params))

    event.listen(db.engine.sync_engine, "before_cursor_execute", capture)
    cases = {
        "recent": dict(limit=20),
        "deep_recent": dict(limit=20, cursor=1000),
        "common_text": dict(limit=20, keyword="common"),
        "common_early_date": dict(
            limit=20, keyword="common", start_at=stamp, end_at=stamp + timedelta(seconds=10)
        ),
        "common_absent_date": dict(
            limit=20,
            keyword="common",
            start_at=stamp - timedelta(days=3),
            end_at=stamp - timedelta(days=2),
        ),
        "common_dense_author": dict(
            limit=20, keyword="common", person_id=original.author_person_id
        ),
        "late_around_empty": dict(limit=20, after_event_id=n + 1, order="oldest"),
    }
    result = {}
    for name, kw in cases.items():
        captured.clear()
        page = await ledger.query_history(conversation_id=original.canonical_conversation_id, **kw)
        sql, params = captured[0]
        c = sqlite3.connect(path)
        plan = c.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
        c.execute(sql, params).fetchall()
        counts = []
        durations = []
        for _ in range(3):
            count = [0]

            def progress(counter=count):
                counter[0] += 100
                return 0

            c.set_progress_handler(progress, 100)
            began = time.perf_counter()
            c.execute(sql, params).fetchall()
            durations.append((time.perf_counter() - began) * 1000)
            c.set_progress_handler(None, 0)
            counts.append(count[0])
        c.close()
        result[name] = {
            "events": len(page.events),
            "has_more": page.has_more,
            "vm_approx": min(counts),
            "sql_ms_min": min(durations),
            "plan": plan,
            "sql": sql,
            "params": params,
        }
    event.remove(db.engine.sync_engine, "before_cursor_execute", capture)
    await db.close()
    return result


async def main():
    result = {
        "environment": {"python": sys.version, "sqlite": sqlite3.sqlite_version},
        "limits": (
            "Actual ORM query SQL on actual current schema; synthetic one-Conversation "
            "text; VM does not include all FTS internal doclist work. Timing is warmed "
            "local SQL only, excludes matching/output. Not production acceptance."
        ),
        "rows": {},
    }
    for size in (5000, 20000):
        result["rows"][str(size)] = await run(size)
        print("completed", size, flush=True)
    (out / "results.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(out / "results.json", flush=True)


asyncio.run(main())
