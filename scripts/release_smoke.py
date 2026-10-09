"""Run source-free production Compose smoke and persistence checks."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
import tomllib
from pathlib import Path
from typing import Any


class SmokeError(RuntimeError):
    """Raised when a release image or deployment contract fails smoke testing."""


class Compose:
    def __init__(self, deploy_directory: Path, project: str, version: str) -> None:
        self.deploy_directory = deploy_directory
        self.environment = os.environ.copy()
        self.environment["YUKI_VERSION"] = version
        self.command = ["docker", "compose", "--project-name", project]

    def run(self, *arguments: str, capture: bool = False) -> str:
        completed = subprocess.run(
            [*self.command, *arguments],
            cwd=self.deploy_directory,
            env=self.environment,
            check=True,
            capture_output=capture,
            encoding="utf-8",
            errors="replace",
        )
        return completed.stdout.strip() if capture else ""


def _read_healthz(compose: Compose) -> dict[str, Any]:
    command = (
        "import json,urllib.request; "
        "print(json.dumps(json.load(urllib.request.urlopen("
        "'http://127.0.0.1:8080/healthz', timeout=3))))"
    )
    return json.loads(compose.run("exec", "-T", "bot", "python", "-c", command, capture=True))


def _assert_core_health(health: dict[str, Any], version: str) -> None:
    expected = {"status": "ok", "version": version, "database": "ok"}
    actual = {key: health.get(key) for key in expected}
    if actual != expected:
        raise SmokeError(f"unexpected /healthz response: {actual}")


def wait_healthy(compose: Compose, service: str, timeout_seconds: float = 120.0) -> str:
    container_id = compose.run("ps", "--quiet", service, capture=True)
    if not container_id:
        raise SmokeError(f"{service} container was not created")
    deadline = time.monotonic() + timeout_seconds
    last_status = "unknown"
    while time.monotonic() < deadline:
        last_status = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Health.Status}}", container_id],
            check=True,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        ).stdout.strip()
        if last_status == "healthy":
            return container_id
        time.sleep(2)
    raise SmokeError(
        f"{service} did not become healthy within {timeout_seconds:.0f}s "
        f"(last status: {last_status})"
    )


def verify_bot(compose: Compose, deploy_directory: Path, version: str) -> None:
    wait_healthy(compose, "bot")
    compose.run("exec", "-T", "bot", "python", "/app/scripts/verify_monty_packaging.py", "direct")
    health = _read_healthz(compose)
    _assert_core_health(health, version)
    migration_command = (
        "import sqlite3; "
        "connection=sqlite3.connect('/app/data/qq_ai_bot.db'); "
        "row=connection.execute('SELECT version_num FROM alembic_version').fetchone(); "
        "connection.close(); "
        "from qq_ai_bot.persistence.schema_guard import canonical_schema_revision; "
        "expected=canonical_schema_revision(); "
        "assert row and row[0] == expected, (row, expected); print('ok')"
    )
    alembic_version = compose.run(
        "exec", "-T", "bot", "python", "-c", migration_command, capture=True
    )
    if alembic_version != "ok":
        raise SmokeError(f"unexpected Alembic version: {alembic_version!r}")
    # Smoke validation is read-only; approval and plugin lifecycle belong to
    # explicit setup, authenticated against the running PluginManager.


def verify_persistence(
    compose: Compose, deploy_directory: Path, sentinels: dict[Path, str]
) -> None:
    database = deploy_directory / "data/qq_ai_bot.db"
    database_size = database.stat().st_size
    compose.run("up", "-d", "--no-deps", "--pull", "never", "--force-recreate", "bot")
    wait_healthy(compose, "bot")
    if not database.exists() or database.stat().st_size < database_size:
        raise SmokeError("database did not survive container recreation")
    for path, value in sentinels.items():
        if path.read_text(encoding="utf-8") != value:
            raise SmokeError(f"persistent sentinel did not survive recreation: {path}")


def prepare_deployment(deploy_directory: Path) -> dict[Path, str]:
    env_file = deploy_directory / ".env"
    if not env_file.exists():
        environment = (deploy_directory / ".env.example").read_text(encoding="utf-8")
        replacements = {
            "LLM_API_KEY=replace-with-api-key": "LLM_API_KEY=release-smoke-key",
            "LLM_MODEL=replace-with-model-name": "LLM_MODEL=release-smoke-model",
            "MEMORY_EMBEDDING_ENABLED=true": "MEMORY_EMBEDDING_ENABLED=false",
            "WEB_MODE=native": "WEB_MODE=disabled",
        }
        for old, new in replacements.items():
            environment = environment.replace(old, new)
        env_file.write_text(environment, encoding="utf-8")
    profiles = deploy_directory / "webui-config/model_profiles.toml"
    if not profiles.exists():
        example = tomllib.loads(
            (deploy_directory / "config/model_profiles.example.toml").read_text(encoding="utf-8")
        )
        profiles.parent.mkdir(parents=True, exist_ok=True)
        profiles.write_text(
            "schema_version = 3\n\n[profiles.main]\n"
            'provider = "openai_compatible"\nprotocol = "chat_completions"\n'
            'base_url_env = "LLM_BASE_URL"\napi_key_env = "LLM_API_KEY"\n'
            'model_env = "LLM_MODEL"\nstructured_output_mode = "function_tool"\n'
            "timeout_seconds = 120.0\nmax_retries = 0\n"
            "default_temperature = 0.0\ndefault_max_output_tokens = 512\n"
            'capabilities = ["tools", "structured_output", "long_context", "reasoning"]\n'
            "\n[routes]\n" + "".join(f'{task} = "main"\n' for task in example["routes"]),
            encoding="utf-8",
        )
    sentinel = deploy_directory / "data/.release-smoke-data"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("data", encoding="utf-8")
    return {sentinel: "data"}


def run_smoke(deploy_directory: Path, version: str, *, full: bool = False) -> None:
    if (deploy_directory / "src").exists() or (deploy_directory / "pyproject.toml").exists():
        raise SmokeError("deployment smoke directory contains project source")
    compose = Compose(deploy_directory, f"yuki-release-smoke-{os.getpid()}", version)
    sentinels = prepare_deployment(deploy_directory)
    try:
        compose.run("up", "-d", "--no-deps", "--pull", "never", "bot")
        verify_bot(compose, deploy_directory, version)
        verify_persistence(compose, deploy_directory, sentinels)
    finally:
        compose.run("rm", "--stop", "--force", "bot")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--deploy-dir", type=Path, required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    run_smoke(args.deploy_dir.resolve(), args.version)
    print(f"source-free smoke passed for Yuki {args.version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
