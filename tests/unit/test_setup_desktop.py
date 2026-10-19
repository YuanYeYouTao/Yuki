"""Deployment choices reach actual Manager resource arguments without changing old containers."""

import runpy
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from yuki.deployment_setup.command import _page_character, _SetupDraft
from yuki.deployment_setup.service import SetupPaths
from yuki.deployment_setup.terminal import TerminalUI
from yuki.sandbox import persistent
from yuki.sandbox.manager import desktop_session
from yuki.sandbox.persistent import PersistentManager
from yuki.workspace.store import WorkspaceStore


@pytest.mark.parametrize("enabled", [True, False])
async def test_real_setup_inputs_configure_only_new_manager_resource_defaults(
    tmp_path, monkeypatch, enabled
):
    answers = iter(["凜", "rin", "y" if enabled else "n", "https://example.com/roles/rin/"])
    draft = _SetupDraft({}, "chat_completions", False, initial=True)
    _page_character(
        SetupPaths(tmp_path),
        TerminalUI(no_color=True, input_fn=lambda _: next(answers), output=StringIO()),
        draft,
    )
    assert draft.initial_character == {"display_name": "凜", "slug": "rin"}
    assert draft.environment["BOT_DISPLAY_NAME"] == "凜"
    assert draft.environment["CLIENT_PUBLIC_URL"] == "https://example.com/roles/rin"
    (tmp_path / ".env").write_text(
        f"DESKTOP_SESSION_ENABLED={draft.environment['DESKTOP_SESSION_ENABLED']}\n",
        encoding="utf-8",
    )
    module = runpy.run_path(str(Path(__file__).parents[2] / "deploy/sandbox/configure-manager.py"))
    env_file = tmp_path / "manager.env"
    env_file.write_text("CUSTOM_OPERATOR_SETTING=keep\n", encoding="utf-8")
    configure = module["configure_store"]
    configure(env_file, tmp_path, check=True)
    assert env_file.read_text() == "CUSTOM_OPERATOR_SETTING=keep\n"
    configure(env_file, tmp_path, check=False)
    value = next(
        line.partition("=")[2]
        for line in env_file.read_text().splitlines()
        if line.startswith("YUKI_MANAGER_DESKTOP_SESSION=")
    )
    manager = PersistentManager(
        tmp_path / "manager",
        WorkspaceStore(tmp_path / "artifacts"),
        "image",
        "network",
        "",
        tmp_path / "home",
        testing=True,
        desktop_session=desktop_session(value),
    )
    manager.home.mkdir(parents=True, exist_ok=True)
    home_file = manager.home / "original-user-file"
    home_file.write_bytes(b"keep original home")
    current = {
        "Id": "original",
        "State": {"Running": True, "StartedAt": "started"},
        "HostConfig": {"RestartPolicy": {"Name": "no"}},
        "NetworkSettings": {"Networks": {"network": {"IPAddress": "172.30.0.2"}}},
        "Mounts": [],
    }
    manager.inspect_container = AsyncMock(side_effect=[None, current])
    manager.command = AsyncMock(return_value=(0, b""))
    monkeypatch.setattr(
        persistent, "Execd", lambda *_: SimpleNamespace(request=AsyncMock(return_value={}))
    )
    try:
        await manager.recover()
        command = manager.command.await_args.args
        assert command[:2] == ("docker", "create")
        assert command[command.index("--memory") + 1] == ("3072m" if enabled else "512m")
        assert float(command[command.index("--cpus") + 1]) == (2 if enabled else 1)
        assert command[command.index("--pids-limit") + 1] == "2048"
        manager.set_setting("resource_memory_mib", "4096")
        assert manager.resource_limits()["memory_mib"] == 4096
        manager.inspect_container = AsyncMock(return_value=current)
        manager.command.reset_mock()
        await manager.recover()
        manager.command.assert_not_awaited()
        assert home_file.read_bytes() == b"keep original home"
    finally:
        await manager.close()
    original = env_file.read_bytes()
    (tmp_path / ".env").write_text(
        f"DESKTOP_SESSION_ENABLED={'false' if enabled else 'true'}\n", encoding="utf-8"
    )
    configure(env_file, tmp_path, check=False)
    assert env_file.read_bytes() == original


def test_deployment_desktop_profile_uses_dotenv_case_and_quotes_without_executing_shell(tmp_path):
    module = runpy.run_path(str(Path(__file__).parents[2] / "deploy/sandbox/configure-manager.py"))
    marker = tmp_path / "must-not-execute"
    (tmp_path / ".env").write_text(
        "export\tdesktop_session_enabled='false' # actual operator choice\n"
        f"UNRELATED=$(touch {marker})\n",
        encoding="utf-8",
    )
    assert module["desktop_profile"](tmp_path) == "false"
    assert not marker.exists()
