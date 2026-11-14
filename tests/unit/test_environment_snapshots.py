"""Whole-computer snapshots, rotation, restore and recovery (task book §11 S1–S19).

The cases are the task book's numbered failure list, written before the Manager code.
Docker and rsync are the modelled engine of ``tests.support.fake_docker``; the Manager's
own records, decisions and recover() are real. The real engine path (runc and runsc)
is ``scripts/sandbox_snapshot_e2e.py``.
"""

import asyncio
import json
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from tests.support.fake_docker import Crash, FakeDocker

from yuki.sandbox import persistent
from yuki.sandbox.environment_snapshots import home_changed
from yuki.sandbox.persistent import PersistentManager
from yuki.workspace.store import WorkspaceStore

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX environment runtime")


@pytest.fixture(autouse=True)
def no_execd(monkeypatch):
    monkeypatch.setattr(
        persistent, "Execd", lambda *_: SimpleNamespace(request=AsyncMock(return_value={}))
    )


def manager_at(tmp_path, docker, **options):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    manager = PersistentManager(
        tmp_path / "manager",
        WorkspaceStore(tmp_path / "artifacts"),
        "base",
        "egress",
        "",
        home,
        testing=True,
        snapshots=tmp_path / "snapshots",
        **options,
    )
    manager.command = docker.command
    manager.available_memory = lambda: 0
    return manager


async def started(tmp_path, docker=None, **options):
    docker = docker or FakeDocker(running=False)
    manager = manager_at(tmp_path, docker, **options)
    await manager.recover()
    return manager, docker


async def snapshot(manager, key=None, source="yuki"):
    key = key or str(uuid4())
    accepted = manager.snapshots.request_snapshot(key, source, {"request_id": key}, None)
    assert "snapshot" in accepted, accepted
    await manager.snapshots.step()
    return manager.snapshots.find(key)["snapshot"]


async def restore(manager, snapshot_id, key=None):
    key = key or str(uuid4())
    accepted = manager.snapshots.request_restore(key, snapshot_id, {"request_id": key})
    assert "restore" in accepted, accepted
    await manager.snapshots.step()
    return manager.snapshots.find(key)["restore"]


async def test_s1_same_request_answers_the_original_snapshot_and_restore(tmp_path):
    manager, docker = await started(tmp_path)
    try:
        first = await snapshot(manager, "same-key")
        again = manager.snapshots.request_snapshot("same-key", "yuki", {}, None)
        assert again["snapshot"]["id"] == first["id"] and first["state"] == "ready"
        assert len(docker.snapshot_tags()) == 1
        done = await restore(manager, first["id"], "restore-key")
        generation = manager.container_id
        assert done["state"] == "done"
        repeated = manager.snapshots.request_restore("restore-key", first["id"], {})
        await manager.snapshots.step()
        assert repeated["restore"]["state"] == "done" and manager.container_id == generation
        # A restore key is not a second snapshot request, and a different target conflicts.
        assert manager.snapshots.request_snapshot("restore-key", "yuki", {}, None) == {
            "error": "idempotency_conflict"
        }
        assert manager.snapshots.request_restore("restore-key", str(uuid4()), {}) == {
            "error": "idempotency_conflict"
        }
    finally:
        await manager.close()


async def test_s2_one_snapshot_or_restore_at_a_time(tmp_path):
    manager, _ = await started(tmp_path)
    try:
        ready = await snapshot(manager)
        manager.snapshots.request_snapshot("first", "yuki", {}, None)
        assert manager.snapshots.request_snapshot("second", "person", {}, None) == {
            "error": "snapshot_in_progress",
            "request_id": "first",
        }
        assert manager.snapshots.request_restore("restore", ready["id"], {}) == {
            "error": "snapshot_in_progress",
            "request_id": "first",
        }
        await manager.snapshots.step()
        manager.snapshots.request_restore("restore", ready["id"], {})
        assert manager.snapshots.request_snapshot("third", "yuki", {}, None) == {
            "error": "restore_in_progress",
            "request_id": "restore",
        }
    finally:
        await manager.close()


async def test_s4_manager_ending_during_the_pause_is_finished_on_recovery(tmp_path):
    manager, docker = await started(tmp_path)
    kept = await snapshot(manager)
    docker.crash = ("rsync",)
    manager.snapshots.request_snapshot("interrupted", "yuki", {}, None)
    with pytest.raises(Crash):
        await manager.snapshots.step()
    assert docker.container["Paused"]
    await manager.close()
    docker.crash = None
    again = manager_at(tmp_path, docker)
    try:
        await again.recover()
        assert not docker.container["Paused"] and again.ready
        failed = again.snapshots.find("interrupted")["snapshot"]
        assert failed["state"] == "failed" and failed["error"] == "interrupted"
        assert not list((tmp_path / "snapshots").glob("*.partial"))
        assert docker.snapshot_tags() == [kept["image"]]
        assert again.snapshots.find(kept["request_id"])["snapshot"]["state"] == "ready"
    finally:
        await again.close()


async def test_s5_restore_interrupted_by_the_manager_converges_on_recovery(tmp_path):
    manager, docker = await started(tmp_path)
    (manager.home / "kept.txt").write_text("at snapshot")
    target = await snapshot(manager)
    (manager.home / "kept.txt").write_text("after snapshot")
    (manager.home / "later.txt").write_text("new")
    docker.crash = ("rsync",)
    manager.snapshots.request_restore("restore", target["id"], {})
    with pytest.raises(Crash):
        await manager.snapshots.step()
    # The environment is gone and HOME is half way: nothing may start in it.
    assert docker.container is None
    await manager.close()
    docker.crash = None
    again = manager_at(tmp_path, docker)
    try:
        await again.recover()
        assert (again.home / "kept.txt").read_text() == "at snapshot"
        assert not (again.home / "later.txt").exists()
        assert docker.container["image"] == target["image"]
        done = again.snapshots.find("restore")["restore"]
        assert done["state"] == "done" and done["generation"] == again.container_id
    finally:
        await again.close()


async def test_s6_s7_rotation_keeps_three_and_defers_the_image_in_use(tmp_path):
    manager, docker = await started(tmp_path, snapshot_keep=3)
    try:
        first = await snapshot(manager)
        # The container is recreated from the first snapshot, so its tag stays in use.
        await restore(manager, first["id"])
        assert docker.container["image"] == first["image"]
        later = [await snapshot(manager) for _ in range(3)]
        assert [row["id"] for row in manager.snapshots.ready()] == [
            row["id"] for row in reversed(later)
        ]
        rotated = manager.snapshots.find(first["request_id"])["snapshot"]
        assert rotated["state"] == "rotated"
        assert later[-1]["rotated"] == [first["id"]]
        assert not (tmp_path / "snapshots" / first["id"]).exists()
        assert first["image"] in docker.snapshot_tags()  # S7: still the container's base
        # Next recreation: the tag is removed once nothing uses it.
        await restore(manager, later[0]["id"])
        assert first["image"] not in docker.snapshot_tags()
        assert len(docker.snapshot_tags()) == 3
        # S6 with no image in use: the oldest tag and directory go at once.
        newest = await snapshot(manager)
        assert later[0]["image"] in docker.snapshot_tags()  # base of the current container
        assert later[1]["image"] in docker.snapshot_tags()
        assert newest["rotated"] == [later[0]["id"]]
        assert len(manager.snapshots.ready()) == 3
    finally:
        await manager.close()


async def test_s8_a_failed_snapshot_removes_its_parts_and_no_older_one(tmp_path):
    manager, docker = await started(tmp_path, snapshot_keep=1)
    try:
        kept = await snapshot(manager)
        docker.fail[("docker", "commit")] = (1, b"write /var/lib/docker: no space left on device")
        failed = await snapshot(manager)
        assert failed["state"] == "failed" and "no space left on device" in failed["error"]
        assert not docker.container["Paused"]
        assert docker.snapshot_tags() == [kept["image"]]
        assert manager.snapshots.find(kept["request_id"])["snapshot"]["state"] == "ready"
        assert manager.setting("checkpoint_image") == kept["image"]
        del docker.fail[("docker", "commit")]
        docker.fail[("rsync",)] = (11, b"rsync: write failed: No space left on device (28)")
        failed = await snapshot(manager)
        assert failed["state"] == "failed" and "No space left" in failed["error"]
        assert docker.snapshot_tags() == [kept["image"]]
        assert sorted(path.name for path in (tmp_path / "snapshots").iterdir()) == [kept["id"]]
    finally:
        await manager.close()


async def test_s9_s10_daily_snapshot_only_after_a_real_change_and_not_under_human_control(
    tmp_path,
):
    # The interval is a product setting; a short one keeps the test real-time.
    manager, docker = await started(tmp_path, snapshot_interval_hours=0.0002)
    try:
        # No snapshot yet: the first daily check takes one.
        await manager.snapshots.step()
        (first,) = manager.snapshots.ready()
        assert first["source"] == "daily"
        # An interval later, nothing changed since that point: no snapshot, no slot taken.
        await asyncio.sleep(0.8)
        await manager.snapshots.step()
        assert len(manager.snapshots.ready()) == 1
        checked = float(manager.setting("snapshot_daily_checked_at"))
        assert checked > first["point_at"]
        # A manual snapshot is always taken.
        assert (await snapshot(manager))["state"] == "ready"
        # S10: due again after a real change, but the person holds the screen.
        await asyncio.sleep(0.8)
        docker.write_rootfs("/etc/changed", "yes")
        manager.set_setting("computer_control", json.dumps({"holder": "human"}))
        await manager.snapshots.step()
        assert len(manager.snapshots.ready()) == 2
        manager.set_setting("computer_control", json.dumps({"holder": "none"}))
        await manager.snapshots.step()
        (newest, *_) = manager.snapshots.ready()
        assert len(manager.snapshots.ready()) == 3 and newest["source"] == "daily"
    finally:
        await manager.close()


async def test_s11_yuki_does_not_snapshot_restore_or_sleep_under_human_control(tmp_path):
    manager, _ = await started(tmp_path)
    try:
        ready = await snapshot(manager)
        manager.set_setting("computer_control", json.dumps({"holder": "human", "since": "now"}))
        for args in (
            {"action": "snapshot"},
            {"action": "restore", "snapshot": ready["id"]},
            {"action": "sleep"},
        ):
            result = await manager.desktop.action("computer_action", args, str(uuid4()))
            assert result["error"] == "control_held", args
        assert len(manager.snapshots.ready()) == 1
        assert manager.snapshots.status()["last_restore"] is None
    finally:
        await manager.close()


async def test_s12_a_removed_snapshot_image_is_reported_not_replaced(tmp_path):
    manager, docker = await started(tmp_path)
    try:
        ready = await snapshot(manager)
        generation = manager.container_id
        del docker.images[ready["image"]]
        failed = await restore(manager, ready["id"])
        assert failed["state"] == "failed" and failed["error"] == "snapshot_image_missing"
        assert manager.container_id == generation and docker.container is not None
        # Recreating a lost container from it is a real creation failure, never the base.
        docker.container = None
        with pytest.raises(RuntimeError, match="environment_create_failed"):
            await manager.recover()
        assert docker.container is None
    finally:
        await manager.close()


async def test_s13_a_lost_container_is_rebuilt_from_the_latest_snapshot_keeping_home(tmp_path):
    manager, docker = await started(tmp_path)
    try:
        docker.write_rootfs("/usr/bin/installed", "binary")
        ready = await snapshot(manager)
        (manager.home / "current.txt").write_text("written after the snapshot")
        docker.container = None
        await manager.recover()
        assert docker.container["image"] == ready["image"]
        assert docker.container["rootfs"]["/usr/bin/installed"] == "binary"
        assert (manager.home / "current.txt").read_text() == "written after the snapshot"
        recreated = manager.environment_status()["recreated_from"]
        assert recreated["snapshot_id"] == ready["id"]
        assert recreated["point_at"] == ready["point_at"]
    finally:
        await manager.close()


async def test_s14_restore_interrupts_running_commands_with_a_real_receipt(tmp_path):
    manager, _ = await started(tmp_path)
    try:
        ready = await snapshot(manager)
        identity, queued = str(uuid4()), str(uuid4())
        with manager.db:
            for job, dispatched in ((identity, 1), (queued, 0)):
                manager.db.execute(
                    "INSERT INTO jobs VALUES (?,?,?,?,?,?,?)",
                    (job, f"call-{job}", "digest", "{}", "running", "{}", time.time()),
                )
                manager.db.execute(
                    "INSERT INTO environment_jobs VALUES (?,?,?,?,?)",
                    (job, "terminal_exec", "session", manager.container_id, dispatched),
                )
        await restore(manager, ready["id"])
        result = manager.get(identity)
        assert result["status"] == "failed" and result["error"] == "environment_restored"
        assert result["interrupted"] is True and result["snapshot_id"] == ready["id"]
        (event,) = manager.completions.pending()["events"]
        assert event["run_id"] == identity and event["request_id"] == f"call-{identity}"
        # A run that never started is not interrupted: it starts in the restored computer.
        assert manager.get(queued)["status"] == "running" and queued in manager.queue._queue
    finally:
        await manager.close()


async def test_s18_nothing_inside_the_environment_reaches_the_snapshots(tmp_path):
    manager, docker = await started(tmp_path)
    try:
        (create,) = (call for call in docker.calls if call[:2] == ("docker", "create"))
        assert not any("snapshot" in argument for argument in create)
        assert str(tmp_path / "snapshots") not in json.dumps(create)
    finally:
        await manager.close()


async def test_s19_the_environment_ending_during_the_pause_fails_only_that_snapshot(tmp_path):
    manager, docker = await started(tmp_path)
    try:
        kept = await snapshot(manager)
        original = docker.command

        async def oom_during_copy(*args, deadline_seconds=30):
            if args[0] == "rsync":
                docker.stop(oom=True)
            return await original(*args, deadline_seconds=deadline_seconds)

        manager.command = oom_during_copy
        failed = await snapshot(manager)
        assert failed["state"] == "failed"
        assert docker.snapshot_tags() == [kept["image"]]
        assert manager.setting("checkpoint_image") == kept["image"]
    finally:
        await manager.close()


def test_home_change_follows_ctime_and_skips_the_media_cache(tmp_path):
    home = tmp_path / "home"
    (home / "inputs/media-cache").mkdir(parents=True)
    profile = home / ".local/share/yuki-computer/chromium/Default"
    (profile / "Cache").mkdir(parents=True)
    time.sleep(0.01)
    assert home_changed(home, time.time()) is None
    marker = time.time()
    time.sleep(0.05)
    (home / "inputs/media-cache/new.jpg").write_text("cache")
    (profile / "Cache/data_0").write_text("cache")
    assert home_changed(home, marker) is None
    (home / "note.txt").write_text("hers")
    assert home_changed(home, marker) in {"/", "note.txt"}
