"""The Compose Manager's own checks, profile programs and live limits (task book F1–F20).

Docker is replaced by a recorder that answers like the engine; the Manager code is real.
"""

import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from yuki.sandbox import persistent
from yuki.sandbox.environment_programs import MODE_PACKAGES
from yuki.sandbox.persistent import PersistentManager
from yuki.workspace.store import WorkspaceStore

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX environment runtime")
RUNNING = {
    "Id": "environment",
    "State": {"Running": True, "StartedAt": "start"},
    "HostConfig": {"RestartPolicy": {"Name": "no"}},
    "NetworkSettings": {"Networks": {"egress": {"IPAddress": "172.30.0.2"}}},
}


def manager_at(tmp_path, **options):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return PersistentManager(
        tmp_path / "manager",
        WorkspaceStore(tmp_path / "artifacts"),
        "image",
        "egress",
        "",
        home,
        **options,
    )


class Engine:
    """docker CLI answers: runtimes, the manifest, installs and updates."""

    def __init__(self, *, runtimes=None, manifest=(), install_code=0, update_code=0):
        self.runtimes = runtimes or {"runc": {"path": "runc"}}
        self.manifest = list(manifest)
        self.install_code, self.update_code = install_code, update_code
        self.calls = []

    async def command(self, *args, deadline_seconds=30):
        self.calls.append(args)
        if args[:2] == ("docker", "info"):
            return 0, json.dumps(self.runtimes).encode()
        if args[:2] == ("docker", "exec") and args[-1] == "/opt/yuki-runtime/manifest.json":
            return 0, json.dumps({"tools": self.manifest}).encode()
        if "/opt/yuki-runtime/environment_programs.py" in args:
            if self.install_code:
                return self.install_code, b"E: Unable to locate package chromium"
            mode = args[args.index("/opt/yuki-runtime/environment_programs.py") + 1]
            installed = {tool["name"] for tool in self.manifest}
            self.manifest += [
                {"name": name, "version": "1"}
                for name in MODE_PACKAGES[mode]
                if name not in installed
            ]
            return 0, b""
        if args[:2] == ("docker", "update"):
            return self.update_code, b"Cannot update: memory limit below current usage"
        return 0, b""


async def test_a_lost_manager_state_never_mints_a_token_over_a_live_environment(tmp_path):
    # F11: the environment and HOME survived, jobs.sqlite3 did not.
    manager = manager_at(tmp_path, testing=True)
    manager.inspect_container = AsyncMock(return_value=RUNNING)
    manager.command = AsyncMock(return_value=(0, b""))
    try:
        with pytest.raises(RuntimeError, match="manager_state_missing"):
            await manager.recover()
        assert manager.setting("execd_token") == ""
        manager.command.assert_not_awaited()
        assert not manager.ready
    finally:
        await manager.close()


@pytest.mark.parametrize(
    ("labels", "adopted"),
    [
        ({"io.yuki.sandbox": "persistent-v1", "io.yuki.sandbox.project": "yuki"}, True),
        # F12: another Compose project's environment on the same engine.
        ({"io.yuki.sandbox": "persistent-v1", "io.yuki.sandbox.project": "other"}, False),
        # F13: the retired host Manager's environment carries no project label.
        ({"io.yuki.sandbox": "persistent-v1"}, False),
        ({"unrelated": "container"}, False),
    ],
)
async def test_only_this_projects_environment_is_adopted(tmp_path, labels, adopted):
    manager = manager_at(tmp_path, testing=True, project="yuki")
    raw = json.dumps([{**RUNNING, "Config": {"Labels": labels}}]).encode()
    manager.command = AsyncMock(return_value=(0, raw))
    try:
        if adopted:
            assert (await manager.inspect_container())["Id"] == "environment"
        else:
            with pytest.raises(RuntimeError, match="environment_name_in_use"):
                await manager.inspect_container()
    finally:
        await manager.close()


@pytest.mark.parametrize(
    ("runtime", "runtimes", "error"),
    [
        ("runc", {"runc": {}}, None),
        ("runsc", {"runc": {}, "runsc": {"runtimeArgs": ["--overlay2=none"]}}, None),
        # F1: configured gVisor that this engine does not have; never a silent runc.
        ("runsc", {"runc": {}}, "gvisor_unavailable"),
        # F3: gVisor's own overlay keeps installs out of committed checkpoints.
        ("runsc", {"runc": {}, "runsc": {"runtimeArgs": []}}, "gvisor_overlay_unsupported"),
    ],
)
async def test_runtime_is_the_configured_one_or_a_named_failure(tmp_path, runtime, runtimes, error):
    manager = manager_at(tmp_path, runtime=runtime)
    manager.command = Engine(runtimes=runtimes).command
    try:
        if error is None:
            await manager.check_runtime()
        else:
            with pytest.raises(RuntimeError, match=error):
                await manager.check_runtime()
    finally:
        await manager.close()


async def test_a_larger_profile_installs_in_place_and_a_smaller_one_keeps_software(tmp_path):
    engine = Engine(manifest=[])
    manager = manager_at(tmp_path, browser_mode="headless")
    manager.command = engine.command
    try:
        await manager.apply_programs()
        installs = [call for call in engine.calls if "--user" in call]
        assert len(installs) == 1 and installs[0][-1] == "headless"
        assert manager.programs == {"missing": list(MODE_PACKAGES["headless"]), "installed": True}
        # F20: the shared -desktop image never makes headless report a desktop.
        assert manager.desktop_manifest["browser_mode"] == "headless"
        assert manager.desktop_manifest["desktop"] is False
        mode = manager.runtime_root / "computer" / "browser-mode"
        assert mode.read_text() == "headless"

        # F18: back to lite: nothing is removed or reinstalled; the mode follows.
        engine.calls.clear()
        manager.browser_mode = "none"
        await manager.apply_programs()
        assert not [call for call in engine.calls if "--user" in call]
        assert {tool["name"] for tool in manager.desktop_manifest["tools"]} >= set(
            MODE_PACKAGES["headless"]
        )
        assert manager.desktop_manifest["browser_mode"] == "none"
        assert mode.read_text() == "none"
    finally:
        await manager.close()


async def test_a_failed_install_reports_why_and_keeps_what_is_installed(tmp_path):
    # F17: the apt failure is reported; the capability is what is actually installed.
    engine = Engine(manifest=[{"name": "chromium", "version": "1"}], install_code=100)
    manager = manager_at(tmp_path, browser_mode="headed")
    manager.command = engine.command
    try:
        await manager.apply_programs()
        assert manager.programs["installed"] is False
        assert "Unable to locate package" in manager.programs["output"]
        assert manager.desktop_manifest["browser_mode"] == "none"
        assert manager.desktop_manifest["desktop"] is False
    finally:
        await manager.close()


async def test_limits_change_in_place_and_a_refusal_is_reported(tmp_path, monkeypatch):
    engine = Engine(update_code=1)
    manager = manager_at(tmp_path)
    manager.command = engine.command
    for option, value in {
        "memory_mib": "1024",
        "swap_mib": "256",
        "cpus": "1.5",
        "pids": "1024",
    }.items():
        manager.set_setting("resource_" + option, value)
    manager.inspect_container = AsyncMock(return_value=RUNNING)
    try:
        await manager.apply_limits(
            {**RUNNING, "HostConfig": {"Memory": 512 * 1024 * 1024, "NanoCpus": 10**9}}
        )
        (update,) = [call for call in engine.calls if call[:2] == ("docker", "update")]
        assert update[update.index("--memory") + 1] == "1024m"
        assert update[update.index("--memory-swap") + 1] == "1280m"
        assert update[update.index("--cpus") + 1] == "1.5"
        assert update[update.index("--pids-limit") + 1] == "1024"
        # F18: Docker's own refusal, as it said it.
        assert "below current usage" in manager.limits_error
        # Already applied: no second update.
        engine.calls.clear()
        manager.limits_error = ""
        applied = {
            "Memory": 1024 * 1024 * 1024,
            "MemorySwap": 1280 * 1024 * 1024,
            "NanoCpus": 1_500_000_000,
            "PidsLimit": 1024,
            "CpuShares": 1024,
        }
        await manager.apply_limits({**RUNNING, "HostConfig": applied})
        assert not engine.calls and not manager.limits_error
    finally:
        await manager.close()


async def test_creation_only_limits_are_reported_not_applied(tmp_path, monkeypatch):
    # F19: /tmp and /dev/shm differ from configuration only until an explicit recreate.
    manager = manager_at(tmp_path, testing=True)
    manager.set_setting("resource_tmp_mib", "512")
    manager.set_setting("resource_shm_mib", "256")
    manager.observed_container = {
        "tmpfs": "rw,nosuid,nodev,size=128m,mode=1777",
        "shm_bytes": 256 * 1024 * 1024,
    }
    monkeypatch.setattr(PersistentManager, "available_memory", staticmethod(lambda: 0))
    monkeypatch.setattr(
        persistent.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(total=1, used=0, free=1),
    )
    try:
        status = manager.environment_status()
        assert status["recreate_required"] == ["tmp_mib"]
        assert status["storage"]["shared_disk"] is True
    finally:
        await manager.close()
