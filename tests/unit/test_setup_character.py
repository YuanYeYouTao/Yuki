"""Real operator setup uses the character's original revision and receipt."""

import argparse
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from tests.conftest import make_settings
from tests.unit.test_admin_api import TOKEN
from tests.unit.test_admin_api import admin as admin_fixture
from tests.unit.test_client_api import bearer
from tests.unit.test_client_api import personal as personal_fixture

from yuki.admin_api import cli
from yuki.admin_api import client as operator_transport
from yuki.deployment_setup.service import (
    EnvironmentDocument,
    SetupConfiguration,
    SetupPaths,
    SetupValidationError,
    apply_pending_configuration,
    commit_configuration,
)
from yuki.persistence.instance_lock import SQLiteApplicationLock

admin = admin_fixture
personal = personal_fixture


async def test_character_cli_updates_and_replays_the_actual_profile_without_control_result_wrapper(
    personal, database, monkeypatch, capsys
):
    client = personal

    @asynccontextmanager
    async def actual_client(_settings):
        previous = client.headers.get("Authorization")
        client.headers.update(bearer(TOKEN))
        try:
            yield client
        finally:
            if previous is None:
                client.headers.pop("Authorization", None)
            else:
                client.headers["Authorization"] = previous

    monkeypatch.setattr(cli, "admin_client", actual_client)
    parser = argparse.ArgumentParser()
    cli.add_admin_parser(parser.add_subparsers())
    original = (await client.get("/admin/v1/character", headers=bearer(TOKEN))).json()["data"]
    args = parser.parse_args(
        [
            "admin",
            "character",
            "update",
            "--revision",
            str(original["revision"]),
            "--display-name",
            "凜",
            "--slug",
            "rin",
            "--request-id",
            str(uuid4()),
        ]
    )
    for replay in [False, True]:
        assert await cli.admin_command(make_settings(database.url), args) == 0
        result = json.loads(capsys.readouterr().out)
        assert result["id"] == original["id"]
        assert result["display_name"] == "凜" and result["slug"] == "rin"
        assert result["revision"] == original["revision"] + 1
        if not replay:
            changed = await client.post(
                "/admin/v1/character",
                headers=bearer(TOKEN),
                json={
                    "request_id": str(uuid4()),
                    "expected_revision": original["revision"] + 1,
                    "bio": "later operator edit",
                },
            )
            assert changed.status_code == 200
        else:
            current = (await client.get("/admin/v1/character", headers=bearer(TOKEN))).json()[
                "data"
            ]
            assert current["revision"] == original["revision"] + 2
            assert current["bio"] == "later operator edit"


async def test_setup_profile_survives_committed_response_loss_and_preserves_the_original_command(
    personal, database, tmp_path, monkeypatch
):
    paths = SetupPaths(tmp_path)
    configuration = SetupConfiguration(
        {"BOT_DISPLAY_NAME": "凜"},
        "",
        None,
        write_model_profiles=False,
        initial_character={"display_name": "凜", "slug": "rin"},
    )
    commit_configuration(paths, EnvironmentDocument(""), configuration)
    original = paths.pending.read_bytes()
    payload = json.loads(original)
    assert payload["character"]["expected_revision"] == 1
    assert "selected_plugins" not in payload
    # Re-entering the same initial configuration keeps its already-issued UUID.
    commit_configuration(paths, EnvironmentDocument(""), configuration)
    assert paths.pending.read_bytes() == original
    client = personal

    @asynccontextmanager
    async def actual_client(_settings):
        client.headers.update(bearer(TOKEN))
        yield client

    monkeypatch.setattr(operator_transport, "admin_client", actual_client)
    monkeypatch.setattr(
        operator_transport,
        "plugin_mutation",
        AsyncMock(
            side_effect=AssertionError("role-only setup must not change plugin configuration")
        ),
    )
    forward = client._transport.handle_async_request
    lose = True

    async def after_commit(request):
        nonlocal lose
        response = await forward(request)
        if lose and request.method == "POST" and request.url.path == "/admin/v1/character":
            lose = False
            assert response.status_code == 200
            raise httpx.ReadError("actual response lost after commit", request=request)
        return response

    monkeypatch.setattr(client._transport, "handle_async_request", after_commit)
    settings = make_settings(database.url)
    lock = SQLiteApplicationLock(settings.sqlite_path)
    lock.acquire()
    try:
        with pytest.raises(RuntimeError, match="unconfirmed"):
            await apply_pending_configuration(paths, settings)
        assert paths.pending.read_bytes() == original
        current = (await client.get("/admin/v1/character", headers=bearer(TOKEN))).json()["data"]
        assert current["revision"] == 2 and current["display_name"] == "凜"
        assert await apply_pending_configuration(paths, settings) == {"character": 1, "plugins": 0}
        assert not paths.pending.exists()
        after = (await client.get("/admin/v1/character", headers=bearer(TOKEN))).json()["data"]
        assert after == current
    finally:
        lock.release()


async def test_setup_does_not_overwrite_a_character_already_edited_in_the_product(
    personal, database, tmp_path, monkeypatch
):
    paths = SetupPaths(tmp_path)
    config = SetupConfiguration(
        {},
        "",
        None,
        write_model_profiles=False,
        initial_character={"display_name": "新角色", "slug": "new-role"},
    )
    commit_configuration(paths, EnvironmentDocument(""), config)
    original = paths.pending.read_bytes()
    response = await personal.post(
        "/admin/v1/character",
        headers=bearer(TOKEN),
        json={"request_id": str(uuid4()), "expected_revision": 1, "display_name": "已确认的名称"},
    )
    assert response.status_code == 200

    @asynccontextmanager
    async def actual_client(_settings):
        personal.headers.update(bearer(TOKEN))
        yield personal

    monkeypatch.setattr(operator_transport, "admin_client", actual_client)
    settings = make_settings(database.url)
    lock = SQLiteApplicationLock(settings.sqlite_path)
    lock.acquire()
    try:
        with pytest.raises(httpx.HTTPStatusError) as error:
            await apply_pending_configuration(paths, settings)
        assert error.value.response.status_code == 409
        assert paths.pending.read_bytes() == original
        current = (await personal.get("/admin/v1/character", headers=bearer(TOKEN))).json()["data"]
        assert current["display_name"] == "已确认的名称" and current["revision"] == 2
    finally:
        lock.release()


async def test_setup_keeps_pending_character_until_the_actual_bot_is_running(database, tmp_path):
    paths = SetupPaths(tmp_path)
    config = SetupConfiguration(
        {},
        "",
        None,
        write_model_profiles=False,
        initial_character={"display_name": "凜", "slug": "rin"},
    )
    commit_configuration(paths, EnvironmentDocument(""), config)
    original = paths.pending.read_bytes()
    with pytest.raises(SetupValidationError, match="启动 Bot"):
        await apply_pending_configuration(paths, make_settings(database.url))
    assert paths.pending.read_bytes() == original


async def test_existing_plugin_only_pending_configuration_remains_independent(database, tmp_path):
    paths = SetupPaths(tmp_path)
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    configuration = SetupConfiguration({}, "", (), write_model_profiles=False)
    commit_configuration(paths, EnvironmentDocument(""), configuration)
    assert json.loads(paths.pending.read_text()) == {"schema_version": 1, "selected_plugins": []}
    assert await apply_pending_configuration(
        paths, make_settings(database.url, plugin_directory=plugins)
    ) == {"character": 0, "plugins": 0}
    assert not paths.pending.exists()


async def test_no_plugin_selection_does_not_mean_disable_plugins(database, tmp_path, monkeypatch):
    from yuki.deployment_setup import service

    paths = SetupPaths(tmp_path)
    paths.pending.parent.mkdir(parents=True)
    paths.pending.write_text('{"schema_version": 1}', encoding="utf-8")
    forbidden = AsyncMock(side_effect=AssertionError("no plugin selection is not an empty grant"))
    monkeypatch.setattr(service, "_bootstrap_pending_plugins", forbidden)
    assert await apply_pending_configuration(paths, make_settings(database.url)) == {
        "character": 0,
        "plugins": 0,
    }
    forbidden.assert_not_awaited()
    assert not paths.pending.exists()
