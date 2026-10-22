"""Measure actual source SQL on a migrated synthetic database; no production traffic."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import statistics
import tempfile
import time
from contextlib import closing
from pathlib import Path
from uuid import UUID

from alembic import command
from alembic.config import Config
from sqlalchemy.dialects import sqlite

from yuki.persistence.database import Database
from yuki.services.history_query import HistorySource, HistoryTarget
from yuki.social.history_sources import history_source_statement
from yuki.social.service import SocialContext

INDEXES = (
    ("ix_conversations_space", "space_id"),
    ("ix_conversations_identity_binding", "identity_binding_id"),
    ("ix_conversations_space_binding", "space_binding_id"),
)


def identity(value: int) -> str:
    return str(UUID(int=value, version=4))


def insert(connection: sqlite3.Connection, table: str, rows: list[dict]) -> None:
    if rows:
        names = tuple(rows[0])
        connection.executemany(
            f"INSERT INTO {table}({','.join(names)}) VALUES({','.join('?' for _ in names)})",
            (tuple(row[name] for name in names) for row in rows),
        )


def seed(path: Path, entries: int, threads: int) -> dict[str, str]:
    now = "2026-10-22 00:00:00"
    timestamps = dict(created_at=now, updated_at=now)
    seen = dict(first_seen_at=now, last_seen_at=now)
    person = identity(1)
    accounts, assignments, spaces, bindings, conversations, aliases = [], [], [], [], [], []
    for index, platform in enumerate(("qq", "telegram")):
        account, stage = identity(10 + index), identity(20 + index)
        accounts.append(
            dict(
                id=account,
                platform=platform,
                external_account_id=f"bot{index}",
                display_name=f"Bot {index}",
                **timestamps,
            )
        )
        assignments.append(
            dict(
                id=stage,
                account_id=account,
                kind="yuki",
                person_id=None,
                status="active",
                revision=1,
                ingest_eligible=1,
                **seen,
                started_at=now,
                ended_at=None,
                **timestamps,
            )
        )
    targets = {}
    for index in range(entries):
        bundle = index // threads
        private = bundle % 2 == 0
        platform_index = bundle // 2 % 2
        platform = ("qq", "telegram")[platform_index]
        owner, binding = identity(100_000 + bundle), identity(200_000 + bundle)
        if index % threads == 0:
            if private:
                accounts.append(
                    dict(
                        id=owner,
                        platform=platform,
                        external_account_id=f"peer{bundle}",
                        display_name=f"Person {bundle}",
                        **timestamps,
                    )
                )
                assignments.append(
                    dict(
                        id=binding,
                        account_id=owner,
                        kind="person",
                        person_id=person,
                        status="active",
                        revision=1,
                        ingest_eligible=1,
                        **seen,
                        started_at=now,
                        ended_at=None,
                        **timestamps,
                    )
                )
            else:
                spaces.append(
                    dict(
                        id=owner,
                        name=f"Space {bundle}",
                        enabled=1,
                        autonomous_enabled=0,
                        require_mention=0,
                        revision=1,
                        **timestamps,
                    )
                )
                bindings.append(
                    dict(
                        id=binding,
                        space_id=owner,
                        platform=platform,
                        external_space_id=f"group{bundle}",
                        display_name=f"Group {bundle}",
                        status="active",
                        revision=1,
                        **seen,
                        **timestamps,
                    )
                )
        conversation, alias = identity(1_000_000 + index), identity(2_000_000 + index)
        conversations.append(
            dict(
                id=conversation,
                kind="private" if private else "space",
                person_id=person if private else None,
                identity_binding_id=binding if private else None,
                space_id=None if private else owner,
                space_binding_id=None if private else binding,
                entry_presence_id=identity(20 + platform_index),
                thread_id=str(index % threads),
                primary_alias_id=alias,
                primary_marker=1,
                generation=1,
                prompt_source_revision=0,
                starts_after_event_id=0,
                last_event_id=0,
                last_generation_change_event_id=0,
                revision=1,
                **timestamps,
            )
        )
        aliases.append(
            dict(
                id=alias,
                conversation_id=conversation,
                scope_key=f"source-probe:{index}",
                is_primary=1,
                **timestamps,
            )
        )
        targets["private_binding" if private else "space_binding"] = binding
        if not private:
            targets["space"] = owner
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("PRAGMA foreign_keys=ON")
        insert(
            connection,
            "persons",
            [
                dict(
                    id=person,
                    merged_into_person_id=None,
                    display_name="Original Person",
                    enabled=1,
                    is_superuser=0,
                    revision=1,
                    **timestamps,
                )
            ],
        )
        for table, rows in (
            ("accounts", accounts),
            ("account_assignments", assignments),
            ("spaces", spaces),
            ("space_bindings", bindings),
            ("canonical_conversations", conversations),
            ("conversation_legacy_aliases", aliases),
        ):
            insert(connection, table, rows)
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
    targets["context"] = conversations[0]["id"]
    return targets


async def source_sql(url: str, targets: dict[str, str]) -> dict[str, str]:
    database = Database(url)
    result = {}
    try:
        async with database.sessions() as session:
            for label, target in (
                ("space", HistoryTarget(space_id=targets["space"])),
                ("private_binding", HistoryTarget(binding_id=targets["private_binding"])),
                ("space_binding", HistoryTarget(binding_id=targets["space_binding"])),
                ("conversation", HistoryTarget(conversation_id=targets["context"])),
                ("telegram", None),
            ):
                statement = await history_source_statement(
                    session,
                    context=SocialContext("probe", "source", targets["context"]),
                    target=target,
                    source=HistorySource(platform="telegram") if label == "telegram" else None,
                )
                for projection in ("scope", "display"):
                    selected = statement
                    if projection == "scope":
                        selected = selected.with_only_columns(
                            selected.selected_columns.conversation_id,
                            selected.selected_columns.platform,
                        )
                    selected = selected.order_by(selected.selected_columns.conversation_id)
                    if projection == "display":
                        selected = selected.limit(21)
                    result[f"{label}_{projection}"] = str(
                        selected.compile(
                            dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True}
                        )
                    )
    finally:
        await database.close()
    return result


def measured(connection: sqlite3.Connection, sql: str) -> dict:
    samples, steps = [], []
    count = 0
    for _ in range(7):
        instructions = 0

        def progressed():
            nonlocal instructions
            instructions += 100
            return 0

        connection.set_progress_handler(progressed, 100)
        started = time.perf_counter()
        count = len(connection.execute(sql).fetchall())
        samples.append((time.perf_counter() - started) * 1000)
        steps.append(instructions)
        connection.set_progress_handler(None, 0)
    return dict(
        rows=count,
        first_ms=samples[0],
        warm_median_ms=statistics.median(samples[1:]),
        approximate_vm_median=statistics.median(steps),
        plan=[row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + sql)],
    )


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entries", type=int, default=20_000)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    paths = (
        "src/yuki/social/history_sources.py",
        "src/yuki/conversation/canonical_db_models.py",
        "migrations/versions/0148_history_source_indexes.py",
    )
    report = dict(
        sqlite=sqlite3.sqlite_version,
        entries=args.entries,
        threads=args.threads,
        source_sha256={path: hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in paths},
    )
    with tempfile.TemporaryDirectory(prefix="yuki-source-query-") as temporary:
        path = Path(temporary) / "sources.db"
        url = f"sqlite+aiosqlite:///{path.as_posix()}"
        os.environ["DATABASE_URL"] = url
        os.environ["SOUL_FILE"] = str(Path("config/SOUL.md").resolve())
        command.upgrade(Config("alembic.ini"), "0148")
        targets = seed(path, args.entries, args.threads)
        statements = asyncio.run(source_sql(url, targets))
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("PRAGMA query_only=ON")
            report["indexed"] = {
                name: measured(connection, sql) for name, sql in statements.items()
            }
            connection.execute("PRAGMA query_only=OFF")
            for name, _column in INDEXES:
                connection.execute(f"DROP INDEX {name}")
            connection.commit()
        # A fresh connection avoids retaining prepared EXPLAIN statements after DDL.
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("PRAGMA query_only=ON")
            report["before"] = {name: measured(connection, sql) for name, sql in statements.items()}
        report["sql"] = statements
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {"sqlite": report["sqlite"], "entries": args.entries, "out": str(args.out)},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
