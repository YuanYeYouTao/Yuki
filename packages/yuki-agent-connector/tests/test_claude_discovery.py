import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from yuki_agent_connector import discovery
from yuki_agent_connector.config import Instance


def executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"native fixture, never launched")
    path.chmod(0o755)
    return path


def current_package(package: Path, relative: str, mapping: bool) -> Path:
    target = executable(package / relative)
    (package / "package.json").write_text(
        json.dumps(
            {
                "name": "@anthropic-ai/claude-code",
                "bin": {"claude": relative} if mapping else relative,
            }
        ),
        encoding="utf-8",
    )
    return target


@pytest.mark.parametrize("platform", ["win32", "linux", "darwin"])
@pytest.mark.parametrize("mapping", [False, True])
def test_current_npm_bin_is_discovered_without_launching_or_listing_old_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str, mapping: bool
) -> None:
    monkeypatch.setattr(discovery.sys, "platform", platform)
    monkeypatch.setattr(discovery.Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(
        discovery.subprocess, "run", lambda *_a, **_k: pytest.fail("No process during discovery")
    )
    prefix = tmp_path / "npm prefix 資料"
    bin_path = prefix if platform == "win32" else prefix / "bin"
    package = (
        prefix / "node_modules/@anthropic-ai/claude-code"
        if platform == "win32"
        else prefix / "lib/node_modules/@anthropic-ai/claude-code"
    )
    relative = "bin/claude.exe" if platform == "win32" else "bin/claude"
    target = current_package(package, relative, mapping)
    executable(package.parent / ".claude-code-old" / relative)
    executable(package / "old-version" / relative)
    # npm's wrapper is not the selected native entrypoint on Windows.
    if platform == "win32":
        executable(bin_path / "claude.cmd")
    monkeypatch.setenv("PATH", str(bin_path))

    found = discovery.candidates("claude")

    assert len(found) == 1
    assert found[0]["command"] == str(target)
    assert found[0]["source"] == f"claude-npm:{package}"
    assert found[0]["driver"] == "claude"


def test_npm_upgrade_refreshes_original_package_bin_and_retains_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(discovery.sys, "platform", "win32")
    monkeypatch.setattr(discovery.Path, "home", lambda: tmp_path / "home")
    prefix = tmp_path / "npm"
    package = prefix / "node_modules/@anthropic-ai/claude-code"
    old = current_package(package, "bin/claude.exe", True)
    monkeypatch.setenv("PATH", str(prefix))
    candidate = discovery.candidates("claude")[0]
    original = Instance(
        "original",
        "claude",
        "Original",
        str(old),
        args=["--original"],
        cwd="original-project",
        env={"CLAUDE_CONFIG_DIR": "original-profile"},
        socket="original-socket",
        endpoint="ws://original-owner",
        installation=candidate["source"],
    )
    current = current_package(package, "updated/claude.exe", False)
    upgraded = discovery.candidates("claude")
    assert len(upgraded) == 1 and old.is_file()
    assert upgraded[0]["id"] == candidate["id"]
    assert upgraded[0]["command"] == str(current)
    monkeypatch.setenv("PATH", "")
    assert discovery.refresh(original) == replace(original, command=str(current))

    (package / "package.json").unlink()
    with pytest.raises(FileNotFoundError):
        discovery.refresh(original)
    manual = replace(original, installation=None)
    assert discovery.refresh(manual) is manual


def test_windows_javascript_bin_does_not_become_a_native_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(discovery.sys, "platform", "win32")
    monkeypatch.setattr(discovery.Path, "home", lambda: tmp_path / "home")
    monkeypatch.setenv("PATH", str(tmp_path))
    current_package(tmp_path / "node_modules/@anthropic-ai/claude-code", "cli.js", True)
    assert discovery.candidates("claude") == []


def test_existing_posix_path_symlink_keeps_source_and_original_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix = tmp_path / "npm"
    bin_path = prefix / "bin"
    package = prefix / "lib/node_modules/@anthropic-ai/claude-code"
    target = current_package(package, "bin/claude", True)
    bin_path.mkdir()
    command = bin_path / "claude"
    try:
        command.symlink_to(target)
    except OSError as error:
        if os.name == "nt" and error.winerror == 1314:
            pytest.skip("Windows user cannot create symbolic links")
        raise
    monkeypatch.setattr(discovery.sys, "platform", "linux")
    monkeypatch.setattr(discovery.Path, "home", lambda: tmp_path / "home")
    monkeypatch.setenv("PATH", str(bin_path))
    original = Instance(
        "original", "claude", "Original", str(command), installation=f"path:{command}"
    )

    found = discovery.candidates("claude")

    assert len(found) == 1
    assert found[0]["source"] == original.installation
    assert found[0]["command"] == str(command)
    assert discovery.refresh(original) == original
