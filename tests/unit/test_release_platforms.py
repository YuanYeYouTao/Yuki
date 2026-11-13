"""Installers select the Docker host platform; release smoke checks exact images."""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

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
# A code as the server issues it: token_urlsafe(24), 32 characters.
CODE = "AbCdEfGhIjKlMnOpQrStUvWxYz012_-9"
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
    # Retired .env platform credentials reach webui-config before the new Bot starts.
    # It runs as root: start.sh hands webui-config to the runtime UID, whatever wrote it.
    migrate = next(command for command in commands if "platforms-migrate" in command)
    assert migrate[:2] == ["compose", "run"] and "--no-deps" in migrate
    assert migrate[migrate.index("--user") + 1] == "0:0"
    assert migrate[-3:] == ["platforms-migrate", "--deployment-root", "/deploy"]
    assert f"{deployment}:/deploy" in migrate
    assert commands.index(migrate) < commands.index(up)
    # Ready is the Bot's own API and its sandbox, before the owner is paired.
    probe = next(command for command in commands if "/livez" in command[-1])
    assert probe[:4] == ["compose", "exec", "-T", "bot"]
    check = next(command for command in commands if command[-1] == "environment-check")
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


def _parse_pairing(text):
    """Yuki Desktop's parsePairing (Yuki-Desktop src/shared/pairing.ts), as written there."""
    match = re.search(r"yuki://pair\?[^\s<>\"'`\u3000-\uffff]+", text, re.IGNORECASE)
    if match:
        query = parse_qs(urlsplit(match[0]).query)
        code = query.get("code", [""])[0]
        if not re.fullmatch(r"[A-Za-z0-9_-]{32}", code):
            return None
        return {"code": code, "server": query.get("server", [None])[0]}
    code = next((token for token in re.split(r"[^A-Za-z0-9_-]+", text) if len(token) == 32), None)
    return {"code": code} if code else None


def _container(tmp_path):
    """The Bot container's commands for shell steps: the real Python, a fake ``yuki``."""
    directory = tmp_path / "container"
    directory.mkdir()
    python = directory / "python"
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    python.chmod(0o755)
    yuki = directory / "yuki"
    yuki.write_text(
        f"""#!{sys.executable}
import json, os, sys
arguments = sys.argv[1:]
with open(os.environ['DOCKER_LOG'], 'a') as writer:
    writer.write(json.dumps(['container', 'yuki', *arguments]) + '\\n')
if arguments[:2] == ['setup', 'desktop-pair']:
    request = json.load(sys.stdin)
    assert request['display_name'] == os.environ['YUKI_OWNER_NAME']
    print(json.dumps({{'person_id': 'person-1', 'code_id': 'c', 'code': {CODE!r}}}))
elif arguments[:3] == ['admin', 'client', 'pair']:
    assert os.environ['YUKI_CONTROL_CREDENTIAL'] == os.environ['YUKI_CONTROL_OPERATOR_TOKEN']
    print(json.dumps({{'code_id': 'c', 'purpose': 'chat', 'code': {CODE!r}}}, indent=2))
else:
    sys.exit(2)
""",
        encoding="utf-8",
    )
    yuki.chmod(0o755)
    return directory


def _fake_docker(tmp_path):
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    container = _container(tmp_path)
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
# A shell step in the Bot runs here, with the deployment's .env as its environment.
if arguments[:2] == ['compose', 'exec'] and 'bot' in arguments:
    command = arguments[arguments.index('bot') + 1:]
    if command[0] == 'sh':
        import subprocess
        environment = dict(os.environ)
        with open('.env') as values:
            environment.update(
                line.rstrip('\\n').split('=', 1) for line in values if '=' in line
            )
        options = arguments[2:arguments.index('bot')]
        environment.update(
            options[index + 1].split('=', 1)
            for index, option in enumerate(options) if option == '-e'
        )
        environment['PATH'] = {str(container)!r} + os.pathsep + environment['PATH']
        sys.exit(subprocess.run(command, env=environment).returncode)
if arguments[0] == 'run' and 'desktop-configure' in arguments:
    sys.stdin.read()
    with open(os.path.join(os.environ['DEPLOY_DIRECTORY'], '.env'), 'a') as env:
        env.write('LLM_MODEL=configured\\n' + os.environ.get('CONFIGURED_ENV', ''))
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


def _assert_pairing_help(stdout, public_url):
    """What a person copies into Yuki Desktop: the full link only with a public address."""
    shown = stdout[stdout.index("Connect Yuki Desktop") :]
    assert _parse_pairing(shown) == (
        {"code": CODE, "server": public_url.rstrip("/")} if public_url else {"code": CODE}
    )
    assert "24 hours" in shown
    if not public_url:
        # No link another computer cannot open; SSH from Yuki Desktop reaches the server.
        assert "yuki://" not in shown and "127.0.0.1" not in shown
        assert "通过 SSH 连接" in shown


# A public address as written in .env (the trailing slash is not part of it), or none.
PUBLIC_URLS = ("", "https://yuki.example.com/base/")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX installer")
@pytest.mark.parametrize("public_url", PUBLIC_URLS)
def test_shell_installer_first_install_configures_names_and_recommends_a_profile(
    tmp_path, public_url
):
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
        "CONFIGURED_ENV": f"CLIENT_PUBLIC_URL={public_url}\n",
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
    _assert_pairing_help(completed.stdout, public_url)
    assert {
        "YUKI_OWNER_PERSON_ID=person-1",
        "LLM_MODEL=configured",
        "YUKI_CHARACTER_NAME=雪",
        "YUKI_SANDBOX_PROFILE=headless",
        # gVisor with --overlay2=none is registered: chosen once and recorded.
        "YUKI_SANDBOX_RUNTIME=runsc",
    } <= set(lines)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX installer")
@pytest.mark.parametrize("public_url", PUBLIC_URLS)
def test_pair_script_prints_a_new_code_for_the_recorded_owner(tmp_path, public_url):
    deployment = tmp_path / "deployment"
    (deployment / "deploy").mkdir(parents=True)
    shutil.copyfile(ROOT / "deploy/pair.sh", deployment / "deploy/pair.sh")
    (deployment / ".env").write_text(
        "YUKI_OWNER_PERSON_ID=person-1\nYUKI_CONTROL_OPERATOR_TOKEN=operator\n"
        f"CLIENT_PUBLIC_URL={public_url}\n",
        encoding="utf-8",
    )
    log = tmp_path / "docker.jsonl"
    completed = subprocess.run(
        ["sh", str(deployment / "deploy/pair.sh")],
        cwd=tmp_path,
        env={
            **os.environ,
            "DOCKER_LOG": str(log),
            "PATH": str(_fake_docker(tmp_path)) + os.pathsep + os.environ["PATH"],
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    commands = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert ["container", "yuki", "admin", "client", "pair", "--person", "person-1"] in commands
    _assert_pairing_help(completed.stdout, public_url)


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
