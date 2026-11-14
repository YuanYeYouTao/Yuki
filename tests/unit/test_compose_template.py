"""A fresh deployment, as Compose renders it from the shipped template."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from tests.unit.test_http_environment import compose_command as compose_fixture

ROOT = Path(__file__).resolve().parents[2]
compose_command = compose_fixture


def _render(
    tmp_path: Path, command: list[str], *arguments: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    for name in ("docker-compose.yml", "docker-compose.local.yml"):
        shutil.copyfile(ROOT / name, tmp_path / name)
    shutil.copyfile(ROOT / ".env.example", tmp_path / ".env")
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("COMPOSE_")
    } | (env or {})
    return subprocess.run(
        [*command, "--project-directory", str(tmp_path), *arguments],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    "files", [["docker-compose.yml"], ["docker-compose.yml", "docker-compose.local.yml"]]
)
def test_fresh_deployment_bot_can_write_webui_config_and_starts_no_qq(
    tmp_path: Path, compose_command: list[str], files: list[str]
) -> None:
    arguments = [argument for name in files for argument in ("--file", name)]
    result = _render(tmp_path, compose_command, *arguments, "config", "--format", "json")
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    mounts = {volume["target"]: volume for volume in services["bot"]["volumes"]}
    assert not mounts["/app/webui-config"].get("read_only")
    assert "snowluma" not in services


def test_snowluma_has_no_default_image(tmp_path: Path, compose_command: list[str]) -> None:
    result = _render(
        tmp_path, compose_command, "--profile", "snowluma", "config", "--format", "json"
    )
    assert result.returncode != 0
    assert "snowluma" in result.stderr and "image" in result.stderr


@pytest.mark.parametrize(
    "files", [["docker-compose.yml"], ["docker-compose.yml", "docker-compose.local.yml"]]
)
def test_enabled_snowluma_reads_the_onebot_config_the_bot_writes(
    tmp_path: Path, compose_command: list[str], files: list[str]
) -> None:
    arguments = [argument for name in files for argument in ("--file", name)]
    result = _render(
        tmp_path,
        compose_command,
        *arguments,
        "--profile",
        "snowluma",
        "config",
        "--format",
        "json",
        env={"SNOWLUMA_IMAGE": "snowluma:owned"},
    )
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    written = {volume["target"]: volume["source"] for volume in services["bot"]["volumes"]}
    read = {volume["target"]: volume["source"] for volume in services["snowluma"]["volumes"]}
    assert written["/app/snowluma-data"] in read.values()
