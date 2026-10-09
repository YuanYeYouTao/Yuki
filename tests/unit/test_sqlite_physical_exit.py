"""Pool events precede physical exit; confirmed release and lost observation differ."""

import asyncio
import sqlite3
import threading

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.util.concurrency import await_only
from tests.support.sqlite_diagnostics_helpers import holders

from qq_ai_bot.persistence.sqlite_diagnostics import install_sqlite_diagnostics


@pytest.mark.parametrize("exit_kind", ["invalidate", "close"])
def test_physical_close_tail_retains_holder_after_preclose_pool_events(tmp_path, caplog, exit_kind):
    engine = create_engine(f"sqlite:///{tmp_path / 'close.db'}", connect_args={"timeout": 0})
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE physical_exit(id INTEGER)"))
    original_close = engine.dialect.do_close
    observed = []

    def close_tail(dbapi):
        with engine.connect() as contender, pytest.raises(OperationalError):
            contender.execute(text("INSERT INTO physical_exit VALUES(2)"))
        observed.append(holders(caplog)[-1])
        original_close(dbapi)

    engine.dialect.do_close = close_tail
    diagnostics = install_sqlite_diagnostics(engine)
    try:
        with engine.connect() as owner:
            owner.execute(text("INSERT INTO physical_exit VALUES(1)"))
            if exit_kind == "invalidate":
                owner.invalidate()
            else:
                engine.pool._close_connection(owner.connection.dbapi_connection)
                # Already physically closed: discard the logical connection too.
                engine.dialect.do_close = original_close
                owner.invalidate()
        assert observed and [row["operation"] for row in observed[0]] == [
            "INSERT INTO physical_exit"
        ]
        assert diagnostics.snapshot()["held_lower_bound"]["count"] == 1
        assert diagnostics.snapshot()["held_release_unknown"]["count"] == 0
        engine.dialect.do_close = original_close
    finally:
        engine.dialect.do_close = original_close
        engine.dispose()


def test_failed_physical_close_ends_observation_without_claiming_release(tmp_path, caplog):
    engine = create_engine(f"sqlite:///{tmp_path / 'failed-close.db'}", connect_args={"timeout": 0})
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE physical_exit(id INTEGER)"))
    original_close = engine.dialect.do_close

    def failing_close(dbapi):
        with engine.connect() as contender, pytest.raises(OperationalError):
            contender.execute(text("INSERT INTO physical_exit VALUES(2)"))
        assert [row["operation"] for row in holders(caplog)[-1]] == ["INSERT INTO physical_exit"]
        raise sqlite3.OperationalError("synthetic physical close failure")

    engine.dialect.do_close = failing_close
    diagnostics = install_sqlite_diagnostics(engine)
    dbapi = None
    try:
        with engine.connect() as owner:
            owner.execute(text("INSERT INTO physical_exit VALUES(1)"))
            dbapi = owner.connection.dbapi_connection
            owner.invalidate()
        report = diagnostics.snapshot()
        assert report["held_lower_bound"]["count"] == 0
        assert report["held_release_unknown"]["count"] == 1
        assert any("release_confirmed=false" in record.getMessage() for record in caplog.records)
    finally:
        engine.dialect.do_close = original_close
        if dbapi is not None:
            original_close(dbapi)
        engine.dispose()


async def test_async_graceful_terminate_retains_holder_through_actual_close(tmp_path, caplog):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'async-close.db'}", connect_args={"timeout": 0}
    )
    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE physical_exit(id INTEGER)"))
    original_terminate = engine.sync_engine.dialect.do_terminate
    observed = []

    async def contender():
        async with engine.connect() as connection:
            with pytest.raises(OperationalError):
                await connection.execute(text("INSERT INTO physical_exit VALUES(2)"))
        observed.append(holders(caplog)[-1])

    def terminate_tail(dbapi):
        await_only(contender())
        original_terminate(dbapi)

    engine.sync_engine.dialect.do_terminate = terminate_tail
    diagnostics = install_sqlite_diagnostics(engine.sync_engine)
    try:
        async with engine.connect() as owner:
            await owner.execute(text("INSERT INTO physical_exit VALUES(1)"))
            await owner.invalidate()
        assert [row["operation"] for row in observed[0]] == ["INSERT INTO physical_exit"]
        report = diagnostics.snapshot()
        assert report["held_lower_bound"]["count"] == 1
        assert report["held_release_unknown"]["count"] == 0
    finally:
        engine.sync_engine.dialect.do_terminate = original_terminate
        await engine.dispose()


async def test_queued_force_terminate_is_unknown_and_does_not_keep_stale_physical_id(
    tmp_path, monkeypatch, caplog
):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'force-close.db'}", connect_args={"timeout": 0}
    )
    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE physical_exit(id INTEGER)"))
    diagnostics = install_sqlite_diagnostics(engine.sync_engine)
    entered, release = threading.Event(), threading.Event()
    queued = []
    blocked = None
    try:
        async with engine.connect() as owner:
            await owner.execute(text("INSERT INTO physical_exit VALUES(1)"))
            dbapi = owner.sync_connection.connection.dbapi_connection
            driver = dbapi.driver_connection
            stop = driver.stop

            def capture_stop():
                future = stop()
                if future is not None:
                    queued.append(future)
                return future

            def occupy_worker():
                entered.set()
                release.wait()

            monkeypatch.setattr(driver, "stop", capture_stop)
            blocked = asyncio.create_task(driver._execute(occupy_worker))
            await asyncio.to_thread(entered.wait)
            # Nongreenlet termination is the adapter's actual forced/GC path.
            engine.sync_engine.dialect.do_terminate(dbapi)
            report = diagnostics.snapshot()
            assert report["held_lower_bound"]["count"] == 0
            assert report["held_release_unknown"]["count"] == 1
            async with engine.connect() as contender:
                with pytest.raises(OperationalError):
                    await contender.execute(text("INSERT INTO physical_exit VALUES(2)"))
            assert holders(caplog)[-1] == []
            assert any(
                "release_confirmed=false" in record.getMessage() for record in caplog.records
            )
            release.set()
            await blocked
            await queued[0]  # Real stop completion, not a delay or timeout assumption.
            await owner.invalidate()
        async with engine.begin() as connection:
            await connection.execute(text("INSERT INTO physical_exit VALUES(3)"))
        assert diagnostics.snapshot()["held_lower_bound"]["count"] == 1
    finally:
        release.set()
        if blocked is not None:
            await blocked
        await engine.dispose()
