"""Upgrade an actual 0154 schema with original ledger and receipt rows."""

import asyncio
import sqlite3
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from tests.unit.test_canonical_ingress import _admit_private

from yuki.conversation.canonical_db_models import CanonicalEventReceiptModel
from yuki.memory.extraction import source_event_fingerprint
from yuki.persistence.metadata import Base
from yuki.persistence.models import ChatEventModel
from yuki.persistence.repository_helpers import _event_record
from yuki.persistence.schema_guard import require_canonical_schema


async def test_0154_upgrade_retains_original_sources_and_complete_receipts(
    database, tmp_path, monkeypatch
):
    uow, admitted = await _admit_private(database, "142301", text="original source 字节")
    first = await uow.append_inbound(admitted.message, admitted)
    complete_key = "142301" + "0" * 124
    historical_keys = [f"200{index:08d}" for index in range(384)]
    async with database.immediate_session() as session:
        source = await session.get(ChatEventModel, first.event.id)
        values = {
            column.name: getattr(source, column.name)
            for column in ChatEventModel.__table__.columns
            if column.name not in {"id", "canonical_event_id", "origin_key", "native_message_id"}
        }
        for key in ("app-request-original", "social-operation:original", "file:resource"):
            session.add(ChatEventModel(**values, origin_key=key, canonical_event_id=str(uuid4())))
        extended = ChatEventModel(
            **values, origin_key=complete_key, canonical_event_id=str(uuid4())
        )
        session.add(extended)
        receipt = await session.scalar(
            select(CanonicalEventReceiptModel).where(
                CanonicalEventReceiptModel.canonical_event_id == first.event.canonical_event_id
            )
        )
        session.add(
            CanonicalEventReceiptModel(
                **{
                    column.name: getattr(receipt, column.name)
                    for column in CanonicalEventReceiptModel.__table__.columns
                    if column.name not in {"id", "ingress_key", "canonical_event_id"}
                },
                ingress_key=complete_key,
                canonical_event_id=extended.canonical_event_id,
            )
        )
        for key in historical_keys:
            canonical_id = str(uuid4())
            session.add(ChatEventModel(**values, origin_key=key, canonical_event_id=canonical_id))
            session.add(
                CanonicalEventReceiptModel(
                    **{
                        column.name: getattr(receipt, column.name)
                        for column in CanonicalEventReceiptModel.__table__.columns
                        if column.name not in {"id", "ingress_key", "canonical_event_id"}
                    },
                    ingress_key=key,
                    canonical_event_id=canonical_id,
                )
            )
    async with database.read_session() as session:
        originals = {
            row.id: source_event_fingerprint(_event_record(row))
            for row in await session.scalars(select(ChatEventModel))
        }
    async with database.engine.connect() as connection:
        await connection.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")

    old = tmp_path / "actual-0154.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{old.as_posix()}")
    await asyncio.to_thread(command.upgrade, Config("alembic.ini"), "0154")
    source_path = Path(database.url.split("///", 1)[1])
    with sqlite3.connect(old) as target:
        target.execute("ATTACH DATABASE ? AS original", (str(source_path),))
        # Copy fixture facts into the frozen old schema using its actual columns.
        # Old migration code, constraints, indexes and trigger SQL remain intact.
        triggers = target.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND sql IS NOT NULL"
        ).fetchall()
        for name, _ in triggers:
            target.execute('DROP TRIGGER "' + name + '"')
        for table in Base.metadata.tables:
            columns = target.execute(f'PRAGMA table_info("{table}")').fetchall()
            if not columns:
                continue
            names = [item[1] for item in columns]
            projected = [
                '"origin_key"'
                if table == "chat_events" and name == "platform_message_id"
                else 'substr("ingress_key",1,128)'
                if table == "canonical_event_receipts" and name == "platform_message_id"
                else '"' + name + '"'
                for name in names
            ]
            target.execute(f'DELETE FROM "{table}"')
            target.execute(
                f'INSERT INTO "{table}" ('
                + ",".join('"' + name + '"' for name in names)
                + ") SELECT "
                + ",".join(projected)
                + f' FROM original."{table}"'
            )
        for _name, sql in triggers:
            target.execute(sql)
        # Preserve an allocated high-water mark even when its last row was deleted.
        target.execute("UPDATE sqlite_sequence SET seq=9000 WHERE name='chat_events'")
        protected = {
            table: target.execute(f'SELECT * FROM "{table}"').fetchall()
            for table in Base.metadata.tables
            if table not in {"chat_events", "canonical_event_receipts"}
            and target.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
        }
    backfill_steps = 0

    def count_backfill_steps():
        nonlocal backfill_steps
        backfill_steps += 100
        return 0

    def is_inbound_backfill(statement):
        return statement.lstrip().startswith("UPDATE chat_events AS e SET native_message_id") and (
            "e.direction='inbound'" in statement
        )

    def before_backfill(connection, _cursor, statement, _parameters, _context, _many):
        if is_inbound_backfill(statement):
            connection.connection.run_async(
                lambda driver: driver.set_progress_handler(count_backfill_steps, 100)
            )

    def after_backfill(connection, _cursor, statement, _parameters, _context, _many):
        if is_inbound_backfill(statement):
            connection.connection.run_async(lambda driver: driver.set_progress_handler(None, 0))

    event.listen(Engine, "before_cursor_execute", before_backfill)
    event.listen(Engine, "after_cursor_execute", after_backfill)
    try:
        await asyncio.to_thread(command.upgrade, Config("alembic.ini"), "head")
    finally:
        event.remove(Engine, "before_cursor_execute", before_backfill)
        event.remove(Engine, "after_cursor_execute", after_backfill)
    # Each event resolves its own receipt. Scanning every receipt for the same
    # account would make this migration quadratic after the indexes are rebuilt.
    assert 0 < backfill_steps < 300 * len(historical_keys)
    await require_canonical_schema(f"sqlite+aiosqlite:///{old.as_posix()}")
    with sqlite3.connect(old) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT * FROM chat_events ORDER BY id").fetchall()
        assert {
            row["id"]: source_event_fingerprint(_event_record(dict(row))) for row in rows
        } == originals
        assert rows[0]["native_message_id"] == "142301"
        assert all(row["native_message_id"] is None for row in rows[1:4])
        assert rows[4]["native_message_id"] == complete_key
        assert [row["native_message_id"] for row in rows[5:]] == historical_keys
        assert [row["origin_key"] for row in rows] == [
            "142301",
            "app-request-original",
            "social-operation:original",
            "file:resource",
            complete_key,
            *historical_keys,
        ]
        assert (
            connection.execute(
                "SELECT ingress_key FROM canonical_event_receipts WHERE canonical_event_id=?",
                (rows[4]["canonical_event_id"],),
            ).fetchone()[0]
            == complete_key
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            connection.execute(
                "SELECT seq FROM sqlite_sequence WHERE name='chat_events'"
            ).fetchone()[0]
            == 9000
        )
        for table, original in protected.items():
            assert [
                tuple(row) for row in connection.execute(f'SELECT * FROM "{table}"')
            ] == original
        # Same prefix, different full transport keys are both valid at the new head.
        receipt = dict(
            connection.execute("SELECT * FROM canonical_event_receipts LIMIT 1").fetchone()
        )
        receipt.pop("id")
        for suffix in ("a", "b"):
            receipt["ingress_key"] = "x" * 128 + suffix
            connection.execute(
                "INSERT INTO canonical_event_receipts ("
                + ",".join(receipt)
                + ") VALUES ("
                + ",".join("?" for _ in receipt)
                + ")",
                tuple(receipt.values()),
            )
