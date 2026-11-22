"""Offline replay of the v7 and v8 extraction instructions (memory layering P3 §11.2).

Runs the production ``memory_extraction`` model route twice per source event, once
with the deployed instruction (switch off) and once with the tightened one (switch
on), and only generates claims: nothing is written to memory. The source database is
opened read-only and copied with SQLite's backup API into a temporary file; the model
runtime records its invocations in that copy, never in the source.

The result file holds event IDs, claim counts, claim kinds and the §11.3 regex count
only, never claim or chat text; judging whether a claim is the long-term rule happens
inside the deployment environment. This script calls a paid model and is not product
code.

Usage (inside the deployment environment, with the bot's model configuration):
    python scripts/replay_extraction_tightening.py --source data/yuki.db \\
        --events replay-sets.json --self-name <character name> --output replay.json

``replay-sets.json`` maps set names to source event IDs, e.g.
``{"A": [101, 102], "B": [201], "C": [301, 302]}`` (task book §11.2 sets A, B, C).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from yuki.cli import _cli_models
from yuki.config import Settings
from yuki.memory.event_extractor import MemoryEventExtractor
from yuki.memory.extraction import (
    EXTRACTION_PROMPT_VERSION,
    TIGHTENED_EXTRACTION_PROMPT_VERSION,
)
from yuki.memory.quality.task_scoped import TASK_SCOPED_PATTERN
from yuki.persistence.database import Database
from yuki.persistence.event_repository import EventLedgerRepository
from yuki.persistence.people_repository import PeopleRepository
from yuki.services.concurrency import ConcurrencyManager


def _copy_read_only(source: Path, target: Path) -> None:
    origin = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    copy = sqlite3.connect(target)
    try:
        origin.backup(copy)
    finally:
        copy.close()
        origin.close()


async def _replay(
    database_path: Path, sets: dict[str, list[int]], self_name: str
) -> dict[str, Any]:
    url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    settings = Settings(database_url=url)
    database = Database(url)
    try:
        async with _cli_models(settings, database) as (active, models):
            ledger = EventLedgerRepository(database)
            extractor = MemoryEventExtractor(
                models,
                ConcurrencyManager(),
                people=PeopleRepository(database),
                timezone=active.default_timezone,
            )
            results: dict[str, list[dict[str, Any]]] = {}
            for name, event_ids in sets.items():
                rows: list[dict[str, Any]] = []
                for event_id in event_ids:
                    event = await ledger.get_event(event_id)
                    if event is None:
                        rows.append({"event_id": event_id, "missing": True})
                        continue
                    context = await ledger.list_scope_before(
                        event.scope, before_event_id=event.id, limit=8, message_only=True
                    )
                    row: dict[str, Any] = {"event_id": event_id}
                    for label, tightened in (("v7", False), ("v8", True)):
                        extracted = await extractor.extract_batch(
                            (event,),
                            context=context,
                            max_output_tokens=active.memory_batch_max_output_tokens,
                            self_name=self_name,
                            tightened=tightened,
                        )
                        claims = [item.claim for item in extracted.output.claims]
                        row[label] = {
                            "claims": len(claims),
                            "kinds": sorted(claim.kind.value for claim in claims),
                            "task_scoped_pattern": sum(
                                bool(TASK_SCOPED_PATTERN.search(claim.content)) for claim in claims
                            ),
                        }
                    rows.append(row)
                results[name] = rows
    finally:
        await database.close()
    return {
        "prompt_versions": {
            "v7": EXTRACTION_PROMPT_VERSION,
            "v8": TIGHTENED_EXTRACTION_PROMPT_VERSION,
        },
        "sets": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", type=Path, required=True, help="production SQLite file")
    parser.add_argument("--events", type=Path, required=True, help="JSON: set name -> event IDs")
    parser.add_argument("--self-name", required=True, help="current character name")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sets = {
        str(name): [int(item) for item in ids]
        for name, ids in json.loads(args.events.read_text(encoding="utf-8")).items()
    }
    with tempfile.TemporaryDirectory() as directory:
        copy = Path(directory) / "replay.db"
        _copy_read_only(args.source, copy)
        report = asyncio.run(_replay(copy, sets, str(args.self_name)))
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(args.output)


if __name__ == "__main__":
    main()
