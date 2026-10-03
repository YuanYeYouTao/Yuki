"""Timing boundaries use real SQLite transactions and deterministic clocks/barriers."""

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import create_async_engine
from tests.unit.test_sqlite_diagnostics import holders

from qq_ai_bot.persistence import sqlite_diagnostics as module
from qq_ai_bot.persistence.diagnostic_writer import DiagnosticWriter
from qq_ai_bot.persistence.sqlite_diagnostics import install_sqlite_diagnostics


@pytest.mark.parametrize("begin", [None, "BEGIN IMMEDIATE"])
def test_acquisition_sql_and_real_commit_are_separate_lower_bound_phases(
    tmp_path, monkeypatch, begin
):
    engine = create_engine(f"sqlite:///{tmp_path / 'phases.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE timing (id INTEGER)"))
    clock = [10.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    execute, commit = engine.dialect.do_execute, engine.dialect.do_commit

    def measured_execute(cursor, statement, parameters, context=None):
        execute(cursor, statement, parameters, context)
        clock[0] += 3

    def measured_commit(connection):
        clock[0] += 5
        commit(connection)

    engine.dialect.do_execute = measured_execute
    engine.dialect.do_commit = measured_commit
    diagnostics = install_sqlite_diagnostics(engine)
    try:
        with engine.connect() as connection:
            if begin:
                connection.execute(text(begin))
            connection.execute(text("INSERT INTO timing VALUES (1)"))
            connection.commit()
        report = diagnostics.snapshot()
        assert report["commit"]["maximum"] == 5
        assert report["held_lower_bound"]["maximum"] == (8 if begin else 5)
        assert report["acquire"]["count"] == bool(begin)
        assert report["first_write"]["count"] == (not begin)
        assert report["sql"]["count"] == 1
        assert report["driver_queue_seconds"] is report["sqlite_wait_seconds"] is None
        assert all(
            sum(report[name]["buckets"]) == report[name]["count"] for name in ("sql", "commit")
        )
    finally:
        engine.dispose()


def test_failed_acquisition_never_becomes_a_holder_and_inner_savepoint_keeps_outer(
    tmp_path, caplog
):
    engine = create_engine(f"sqlite:///{tmp_path / 'nested.db'}", connect_args={"timeout": 0})
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE timing (id INTEGER)"))
    diagnostics = install_sqlite_diagnostics(engine)
    try:
        with engine.connect() as owner:
            owner.execute(text("BEGIN IMMEDIATE"))
            with owner.begin_nested():
                owner.execute(text("INSERT INTO timing VALUES (1)"))
            nested = owner.begin_nested()
            owner.execute(text("UPDATE timing SET id = 2"))
            nested.rollback()
            with engine.connect() as waiting, pytest.raises(OperationalError):
                waiting.execute(text("BEGIN IMMEDIATE"))
            assert len(holders(caplog)[-1]) == 1
            assert holders(caplog)[-1][0]["precision"] == "lower_bound_after_first_write"
            owner.commit()
        report = diagnostics.snapshot()
        assert report["acquire"]["count"] == 2  # successful and failed attempts
        assert report["held_lower_bound"]["count"] == 1
    finally:
        engine.dispose()


def test_outermost_physical_savepoint_release_clears_holder(tmp_path, caplog):
    engine = create_engine(f"sqlite:///{tmp_path / 'savepoint.db'}", connect_args={"timeout": 0})
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE timing (id INTEGER)"))
    diagnostics = install_sqlite_diagnostics(engine)
    try:
        with engine.connect() as owner:
            nested = owner.begin_nested()
            owner.execute(text("INSERT INTO timing VALUES (1)"))
            nested.commit()
            assert diagnostics.snapshot()["held_lower_bound"]["count"] == 1
            with engine.connect() as later:
                later.execute(text("UPDATE timing SET id = 3"))
                with engine.connect() as waiting, pytest.raises(OperationalError):
                    waiting.execute(text("DELETE FROM timing"))
                observed = holders(caplog)[-1]
                assert [row["operation"] for row in observed] == ["UPDATE timing"]
    finally:
        engine.dispose()


async def test_cancelled_async_transaction_finishes_rollback_before_new_writer(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cancel.db'}")
    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE timing (id INTEGER)"))
    diagnostics = install_sqlite_diagnostics(engine.sync_engine)
    entered = asyncio.Event()

    async def cancelled_writer():
        async with engine.begin() as connection:
            await connection.execute(text("INSERT INTO timing VALUES (1)"))
            entered.set()
            await asyncio.Future()

    try:
        task = asyncio.create_task(cancelled_writer())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert diagnostics.snapshot()["held_lower_bound"]["count"] == 1
        async with engine.begin() as connection:
            assert await connection.scalar(text("SELECT COUNT(*) FROM timing")) == 0
            await connection.execute(text("INSERT INTO timing VALUES (2)"))
        assert diagnostics.snapshot()["held_lower_bound"]["count"] == 2
    finally:
        await engine.dispose()


async def test_diagnostic_queue_wait_is_distinct_from_consumer_commit_and_shutdown(monkeypatch):
    from qq_ai_bot.persistence import diagnostic_writer as writer_module

    clock = [0.0]
    monkeypatch.setattr(writer_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    entered, release = asyncio.Event(), asyncio.Event()
    diagnostics = DiagnosticWriter()

    async def first():
        entered.set()
        await release.wait()

    async def second():
        clock[0] = 12

    await diagnostics.start()
    try:
        assert diagnostics.submit("first", 1, first)
        await entered.wait()
        clock[0] = 2
        assert diagnostics.submit("second", 1, second)
        clock[0] = 10
        release.set()
        await diagnostics.drain()
        health = await diagnostics.health()
        assert health["queue_wait"]["maximum"] == 8
        assert health["commit_call"]["maximum"] == 10
        assert health["queue_wait"]["count"] == health["commit_call"]["count"] == 2
        assert health["committed"] == 2
    finally:
        release.set()
        await diagnostics.close()
