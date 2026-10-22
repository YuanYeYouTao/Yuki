"""Cancellation retains actual connector, checkout and reader-close ownership."""

import asyncio
import sqlite3
import threading

import pytest
from sqlalchemy import text

from yuki.persistence.database import Database
from yuki.persistence.schema_guard import require_canonical_schema


async def test_cancelled_connector_closes_the_connection_returned_by_its_thread(
    tmp_path, monkeypatch
):
    entered, release = threading.Event(), threading.Event()
    native = []
    closed = []
    connect = sqlite3.connect

    class Connection(sqlite3.Connection):
        def close(self):
            closed.append(self)
            return super().close()

    def blocked_connect(*args, **kwargs):
        connection = connect(*args, **kwargs, factory=Connection)
        native.append(connection)
        entered.set()
        assert release.wait(5)
        return connection

    monkeypatch.setattr(sqlite3, "connect", blocked_connect)
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'connector.db'}")
    task = asyncio.create_task(database._connect_sqlite())
    try:
        await asyncio.wait_for(asyncio.to_thread(entered.wait), 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed == native
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        for connection in native:
            if connection not in closed:
                connection.close()
        await database.close()


async def test_checkout_cancellation_waits_for_actual_database_operation(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    next_entered = asyncio.Event()
    connect = sqlite3.connect
    native = []

    def trace(statement):
        if statement == "PRAGMA foreign_keys=ON" and not release.is_set():
            entered.set()
            assert release.wait(5)

    def instrumented_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        native.append(connection)
        connection.set_trace_callback(trace)
        return connection

    monkeypatch.setattr(sqlite3, "connect", instrumented_connect)
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'checkout.db'}")

    async def original():
        async with database.immediate_session():
            pytest.fail("cancelled checkout reached the business body")

    async def successor():
        async with database.immediate_session() as writer:
            next_entered.set()
            await writer.execute(text("SELECT 2"))

    task = asyncio.create_task(original())
    next_task = None
    try:
        await asyncio.wait_for(asyncio.to_thread(entered.wait), 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        next_task = asyncio.create_task(successor())
        await asyncio.sleep(0)
        assert not task.done() and not next_entered.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(next_task, 5)
        assert database.writer_available
        assert len(native) == 1
    finally:
        release.set()
        await asyncio.gather(task, *([next_task] if next_task else []), return_exceptions=True)
        await database.close()


async def test_owned_reader_does_not_exit_before_its_real_rollback(tmp_path):
    entered, release = threading.Event(), threading.Event()
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'reader.db'}")

    def trace(statement):
        if statement == "ROLLBACK":
            entered.set()
            assert release.wait(5)

    async def reading():
        async with database.read_session() as reader:
            await reader.execute(text("BEGIN"))
            await reader.execute(text("SELECT 1"))
            driver = (await (await reader.connection()).get_raw_connection()).driver_connection
            await driver.set_trace_callback(trace)

    task = asyncio.create_task(reading())
    try:
        await asyncio.wait_for(asyncio.to_thread(entered.wait), 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        async with database.read_session() as reader:
            assert await reader.scalar(text("SELECT 2")) == 2
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await database.close()


@pytest.mark.parametrize("phase", ["configure", "read"])
async def test_owned_reader_checkout_cancellation_waits_for_the_actual_operation(
    tmp_path, monkeypatch, phase
):
    entered, release = threading.Event(), threading.Event()
    connect = sqlite3.connect

    def trace(statement):
        target = "PRAGMA foreign_keys=ON" if phase == "configure" else "SELECT 2"
        if statement == target and not release.is_set():
            entered.set()
            assert release.wait(5)

    def instrumented_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(trace)
        return connection

    monkeypatch.setattr(sqlite3, "connect", instrumented_connect)
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'reader-checkout.db'}")
    if phase == "read":
        async with database.read_session() as reader:
            await reader.scalar(text("SELECT 3"))

    async def reading():
        async with database.read_session() as reader:
            await reader.execute(text("SELECT 2"))
            pytest.fail("cancelled reader checkout reached its result consumer")

    task = asyncio.create_task(reading())
    try:
        await asyncio.wait_for(asyncio.to_thread(entered.wait), 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done(), "reader returned while its actual database operation still runs"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert database.engine.sync_engine.pool.checkedout() == 0
        assert await database.ping()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await database.close()


async def test_native_pool_reuses_connections_without_probes_and_replaces_invalidated_driver(
    tmp_path, monkeypatch
):
    connect = sqlite3.connect
    native, closed, statements = [], [], []

    class Connection(sqlite3.Connection):
        def close(self):
            closed.append(self)
            return super().close()

    def instrumented_connect(*args, **kwargs):
        connection = connect(*args, **kwargs, factory=Connection)
        connection.set_trace_callback(statements.append)
        native.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", instrumented_connect)
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'pool.db'}")
    try:
        for _ in range(3):
            async with database.read_session() as reader:
                assert await reader.scalar(text("SELECT 7")) == 7
        for _ in range(2):
            async with database.immediate_session() as writer:
                assert await writer.scalar(text("SELECT 8")) == 8
        assert len(native) == 1
        assert statements.count("SELECT 7") == 3
        assert statements.count("SELECT 8") == 2
        assert "SELECT 1" not in statements

        async with database.read_session() as reader:
            connection = await reader.connection()
            await connection.invalidate()
        assert closed == native
        async with database.read_session() as reader:
            assert await reader.scalar(text("SELECT 9")) == 9
        assert len(native) == 2
        assert closed == native[:1]
    finally:
        await database.close()
    assert closed == native


async def test_schema_check_cancellation_owns_its_late_native_connection(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    connect = sqlite3.connect
    native, closed = [], []

    class Connection(sqlite3.Connection):
        def close(self):
            closed.append(self)
            return super().close()

    def blocked_connect(*args, **kwargs):
        connection = connect(*args, **kwargs, factory=Connection)
        native.append(connection)
        entered.set()
        assert release.wait(5)
        return connection

    monkeypatch.setattr(sqlite3, "connect", blocked_connect)
    task = asyncio.create_task(
        require_canonical_schema(f"sqlite+aiosqlite:///{tmp_path / 'schema-cancel.db'}")
    )
    try:
        await asyncio.wait_for(asyncio.to_thread(entered.wait), 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done(), "schema owner left its original connector running"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed == native
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        for connection in native:
            if connection not in closed:
                connection.close()
