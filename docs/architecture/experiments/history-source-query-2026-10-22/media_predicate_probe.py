"""Compare real IN/EXISTS reads with dense/sparse media and positive/empty text."""

import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
import statistics
import tempfile
import time
from contextlib import closing
from pathlib import Path

from query_stream_probe import corpus
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.persistence.database import Database
from yuki.persistence.event_repository import EventLedgerRepository
from yuki.persistence.models import ChatEventModel, ConversationMediaItemModel


async def main(output: Path):
    result = {
        "sqlite": sqlite3.sqlite_version,
        "count": 20_000,
        "source_sha256": hashlib.sha256(
            await asyncio.to_thread(Path("src/yuki/persistence/event_repository.py").read_bytes)
        ).hexdigest(),
        "results": [],
    }
    with tempfile.TemporaryDirectory(prefix="yuki-media-predicate-") as directory:
        path = Path(directory) / "media.db"
        conversations = await corpus(path, result["count"])
        for density in ("dense", "sparse"):
            if density == "sparse":
                with closing(sqlite3.connect(path)) as sql, sql:
                    original = sql.execute(
                        "SELECT min(id) FROM chat_events WHERE content LIKE ?",
                        ("%稀有锚点001%",),
                    ).fetchone()[0]
                    sql.execute(
                        "DELETE FROM conversation_media_items WHERE source_event_id!=?", (original,)
                    )
            database = Database(f"sqlite+aiosqlite:///{path.as_posix()}")
            ledger = EventLedgerRepository(database)
            statements = []
            instructions = 0

            def capture(_c, _cur, statement, parameters, _ctx, _many, _statements=statements):
                if statement.lstrip().upper().startswith("SELECT"):
                    _statements.append((statement, parameters))

            def progress():
                nonlocal instructions
                instructions += 1000
                return 0

            event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
            try:
                async with database.engine.connect() as connection:
                    raw = await connection.get_raw_connection()
                    await raw.driver_connection.set_progress_handler(progress, 1000)
                    media = select(ConversationMediaItemModel.source_event_id).where(
                        ConversationMediaItemModel.conversation_id.in_(conversations),
                        ConversationMediaItemModel.source_snapshot_json.is_(None),
                        ConversationMediaItemModel.kind == "image",
                    )
                    exists = media.where(
                        ConversationMediaItemModel.source_event_id == ChatEventModel.id
                    ).exists()
                    for keyword in ("稀有锚点001", "常见", ""):
                        for shape in ("in", "exists"):
                            samples, plans = [], None
                            for _repeat in range(4):
                                statements.clear()
                                instructions = 0
                                started = time.perf_counter()
                                async with AsyncSession(bind=connection) as session:
                                    await session.execute(text("BEGIN"))
                                    page = await ledger.query_history(
                                        session=session,
                                        conversation_id=tuple(conversations),
                                        keyword=keyword,
                                        limit=20,
                                        predicates=(
                                            ChatEventModel.id.in_(media)
                                            if shape == "in"
                                            else exists,
                                        ),
                                    )
                                elapsed = (time.perf_counter() - started) * 1000
                                statement, parameters = statements[0]
                                if plans is None:
                                    plans = [
                                        list(row)
                                        for row in (
                                            await connection.exec_driver_sql(
                                                "EXPLAIN QUERY PLAN " + statement, parameters
                                            )
                                        ).all()
                                    ]
                                    await connection.rollback()
                                samples.append(
                                    dict(
                                        ms=elapsed,
                                        approx_vm_1000=instructions,
                                        returned=len(page.events),
                                        has_more=page.has_more,
                                    )
                                )
                            result["results"].append(
                                dict(
                                    density=density,
                                    keyword=keyword or "(plain media)",
                                    shape=shape,
                                    warm_median_ms=statistics.median(
                                        row["ms"] for row in samples[1:]
                                    ),
                                    samples=samples,
                                    plan=plans,
                                )
                            )
                    await raw.driver_connection.set_progress_handler(None, 0)
            finally:
                event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
                await database.close()
    await asyncio.to_thread(
        output.write_text, json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"sqlite": result["sqlite"], "out": str(output)}))


if __name__ == "__main__":
    os.environ.setdefault("SOUL_FILE", str(Path("config/SOUL.md").resolve()))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    asyncio.run(main(parser.parse_args().out))
