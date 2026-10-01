"""Optional benchmark probes preserve default behavior and never log query content."""

import asyncio
import gc
import json

import aiosqlite
import pytest
from sqlalchemy import text
from sqlalchemy.sql.elements import ClauseElement

from qq_ai_bot.memory.context import MemoryContextService
from qq_ai_bot.memory.quality.diagnostics import benchmark_diagnostics, diagnostic_case
from qq_ai_bot.persistence.database import Database


@pytest.mark.asyncio
async def test_diagnostics_are_disabled_without_explicit_environment(monkeypatch, capsys):
    monkeypatch.delenv("YUKI_MEMORY_QUALITY_DIAGNOSTICS", raising=False)
    execute = aiosqlite.Connection._execute

    @benchmark_diagnostics
    async def benchmark():
        assert aiosqlite.Connection._execute is execute
        return "unchanged"

    assert await benchmark() == "unchanged"
    assert capsys.readouterr().err == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_diagnostics_observe_sql_and_restore_every_hook_on_exit(monkeypatch, capsys, fail):
    monkeypatch.setenv("YUKI_MEMORY_QUALITY_DIAGNOSTICS", "1")
    sentinel = "private query content must never appear in diagnostic output"
    database = None

    async def search(service, **kwargs):
        async with database.sessions() as session:
            assert await session.scalar(text("SELECT :secret"), {"secret": sentinel}) == sentinel
        if fail:
            raise RuntimeError("synthetic failure")
        return sentinel

    monkeypatch.setattr(MemoryContextService, "search", search)
    initialize = Database.__init__
    originals = []

    def initialize_and_observe(self, *args, **kwargs):
        initialize(self, *args, **kwargs)
        engine = self.engine.sync_engine
        originals.append((engine, engine.pool, engine.dialect.do_ping, engine.pool._do_get))

    monkeypatch.setattr(Database, "__init__", initialize_and_observe)
    execute = aiosqlite.Connection._execute
    compile_statement = ClauseElement._compile_w_cache
    database_init = Database.__init__
    callbacks = list(gc.callbacks)
    tasks = asyncio.all_tasks()

    @benchmark_diagnostics
    async def benchmark():
        nonlocal database
        database = Database("sqlite+aiosqlite:///:memory:")
        try:
            with diagnostic_case("synthetic_case"):
                return await MemoryContextService.search(object())
        finally:
            await database.close()

    if fail:
        with pytest.raises(RuntimeError, match="synthetic failure"):
            await benchmark()
    else:
        assert await benchmark() == sentinel
    assert aiosqlite.Connection._execute is execute
    assert ClauseElement._compile_w_cache is compile_statement
    assert Database.__init__ is database_init
    assert MemoryContextService.search is search
    assert gc.callbacks == callbacks
    assert asyncio.all_tasks() == tasks
    engine, pool, original_ping, original_pool_get = originals[0]
    assert engine.dialect.do_ping == original_ping
    assert pool._do_get == original_pool_get
    output = capsys.readouterr().err
    assert sentinel not in output and "SELECT :secret" not in output
    payload = json.loads(output.removeprefix("MEMORY_QUALITY_PHASES "))
    sample = payload["samples"][0]
    assert sample["case_id"] == "synthetic_case" and sample["query_index"] == 1
    assert sample["sql_operation_counts"] == {"SELECT": 1}
    assert sample["compile_cache_counts"] == {"CACHE_MISS": 1}
    assert sample["worker_operation_counts"]["execute"] > 0
    assert sample["wall_ms"] >= sample["worker_ms"] > 0
    assert sample["queue_ms"] >= 0 and sample["wake_ms"] >= 0
    assert sample["main_thread_cpu_ms"] >= 0
