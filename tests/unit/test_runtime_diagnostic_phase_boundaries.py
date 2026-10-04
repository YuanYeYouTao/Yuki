"""Acquisition starts before pool/ping; locks and scheduler phases keep real ordering."""

import asyncio
import time
from types import SimpleNamespace

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from tests.support.social_identity_cases import social_env

from qq_ai_bot.execution_trace import phases
from qq_ai_bot.persistence import sqlite_diagnostics
from qq_ai_bot.persistence.sqlite_diagnostics import install_sqlite_diagnostics
from qq_ai_bot.runtime import work_scheduler
from qq_ai_bot.runtime.protocol_store import ProtocolStore
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.runtime.work_scheduler import WorkScheduler


async def test_application_acquisition_includes_pool_queue_and_ping_before_first_select(
    tmp_path, monkeypatch
):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'phase.db').as_posix()}",
        pool_size=1,
        max_overflow=0,
        pool_pre_ping=True,
    )
    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE sample (id INTEGER)"))
    clock = [0.0]
    monkeypatch.setattr(sqlite_diagnostics, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    dialect = engine.sync_engine.dialect
    ping, execute = dialect.do_ping, dialect.do_execute

    def timed_ping(connection):
        result = ping(connection)
        clock[0] += 2
        return result

    def timed_select(*args):
        result = execute(*args)
        clock[0] += 3
        return result

    monkeypatch.setattr(dialect, "do_ping", timed_ping)
    monkeypatch.setattr(dialect, "do_execute", timed_select)
    diagnostics = install_sqlite_diagnostics(engine.sync_engine)

    async def second():
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT 1")) == 1

    try:
        async with engine.connect() as first:
            assert await first.scalar(text("SELECT 1")) == 1
            task = asyncio.create_task(second())
            await asyncio.sleep(0)
            assert not task.done()
            assert diagnostics.snapshot()["read_sql"]["count"] == 1
            clock[0] += 5
        await task
        report = diagnostics.snapshot()
        assert report["connection_acquisition"]["maximum"] == 7  # queue 5 + ping 2
        assert report["read_sql"]["count"] == 2
        assert report["read_sql"]["maximum"] == 3
        assert report["pool_wait_seconds"] is report["driver_queue_seconds"] is None
    finally:
        await engine.dispose()


async def test_protocol_lock_wait_is_measured_before_acquire_without_changing_publication(
    database, monkeypatch
):
    store = ProtocolStore(database)
    clock = [0.0]
    monkeypatch.setattr(phases, "time", SimpleNamespace(perf_counter=lambda: clock[0]))
    metrics = {}
    token = phases.current_metrics.set(metrics)
    try:
        async with store._lock:
            task = asyncio.create_task(store.put_bytes(b"synthetic"))
            await asyncio.sleep(0)
            assert not task.done() and not store.prepared_refs
            clock[0] = 5
        digest = await task
        assert await store.get_bytes(digest) == b"synthetic"
        assert metrics["protocol_lock_wait_seconds"] == 5
        assert metrics["protocol_lock_held_seconds"] == 0
    finally:
        phases.current_metrics.reset(token)


async def test_scheduler_maintenance_and_resumers_remain_serial_and_separately_timed(
    database, tmp_path, monkeypatch
):
    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    lease = await repository.acquire(env.context.conversation_id, 1)
    source = {"origin": "user_message", "principal_kind": "person", "actor_person_id": env.person}
    accepted = [
        await repository.accept(lease, source_key=key, source=source, goal=key)
        for key in ("first", "second")
    ]
    await repository.release(lease)
    clock, calls = [0.0], []
    monkeypatch.setattr(
        work_scheduler,
        "time",
        SimpleNamespace(
            perf_counter=lambda: clock[0],
            monotonic=time.monotonic,
            time=time.time,
        ),
    )
    for name, seconds in (
        ("repair_abandoned_inputs", 3),
        ("wake_context_rollups", 4),
        ("reclaim_terminal", 5),
    ):
        original = getattr(repository, name)

        async def measured(*args, name=name, seconds=seconds, original=original):
            calls.append(name)
            result = await original(*args)
            clock[0] += seconds
            return result

        monkeypatch.setattr(repository, name, measured)

    async def cleanup(self):
        calls.append("cleanup")
        clock[0] += 6

    async def resume(item):
        calls.append(item["id"])
        clock[0] += 8

    monkeypatch.setattr(ProtocolStore, "cleanup", cleanup)
    scheduler = WorkScheduler(
        repository, SimpleNamespace(resume=resume, last_error=None), chat_admission_enabled=True
    )
    await scheduler.drain_once()
    report = (await scheduler.health())["phase_timings"]
    assert calls == [
        "repair_abandoned_inputs",
        "wake_context_rollups",
        "reclaim_terminal",
        "cleanup",
        *(item["id"] for item in accepted),
    ]
    for name, maximum in (
        ("repair_inputs", 3),
        ("wake_rollups", 4),
        ("reclaim", 5),
        ("protocol_cleanup", 6),
        ("serial_resumer", 8),
    ):
        assert report[name]["maximum"] == maximum
    assert report["serial_resumer"]["count"] == 2
    assert report["selection"]["count"] == 1
