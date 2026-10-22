"""Observe caller-owned snapshot and WAL checkpoint progress in a tiny migrated DB."""

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from alembic import command
from alembic.config import Config
from source_probe import seed
from sqlalchemy import select, text, update

from yuki.identity.db_models import SpaceBindingModel
from yuki.persistence.database import Database
from yuki.services.history_query import HistoryTarget
from yuki.social.history_sources import history_source_statement
from yuki.social.service import SocialContext


def checkpoint(path: Path, mode: str) -> dict:
    with closing(sqlite3.connect(path)) as connection:
        result = connection.execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
        page_size = connection.execute("PRAGMA page_size").fetchone()[0]
    wal = Path(str(path) + "-wal")
    return {
        "mode": mode,
        "busy": result[0],
        "log_frames": result[1],
        "checkpointed_frames": result[2],
        "page_size": page_size,
        "wal_bytes": wal.stat().st_size if wal.exists() else 0,
    }


async def observe(path: Path, url: str, targets: dict) -> dict:
    database = Database(url)
    try:
        async with database.engine.connect() as connection:
            await connection.exec_driver_sql("SELECT 1")
        baseline = await asyncio.to_thread(checkpoint, path, "TRUNCATE")
        async with database.sessions() as reader:
            await reader.execute(text("BEGIN"))
            statement = await history_source_statement(
                reader,
                context=SocialContext("wal-probe", "source", targets["context"]),
                target=HistoryTarget(space_id=targets["space"]),
            )
            stream = await reader.stream(statement.execution_options(yield_per=1))
            original = (await stream.mappings().fetchmany(1))[0]["conversation_name"]
            await stream.close()
            cursor_closed = stream.closed
            transaction_active_after_cursor_close = reader.in_transaction()

            changed = "updated after reader snapshot"
            async with database.immediate_session() as writer:
                await writer.execute(
                    update(SpaceBindingModel)
                    .where(SpaceBindingModel.id == targets["space_binding"])
                    .values(display_name=changed)
                )
            value = select(SpaceBindingModel.display_name).where(
                SpaceBindingModel.id == targets["space_binding"]
            )
            old_value_in_snapshot = await reader.scalar(value)
            async with database.sessions() as current:
                committed_value = await current.scalar(value)
            while_owner_holds_snapshot = await asyncio.to_thread(checkpoint, path, "PASSIVE")
            # This is the probe's transaction owner ending its own read, not query code.
            await reader.rollback()
            transaction_active_after_owner_end = reader.in_transaction()
        after_owner_end = await asyncio.to_thread(checkpoint, path, "PASSIVE")
        final_truncate = await asyncio.to_thread(checkpoint, path, "TRUNCATE")
        assert cursor_closed and transaction_active_after_cursor_close
        assert not transaction_active_after_owner_end
        assert original == old_value_in_snapshot and committed_value == changed
        assert (
            while_owner_holds_snapshot["log_frames"]
            > (while_owner_holds_snapshot["checkpointed_frames"])
        )
        assert after_owner_end["checkpointed_frames"] == after_owner_end["log_frames"]
        return {
            "baseline": baseline,
            "cursor_closed": cursor_closed,
            "transaction_active_after_cursor_close": transaction_active_after_cursor_close,
            "writer_commit_visible_to_fresh_reader": committed_value == changed,
            "original_value": original,
            "value_inside_old_snapshot": old_value_in_snapshot,
            "while_owner_holds_snapshot": while_owner_holds_snapshot,
            "transaction_active_after_owner_end": transaction_active_after_owner_end,
            "after_owner_end": after_owner_end,
            "final_truncate": final_truncate,
        }
    finally:
        await database.close()


def main(output: Path) -> None:
    logging.basicConfig(level=logging.WARNING)
    inputs = (
        "src/yuki/persistence/database.py",
        "src/yuki/social/history_sources.py",
        "migrations/versions/0148_history_source_indexes.py",
    )
    result = {
        "sqlite": sqlite3.sqlite_version,
        "schema": "0148",
        "conversation_count": 8,
        "not_a_load_benchmark": True,
        "source_sha256": {
            path: hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in inputs
        },
    }
    os.environ["SOUL_FILE"] = str(Path("config/SOUL.md").resolve())
    with tempfile.TemporaryDirectory(prefix="yuki-history-wal-") as directory:
        path = Path(directory) / "wal.db"
        url = f"sqlite+aiosqlite:///{path.as_posix()}"
        os.environ["DATABASE_URL"] = url
        command.upgrade(Config("alembic.ini"), "0148")
        targets = seed(path, 8, 2)
        result["observation"] = asyncio.run(observe(path, url, targets))
    result["temporary_database_removed"] = not path.exists()
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"sqlite": result["sqlite"], "out": str(output)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    main(parser.parse_args().out)
