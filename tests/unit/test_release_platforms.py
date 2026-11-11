"""Installers select the Docker host platform; release smoke checks exact images."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
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


VERSION = "9.8.7"
# The engine the fakes report: 8 GiB of memory and no gVisor.
ENGINE = {"memory": str(8 * 1024**3), "runtimes": '{"runc":{"path":"runc"}}'}


def _bundle(tmp_path):
    """A release archive with the files the installer uses, in both archive formats."""
    root = f"yuki-{VERSION}-deploy"
    files = {
        "docker-compose.yml": "name: released\n",
        ".env.example": "USER_CONFIG=template\n",
        "deploy/sandbox/prepare.sh": (ROOT / "deploy/sandbox/prepare.sh").read_text(),
    }
    tar_path, zip_path = tmp_path / f"{root}.tar.gz", tmp_path / f"{root}.zip"
    with tarfile.open(tar_path, "w:gz") as archive, zipfile.ZipFile(zip_path, "w") as package:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(f"{root}/{name}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
            package.writestr(f"{root}/{name}", data)
    return tar_path, zip_path


def _deployment(tmp_path):
    deployment = tmp_path / "existing deployment"
    deployment.mkdir()
    (deployment / "docker-compose.yml").write_text("name: existing\n", encoding="utf-8")
    (deployment / ".env.example").write_text("USER_CONFIG=template\n", encoding="utf-8")
    (deployment / ".env").write_text("USER_CONFIG=keep\n", encoding="utf-8")
    (deployment / "docker-compose.override.yml").write_text("services: {}\n", encoding="utf-8")
    return deployment


def _assert_upgrade(commands, platform, deployment):
    """An existing deployment: release files replaced, .env kept and completed, started."""
    pulls = [command for command in commands if command[0] == "pull"]
    assert [command[-1] for command in pulls] == [
        f"ghcr.io/yuanyeyoutao/yuki:{VERSION}",
        f"ghcr.io/yuanyeyoutao/yuki-environment:{VERSION}-desktop",
    ]
    assert all(command[command.index("--platform") + 1] == platform for command in pulls)
    # .env exists: no configuration wizard runs again.
    assert not any(command[0] == "run" for command in commands)
    assert ["volume", "inspect", "yuki-sandbox-home"] in commands
    up = next(command for command in commands if command[:2] == ["compose", "up"])
    assert "-d" in up and "--remove-orphans" in up
    check = next(command for command in commands if command[:2] == ["compose", "exec"])
    assert check[-3:] == ["yuki", "setup", "environment-check"]
    assert check[check.index("--user") + 1] == "10001:10001"
    environment = (deployment / ".env").read_text(encoding="utf-8").splitlines()
    assert environment[0] == "USER_CONFIG=keep"
    assert {
        f"YUKI_VERSION={VERSION}",
        "YUKI_SANDBOX_PROFILE=desktop",
        "YUKI_SANDBOX_RUNTIME=runc",
    } <= set(environment)
    assert not any(line.startswith("YUKI_CHARACTER_NAME=") for line in environment)
    # An existing deployment already has its owner: no new Person, grant or code.
    assert not any("desktop-pair" in " ".join(command) for command in commands)
    assert (deployment / "docker-compose.yml").read_text(encoding="utf-8") == "name: released\n"
    assert (deployment / "docker-compose.override.yml").read_text() == "services: {}\n"


@pytest.mark.parametrize(("architecture", "platform"), ARCHITECTURES)
def test_powershell_installer_selects_docker_host_platform(tmp_path, architecture, platform):
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is not installed")
    deployment = _deployment(tmp_path)
    _tar, bundle = _bundle(tmp_path)
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
        elseif ($args[2] -eq '{{.MemTotal}}') { Write-Output $env:DOCKER_MEMORY }
        elseif ($args[2] -eq '{{json .Runtimes}}') { Write-Output $env:DOCKER_RUNTIMES }
    }
    if ($args[0] -eq 'image' -or $args[0] -eq 'inspect') { $global:LASTEXITCODE = 1 }
}
& $env:INSTALLER -InstallDir $env:DEPLOY_DIRECTORY -Version 9.8.7 -Yes -Profile desktop `
    -Bundle $env:BUNDLE
""",
        encoding="utf-8",
    )
    environment = {
        **os.environ,
        "DOCKER_ARCH": architecture,
        "DOCKER_MEMORY": ENGINE["memory"],
        "DOCKER_RUNTIMES": ENGINE["runtimes"],
        "DOCKER_LOG": str(log),
        "INSTALLER": str(ROOT / "install.ps1"),
        "DEPLOY_DIRECTORY": str(deployment),
        "BUNDLE": str(bundle),
    }
    completed = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-File", str(harness)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    commands = [json.loads(line) for line in log.read_text(encoding="utf-8-sig").splitlines()]
    _assert_upgrade(commands, platform, deployment)


def _fake_docker(tmp_path):
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    docker = bin_directory / "docker"
    docker.write_text(
        f"""#!{sys.executable}
import json, os, sys
arguments = sys.argv[1:]
with open(os.environ['DOCKER_LOG'], 'a') as writer:
    writer.write(json.dumps(arguments) + '\\n')
if arguments[:2] == ['info', '--format']:
    print({{
        '{{{{.Architecture}}}}': os.environ['DOCKER_ARCH'],
        '{{{{.OSType}}}}': 'linux',
        '{{{{.MemTotal}}}}': os.environ['DOCKER_MEMORY'],
        '{{{{json .Runtimes}}}}': os.environ['DOCKER_RUNTIMES'],
    }}[arguments[2]])
if any('desktop-pair' in argument for argument in arguments):
    owner = arguments[arguments.index('-e') + 1]
    print('person-1 PAIR-' + owner.split('=', 1)[1])
if arguments[0] == 'run' and 'desktop-configure' in arguments:
    sys.stdin.read()
    with open(os.path.join(os.environ['DEPLOY_DIRECTORY'], '.env'), 'a') as env:
        env.write('LLM_MODEL=configured\\n')
# Nothing is local yet, and no environment of the old host Manager exists.
sys.exit(1 if arguments[:2] == ['image', 'inspect'] or arguments[0] == 'inspect' else 0)
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    return bin_directory


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX installer")
@pytest.mark.parametrize(("architecture", "platform"), ARCHITECTURES)
def test_shell_installer_selects_docker_host_platform(tmp_path, architecture, platform):
    deployment = _deployment(tmp_path)
    bundle, _zip = _bundle(tmp_path)
    log = tmp_path / "docker.jsonl"
    environment = {
        **os.environ,
        "DOCKER_ARCH": architecture,
        "DOCKER_MEMORY": ENGINE["memory"],
        "DOCKER_RUNTIMES": ENGINE["runtimes"],
        "DOCKER_LOG": str(log),
        "DEPLOY_DIRECTORY": str(deployment),
        "PATH": str(_fake_docker(tmp_path)) + os.pathsep + os.environ["PATH"],
    }
    command = [
        "sh",
        str(ROOT / "install.sh"),
        "--dir",
        str(deployment),
        "--version",
        VERSION,
        "--yes",
        "--profile",
        "desktop",
        "--bundle",
        str(bundle),
    ]
    completed = subprocess.run(
        command,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    commands = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    _assert_upgrade(commands, platform, deployment)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX installer")
def test_shell_installer_first_install_configures_names_and_recommends_a_profile(tmp_path):
    deployment = tmp_path / "new"
    bundle, _zip = _bundle(tmp_path)
    answers = tmp_path / "model.json"
    answers.write_text('{"protocol": "responses", "base_url": "http://model", "model": "m"}')
    log = tmp_path / "docker.jsonl"
    environment = {
        **os.environ,
        "DOCKER_ARCH": "arm64",
        # 3 GiB: the installer recommends headless, without asking under --yes.
        "DOCKER_MEMORY": str(3 * 1024**3),
        "DOCKER_RUNTIMES": '{"runc":{},"runsc":{"path":"runsc","runtimeArgs":["--overlay2=none"]}}',
        "DOCKER_LOG": str(log),
        "DEPLOY_DIRECTORY": str(deployment),
        "PATH": str(_fake_docker(tmp_path)) + os.pathsep + os.environ["PATH"],
    }
    completed = subprocess.run(
        [
            "sh",
            str(ROOT / "install.sh"),
            "--dir",
            str(deployment),
            "--version",
            VERSION,
            "--yes",
            "--answers",
            str(answers),
            "--character-name",
            "雪",
            "--owner-name",
            "小林",
            "--bundle",
            str(bundle),
        ],
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    commands = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    configure = next(command for command in commands if command[0] == "run")
    assert configure[-3:] == ["desktop-configure", "--deployment-root", "/deploy"]
    assert "-i" in configure and "-it" not in configure
    lines = (deployment / ".env").read_text(encoding="utf-8").splitlines()
    # The owner becomes the first Person and administrator; Yuki Desktop gets a code.
    pair = next(command for command in commands if "desktop-pair" in " ".join(command))
    assert pair[:3] == ["compose", "exec", "-T"] and "YUKI_OWNER_NAME=小林" in pair
    assert "superuser_request_id" in pair[-1]
    assert "Binding code: PAIR-小林" in completed.stdout
    assert "http://127.0.0.1:8080" in completed.stdout
    assert {
        "YUKI_OWNER_PERSON_ID=person-1",
        "LLM_MODEL=configured",
        "YUKI_CHARACTER_NAME=雪",
        "YUKI_SANDBOX_PROFILE=headless",
        # gVisor with --overlay2=none is registered: chosen once and recorded.
        "YUKI_SANDBOX_RUNTIME=runsc",
    } <= set(lines)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX installer")
def test_shell_installer_without_terminal_or_yes_refuses_before_writing(tmp_path):
    target = tmp_path / "new"
    completed = subprocess.run(
        ["sh", str(ROOT / "install.sh"), "--dir", str(target)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode != 0 and "--yes" in completed.stderr
    assert not target.exists()


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
        return json.dumps([metadata])

    monkeypatch.setattr(subprocess, "check_output", execute)
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs: commands.append(command))
    if mismatch is None:
        verify_image_identity(compose, "3.9.0", platform, "expected-commit")
    else:
        with pytest.raises(SmokeError):
            verify_image_identity(compose, "3.9.0", platform, "expected-commit")
    assert commands == [["docker", "image", "inspect", "--platform", platform, "image:3.9.0"]]
