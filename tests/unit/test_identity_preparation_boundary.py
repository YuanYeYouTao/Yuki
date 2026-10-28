"""Real native probes begin after preparation connections return to the pool."""

import asyncio
import json
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import event
from tests.support.social_identity_cases import social_env

from yuki.identity.canonical_repository import find_space_binding
from yuki.identity.db_models import CanonicalPersonModel
from yuki.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork


@pytest.mark.parametrize("entry", ["inbound", "outbound", "external"])
async def test_fresh_ledger_append_reuses_prepared_json(database, monkeypatch, entry):
    from tests.unit.test_canonical_ingress import _Bot, _message, _stack

    registry, resolver, uow = await _stack(database)
    bot = _Bot("8000")
    registry.connect(bot, provider_id="snowluma")
    marker = "codec-source-marker"
    parts = ({"type": "text", "data": {"text": marker}},)
    message = replace(_message(message_id="codec-source", text=marker), segments=parts)
    admitted = await resolver.pre_admit(bot, message)
    source = await uow.append_inbound(admitted.message, admitted) if entry != "inbound" else None
    active = ContextVar("fresh_append_writer", default=False)
    writer = database.immediate_session
    loads = json.loads

    @asynccontextmanager
    async def checked_writer():
        async with writer() as session:
            token = active.set(True)
            try:
                yield session
            finally:
                active.reset(token)

    def checked_loads(value, *args, **kwargs):
        if isinstance(value, str) and marker in value:
            assert not active.get(), "fresh event decoded its prepared payload inside writer"
        return loads(value, *args, **kwargs)

    monkeypatch.setattr(database, "immediate_session", checked_writer)
    monkeypatch.setattr(json, "loads", checked_loads)
    if entry == "inbound":
        result = await uow.append_inbound(admitted.message, admitted)
    elif entry == "outbound":
        result = await ScopedEventLedgerUnitOfWork(database).append(
            scope=source.scope.scope,
            origin_key="codec-outbound",
            sender_user_id="8000",
            direction="outbound",
            content=marker,
            segments=parts,
            source_event_id=source.event.id,
        )
    else:
        result = await ScopedEventLedgerUnitOfWork(database).append_external(
            scope=source.scope.scope,
            origin_key="codec-external",
            source_plugin_id="fixture",
            external_source="fixture",
            external_event_key="codec-original-key",
            external_event_type="fixture",
            external_payload={"text": marker},
            external_target_id=admitted.person_id,
            content=marker,
            occurred_at=datetime.now(UTC),
        )
    assert result.created and result.event.content == marker
    if entry == "external":
        assert result.event.external_payload == {"text": marker}
    else:
        assert result.event.segments[0] == parts[0]


async def test_cold_identity_writer_releases_before_decoding_original_reply(database, monkeypatch):
    from tests.unit.test_canonical_ingress import _Bot, _message, _stack

    from yuki.identity import ingress
    from yuki.persistence import repository_helpers

    registry, resolver, uow = await _stack(database)
    bot = _Bot("8000")
    registry.connect(bot, provider_id="snowluma")
    original_message = _message(message_id="reply-source", text="original reply contents")
    original = await resolver.pre_admit(bot, original_message)
    await uow.append_inbound(original.message, original)
    existing = ingress._existing_hydrated
    forced = False

    async def cold_once(*args, **kwargs):
        nonlocal forced
        if not forced:
            forced = True
            raise ingress._ColdIdentityWrite()
        return await existing(*args, **kwargs)

    writer_active = ContextVar("cold_reply_writer", default=False)
    original_writer = database.immediate_session
    decode = repository_helpers._event_record
    decoded = []

    @asynccontextmanager
    async def writer():
        async with original_writer() as session:
            token = writer_active.set(True)
            try:
                yield session
            finally:
                writer_active.reset(token)

    def outside_writer(row):
        assert not writer_active.get(), "original reply was decoded while holding writer"
        decoded.append(row.id)
        return decode(row)

    monkeypatch.setattr(ingress, "_existing_hydrated", cold_once)
    monkeypatch.setattr(database, "immediate_session", writer)
    monkeypatch.setattr(repository_helpers, "_event_record", outside_writer)
    admitted = await resolver.pre_admit(
        bot,
        _message(
            message_id="cold-reply",
            reply_to_message_id=original_message.native_message_id,
        ),
    )
    assert forced and not admitted.dropped
    assert admitted.message.reply_text == "original reply contents"
    assert decoded == [admitted.message.reply_to_event_id]


@pytest.mark.parametrize("entry", ["read_members", "takeover", "recover"])
async def test_native_identity_probe_releases_preparation_connections(
    database, tmp_path, monkeypatch, entry
):
    env = await social_env(database, tmp_path)
    async with database.read_session() as reader:
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
            "writer_connection_acquisition",
            "read_sql",
            "writer_admission_wait",
            "acquire",
            "sql",
            "commit",
            "writer_transaction",
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
