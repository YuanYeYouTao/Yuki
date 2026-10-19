"""CLI and installer failures, using a file-backed Docker transport test double."""

from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import json
import os
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
INSTALLER = ROOT / "deploy/sandbox/install-yuki-client.sh"


@pytest.mark.parametrize("arguments", [["--version"], ["client", "--help"], ["acp", "--help"]])
def test_environment_cli_does_not_require_bot_persona_or_database(tmp_path, arguments):
    result = subprocess.run(
        [str(Path(sys.executable).with_name("yuki")), *arguments],
        cwd=tmp_path,
        env={**os.environ, "BOT_PERSONA_FILE": str(tmp_path / "absent.md")},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    if arguments == ["--version"]:
        assert result.stdout.strip() == f"yuki {importlib.metadata.version('yuki')}"


FAKE_DOCKER = r"""#!/usr/bin/env python3
import json, os, pathlib, shutil, subprocess, sys
root = pathlib.Path(os.environ['DOCKER_TEST_ROOT'])
args = sys.argv[1:]
with (root / 'calls.jsonl').open('a') as f: f.write(json.dumps(args) + '\n')
def mapped(path): return root / 'original' / path.removeprefix('/')
if args[0] == 'inspect':
    if '.Image' in args[2]: print('original-image')
    else: print(os.environ.get('DOCKER_LABEL', 'persistent-v1'))
elif args[:2] == ['image', 'inspect']:
    selected = os.environ.get('CLIENT_PLATFORM', 'linux/arm64')
    print('linux/arm64' if args[-1] == 'original-image' else selected)
elif args[0] == 'create':
    (root / 'holder').touch(); print('fixture-holder')
elif args[0] == 'rm':
    (root / 'holder').unlink()
elif args[0] == 'cp':
    source, target = args[1:]
    if source.startswith('fixture-holder:'):
        shutil.copytree(root / 'image' / 'client', target)
    elif source.startswith('original:'):
        try: shutil.copyfile(mapped(source.split(':', 1)[1]), target)
        except FileNotFoundError: sys.exit(1)
    else:
        destination = mapped(target.split(':', 1)[1])
        if os.environ.get('FAIL_COPY') and not (root / 'failed-copy').exists():
            destination.mkdir(parents=True, exist_ok=True)
            (destination / 'partial').touch()
            (root / 'failed-copy').touch()
            sys.exit(42)
        shutil.copytree(source.removesuffix('/.'), destination, dirs_exist_ok=True)
elif args[0] == 'exec':
    assert args[1:4] == ['--user', '0:0', 'original']
    command = args[4:]
    if command[0] == 'test': sys.exit(0 if mapped(command[-1]).exists() else 1)
    if command[0] == 'chown': sys.exit(0)
    if command[0].endswith('/python'):
        command[0] = os.environ['REAL_PYTHON']
        command = [str(mapped(value)) if value.startswith('/opt/yuki-client/') else value
                   for value in command]
        for location in ['/opt/yuki-client', '/opt']:
            command[-1] = command[-1].replace('Path(' + json.dumps(location) + ')',
                'Path(' + json.dumps(str(mapped(location))) + ')')
        manifest = '/opt/yuki-client/INSTALLATION.json'
        command[-1] = command[-1].replace(manifest, str(mapped(manifest)))
    else:
        assert command[0] == '/opt/yuki-client/bin/yuki'
        command[0] = str(pathlib.Path(os.environ['REAL_PYTHON']).with_name('yuki'))
    sys.exit(subprocess.run(command).returncode)
else: raise AssertionError(args)
"""


def install_fixture(tmp_path):
    directory = tmp_path / "bin"
    directory.mkdir()
    docker = directory / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    image = tmp_path / "image/client"
    image.mkdir(parents=True)
    lock = tmp_path / "uv.lock"
    lock.write_bytes(b"exact lock bytes")
    manifest = runpy.run_path(str(ROOT / "deploy/sandbox/client-manifest.py"))["manifest"](
        lock, "source-commit"
    )
    assert manifest["lock_sha256"] == hashlib.sha256(lock.read_bytes()).hexdigest()
    (image / "INSTALLATION.json").write_text(json.dumps(manifest, sort_keys=True) + "\n")
    (image / "client-code").write_text("selected image code")
    shutil.copyfile(ROOT / "deploy/sandbox/client-manifest.py", image / "verify-installation.py")
    home = tmp_path / "original/home/yuki"
    home.mkdir(parents=True)
    (home / "original.txt").write_text("keep original home")
    env = {
        **os.environ,
        "PATH": str(directory) + os.pathsep + os.environ["PATH"],
        "DOCKER_TEST_ROOT": str(tmp_path),
        "REAL_PYTHON": sys.executable,
    }

    def run(*options, **settings):
        return subprocess.run(
            [
                "sh",
                str(INSTALLER),
                "--container",
                "original",
                "--image",
                "selected-image",
                *options,
            ],
            env={**env, **settings},
            capture_output=True,
            text=True,
        )

    return run, home


def test_installer_preserves_original_home_and_recovers_an_interrupted_copy(tmp_path):
    run, home = install_fixture(tmp_path)
    assert run(FAIL_COPY="1").returncode != 0
    assert not (tmp_path / "holder").exists()
    assert run().returncode != 0
    recovered = run("--replace")
    assert recovered.returncode == 0, recovered.stderr
    assert (tmp_path / "original/opt/yuki-client/client-code").read_text() == "selected image code"
    assert (home / "original.txt").read_text() == "keep original home"
    assert run().returncode == 0
    calls = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert not any(call[0] in {"start", "stop", "restart", "pull"} for call in calls)
    assert all(call[-1] == "fixture-holder" for call in calls if call[0] == "rm")


@pytest.mark.parametrize(
    "settings", [{"DOCKER_LABEL": "other"}, {"CLIENT_PLATFORM": "linux/amd64"}]
)
def test_installer_refuses_an_unrelated_container_or_incompatible_binary_platform(
    tmp_path, settings
):
    run, home = install_fixture(tmp_path)
    assert run(**settings).returncode != 0
    assert not (tmp_path / "holder").exists()
    assert not (tmp_path / "original/opt").exists()
    assert (home / "original.txt").read_text() == "keep original home"


def test_installer_does_not_follow_a_client_symlink_into_original_home(tmp_path):
    run, home = install_fixture(tmp_path)
    target = tmp_path / "original/opt/yuki-client"
    target.parent.mkdir()
    target.symlink_to(home, target_is_directory=True)
    assert run("--replace").returncode != 0
    assert not (tmp_path / "holder").exists()
    assert target.is_symlink()
    assert (home / "original.txt").read_text() == "keep original home"
    assert not (home / "client-code").exists()


def test_installation_verifier_checks_actual_recorded_bytes_and_missing_files(tmp_path):
    metadata = tmp_path / "fixture-1.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text("Name: fixture\nVersion: 1.0\n")
    code = tmp_path / "original.py"
    content = b"original package bytes"
    code.write_bytes(content)
    digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode().rstrip("=")
    (metadata / "RECORD").write_text(f"original.py,sha256={digest},{len(content)}\n")
    package = importlib.metadata.Distribution.at(metadata)
    verify = runpy.run_path(str(ROOT / "deploy/sandbox/client-manifest.py"))["verify_records"]
    verify([package])
    code.write_bytes(b"partial or stale copy")
    with pytest.raises(ValueError, match="installed_client_file_changed"):
        verify([package])
    code.unlink()
    with pytest.raises(FileNotFoundError):
        verify([package])


def test_installer_does_not_claim_success_when_a_declared_dependency_is_missing(tmp_path):
    run, home = install_fixture(tmp_path)
    source = tmp_path / "image/client/INSTALLATION.json"
    value = json.loads(source.read_text())
    value["packages"].append(
        {
            "name": "yuki-missing-installer-test-dependency",
            "version": "1",
            "record_sha256": "absent",
        }
    )
    source.write_text(json.dumps(value, sort_keys=True) + "\n")
    result = run()
    assert result.returncode != 0
    assert "PackageNotFoundError" in result.stderr
    assert (home / "original.txt").read_text() == "keep original home"
