"""NoneBot owns its shielded connection hooks until its lifespan has exited."""

import asyncio

import pytest
from nonebot.adapters.onebot.v11 import Bot
from nonebot.config import Config, Env
from nonebot.drivers.fastapi import Driver
from sqlalchemy import select
from tests.conftest import make_settings
from tests.support.canonical_identity import ensure_presence
from tests.support.gateway import builtin_registry

from yuki import main
from yuki.adapters.onebot.provider_adapter import SnowLumaOneBotAdapter
from yuki.application.lifecycle import LifecycleRegistry
from yuki.container import ApplicationContainer
from yuki.conversation.canonical_db_models import PersonActiveRouteModel
from yuki.identity.ingress import _ensure_person_id
from yuki.identity.routing import PresenceRouter, RouteMonitor
from yuki.persistence.instance_lock import SQLiteApplicationLock


def stack(database, monkeypatch):
    driver = Driver(Env(_env_file=None), Config(_env_file=None))
    monkeypatch.setattr(Driver, "_adapters", {})
    monkeypatch.setattr(Driver, "_bot_connection_hook", set())
    monkeypatch.setattr(Driver, "_bot_disconnection_hook", set())
    monkeypatch.setattr(main.nonebot, "init", lambda **_: None)
    monkeypatch.setattr(main.nonebot, "get_driver", lambda: driver)
    monkeypatch.setattr(main.nonebot, "_driver", driver)
    monkeypatch.setattr(main.nonebot, "load_plugin", lambda _: None)
    monkeypatch.setattr(main, "configure_logging", lambda _: None)
    monkeypatch.setattr(main, "load_http_environment", lambda: None)
    monkeypatch.setattr("yuki.container._container", None)

    async def fixture_schema(url):
        # The shared fixture creates the real current schema without Alembic.
        assert url == database.url

    monkeypatch.setattr(main, "require_canonical_schema", fixture_schema)

    # Use the real container start/close and DB owner with only the resources
    # involved in this entrypoint; no background model or network workers.
    container = ApplicationContainer.__new__(ApplicationContainer)
    container.database = database
    container.gateway_registry = builtin_registry()
    container.presence_router = PresenceRouter(database, container.gateway_registry)
    container.route_monitor = RouteMonitor(container.presence_router)
    container.plugin_events = None
    container.lifecycle = LifecycleRegistry()
    container.lifecycle.register("database", close=database.close)
    monkeypatch.setattr("yuki.gateway.registry._PROCESS_REGISTRY", container.gateway_registry)

    async def create(_settings):
        return container

    monkeypatch.setattr(ApplicationContainer, "create", create)
    settings = make_settings(database.url, onebot_access_token="lifespan-test")
    return driver, container, settings


async def seed(database):
    async with database.immediate_session() as writer:
        presence_id = await ensure_presence(writer, "8000")
        person_id = await _ensure_person_id(writer, "1001", platform="qq")
    return presence_id, person_id


async def test_adapter_shutdown_disconnect_finishes_before_container_database_close(
    database, monkeypatch
):
    _presence_id, person_id = await seed(database)
    driver, container, settings = stack(database, monkeypatch)
    completed = asyncio.Event()
    failures = []
    reconciled = []
    reconcile = container.route_monitor.on_connection_change

    async def observe():
        try:
            result = await reconcile()
            reconciled.append(result)
            return result
        except BaseException as error:
            failures.append(error)
            raise
        finally:
            completed.set()

    monkeypatch.setattr(container.route_monitor, "on_connection_change", observe)

    async def stop(adapter):
        adapter.bot_disconnect(bot)

    monkeypatch.setattr(SnowLumaOneBotAdapter, "_stop", stop)
    main.bootstrap(settings)
    adapter = driver._adapters[SnowLumaOneBotAdapter.get_name()]
    bot = Bot(adapter, "8000")
    async with driver.server_app.router.lifespan_context(driver.server_app):
        adapter.bot_connect(bot)
        await asyncio.wait_for(completed.wait(), 5)
        assert not failures
        async with database.read_session() as reader:
            assert (
                await reader.scalar(
                    select(PersonActiveRouteModel.person_id).where(
                        PersonActiveRouteModel.person_id == person_id
                    )
                )
                == person_id
            )
        completed.clear()
    assert completed.is_set()
    assert len(reconciled) == 2
    assert failures == []
    assert not database.writer_available


@pytest.mark.parametrize("change", ["connect", "disconnect"])
async def test_shutdown_joins_connection_hook_already_inside_reconcile(
    database, monkeypatch, change
):
    presence_id, person_id = await seed(database)
    driver, container, settings = stack(database, monkeypatch)
    entered = asyncio.Event()
    release = asyncio.Event()
    stopping = asyncio.Event()
    completed = asyncio.Event()
    failures = []
    routes = []
    reconcile = container.route_monitor.on_connection_change

    async def observe():
        entered.set()
        await release.wait()
        try:
            result = await reconcile()
            async with database.read_session() as reader:
                routes.append((await reader.get(PersonActiveRouteModel, person_id)).presence_id)
            return result
        except BaseException as error:
            failures.append(error)
            raise
        finally:
            completed.set()

    monkeypatch.setattr(container.route_monitor, "on_connection_change", observe)

    async def stop(_adapter):
        stopping.set()

    monkeypatch.setattr(SnowLumaOneBotAdapter, "_stop", stop)
    main.bootstrap(settings)
    adapter = driver._adapters[SnowLumaOneBotAdapter.get_name()]
    bot = Bot(adapter, "8000")
    expected_presence = presence_id

    async def finish_original_hook():
        await stopping.wait()
        release.set()

    releaser = asyncio.create_task(finish_original_hook())
    try:
        async with driver.server_app.router.lifespan_context(driver.server_app):
            if change == "connect":
                adapter.bot_connect(bot)
            else:
                container.gateway_registry.connect(
                    bot, provider_id="snowluma", presence_id=presence_id
                )
                assert await container.presence_router.cas_takeover_person(person_id) == "taken"
                async with database.immediate_session() as writer:
                    expected_presence = await ensure_presence(writer, "8001")
                container.gateway_registry.connect(
                    Bot(adapter, "8001"),
                    provider_id="snowluma",
                    presence_id=expected_presence,
                )
                driver._bots[bot.self_id] = bot
                adapter.bots[bot.self_id] = bot
                adapter.bot_disconnect(bot)
            await asyncio.wait_for(entered.wait(), 5)
            assert not completed.is_set()
        await releaser
    finally:
        releaser.cancel()
        await asyncio.gather(releaser, return_exceptions=True)
    assert completed.is_set()
    assert failures == []
    assert routes == [expected_presence]
    assert not database.writer_available


async def test_normal_disconnect_reconciles_while_application_is_live(database, monkeypatch):
    _presence_id, person_id = await seed(database)
    async with database.immediate_session() as writer:
        other_presence = await ensure_presence(writer, "8001")
    driver, container, settings = stack(database, monkeypatch)
    completed = asyncio.Event()
    reconcile = container.route_monitor.on_connection_change
    changes = []

    async def observe():
        result = await reconcile()
        changes.append(result)
        completed.set()
        return result

    monkeypatch.setattr(container.route_monitor, "on_connection_change", observe)
    main.bootstrap(settings)
    adapter = driver._adapters[SnowLumaOneBotAdapter.get_name()]
    bot = Bot(adapter, "8000")
    other_bot = Bot(adapter, "8001")
    async with driver.server_app.router.lifespan_context(driver.server_app):
        adapter.bot_connect(bot)
        await asyncio.wait_for(completed.wait(), 5)
        completed.clear()
        adapter.bot_connect(other_bot)
        await asyncio.wait_for(completed.wait(), 5)
        completed.clear()
        adapter.bot_disconnect(bot)
        await asyncio.wait_for(completed.wait(), 5)
        assert len(changes) == 3
        assert database.writer_available
        async with database.read_session() as reader:
            assert (
                await reader.get(PersonActiveRouteModel, person_id)
            ).presence_id == other_presence


@pytest.mark.parametrize("phase", ["create", "start"])
async def test_failed_startup_releases_original_lock_and_preserves_error(
    database, monkeypatch, phase
):
    driver, container, settings = stack(database, monkeypatch)
    failure = RuntimeError(f"original {phase} failure")
    closed = []
    close = container.close

    async def observe_close():
        closed.append(container)
        await close()

    monkeypatch.setattr(container, "close", observe_close)

    async def fail():
        raise failure

    if phase == "create":

        async def create(_settings):
            raise failure

        monkeypatch.setattr(ApplicationContainer, "create", create)
    else:
        container.lifecycle.register("startup_failure", start=fail)
    main.bootstrap(settings)
    with pytest.raises(RuntimeError) as raised:
        async with driver.server_app.router.lifespan_context(driver.server_app):
            pytest.fail("failed startup reached the running application")
    assert raised.value is failure
    assert closed == ([] if phase == "create" else [container])
    lock = SQLiteApplicationLock(settings.sqlite_path)
    try:
        lock.acquire()
    finally:
        lock.release()
    assert database.writer_available is (phase == "create")


@pytest.mark.parametrize("origin", ["startup", "framework_cancel"])
async def test_cleanup_failure_preserves_original_lifespan_error_and_releases_lock(
    database, monkeypatch, origin
):
    driver, container, settings = stack(database, monkeypatch)
    failure = (
        RuntimeError("original startup failure")
        if origin == "startup"
        else asyncio.CancelledError("original application cancellation")
    )
    cleanup = OSError("original container cleanup failure")
    close = container.close

    async def fail():
        raise failure

    async def close_then_fail():
        await close()
        raise cleanup

    if origin == "startup":
        container.lifecycle.register("startup_failure", start=fail)
    monkeypatch.setattr(container, "close", close_then_fail)
    main.bootstrap(settings)
    with pytest.raises(type(failure)) as raised:
        async with driver.server_app.router.lifespan_context(driver.server_app):
            raise failure
    assert raised.value is failure
    assert failure.__cause__ is cleanup
    assert not database.writer_available
    lock = SQLiteApplicationLock(settings.sqlite_path)
    try:
        lock.acquire()
    finally:
        lock.release()
