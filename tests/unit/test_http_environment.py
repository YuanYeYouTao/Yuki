"""Startup proxy settings preserve process authority without exporting Bot keys."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from yuki.config import Settings


def test_bot_loads_http_environment_before_initialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from yuki import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(os, "environ", {"USERPROFILE": str(tmp_path), "HOME": str(tmp_path)})
    (tmp_path / "character").mkdir()
    soul_file = tmp_path / "character/SOUL.md"
    soul_file.write_text("Test persona", encoding="utf-8")
    (tmp_path / ".env").write_text("ALL_PROXY=http://bot.example:8080\n", encoding="utf-8")

    class InitializationObserved(Exception):
        pass

    def observe_initialization(**_kwargs: object) -> None:
        assert os.environ["ALL_PROXY"] == "http://bot.example:8080"
        raise InitializationObserved

    monkeypatch.setattr(main.nonebot, "init", observe_initialization)
    with pytest.raises(InitializationObserved):
        main.bootstrap(Settings(_env_file=None, soul_file=soul_file))


def test_management_cli_loads_http_environment_without_exporting_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from yuki import cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(os, "environ", {"USERPROFILE": str(tmp_path), "HOME": str(tmp_path)})
    (tmp_path / "character").mkdir()
    (tmp_path / "character/SOUL.md").write_text("Test persona", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["yuki", "init-db"])
    (tmp_path / ".env").write_text(
        "SOUL_FILE=character/SOUL.md\nALL_PROXY=http://cli.example:8080\n"
        "LLM_API_KEY=private-bot-key\n",
        encoding="utf-8",
    )
    observed: list[str] = []

    def initialize_database(settings: Settings) -> None:
        observed.append(os.environ["ALL_PROXY"])
        assert settings.llm_api_key == "private-bot-key"
        assert "LLM_API_KEY" not in os.environ

    monkeypatch.setattr(cli, "_init_database", initialize_database)
    cli.main()
    assert observed == ["http://cli.example:8080"]


def test_setup_loads_proxy_from_deployment_root_before_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from yuki.deployment_setup import command

    deployment = tmp_path / "deployment"
    deployment.mkdir()
    (tmp_path / ".env").write_text("ALL_PROXY=http://wrong.example:8080\n", encoding="utf-8")
    (deployment / ".env").write_text("ALL_PROXY=http://setup.example:8080\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(os, "environ", {})

    class HttpObserved(Exception):
        pass

    def verify_health(_url: str, *, timeout_seconds: float) -> dict[str, object]:
        assert timeout_seconds == 1
        assert os.environ["ALL_PROXY"] == "http://setup.example:8080"
        raise HttpObserved

    monkeypatch.setattr(command, "verify_health", verify_health)
    args = argparse.Namespace(
        deployment_root=deployment,
        no_color=True,
        setup_action="verify",
        health_url="http://127.0.0.1:8080/healthz",
        timeout=1,
    )
    with pytest.raises(HttpObserved):
        command.run_setup_command(args)


@pytest.fixture(scope="module")
def compose_command() -> list[str]:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker Compose CLI is unavailable; configuration parsing only")
    command = [docker, "compose"]
    result = subprocess.run([*command, "version"], capture_output=True, check=False)
    if result.returncode:
        pytest.skip("Docker Compose CLI is unavailable; configuration parsing only")
    return command


@pytest.mark.parametrize("name", ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"])
@pytest.mark.parametrize("source", ["file", "shell", "mixed", "empty"])
def test_compose_preserves_proxy_alias_selection_without_starting_containers(
    tmp_path: Path, compose_command: list[str], name: str, source: str
) -> None:
    root = Path(__file__).resolve().parents[2]
    compose_file = tmp_path / "docker-compose.yml"
    shutil.copyfile(root / "docker-compose.yml", compose_file)
    env_file = tmp_path / ".env"
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() not in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}
    }
    environment.pop("COMPOSE_ENV_FILES", None)
    environment.pop("COMPOSE_DISABLE_ENV_FILE", None)
    entries = ["YUKI_VERSION=test-proxy-config"]
    if source == "file":
        entries.append(f"{name}=file-value")
        expected = "file-value"
    elif source == "shell":
        environment[name] = "shell-value"
        expected = "shell-value"
    elif source == "mixed":
        environment[name] = "shell-upper"
        entries.append(f"{name.lower()}=file-lower")
        expected = "file-lower"
    else:
        entries.extend((f"{name}=file-upper", f"{name.lower()}="))
        expected = ""
    env_file.write_text("\n".join(entries) + "\n", encoding="utf-8")

    result = subprocess.run(
        [
            *compose_command,
            "--env-file",
            str(env_file),
            "--file",
            str(compose_file),
            "--profile",
            "snowluma",
            "config",
            "--format",
            "json",
        ],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    services = json.loads(result.stdout)["services"]
    bot = services["bot"]
    assert bot["environment"][name] == expected
    assert bot["environment"][name.lower()] == expected
    assert "host.docker.internal=host-gateway" in bot["extra_hosts"]
    assert name not in services["snowluma"]["environment"]
    assert name.lower() not in services["snowluma"]["environment"]
