import json
from dataclasses import replace
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any

import pytest

from yuki_agent_connector import discovery
from yuki_agent_connector.config import Instance


@pytest.fixture
def windows_installs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(discovery.sys, "platform", "win32")
    monkeypatch.setattr(discovery.subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "native-home"))

    def appx_only(args: list[str], **kwargs: Any) -> CompletedProcess[str]:
        assert args[0] == "powershell", "Discovery must not execute native agents or npm scripts"
        return CompletedProcess(args, 0, "[]", "")

    monkeypatch.setattr(discovery.subprocess, "run", appx_only)
    return tmp_path


def native_file(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fixture executable, never launched")
    path.chmod(0o755)
    return path


def npm_install(prefix: Path, layout: str, target: str) -> tuple[Path, Path]:
    package = prefix / "node_modules/@openai/codex"
    package.mkdir(parents=True, exist_ok=True)
    (package / "package.json").write_text(
        json.dumps({"name": "@openai/codex", "bin": {"codex": "bin/codex.js"}})
    )
    native_file(prefix / "codex.cmd")
    dependency = "codex-win32-arm64" if target.startswith("aarch64") else "codex-win32-x64"
    if layout == "vendor":
        vendor = package / "vendor"
    else:
        modules = package / "node_modules" if layout == "nested" else prefix / "node_modules"
        platform_package = modules / "@openai" / dependency
        platform_package.mkdir(parents=True, exist_ok=True)
        (platform_package / "package.json").write_text(json.dumps({"os": ["win32"]}))
        vendor = platform_package / "vendor"
    relative = "codex/codex.exe" if layout == "vendor" else "bin/codex.exe"
    return package, native_file(vendor / target / relative)


def daemon_install(home: Path, entrypoint: str = "bin/codex.exe") -> tuple[Path, Path]:
    current = home / "packages/app-server-daemon/current"
    current.mkdir(parents=True, exist_ok=True)
    manifest = current / "codex-package.json"
    manifest.write_text(
        json.dumps({"layoutVersion": 1, "version": "fixture", "entrypoint": entrypoint})
    )
    return manifest, native_file(current / entrypoint)


@pytest.mark.parametrize("layout", ["nested", "hoisted", "vendor"])
@pytest.mark.parametrize("target", ["x86_64-pc-windows-msvc", "aarch64-pc-windows-msvc"])
def test_windows_npm_shim_resolves_its_installed_native_package_without_launching(
    windows_installs: Path,
    monkeypatch: pytest.MonkeyPatch,
    layout: str,
    target: str,
) -> None:
    prefix = windows_installs / "npm prefix 資料"
    package, executable = npm_install(prefix, layout, target)
    monkeypatch.setenv("PATH", str(prefix))
    # npm upgrade leftovers are not additional active installations.
    native_file(prefix / "node_modules/@openai/.codex-old/vendor" / target / "bin/codex.exe")

    found = discovery.candidates()

    assert len(found) == 1
    candidate = found[0]
    assert candidate["command"] == str(executable)
    assert candidate["source"] == f"npm:{target}:{package}"
    assert candidate["driver"] == "codex"


def test_npm_source_survives_upgrade_without_changing_original_owner(
    windows_installs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix = windows_installs / "npm"
    target = "x86_64-pc-windows-msvc"
    package, old_executable = npm_install(prefix, "nested", target)
    monkeypatch.setenv("PATH", str(prefix))
    original_candidate = discovery.candidates()[0]
    original = Instance(
        "original",
        "codex",
        "Original",
        str(old_executable),
        args=["--original"],
        cwd="original-project",
        env={"CODEX_HOME": "original-profile"},
        socket="original-socket",
        endpoint="ws://original-owner",
        installation=original_candidate["source"],
    )
    (package / "node_modules/@openai/codex-win32-x64/package.json").unlink()
    _, current = npm_install(prefix, "hoisted", target)
    after_upgrade = discovery.candidates()[0]

    assert after_upgrade["id"] == original_candidate["id"]
    assert after_upgrade["command"] == str(current)
    # An already selected installation does not change with the caller's PATH/profile.
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("CODEX_HOME", str(windows_installs / "other-profile"))
    assert discovery.refresh(original) == replace(original, command=str(current))

    (package / "package.json").unlink()
    with pytest.raises(FileNotFoundError):
        discovery.refresh(original)


def test_daemon_current_manifest_owns_executable_and_upgrade_identity(
    windows_installs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = windows_installs / "native-home"
    manifest, executable = daemon_install(home)
    native_file(home / "packages/app-server-daemon/releases/old/bin/codex.exe")
    original_candidate = discovery.candidates()[0]
    original = Instance(
        "original",
        "codex",
        "Original",
        str(executable),
        env={"CODEX_HOME": str(home)},
        socket="original-socket",
        installation=original_candidate["source"],
    )
    _, current = daemon_install(home, "updated-bin/codex.exe")
    after_upgrade = discovery.candidates()[0]

    assert original_candidate["source"] == f"codex-package:{manifest}"
    assert original_candidate["command"] == str(executable)
    assert after_upgrade["id"] == original_candidate["id"]
    monkeypatch.setenv("CODEX_HOME", str(windows_installs / "different-profile"))
    assert discovery.refresh(original) == replace(original, command=str(current))

    manifest.unlink()
    with pytest.raises(FileNotFoundError):
        discovery.refresh(original)
    monkeypatch.setenv("CODEX_HOME", str(home))
    assert discovery.candidates() == []


@pytest.mark.parametrize("platform", ["win32", "linux", "darwin"])
def test_daemon_package_uses_default_or_explicit_codex_home(
    windows_installs: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    monkeypatch.setattr(discovery.sys, "platform", platform)
    user_home = windows_installs / "user"
    monkeypatch.setattr(discovery.Path, "home", lambda: user_home)
    monkeypatch.delenv("CODEX_HOME")
    entrypoint = "bin/codex.exe" if platform == "win32" else "bin/codex"
    _, default = daemon_install(user_home / ".codex", entrypoint)
    _, selected = daemon_install(windows_installs / "selected-profile", entrypoint)
    assert discovery.candidates()[0]["command"] == str(default)

    monkeypatch.chdir(windows_installs)
    monkeypatch.setenv("CODEX_HOME", "selected-profile")
    assert discovery.candidates()[0]["command"] == str(selected)


def test_manual_path_does_not_follow_another_available_installation(
    windows_installs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon_install(windows_installs / "native-home")
    manual = Instance("manual", "codex", "Manual", str(windows_installs / "missing.exe"))
    monkeypatch.setattr(
        discovery,
        "candidates",
        lambda: pytest.fail("Manual commands do not select another install"),
    )
    assert discovery.refresh(manual) is manual


def test_appx_npm_and_daemon_are_distinct_available_sources(
    windows_installs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = windows_installs / "WindowsApps/Codex-version/app/resources/codex.exe"
    native_file(store)
    package, npm = npm_install(windows_installs / "npm", "nested", "x86_64-pc-windows-msvc")
    manifest, daemon = daemon_install(windows_installs / "native-home")
    monkeypatch.setenv("PATH", str(windows_installs / "npm"))

    def appx_only(args: list[str], **kwargs: Any) -> CompletedProcess[str]:
        assert args[0] == "powershell"
        return CompletedProcess(
            args, 0, json.dumps({"Name": "OpenAI.Codex", "InstallLocation": str(store.parents[2])})
        )

    monkeypatch.setattr(discovery.subprocess, "run", appx_only)
    found = discovery.candidates()
    assert {item["command"] for item in found} == {str(store), str(npm), str(daemon)}
    assert {item["source"] for item in found} == {
        "appx:OpenAI.Codex:app/resources/codex.exe",
        f"npm:x86_64-pc-windows-msvc:{package}",
        f"codex-package:{manifest}",
    }
    assert len({item["id"] for item in found}) == 3
