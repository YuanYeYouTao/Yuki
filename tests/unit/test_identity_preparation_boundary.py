"""Real native probes begin after preparation connections return to the pool."""

import asyncio
import json
import time

import pytest
from sqlalchemy import event
from tests.support.social_identity_cases import social_env

from yuki.identity.canonical_repository import find_space_binding
from yuki.identity.db_models import CanonicalPersonModel


@pytest.mark.parametrize("entry", ["read_members", "takeover", "recover"])
async def test_native_identity_probe_releases_preparation_connections(
    database, tmp_path, monkeypatch, entry
):
    env = await social_env(database, tmp_path)
    async with database.sessions() as reader:
        binding_id = (await find_space_binding(reader, "20001")).id
    pool = database.engine.sync_engine.pool
    checked_out = set()
    observations = []
    first_network = None
    started = time.perf_counter()
    entered, release = asyncio.Event(), asyncio.Event()
    original = env.bot.call_api
    before = database.sqlite_diagnostics.snapshot()

    def checkout(_dbapi, record, _proxy):
        checked_out.add(id(record))
        observations.append("checkout")

    def checkin(dbapi, record):
        # Pool return follows the physical reset/rollback, unlike Engine rollback events.
        driver = dbapi.driver_connection
        assert not driver.in_transaction
        checked_out.remove(id(record))
        observations.append("checkin")

    async def network(action, **params):
        nonlocal first_network
        if action == "get_group_member_info":
            assert observations and observations[-1] == "checkin"
            assert not checked_out
            assert observations.count("checkout") == observations.count("checkin")
            if first_network is None:
                first_network = time.perf_counter()
            entered.set()
            await release.wait()
        return await original(action, **params)

    monkeypatch.setattr(env.bot, "call_api", network)
    event.listen(pool, "checkout", checkout)
    event.listen(pool, "checkin", checkin)
    pending = None
    try:
        operation = {
            "read_members": lambda: env.router.accessible_group_connections(env.space),
            "takeover": lambda: env.router.cas_takeover_space(env.space),
            "recover": lambda: env.router.group_recovery_candidate(binding_id),
        }[entry]
        pending = asyncio.create_task(operation())
        await asyncio.wait_for(entered.wait(), 10)
        assert not checked_out and not pending.done()
        # This supplements physical checkin evidence; WAL writer progress alone is insufficient.
        async with database.immediate_session() as writer:
            person = await writer.get(CanonicalPersonModel, env.person)
            person.display_name = "unrelated write while network waits"
        assert not checked_out and not pending.done()
        release.set()
        result = await asyncio.wait_for(pending, 10)
        assert result
        assert not checked_out
    finally:
        release.set()
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        event.remove(pool, "checkout", checkout)
        event.remove(pool, "checkin", checkin)
    after = database.sqlite_diagnostics.snapshot()
    assert first_network is not None
    metrics = {
        key: {
            "count": after[key]["count"] - before[key]["count"],
            "seconds": after[key]["seconds"] - before[key]["seconds"],
        }
        for key in (
            "connection_acquisition",
            "read_sql",
            "acquire",
            "sql",
            "commit",
            "held_lower_bound",
        )
    }
    print(
        json.dumps(
            {
                "entry": entry,
                "environment": "isolated SQLite; one live fake QQ handle; no remote requests",
                "prepare_before_network_seconds": first_network - started,
                "returned_before_network": True,
                "checkout_count": observations.count("checkout"),
                "checkin_count": observations.count("checkin"),
                "measurement_scope": "route operation plus one concurrent writer",
                "pool_wait_seconds": None,
                "driver_queue_seconds": None,
                "sqlite_wait_seconds": None,
                "diagnostics": metrics,
            },
            sort_keys=True,
        )
    )
