"""Offline real repository streaming probe; synthetic isolated full-schema databases only.

Run with --out PATH [--rows 5000 --rows 20000]. Every case is a fresh child
process, so sample 0 includes its first connection/OpenCC conversion; later
samples are same-process warm reads. Timings include diagnostic hooks and are
not production latency or a universal performance claim. Corpus bodies repeat
on purpose: worst-case literal false positives without random-data FTS noise.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncScalarResult, AsyncSession
from tests.conftest import _seed_database
from tests.support.event_seed import seed_event

from yuki.domain.conversations import ScopeType
from yuki.identity.write_settings import IdentityWriteSettings, configure_identity_write_settings
from yuki.persistence.database import Database
from yuki.persistence.event_repository import EventLedgerRepository
from yuki.persistence.history_search import namespace_terms, prepare_terms


def memory():
    path = Path("/proc/self/status")
    if not path.exists():
        return None
    data = {}
    for line in path.read_text().splitlines():
        if line.startswith(("VmRSS:", "VmHWM:")):
            key, value, _unit = line.split()
            data[key.rstrip(":") + "_kib"] = int(value)
    return data


async def corpus(path, count):
    configure_identity_write_settings(IdentityWriteSettings())
    database = Database(f"sqlite+aiosqlite:///{path.as_posix()}")
    try:
        await database.create_schema()
        await _seed_database(database)
        ledger = EventLedgerRepository(database)
        seeds = []
        for index, group in enumerate(("2001", "2002", "2999", "3001")):
            seed, _ = await seed_event(
                ledger,
                bot_user_id="8000",
                scope_type=ScopeType.GROUP,
                group_id=group,
                sender_user_id="10001",
                direction="inbound",
                platform_message_id=f"template-{index}",
                content="template",
                segments=({"type": "image", "data": {}},),
            )
            seeds.append(seed)
    finally:
        await database.close()
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        templates = [
            dict(connection.execute("SELECT * FROM chat_events WHERE id=?", (row.id,)).fetchone())
            for row in seeds
        ]
        media = [
            dict(
                connection.execute(
                    "SELECT * FROM conversation_media_items WHERE source_event_id=?", (row.id,)
                ).fetchone()
            )
            for row in seeds
        ]
        fields = [name for name in templates[0] if name != "id"]
        media_fields = tuple(media[0])
        tokens = {}
        bodies = ("abc ... bcd 常见消息", "abc ... bcd 常见消息 稀有锚点001")
        for index, template in enumerate(templates):
            for body in bodies:
                tokens[index, body] = namespace_terms(
                    template["canonical_conversation_id"],
                    prepare_terms(body),
                    author_person_id=template["author_person_id"],
                    author_kind=template["author_kind"],
                )
        attachment = json.dumps([{"type": "image", "data": {"caption": "x" * 4096}}])
        for number in range(count):
            index = number % len(templates)
            body = bodies[1 if number == 0 else 0]
            row = dict(
                templates[index],
                canonical_event_id=str(uuid4()),
                platform_message_id=f"probe-{number}",
                content=body,
                segments_json=attachment,
            )
            event_id = connection.execute(
                "INSERT INTO chat_events("
                + ",".join(fields)
                + ") VALUES("
                + ",".join("?" for _ in fields)
                + ") RETURNING id",
                [row[name] for name in fields],
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO chat_history_fts(rowid,tokens) VALUES(?,?)",
                (event_id, tokens[index, body]),
            )
            item = dict(media[index], source_event_id=event_id)
            connection.execute(
                "INSERT INTO conversation_media_items("
                + ",".join(media_fields)
                + ") VALUES("
                + ",".join("?" for _ in media_fields)
                + ")",
                [item[name] for name in media_fields],
            )
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return [row.canonical_conversation_id for row in seeds]


async def sample(path, conversations, case):
    import yuki.persistence.event_repository as module

    database = Database(f"sqlite+aiosqlite:///{path.as_posix()}")
    ledger = EventLedgerRepository(database)
    cases = {
        "plain_recent": {},
        "rare": dict(keyword="稀有锚点001"),
        "rare_wide_date": dict(keyword="稀有锚点001", start_at=datetime(2000, 1, 1, tzinfo=UTC)),
        "rare_wide_date_media": dict(
            keyword="稀有锚点001", start_at=datetime(2000, 1, 1, tzinfo=UTC), has=("image",)
        ),
        "common": dict(keyword="常见"),
        "all_false_positive": dict(keyword="abcd"),
        "exclude_only": dict(exclude=("abc",)),
    }
    original_match, original_record = module.match_fields, module._event_record
    original_stream, original_fetch = AsyncSession.stream_scalars, AsyncScalarResult.fetchmany
    verified = built = instructions = fetches = 0
    opened_at = first_fetch_at = None
    instructions_at_open = instructions_at_first_fetch = None
    statements = []
    timings = []
    start = 0.0

    def match(*args):
        nonlocal verified
        verified += 1
        return original_match(*args)

    def record(*args):
        nonlocal built
        built += 1
        return original_record(*args)

    def progress():
        nonlocal instructions
        instructions += 1000
        return 0

    def sql(_c, _cur, statement, parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append((statement, parameters))

    async def stream(self, *args, **kwargs):
        nonlocal opened_at, instructions_at_open
        result = await original_stream(self, *args, **kwargs)
        opened_at = (time.perf_counter() - start) * 1000
        instructions_at_open = instructions
        return result

    async def fetch(self, *args, **kwargs):
        nonlocal fetches, first_fetch_at, instructions_at_first_fetch
        rows = await original_fetch(self, *args, **kwargs)
        fetches += 1
        if first_fetch_at is None:
            first_fetch_at = (time.perf_counter() - start) * 1000
            instructions_at_first_fetch = instructions
        return rows

    module.match_fields, module._event_record = match, record
    AsyncSession.stream_scalars, AsyncScalarResult.fetchmany = stream, fetch
    event.listen(database.engine.sync_engine, "before_cursor_execute", sql)
    try:
        for number in range(3):
            verified = built = instructions = fetches = 0
            opened_at = first_fetch_at = None
            instructions_at_open = instructions_at_first_fetch = None
            statements.clear()
            start = time.perf_counter()
            before_memory = memory()
            async with database.sessions() as session:
                await session.execute(text("BEGIN"))
                connection = await session.connection()
                raw = await connection.get_raw_connection()
                await raw.driver_connection.set_progress_handler(progress, 1000)
                page = await ledger.query_history(
                    session=session, conversation_id=tuple(conversations), limit=20, **cases[case]
                )
                await raw.driver_connection.set_progress_handler(None, 0)
            elapsed = (time.perf_counter() - start) * 1000
            captured = list(statements)
            plans = []
            async with database.engine.connect() as connection:
                for statement, parameters in captured:
                    plans.append(
                        [
                            list(row)
                            for row in (
                                await connection.exec_driver_sql(
                                    "EXPLAIN QUERY PLAN " + statement, parameters
                                )
                            ).all()
                        ]
                    )
            timings.append(
                dict(
                    sample=number,
                    process_first_read=number == 0,
                    elapsed_ms=elapsed,
                    returned=len(page.events),
                    has_more=page.has_more,
                    sql_count=len(captured),
                    verified_rows=verified,
                    full_event_records=built,
                    fetches=fetches,
                    stream_open_ms=opened_at,
                    first_fetch_ms=first_fetch_at,
                    approx_sqlite_vm_1000=instructions,
                    approx_vm_at_open=instructions_at_open,
                    approx_vm_at_first_fetch=instructions_at_first_fetch,
                    plans=plans,
                    memory_before=before_memory,
                    memory_after=memory(),
                )
            )
    finally:
        module.match_fields, module._event_record = original_match, original_record
        AsyncSession.stream_scalars, AsyncScalarResult.fetchmany = original_stream, original_fetch
        event.remove(database.engine.sync_engine, "before_cursor_execute", sql)
        await database.close()
    return dict(case=case, samples=timings)


if __name__ == "__main__":
    os.environ.setdefault("SOUL_FILE", str(Path("config/SOUL.md").resolve()))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--rows", type=int, action="append")
    parser.add_argument(
        "--sample",
        choices=(
            "plain_recent",
            "rare",
            "rare_wide_date",
            "rare_wide_date_media",
            "common",
            "all_false_positive",
            "exclude_only",
        ),
    )
    parser.add_argument("--database", type=Path)
    parser.add_argument("--conversations")
    args = parser.parse_args()
    if args.sample:
        result = asyncio.run(sample(args.database, json.loads(args.conversations), args.sample))
        print(json.dumps(result, ensure_ascii=False))
    else:
        result = dict(
            python=platform.python_version(),
            sqlite=sqlite3.sqlite_version,
            source_sha256=hashlib.sha256(
                Path("src/yuki/persistence/event_repository.py").read_bytes()
            ).hexdigest(),
            corpus=(
                "4 actual Conversations; repeated gram false positives; one oldest rare hit; "
                "4KiB attachment JSON per row; original template rows excluded by keywords"
            ),
            vm_excludes_fts_internal_work=True,
            rows=[],
        )
        with tempfile.TemporaryDirectory(prefix="yuki-history-stream-") as directory:
            for count in args.rows or (5000, 20000):
                path = Path(directory) / f"probe-{count}.db"
                conversations = asyncio.run(corpus(path, count))
                cases = []
                for case in (
                    "plain_recent",
                    "rare",
                    "rare_wide_date",
                    "rare_wide_date_media",
                    "common",
                    "all_false_positive",
                    "exclude_only",
                ):
                    completed = subprocess.run(
                        [
                            sys.executable,
                            str(Path(__file__).resolve()),
                            "--sample",
                            case,
                            "--database",
                            str(path),
                            "--conversations",
                            json.dumps(conversations),
                        ],
                        check=True,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                    )
                    cases.append(json.loads(completed.stdout))
                result["rows"].append(
                    dict(
                        events=count,
                        conversations=len(conversations),
                        database_bytes=path.stat().st_size,
                        cases=cases,
                    )
                )
        output = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        args.out.write_text(output, encoding="utf-8")
        print(
            json.dumps(
                dict(
                    sqlite=result["sqlite"],
                    counts=[row["events"] for row in result["rows"]],
                    out=str(args.out),
                ),
                ensure_ascii=False,
            )
        )
