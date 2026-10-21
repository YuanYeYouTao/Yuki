"""Installers select the Docker host platform; release smoke checks exact images."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from scripts.release_smoke import Compose, SmokeError, verify_image_identity

ROOT = Path(__file__).resolve().parents[2]
ARCHITECTURES = (
    ("amd64", "linux/amd64"),
    ("x86_64", "linux/amd64"),
    ("arm64", "linux/arm64"),
    ("aarch64", "linux/arm64"),
)


def _deployment(tmp_path):
    deployment = tmp_path / "existing deployment"
    deployment.mkdir()
    (deployment / "docker-compose.yml").write_text("name: existing\n", encoding="utf-8")
    (deployment / ".env.example").write_text("USER_CONFIG=template\n", encoding="utf-8")
    (deployment / ".env").write_text("USER_CONFIG=keep\n", encoding="utf-8")
    return deployment


def _assert_native_setup(commands, platform, deployment):
    pull = next(command for command in commands if command[0] == "pull")
    run = next(command for command in commands if command[0] == "run")
    for command in (pull, run):
        assert command[command.index("--platform") + 1] == platform
        assert "ghcr.io/yuanyeyoutao/yuki:9.8.7" in command
    assert run[run.index("--entrypoint") + 1] == "yuki"
    assert run[-3:] == ["setup", "--deployment-root", "/deploy"]
    assert not any(command[0] in {"up", "stop", "down", "restart"} for command in commands)
    assert (deployment / ".env").read_text(encoding="utf-8") == "USER_CONFIG=keep\n"
    assert (deployment / "docker-compose.yml").read_text(encoding="utf-8") == "name: existing\n"


@pytest.mark.parametrize(("architecture", "platform"), ARCHITECTURES)
def test_powershell_installer_selects_docker_host_platform(tmp_path, architecture, platform):
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is not installed")
    deployment = _deployment(tmp_path)
    log = tmp_path / "docker.jsonl"
    harness = tmp_path / "installer-harness.ps1"
    harness.write_text(
        """
function global:docker {
    @($args) | ConvertTo-Json -Compress | Add-Content -LiteralPath $env:DOCKER_LOG
    $global:LASTEXITCODE = 0
    if ($args[0] -eq 'info' -and $args.Count -gt 1) {
        if ($args[2] -eq '{{.Architecture}}') { Write-Output $env:DOCKER_ARCH }
        elseif ($args[2] -eq '{{.OSType}}') { Write-Output 'linux' }
    }
}
& $env:INSTALLER -InstallDir $env:DEPLOY_DIRECTORY -Version 9.8.7
""",
        encoding="utf-8",
    )
    environment = {
        **os.environ,
        "DOCKER_ARCH": architecture,
        "DOCKER_LOG": str(log),
        "INSTALLER": str(ROOT / "install.ps1"),
        "DEPLOY_DIRECTORY": str(deployment),
    }
    completed = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-File", str(harness)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    commands = [json.loads(line) for line in log.read_text(encoding="utf-8-sig").splitlines()]
    _assert_native_setup(commands, platform, deployment)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX installer needs a POSIX PTY")
@pytest.mark.parametrize(("architecture", "platform"), ARCHITECTURES)
def test_shell_installer_selects_docker_host_platform(tmp_path, architecture, platform):
    deployment = _deployment(tmp_path)
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    log = tmp_path / "docker.jsonl"
    docker = bin_directory / "docker"
    docker.write_text(
        f"""#!{sys.executable}
import json, os, sys
with open(os.environ['DOCKER_LOG'], 'a') as writer:
    writer.write(json.dumps(sys.argv[1:]) + '\\n')
if sys.argv[1:3] == ['info', '--format']:
    print(os.environ['DOCKER_ARCH'] if sys.argv[3] == '{{{{.Architecture}}}}' else 'linux')
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    environment = {
        **os.environ,
        "DOCKER_ARCH": architecture,
        "DOCKER_LOG": str(log),
        "PATH": str(bin_directory) + os.pathsep + os.environ["PATH"],
    }
    command = shlex.join(
        [
            "sh",
            str(ROOT / "install.sh"),
            "--dir",
            str(deployment),
            "--version",
            "9.8.7",
        ]
    )
    completed = subprocess.run(
        ["script", "-q", "-e", "-c", command, "/dev/null"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    commands = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    _assert_native_setup(commands, platform, deployment)


@pytest.mark.parametrize("platform", ["linux/amd64", "linux/arm64"])
def test_smoke_platform_is_local_to_its_compose_environment(tmp_path, monkeypatch, platform):
    monkeypatch.setenv("DOCKER_DEFAULT_PLATFORM", "operator-setting")
    compose = Compose(tmp_path, "isolated-smoke", "3.9.0", platform=platform)
    assert compose.environment["DOCKER_DEFAULT_PLATFORM"] == platform
    assert os.environ["DOCKER_DEFAULT_PLATFORM"] == "operator-setting"


@pytest.mark.parametrize("platform", ["linux/amd64", "linux/arm64"])
@pytest.mark.parametrize("mismatch", [None, "version", "revision", "architecture"])
def test_smoke_checks_the_selected_image_before_running_it(
    tmp_path,
    monkeypatch,
    platform,
    mismatch,
):
    metadata = {
        "Os": "linux",
        "Architecture": platform.split("/")[1],
        "Config": {
            "Labels": {
                "org.opencontainers.image.version": "3.9.0",
                "org.opencontainers.image.revision": "expected-commit",
            }
        },
    }
    if mismatch == "architecture":
        metadata["Architecture"] = "wrong-architecture"
    elif mismatch is not None:
        metadata["Config"]["Labels"]["org.opencontainers.image." + mismatch] = "wrong-" + mismatch
    compose = Compose(tmp_path, "identity-test", "3.9.0", platform=platform)
    monkeypatch.setattr(
        compose,
        "run",
        lambda *args, **kwargs: json.dumps({"services": {"bot": {"image": "image:3.9.0"}}}),
    )
    commands = []

    def execute(command, **kwargs):
        commands.append(command)
        assert command == ["docker", "image", "inspect", "--platform", platform, "image:3.9.0"]
        return json.dumps([metadata])

    monkeypatch.setattr(subprocess, "check_output", execute)
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs: commands.append(command))
    if mismatch is None:
        verify_image_identity(compose, "3.9.0", platform, "expected-commit")
    else:
        with pytest.raises(SmokeError):
            verify_image_identity(compose, "3.9.0", platform, "expected-commit")
    assert commands == [["docker", "image", "inspect", "--platform", platform, "image:3.9.0"]]
