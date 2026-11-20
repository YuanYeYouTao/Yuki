"""E2E: protocol object GC reaches unowned files behind owned ones and drains a backlog.

Replays the 2026-11-20 production shape: 41k objects still owned by resumable
(failed) Work sorted before 97k unowned objects, the 2 GiB total filled up, and
every new checkpoint suspended with ``work_protocol_storage_capacity``. Each
scenario uses a real SQLite file, real ``ProtocolStore`` files and the real
``WorkScheduler`` maintenance tick, opening a fresh ``Database`` per bot start.
Facts are written to ``test-results/protocol-gc-backlog-e2e.json``.

Failure cases written before the change:

- unowned objects sorted after more owned objects than one GC page are never
  reclaimed, because each pass re-reads the same owned prefix;
- a bot restart loses the scan position and starts again from the oldest owned
  rows, so a deployment cadence shorter than a full scan reclaims nothing;
- a backlog larger than one page drains at one page per 600 s maintenance window;
- after the backlog is gone the scheduler keeps paying a cleanup on every tick;
- an owned object, or an unowned object still inside the grace period, is deleted;
- an object re-published by a live Work after it became unowned is deleted;
- a deletion interrupted after marking (file present) or after unlinking (file
  gone, row left) is not finished on the next start;
- the usage counter does not drop by exactly the reclaimed bytes, so capacity
  stays exhausted after the files are gone.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, func, select, update
from tests.unit.test_work_protocol_continuity import _control

from yuki.persistence.database import Database
from yuki.runtime.protocol_schema import objects, refs, usage
from yuki.runtime.protocol_store import ProtocolStore
from yuki.runtime.work_scheduler import WorkScheduler

ARTIFACT = Path(__file__).resolve().parents[2] / "test-results" / "protocol-gc-backlog-e2e.json"
RESULTS: dict[str, Any] = {}
DAY = 86400.0
# Larger than one GC page, so an owned prefix hides every unowned object from
# a scan that restarts at the oldest row.
OWNED = 150
UNOWNED = 200


@pytest.fixture(scope="module", autouse=True)
def _write_artifact():
    yield
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(
        json.dumps(
            {
                "suite": "tests/integration/test_protocol_gc_backlog_e2e.py",
                "generated_at": datetime.now(UTC).isoformat(),
                "scenarios": RESULTS,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )


async def _publish(database, work_id: str, values: list[Any]) -> list[str]:
    store = ProtocolStore(database)
    digests = [await store.put(value) for value in values]
    async with store.publication(work_id) as prepared:
        async with database.immediate_session() as writer:
            await store.publish_refs(writer, work_id, prepared)
    return digests


async def _age(database, digests: list[str], start: float) -> None:
    """Order rows like production: earlier digests were prepared earlier."""
    async with database.immediate_session() as writer:
        for offset, digest in enumerate(digests):
            await writer.execute(
                update(objects).where(objects.c.sha256 == digest).values(prepared_at=start + offset)
            )


async def _release(database, work_id: str, digests: list[str]) -> None:
    async with database.immediate_session() as writer:
        await writer.execute(
            delete(refs).where(refs.c.work_id == work_id, refs.c.sha256.in_(digests))
        )


async def _rows(database, digests: list[str]) -> set[str]:
    async with database.read_session() as reader:
        return set(
            await reader.scalars(select(objects.c.sha256).where(objects.c.sha256.in_(digests)))
        )


async def _usage(database) -> tuple[int, int]:
    async with database.read_session() as reader:
        counted = await reader.scalar(select(usage.c.byte_size))
        actual = await reader.scalar(select(func.coalesce(func.sum(objects.c.byte_size), 0)))
    return int(counted), int(actual)


class _Start:
    """One bot start: a fresh Database and scheduler over the same SQLite file."""

    def __init__(self, url: str) -> None:
        self.database = Database(url)
        self.cleanups = 0

    async def __aenter__(self) -> _Start:
        from yuki.runtime.work_repository import WorkRepository

        async def resume(item):
            return None

        self.scheduler = WorkScheduler(
            WorkRepository(self.database), resume, chat_admission_enabled=True
        )
        # The first tick after start runs maintenance, as in production.
        self.scheduler._last_reclaim = float("-inf")
        return self

    async def __aexit__(self, *exc) -> None:
        await self.database.close()

    async def tick(self) -> None:
        before = self.scheduler._phase_timings["protocol_cleanup"].count
        await self.scheduler.dispatch_once()
        self.cleanups += self.scheduler._phase_timings["protocol_cleanup"].count - before


async def test_restart_reaches_unowned_objects_behind_owned_prefix(database, tmp_path):
    control = await _control(database, tmp_path)
    identity = control.current["id"]
    owned = await _publish(database, identity, [{"owned": index} for index in range(OWNED)])
    unowned = await _publish(database, identity, [{"unowned": index} for index in range(UNOWNED)])
    young = await _publish(database, identity, [{"young unowned": 0}])
    reused = await _publish(database, identity, [{"reused later": 0}])
    old = time.time() - 30 * DAY
    await _age(database, owned, old)
    await _age(database, unowned + reused, old + OWNED)
    await _release(database, identity, unowned + young + reused)
    store = ProtocolStore(database)
    paths = {digest: store._path(digest) for digest in owned + unowned + young + reused}
    counted, actual = await _usage(database)
    assert counted == actual

    # A live Work re-publishes one unowned object before the next start.
    assert await _publish(database, identity, [{"reused later": 0}]) == reused

    ticks = []
    # Each pass is a new bot start that runs only one maintenance tick, so a
    # scan position kept in memory can never carry across restarts.
    for _ in range(3):
        async with _Start(database.url) as start:
            await start.tick()
            ticks.append(len(await _rows(database, unowned)))
    async with _Start(database.url) as start:
        drain_ticks = 0
        while await _rows(database, unowned):
            drain_ticks += 1
            assert drain_ticks <= UNOWNED, "backlog does not drain within consecutive ticks"
            await start.tick()
        drained_cleanups = start.cleanups
        for _ in range(5):
            await start.tick()
        idle_cleanups = start.cleanups - drained_cleanups

    remaining_unowned = await _rows(database, unowned)
    counted, actual = await _usage(database)
    RESULTS["restart_owned_prefix"] = {
        "owned": OWNED,
        "unowned": UNOWNED,
        "unowned_left_after_each_single_tick_start": ticks,
        "extra_ticks_to_drain": drain_ticks,
        "cleanups_while_draining": drained_cleanups,
        "cleanups_on_five_idle_ticks": idle_cleanups,
        "unowned_rows_left": len(remaining_unowned),
        "unowned_files_left": sum(paths[digest].exists() for digest in unowned),
        "owned_rows_kept": len(await _rows(database, owned)),
        "young_kept": young[0] in await _rows(database, young),
        "reused_kept": reused[0] in await _rows(database, reused),
        "usage_counter": counted,
        "usage_actual": actual,
    }
    assert ticks[0] < UNOWNED, "the first tick after a restart reclaims nothing"
    assert remaining_unowned == set()
    assert not any(paths[digest].exists() for digest in unowned)
    assert await _rows(database, owned) == set(owned)
    assert all(paths[digest].exists() for digest in owned)
    for index, digest in enumerate(owned[:3]):
        assert await store.get(digest) == {"owned": index}
    assert await _rows(database, young) == set(young)
    assert paths[young[0]].exists()
    assert await _rows(database, reused) == set(reused)
    assert await store.get(reused[0]) == {"reused later": 0}
    assert counted == actual
    # One pass may still confirm the store is empty; after it the 600 s window applies.
    assert idle_cleanups <= 1, "cleanup keeps running on every tick after the backlog drained"


async def test_next_start_finishes_interrupted_deletions(database, tmp_path):
    control = await _control(database, tmp_path)
    identity = control.current["id"]
    owned = await _publish(database, identity, [{"owned": index} for index in range(OWNED)])
    marked, unlinked = await _publish(database, identity, [{"marked": 0}, {"unlinked": 0}])
    old = time.time() - 30 * DAY
    await _age(database, owned, old)
    await _age(database, [marked, unlinked], old + OWNED)
    await _release(database, identity, [marked, unlinked])
    store = ProtocolStore(database)
    # Crash after the deleting mark committed, and after one file was unlinked.
    async with database.immediate_session() as writer:
        await writer.execute(
            update(objects).where(objects.c.sha256.in_([marked, unlinked])).values(deleting=True)
        )
    store._path(unlinked).unlink()
    before, _ = await _usage(database)
    sizes = {marked: store._path(marked).stat().st_size}

    async with _Start(database.url) as start:
        await start.tick()

    counted, actual = await _usage(database)
    RESULTS["interrupted_deletion"] = {
        "marked_row_left": marked in await _rows(database, [marked]),
        "marked_file_left": store._path(marked).exists(),
        "unlinked_row_left": unlinked in await _rows(database, [unlinked]),
        "usage_before": before,
        "usage_after": counted,
        "usage_actual": actual,
    }
    assert await _rows(database, [marked, unlinked]) == set()
    assert not store._path(marked).exists()
    assert await _rows(database, owned) == set(owned)
    assert counted == actual
    assert counted < before - sizes[marked]
