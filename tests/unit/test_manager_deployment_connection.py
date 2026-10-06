"""Default deployment connects one Manager/store and verifies actual read access."""

from __future__ import annotations

import asyncio
import json
import os
import runpy
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from qq_ai_bot.deployment_setup.environment_check import check_environment

ROOT = Path(__file__).resolve().parents[2]
helper = runpy.run_path(str(ROOT / "deploy/sandbox/configure-manager.py"))
configure = helper["configure_store"]


def test_manager_store_configuration_preserves_default_and_existing_values(tmp_path):
    environment = tmp_path / "deployment.env"
    assert configure(environment, None, check=False) == Path("/opt/yuki-qqbot/workspace")
    assert not environment.exists()
    root = tmp_path / "deployment space 中文"
    root.mkdir()
    environment.write_text('YUKI_OTHER_SETTING="preserved"\n', encoding="utf-8")
    assert configure(environment, root, check=True) == root / "workspace"
    assert environment.read_text() == 'YUKI_OTHER_SETTING="preserved"\n'
    assert configure(environment, root, check=False) == root / "workspace"
    saved = environment.read_bytes()
    assert 'YUKI_OTHER_SETTING="preserved"' in saved.decode()
    assert configure(environment, None, check=False) == root / "workspace"
    assert configure(environment, root, check=False) == root / "workspace"
    assert environment.read_bytes() == saved
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(ValueError, match="already_configured"):
        configure(environment, other, check=False)
    assert environment.read_bytes() == saved


@pytest.mark.parametrize("value", ["relative", '"one"\nYUKI_MANAGER_WORKSPACE_STORE="two"'])
def test_manager_rejects_ambiguous_or_relative_store_without_changes(tmp_path, value):
    environment = tmp_path / "deployment.env"
    environment.write_text("YUKI_MANAGER_WORKSPACE_STORE=" + value + "\n")
    previous = environment.read_bytes()
    with pytest.raises(ValueError):
        configure(environment, None, check=False)
    assert environment.read_bytes() == previous


def test_manager_unquoted_hash_is_a_literal_path_character(tmp_path):
    environment = tmp_path / "deployment.env"
    literal = tmp_path.as_posix() + "/root#old/workspace"
    previous = (f"YUKI_MANAGER_WORKSPACE_STORE={literal}\n").encode()
    environment.write_bytes(previous)
    assert configure(environment, None, check=False) == Path(literal)
    assert environment.read_bytes() == previous


@pytest.mark.parametrize(
    "literal",
    [
        "/opt/root space/#$`/中文/workspace",
        '/opt/root"quote\\backslash/workspace',
    ],
)
def test_manager_helper_quoted_special_characters_round_trip(literal):
    encoded = literal.replace("\\", "\\\\").replace('"', '\\"')
    assert helper["_store_value"]('"' + encoded + '"') == literal


@pytest.mark.parametrize(
    "value",
    [
        '/opt/"old"/workspace',
        '"/opt/old"/workspace',
        "'/opt/old/workspace'",
        '"/opt/old\\u0023/workspace"',
        "/opt/old\\ workspace",
        "/opt/old\\\n/workspace",
        '"/opt/old\n/workspace"',
    ],
)
def test_manager_ambiguous_legacy_values_fail_closed_without_rewriting_other_keys(tmp_path, value):
    environment = tmp_path / "deployment.env"
    previous = (
        b'YUKI_OTHER_SETTING="untouched"\r\n'
        + ("YUKI_MANAGER_WORKSPACE_STORE=" + value + "\n").encode()
    )
    environment.write_bytes(previous)
    with pytest.raises(ValueError):
        configure(environment, None, check=False)
    assert environment.read_bytes() == previous


def test_manager_adding_own_setting_preserves_other_key_bytes_exactly(tmp_path):
    environment = tmp_path / "deployment.env"
    previous = '# retained 中文\r\nYUKI_OTHER_SETTING="literal # $"\r\n'.encode()
    environment.write_bytes(previous)
    root = tmp_path / "root $ # 中文"
    root.mkdir()
    configure(environment, root, check=False)
    assert environment.read_bytes().startswith(previous)
    saved = environment.read_bytes()
    assert configure(environment, None, check=False) == root / "workspace"
    assert environment.read_bytes() == saved


@pytest.mark.parametrize("failure", ["connection", "not_ready", "file_interface"])
async def test_environment_check_fails_closed_and_never_executes_code(
    tmp_path, monkeypatch, failure
):
    monkeypatch.setattr(os, "geteuid", lambda: 10001, raising=False)
    statuses = {
        "connection": [{"error": "sandbox_unavailable"}],
        "not_ready": [{"ready": False, "workspace": "/workspace"}],
        "file_interface": [{"ready": True, "workspace": "/workspace"}, {"error": "unknown_tool"}],
    }
    calls = AsyncMock(side_effect=statuses[failure])
    monkeypatch.setattr("qq_ai_bot.deployment_setup.environment_check.SandboxClient.execute", calls)
    result = await check_environment(tmp_path / "manager.sock", tmp_path)
    assert result["ok"] is False
    assert all(
        call.args[0] in {"environment_status", "workspace_list"} for call in calls.await_args_list
    )
    assert calls.await_count == (2 if failure == "file_interface" else 1)


async def test_environment_check_requires_bot_identity_before_socket_access(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    calls = AsyncMock()
    monkeypatch.setattr("qq_ai_bot.deployment_setup.environment_check.SandboxClient.execute", calls)
    assert (await check_environment(tmp_path / "manager.sock", tmp_path))[
        "error"
    ] == "bot_uid_required"
    calls.assert_not_awaited()


async def test_environment_check_rejects_missing_store_before_socket_access(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 10001, raising=False)
    calls = AsyncMock()
    monkeypatch.setattr("qq_ai_bot.deployment_setup.environment_check.SandboxClient.execute", calls)
    result = await check_environment(tmp_path / "manager.sock", tmp_path / "missing")
    assert result["error"] == "artifact_store_not_writable"
    calls.assert_not_awaited()


@pytest.mark.skipif(
    os.name != "posix", reason="Actual Manager directory FDs/AF_UNIX run in Linux CI"
)
async def test_environment_check_real_manager_socket_is_read_only(tmp_path):
    from qq_ai_bot.sandbox.persistent import PersistentManager
    from qq_ai_bot.workspace.store import WorkspaceStore

    home, artifacts = tmp_path / "home", tmp_path / "artifacts"
    (home / "workspace").mkdir(parents=True)
    (home / "workspace" / "private-name.txt").write_text("private file content")
    artifacts.mkdir()
    manager = PersistentManager(
        tmp_path / "manager",
        WorkspaceStore(artifacts),
        "unused",
        "unused",
        "unused",
        home,
        testing=True,
    )
    manager.ready = True
    manager.command = AsyncMock(side_effect=AssertionError("check must not touch Docker/execd"))
    socket = tmp_path / "manager.sock"
    server = await asyncio.start_unix_server(manager.serve, str(socket))
    before = list(manager.db.iterdump())
    files = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    try:
        result = await check_environment(socket, artifacts, expected_uid=os.geteuid())
        assert result["ok"] is True
        assert "private-name" not in json.dumps(result)
        assert "private file content" not in json.dumps(result)
        assert list(manager.db.iterdump()) == before
        assert sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*")) == files
        manager.command.assert_not_awaited()
    finally:
        server.close()
        await server.wait_closed()
        manager.db.close()


def test_default_deployment_manager_mount_and_uid_contract():
    from scripts.build_release_bundle import _EMPTY_DIRECTORIES

    from qq_ai_bot.deployment_setup.command import _PERSISTENT_DIRECTORIES

    assert {"workspace", "social-transfer"} <= set(_PERSISTENT_DIRECTORIES)
    assert {"workspace", "social-transfer"} <= set(_EMPTY_DIRECTORIES)
    compose = (ROOT / "docker-compose.yml").read_text()
    unit = (ROOT / "deploy/sandbox/yuki-sandbox.service").read_text()
    installer = (ROOT / "deploy/sandbox/install-manager.sh").read_text()
    assert "./workspace:/app/workspace" in compose
    assert "/run/yuki-sandbox:/run/yuki-sandbox:ro" in compose
    assert "Environment=YUKI_MANAGER_WORKSPACE_STORE=/opt/yuki-qqbot/workspace" in unit
    assert "--workspace ${YUKI_MANAGER_WORKSPACE_STORE}" in unit
    assert "EnvironmentFile=-/etc/yuki-sandbox/deployment.env" in unit
    assert "--persistent-home /var/lib/yuki-sandbox/home" in unit
    assert "--deployment-root" in installer and '"$workspace"' in installer
    assert "systemctl is-active --quiet yuki-sandbox" in installer
    assert "Group=10001" in unit and "RuntimeDirectoryMode=0750" in unit
    assert "/var/run/docker.sock" not in compose


def test_setup_environment_check_is_a_distinct_explicit_command():
    import argparse

    from qq_ai_bot.deployment_setup.command import add_setup_parser

    parser = argparse.ArgumentParser()
    add_setup_parser(parser.add_subparsers(dest="command"))
    assert parser.parse_args(["setup", "environment-check"]).setup_action == "environment-check"


@pytest.mark.parametrize("available,exit_code", [(True, 0), (False, 1)])
def test_setup_environment_check_returns_readiness_exit_status(
    tmp_path, monkeypatch, available, exit_code
):
    import argparse
    from types import SimpleNamespace

    from qq_ai_bot.deployment_setup.command import run_setup_command

    settings = SimpleNamespace(
        sandbox_socket=tmp_path / "manager.sock", workspace_directory=tmp_path
    )
    output = []
    monkeypatch.setattr(
        "qq_ai_bot.deployment_setup.command.TerminalUI",
        lambda **kwargs: SimpleNamespace(line=output.append),
    )
    monkeypatch.setattr("qq_ai_bot.deployment_setup.command.Settings", lambda: settings)
    probe = AsyncMock(
        return_value={"ok": available, "manager": "connected" if available else "missing"}
    )
    monkeypatch.setattr("qq_ai_bot.deployment_setup.environment_check.check_environment", probe)
    code = run_setup_command(
        argparse.Namespace(
            deployment_root=tmp_path, no_color=True, setup_action="environment-check"
        )
    )
    assert code == exit_code
    probe.assert_awaited_once_with(settings.sandbox_socket, settings.workspace_directory)
    assert json.loads(output[0])["ok"] is available
