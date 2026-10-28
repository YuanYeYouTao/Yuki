"""Isolated real App Social cost probe, shared across two exact source snapshots."""

import asyncio
import contextvars
import inspect
import json
import os
import platform
import shutil
import sqlite3
import statistics
import time
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import event, select, text
from tests.conftest import _database_template as original_template
from tests.unit.test_client_files import admin as admin
from tests.unit.test_client_files import app_context, owner
from tests.unit.test_client_files import app_env as app_env

from yuki.persistence.models import ChatEventModel
from yuki.social.db_models import SocialOperationModel

request_owner = contextvars.ContextVar("social_cost_request", default=None)
PAYLOAD = "Foreground App Social benchmark: 同一条消息，保持原始内容。"
COUNT = int(os.environ.get("SOCIAL_COST_COUNT", "64"))
TRIALS = int(os.environ.get("SOCIAL_COST_TRIALS", "3"))


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def _database_template(tmp_path_factory):
    supplied = os.environ.get("SOCIAL_COST_TEMPLATE")
    if supplied:
        return Path(supplied)
    path = await inspect.unwrap(original_template)(tmp_path_factory)
    capture = os.environ.get("SOCIAL_COST_CAPTURE")
    if capture:
        shutil.copyfile(path, capture)
    return path


def percentile(values, p):
    ordered = sorted(values)
    return ordered[max(0, __import__("math").ceil(p * len(ordered)) - 1)]


@pytest.mark.parametrize("trial", range(TRIALS))
async def test_real_local_social_cost(app_env, database, monkeypatch, trial):
    env = app_env
    setup_started = time.perf_counter()
    device, _ = await owner(env, database)
    base = await app_context(env, device)
    setup_ms = (time.perf_counter() - setup_started) * 1000
    provider_calls = len(env.calls)
    async with database.read_session() as session:
        pragmas = {
            name: (await session.execute(text(f"PRAGMA {name}"))).scalar()
            for name in ("journal_mode", "synchronous", "busy_timeout", "page_size")
        }
    active = None
    raw = {}
    writer = database.immediate_session

    def target():
        rid = request_owner.get()
        if active is None:
            return None
        if rid is None:
            return active["background"]
        return raw[rid]

    def sql(_connection, _cursor, statement, _parameters, _context, _many):
        item = target()
        if item is not None:
            normalized = " ".join(statement.upper().split())
            kind = (
                "BEGIN_IMMEDIATE"
                if normalized.startswith("BEGIN IMMEDIATE")
                else normalized.split(" ", 1)[0]
            )
            item["sql"][kind] += 1

    def txn(kind):
        def record(_connection):
            item = target()
            if item is not None:
                item["transaction_events"][kind] += 1

        return record

    @asynccontextmanager
    async def instrument_writer(*args, **kwargs):
        item = target()
        started = time.perf_counter()
        async with writer(*args, **kwargs) as session:
            entered = time.perf_counter()
            yield session
        exited = time.perf_counter()
        if item is not None:
            item["writers"].append(
                {"entry_ms": (entered - started) * 1000, "context_ms": (exited - entered) * 1000}
            )

    monkeypatch.setattr(database, "immediate_session", instrument_writer)
    callbacks = {
        "before_cursor_execute": sql,
        "begin": txn("begin"),
        "commit": txn("commit"),
        "rollback": txn("rollback"),
    }
    for name, callback in callbacks.items():
        event.listen(database.engine.sync_engine, name, callback)

    all_ids = set()

    async def send(rid, measure):
        context = replace(base, turn_id=f"perf-{trial}-{rid}", call_id="send")
        token = request_owner.set(rid if measure else None)
        if measure:
            raw[rid] = {
                "request": rid,
                "sql": Counter(),
                "transaction_events": Counter(),
                "writers": [],
            }
        arrived = time.perf_counter()
        try:
            result = await env.social.execute("send", {"text": PAYLOAD}, context)
        finally:
            done = time.perf_counter()
            request_owner.reset(token)
        assert result["status"] == "succeeded", result
        identity = result["event_id"]
        assert identity not in all_ids
        all_ids.add(identity)
        if measure:
            raw[rid].update(
                latency_ms=(done - arrived) * 1000,
                arrived=arrived,
                completed=done,
                event_id=identity,
            )
        return identity

    phases = []
    try:
        for mode, parallel in (("sequential", 1), ("batch_4", 4)):
            warm_started = time.perf_counter()
            for start in range(0, 8, parallel):
                await asyncio.gather(
                    *(send(f"warm-{mode}-{i}", False) for i in range(start, start + parallel))
                )
            warm_ms = (time.perf_counter() - warm_started) * 1000
            raw.clear()
            active = {
                "background": {"sql": Counter(), "transaction_events": Counter(), "writers": []}
            }
            measured_started = time.perf_counter()
            for start in range(0, COUNT, parallel):
                await asyncio.gather(
                    *(send(f"{mode}-{i}", True) for i in range(start, min(COUNT, start + parallel)))
                )
            measured_done = time.perf_counter()
            background = active["background"]
            active = None
            samples = list(raw.values())
            ids = [item["event_id"] for item in samples]
            async with database.read_session() as session:
                events = list(
                    await session.scalars(select(ChatEventModel).where(ChatEventModel.id.in_(ids)))
                )
                receipts = list(
                    await session.scalars(
                        select(SocialOperationModel).where(SocialOperationModel.event_id.in_(ids))
                    )
                )
            assert len(events) == len(receipts) == COUNT
            assert all(
                row.content == PAYLOAD
                and row.direction == "outbound"
                and row.canonical_conversation_id == base.conversation_id
                for row in events
            )
            assert all(row.status == "succeeded" for row in receipts)
            assert len(env.calls) == provider_calls
            origin = min(item["arrived"] for item in samples)
            for item in samples:
                item["arrival_offset_ms"] = (item.pop("arrived") - origin) * 1000
                item["completion_offset_ms"] = (item.pop("completed") - origin) * 1000
            sql_total, tx_total = Counter(), Counter()
            for item in samples:
                sql_total.update(item["sql"])
                tx_total.update(item["transaction_events"])
            latencies = [item["latency_ms"] for item in samples]
            phases.append(
                {
                    "mode": mode,
                    "count": COUNT,
                    "batch_size": parallel,
                    "inter_completed_batch_sleep_ms": 0,
                    "warmup_count": 8,
                    "warmup_ms_excluded": warm_ms,
                    "wall_ms": (measured_done - measured_started) * 1000,
                    "throughput_per_second": COUNT / (measured_done - measured_started),
                    "p50_ms": statistics.median(latencies),
                    "p95_ms": percentile(latencies, 0.95),
                    "sql": sql_total,
                    "transaction_events": tx_total,
                    "writer_context_count": sum(len(item["writers"]) for item in samples),
                    "background_excluded": background,
                    "verified_events": len(events),
                    "verified_succeeded_receipts": len(receipts),
                    "samples": samples,
                }
            )
    finally:
        active = None
        for name, callback in callbacks.items():
            event.remove(database.engine.sync_engine, name, callback)
    path = Path(os.environ["SOCIAL_COST_OUTPUT"])
    contents = (
        json.loads(path.read_text())  # noqa: ASYNC240 - report I/O follows measured phases
        if path.exists()  # noqa: ASYNC240 - report I/O follows measured phases
        else {
            "revision": os.environ["SOCIAL_COST_REVISION"],
            "payload": PAYLOAD,
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
            "pragmas": pragmas,
            "trials": [],
        }
    )
    contents["trials"].append(
        {
            "trial": trial,
            "app_pair_and_context_setup_ms_excluded": setup_ms,
            "provider_calls_before_warmup": provider_calls,
            "provider_calls_after_measurement": len(env.calls),
            "phases": phases,
        }
    )
    path.write_text(json.dumps(contents, indent=2, ensure_ascii=False) + "\n")  # noqa: ASYNC240
