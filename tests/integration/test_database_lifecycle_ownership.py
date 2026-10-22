"""The production constructor transfers its actual database to the app lifespan."""

import asyncio

import pytest
from sqlalchemy import event
from tests.conftest import make_settings
from tests.support.model_profiles import write_fake_profiles
from tests.unit.test_sticker_library import LocalDirectoryBridge

from yuki.admin.config_service import RuntimeConfigService
from yuki.client_api.errors import ClientError
from yuki.container import ApplicationContainer
from yuki.persistence.database import Database


def app_settings(database, tmp_path):
    return make_settings(
        database.url,
        model_profiles_file=write_fake_profiles(tmp_path / "models.toml"),
        onebot_access_token="",
        telegram_bot_token="",
        feishu_app_id="",
        feishu_app_secret="",
        plugin_system_enabled=False,
        plugin_directory=tmp_path / "plugins",
        workspace_directory=tmp_path / "artifacts",
        conversation_media_cache_directory=tmp_path / "media",
        social_transfer_directory=tmp_path / "transfer",
        semantic_participation_state_path=tmp_path / "participation.sqlite3",
        memory_embedding_enabled=False,
        memory_embedding_worker_enabled=False,
        memory_self_reflection_enabled=False,
        memory_dream_enabled=False,
        memory_maintenance_enabled=False,
        conversation_proactive_wakeup_enabled=False,
        conversation_autonomous_enabled=False,
    )


def observe_drivers(database):
    drivers = []
    event.listen(
        database.engine.sync_engine,
        "connect",
        lambda connection, _: drivers.append(connection._connection),
    )
    return drivers


def assert_physically_closed(drivers):
    assert drivers
    assert all(driver.physical_exit.done() and driver._connection is None for driver in drivers)


@pytest.mark.parametrize("injected", [False, True])
async def test_real_application_lifespan_closes_only_its_own_database(
    database, tmp_path, monkeypatch, injected
):
    settings = app_settings(database, tmp_path)
    app = (
        ApplicationContainer(settings, database=database)
        if injected
        else await ApplicationContainer.create(settings)
    )
    drivers = observe_drivers(app.database)
    # create() already read startup overrides, so force a fresh observable pool.
    await app.database.engine.dispose()
    await app.character.initialize("Lifecycle fixture")
    bridge = LocalDirectoryBridge(
        tmp_path / "manager-workspace",
        app.workspace_service.store,
        cache_root=app.conversation_media.root.parent,
    )

    async def manager(method, arguments, *, request_id, **_kwargs):
        if method == "list_code_completions":
            return {"events": [], "next_cursor": 0, "has_more": False}
        return await bridge.execute(method, arguments, request_id=request_id)

    monkeypatch.setattr(app.sandbox_client, "execute", manager)
    try:
        await app.start()
    finally:
        await asyncio.wait_for(app.close(), 5)
    assert app.database.writer_available is injected
    if injected:
        # The external owner remains usable and closes in the existing fixture.
        assert await database.ping()
    else:
        assert_physically_closed(drivers)


async def test_create_cancellation_closes_its_created_database(database, tmp_path, monkeypatch):
    settings = app_settings(database, tmp_path)
    owned = Database(database.url)
    drivers = observe_drivers(owned)
    entered = asyncio.Event()
    initialize = RuntimeConfigService.initialize

    async def pause_after_real_read(service):
        await initialize(service)
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("yuki.container.Database", lambda _url: owned)
    monkeypatch.setattr(RuntimeConfigService, "initialize", pause_after_real_read)
    task = asyncio.create_task(ApplicationContainer.create(settings))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not owned.writer_available
    assert_physically_closed(drivers)


async def test_identity_start_failure_rolls_back_the_actual_database_owner(database, tmp_path):
    app = await ApplicationContainer.create(app_settings(database, tmp_path))
    drivers = observe_drivers(app.database)
    await app.database.engine.dispose()
    # No Character was configured: the real prelude must fail before workers start.
    with pytest.raises(ClientError, match="unavailable"):
        await app.start()
    assert not app.database.writer_available
    assert_physically_closed(drivers)
