"""Repeated cancellation retains the original SQLite read and cursor owner."""

import asyncio
import threading

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncScalarResult, AsyncSession
from tests.unit.test_history_query_repository import _event

from yuki.persistence.repositories import EventLedgerRepository


async def test_repeated_cancel_waits_for_original_stream_open(database, monkeypatch):
    ledger = EventLedgerRepository(database)
    event = await _event(database, ledger, "stream-owner", "source")
    entered, release = threading.Event(), threading.Event()
    producers = []
    original = AsyncSession.stream_scalars

    async def observe(session, *args, **kwargs):
        producers.append(asyncio.current_task())
        return await original(session, *args, **kwargs)

    def blocking_read():
        entered.set()
        assert release.wait(5), "original SQLite read was not released"
        return 1

    monkeypatch.setattr(AsyncSession, "stream_scalars", observe)
    async with database.read_session() as reader:
        await reader.execute(text("BEGIN"))
        connection = await reader.connection()
        driver = (await connection.get_raw_connection()).driver_connection
        await driver.create_function("history_owner_wait", 0, blocking_read)
        query = asyncio.create_task(
            ledger.query_history(
                session=reader,
                conversation_id=event.canonical_conversation_id,
                predicates=(text("history_owner_wait() > 0"),),
            )
        )
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            query.cancel("original_read_cancel")
            await asyncio.sleep(0)
            query.cancel("later_read_cancel")
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert producers and not producers[0].done()
            assert not query.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError) as failed:
            await query
        assert failed.value.args == ("original_read_cancel",)
        assert reader.in_transaction()
        assert await reader.scalar(text("SELECT 1")) == 1
    assert await database.ping()


@pytest.mark.parametrize("supplied", [False, True])
async def test_repeated_cancel_finishes_actual_cursor_close(database, monkeypatch, supplied):
    ledger = EventLedgerRepository(database)
    event = await _event(database, ledger, "cursor-owner", "source")
    entered, release = asyncio.Event(), asyncio.Event()
    opened = []
    open_stream = AsyncSession.stream_scalars
    close_stream = AsyncScalarResult.close

    async def observe_open(session, *args, **kwargs):
        stream = await open_stream(session, *args, **kwargs)
        opened.append(stream)
        asyncio.get_running_loop().call_soon(query.cancel, "original_cursor_cancel")
        return stream

    async def wait_close(stream):
        entered.set()
        await release.wait()
        await close_stream(stream)

    monkeypatch.setattr(AsyncSession, "stream_scalars", observe_open)
    monkeypatch.setattr(AsyncScalarResult, "close", wait_close)

    async def check(reader=None):
        nonlocal query
        query = asyncio.create_task(
            ledger.query_history(session=reader, conversation_id=event.canonical_conversation_id)
        )
        try:
            async with asyncio.timeout(5):
                await entered.wait()
                query.cancel("later_cursor_cancel")
                await asyncio.sleep(0)
                query.cancel("last_cursor_cancel")
                await asyncio.sleep(0)
                assert not query.done()
                assert opened and not opened[0].closed
                release.set()
                with pytest.raises(asyncio.CancelledError) as failed:
                    await query
            assert failed.value.args == ("original_cursor_cancel",)
            assert all(stream.closed for stream in opened)
        finally:
            release.set()
            await asyncio.gather(query, return_exceptions=True)

    query = None
    if supplied:
        async with database.read_session() as reader:
            await reader.execute(text("BEGIN"))
            await check(reader)
            assert reader.in_transaction()
            assert await reader.scalar(text("SELECT 1")) == 1
    else:
        await check()
    assert await database.ping()
