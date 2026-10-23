"""A real failed activation closes the original resources, including injected DB ownership."""

import asyncio

import pytest
from pydantic import SecretStr
from tests.integration.test_database_lifecycle_ownership import app_settings

from yuki.container import ApplicationContainer


async def test_failure_after_workspace_start_closes_owned_resources_without_replacing_them(
    database, tmp_path, monkeypatch
):
    app = await ApplicationContainer.create(app_settings(database, tmp_path))
    await app.character.initialize("Failure fixture")
    store = app.workspace_service.store
    main = app.main_agent_contract
    assert app.chat.main_contract is app.runtime.runner.main_contract is main
    assert main.state.store is store

    async def failed_freeze():
        raise RuntimeError("injected original declaration failure")

    monkeypatch.setattr(main, "definitions", failed_freeze)
    with pytest.raises(RuntimeError, match="original declaration failure"):
        await asyncio.wait_for(app.start(), 5)
    assert app.workspace_service.store is store and app.workspace_service._task is None
    cleaner = app.conversation_media._cleaner
    assert cleaner is None or cleaner.done()
    assert not app.emoji_library._tasks
    assert not app.database.writer_available
    assert await database.ping()


@pytest.mark.parametrize("factory", [False, True])
async def test_manifest_open_failure_precedes_clients_and_keeps_actual_database_owner(
    database, tmp_path, monkeypatch, factory
):
    from tests.integration.test_database_lifecycle_ownership import (
        assert_physically_closed,
        observe_drivers,
    )

    from yuki.application.modules.media import MediaModule
    from yuki.application.modules.model_runtime import ModelRuntimeModule
    from yuki.persistence.database import Database
    from yuki.workspace.store import WorkspaceError, WorkspaceStore

    settings = app_settings(database, tmp_path)
    store = WorkspaceStore(settings.workspace_directory)
    index = store.root / "manifest.sqlite3"
    (store.root / "linked.sqlite3").hardlink_to(index)
    entered = []

    def unexpected_clients(module):
        entered.append(type(module).__name__)
        raise AssertionError("workspace must open before any HTTP owner is constructed")

    monkeypatch.setattr(ModelRuntimeModule, "build", unexpected_clients)
    monkeypatch.setattr(MediaModule, "build", unexpected_clients)
    owned = Database(database.url)
    drivers = observe_drivers(owned)
    monkeypatch.setattr("yuki.container.Database", lambda _url: owned)
    try:
        with pytest.raises(WorkspaceError, match="unsafe_workspace_index"):
            if factory:
                await ApplicationContainer.create(settings)
            else:
                ApplicationContainer(settings, database=database)
        assert entered == []
        if factory:
            assert not owned.writer_available
            assert_physically_closed(drivers)
        assert database.writer_available and await database.ping()
    finally:
        await owned.close()


@pytest.mark.parametrize("phase", ["identity", "manifest"])
async def test_start_failure_closes_all_constructed_http_owners_even_before_their_start(
    database, tmp_path, monkeypatch, phase
):
    import httpx

    from yuki.client_api.errors import ClientError

    original_client = httpx.AsyncClient
    clients = []

    class ObservedClient(original_client):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            clients.append(self)

    monkeypatch.setattr(httpx, "AsyncClient", ObservedClient)
    settings = app_settings(database, tmp_path).model_copy(
        update={
            "feishu_app_id": "offline-start-failure",
            "feishu_app_secret": SecretStr("offline-only"),
        }
    )
    app = await ApplicationContainer.create(settings)
    failure = RuntimeError("original manifest failure")
    if phase == "manifest":
        await app.character.initialize("Failure fixture")

        async def failed_freeze():
            raise failure

        monkeypatch.setattr(app.main_agent_contract, "definitions", failed_freeze)
    try:
        with pytest.raises(ClientError if phase == "identity" else RuntimeError) as caught:
            await asyncio.wait_for(app.start(), 5)
        if phase == "manifest":
            assert caught.value is failure
        assert clients and all(client.is_closed for client in clients)
        assert not app.database.writer_available
        assert not app.emoji_library._tasks
        assert await database.ping()
    finally:
        for client in clients:
            await client.aclose()
