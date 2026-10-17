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

from qq_ai_bot.config import Settings
from qq_ai_bot.http_environment import load_http_environment


def test_dotenv_exports_only_standard_http_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment: dict[str, str] = {}
    monkeypatch.setattr(os, "environ", environment)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "HTTP_PROXY=http://upper.example:8080\n"
        "http_proxy=http://lower.example:8081\n"
        "HTTPS_PROXY=http://secure.example:8082\n"
        "ALL_PROXY=socks5://fallback.example:1080\n"
        "NO_PROXY=localhost,api.example\n"
        "SSL_CERT_FILE=/certs/root.pem\n"
        "SSL_CERT_DIR=/certs\n"
        "LLM_API_KEY=private-bot-key\n"
        "YUKI_CONTROL_CREDENTIAL=private-control-key\n",
        encoding="utf-8",
    )

    load_http_environment(env_file)

    assert environment == {
        "HTTP_PROXY": "http://lower.example:8081",
        "HTTPS_PROXY": "http://secure.example:8082",
        "ALL_PROXY": "socks5://fallback.example:1080",
        "NO_PROXY": "localhost,api.example",
        "SSL_CERT_FILE": "/certs/root.pem",
        "SSL_CERT_DIR": "/certs",
    }


@pytest.mark.parametrize("name", ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"])
@pytest.mark.parametrize("lowercase", [False, True])
@pytest.mark.parametrize("process_value", ["", "process-value"])
def test_process_proxy_family_precedes_file_aliases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    lowercase: bool,
    process_value: str,
) -> None:
    process_name = name.lower() if lowercase else name
    environment = {process_name: process_value}
    monkeypatch.setattr(os, "environ", environment)
    env_file = tmp_path / ".env"
    env_file.write_text(f"{name}=file-upper\n{name.lower()}=file-lower\n", encoding="utf-8")

    load_http_environment(env_file)

    assert environment == {process_name: process_value}


def test_empty_lowercase_file_value_is_not_replaced_by_uppercase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment: dict[str, str] = {}
    monkeypatch.setattr(os, "environ", environment)
    env_file = tmp_path / ".env"
    env_file.write_text("ALL_PROXY=http://upper.example:8080\nall_proxy=\n", encoding="utf-8")

    load_http_environment(env_file)

    assert environment == {"ALL_PROXY": ""}


@pytest.mark.parametrize("name", ["SSL_CERT_FILE", "SSL_CERT_DIR"])
@pytest.mark.parametrize("value", ["", "/process/certificates"])
def test_process_ca_group_precedes_file_ca_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    environment = {name: value}
    monkeypatch.setattr(os, "environ", environment)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "SSL_CERT_FILE=/file/root.pem\nSSL_CERT_DIR=/file/certs\n", encoding="utf-8"
    )

    load_http_environment(env_file)

    assert environment == {name: value}


def test_missing_dotenv_does_not_change_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = {"UNCHANGED": "value"}
    monkeypatch.setattr(os, "environ", environment)

    load_http_environment(tmp_path / "missing.env")

    assert environment == {"UNCHANGED": "value"}


def test_bot_loads_http_environment_before_initialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qq_ai_bot import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(os, "environ", {"USERPROFILE": str(tmp_path), "HOME": str(tmp_path)})
    (tmp_path / "config").mkdir()
    (tmp_path / "config/persona.md").write_text("Test persona", encoding="utf-8")
    (tmp_path / ".env").write_text("ALL_PROXY=http://bot.example:8080\n", encoding="utf-8")

    class InitializationObserved(Exception):
        pass

    def observe_initialization(**_kwargs: object) -> None:
        assert os.environ["ALL_PROXY"] == "http://bot.example:8080"
        raise InitializationObserved

    monkeypatch.setattr(main.nonebot, "init", observe_initialization)
    with pytest.raises(InitializationObserved):
        main.bootstrap(Settings(_env_file=None))


def test_management_cli_loads_http_environment_without_exporting_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qq_ai_bot import cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(os, "environ", {"USERPROFILE": str(tmp_path), "HOME": str(tmp_path)})
    (tmp_path / "config").mkdir()
    (tmp_path / "config/persona.md").write_text("Test persona", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["yuki", "init-db"])
    (tmp_path / ".env").write_text(
        "ALL_PROXY=http://cli.example:8080\nLLM_API_KEY=private-bot-key\n", encoding="utf-8"
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
    from qq_ai_bot.deployment_setup import command

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
