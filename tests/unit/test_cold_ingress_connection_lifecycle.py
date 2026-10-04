"""Cold membership preparation releases the read connection and freezes its owner."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from tests.unit.test_canonical_ingress import _Bot, _message, _stack

from qq_ai_bot.conversation.canonical_db_models import SpaceBindingIngestRouteModel
from qq_ai_bot.identity.db_models import PresenceModel, SpaceBindingModel


@pytest.mark.asyncio
async def test_cold_pin_installed_during_probe_is_not_overwritten(database):
    registry, resolver, _ = await _stack(database)
    bot = _Bot("8000")
    registry.connect(bot)

    async def probe(*_args):
        async with database.immediate_session() as session:
            binding = await session.scalar(
                select(SpaceBindingModel).where(SpaceBindingModel.external_space_id == "2001")
            )
            presence = await session.scalar(
                select(PresenceModel).where(PresenceModel.external_account_id == "8000")
            )
            now = datetime.now(UTC)
            session.add(
                SpaceBindingIngestRouteModel(
                    space_binding_id=binding.id,
                    ingest_presence_id=presence.id,
                    route_generation=7,
                    revision=7,
                    paused=True,
                    created_at=now,
                    updated_at=now,
                )
            )
        return True

    resolver._router._probe = probe
    admitted = await resolver.pre_admit(bot, _message(message_id="pin-race", group_id="2001"))
    assert admitted is not None and admitted.dropped and admitted.reason == "paused"
    async with database.sessions() as session:
        route = await session.scalar(select(SpaceBindingIngestRouteModel))
        assert route.paused and route.route_generation == 7 and route.revision == 7


@pytest.mark.asyncio
async def test_cold_probe_deadline_expires_before_route_cas(database):
    registry, resolver, _ = await _stack(database)
    bot = _Bot("8000")
    registry.connect(bot)

    async def probe(*_args):
        await asyncio.Event().wait()
        return True

    resolver._router._probe = probe
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            resolver.pre_admit(bot, _message(message_id="probe-deadline", group_id="2001")), 12
        )
    async with database.sessions() as session:
        assert not list(await session.scalars(select(SpaceBindingIngestRouteModel)))


@pytest.mark.asyncio
async def test_cold_probe_can_checkout_pool_size_one_and_accepted_event_outlives_socket(database):
    registry, resolver, uow = await _stack(database)
    bot = _Bot("8000")
    registry.connect(bot)
    engine = create_async_engine(database.url, pool_size=1, max_overflow=0, pool_timeout=0.1)
    event.listen(engine.sync_engine, "connect", database._configure_sqlite_connection)
    original = database.sessions
    database.sessions = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    probes = 0

    async def probe(*_args):
        nonlocal probes
        probes += 1
        async with database.sessions() as session:
            assert await session.scalar(text("SELECT 1")) == 1
        return True

    resolver._router._probe = probe
    try:
        admitted = await asyncio.wait_for(
            resolver.pre_admit(bot, _message(message_id="cold-single", group_id="2001")), 2
        )
        assert admitted is not None and not admitted.dropped and probes == 1
        registry.disconnect(bot)
        appended = await uow.append_inbound(admitted.message, admitted)
        assert appended.event.id > 0
    finally:
        database.sessions = original
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed", ["binding_revision", "presence_revision", "disconnect", "reconnect"]
)
async def test_cold_owner_change_during_probe_never_installs_route(database, changed):
    registry, resolver, _ = await _stack(database)
    bot = _Bot("8000")
    registry.connect(bot)

    async def probe(*_args):
        if changed in {"binding_revision", "presence_revision"}:
            async with database.immediate_session() as session:
                model = SpaceBindingModel if changed == "binding_revision" else PresenceModel
                identity = (
                    SpaceBindingModel.external_space_id == "2001"
                    if changed == "binding_revision"
                    else PresenceModel.external_account_id == "8000"
                )
                row = await session.scalar(select(model).where(identity))
                row.revision += 1
        else:
            registry.disconnect(bot)
            if changed == "reconnect":
                registry.connect(bot)
        return True

    resolver._router._probe = probe
    admitted = await resolver.pre_admit(bot, _message(message_id=changed, group_id="2001"))
    assert admitted is not None and admitted.dropped
    async with database.sessions() as session:
        assert not list(await session.scalars(select(SpaceBindingIngestRouteModel)))


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_type", [RuntimeError, asyncio.CancelledError])
async def test_cold_commit_acknowledgement_loss_propagates_without_retry_or_undo(
    database, monkeypatch, failure_type
):
    registry, resolver, _ = await _stack(database)
    bot = _Bot("8000")
    registry.connect(bot)
    original = database.immediate_session
    commits = 0

    @asynccontextmanager
    async def uncertain_commit():
        nonlocal commits
        async with original() as session:
            yield session
        commits += 1
        raise failure_type("synthetic commit acknowledgement lost")

    monkeypatch.setattr(database, "immediate_session", uncertain_commit)
    with pytest.raises(failure_type, match="acknowledgement lost"):
        await resolver.pre_admit(bot, _message(message_id="unknown", group_id="2001"))
    assert commits == 1
    async with database.sessions() as session:
        route = await session.scalar(select(SpaceBindingIngestRouteModel))
        assert route is not None and route.route_generation == 1


@pytest.mark.asyncio
async def test_cold_cancel_before_cas_does_not_install_route(database, monkeypatch):
    registry, resolver, _ = await _stack(database)
    bot = _Bot("8000")
    registry.connect(bot)
    entered = asyncio.Event()

    async def hold():
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(resolver._router, "_await_cas_hold", hold)
    pending = asyncio.create_task(
        resolver.pre_admit(bot, _message(message_id="cancel-cas", group_id="2001"))
    )
    await asyncio.wait_for(entered.wait(), 2)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    async with database.sessions() as session:
        assert not list(await session.scalars(select(SpaceBindingIngestRouteModel)))
