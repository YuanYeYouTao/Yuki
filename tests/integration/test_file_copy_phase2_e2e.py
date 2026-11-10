"""Copy artifacts and owner's-computer files into the Yuki environment (F08).

Isolated SQLite, the real Connector package over TLS WSS, and a real testing
Manager on a Unix socket; the model tool goes through its declared schema and the
direct dispatcher. Cases follow ``docs/operations/file-copy-phase2-20261110.md``
(E1-E19). The run writes ``test-results/file-copy-phase2-<commit>.json``.
"""

import asyncio
import io
import json
import os
import random
import shutil
import sqlite3
import tempfile
import time
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from tests.integration.test_external_agent_bridge import bridge as bridge
from tests.integration.test_file_copy_e2e import (
    direct_tools,
    manager_socket,
    seeded_sources,
    sha,
    source_revision,
)
from tests.integration.test_local_device_transport import actual_connector
from yuki_agent_connector import PROTOCOL_VERSION
from yuki_agent_connector import __version__ as connector_version

from yuki.conversation.media_service import ConversationMediaService
from yuki.sandbox.client import SandboxClient
from yuki.sandbox.persistent import PersistentManager
from yuki.services.image_preprocessor import ImagePreprocessor
from yuki.workspace.inspect import WorkspaceInspector
from yuki.workspace.service import WorkspaceService
from yuki.workspace.store import WorkspaceError

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Manager safe FDs require POSIX")

TOOL = "copy_file"


def residue(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.glob(".yuki-write-*")) if directory.exists() else []


class LostAnswer:
    """A Manager writer whose final answer never reaches the Bot."""

    def __init__(self, writer):
        self.writer = writer
        self.lines = 0

    def write(self, data: bytes) -> None:
        self.lines += 1
        if self.lines == 1:  # ``ready``; the result line after the bytes is lost.
            self.writer.write(data)

    async def drain(self) -> None:
        await self.writer.drain()


async def test_copy_into_yuki_environment_from_artifact_and_computer(bridge, tmp_path, monkeypatch):
    revision, dirty = source_revision()
    evidence: dict = {
        "commit": revision,
        "worktree_dirty": dirty,
        "connector_version": connector_version,
        "connector_protocol": PROTOCOL_VERSION,
        "cases": [],
    }
    device_root = tmp_path / "device-home"
    device_root.mkdir()
    sources = seeded_sources()
    for name, data in sources.items():
        (device_root / name).write_bytes(data)
    device_id = bridge.device["device_id"]
    store = bridge.env.store
    devices = bridge.service.local_devices
    original_request = devices.file_request

    def record(case, **facts):
        evidence["cases"].append({"case": case, **facts})

    def no_source(*_args, **_kwargs):
        raise AssertionError("a recorded copy must not read its source again")

    try:
        async with manager_socket(tmp_path, store, tmp_path / "media-cache") as (manager, socket):
            client = SandboxClient(socket)
            client.local_devices = devices
            media = ConversationMediaService(
                bridge.database, tmp_path / "media-cache", None, ImagePreprocessor()
            )
            workspace = WorkspaceService(
                store,
                sandbox=client,
                visual_inspector=WorkspaceInspector(store, ImagePreprocessor()),
                conversation_media=media,
            )
            call = direct_tools(bridge, workspace)
            root = manager.files.root

            async def service(arguments, request_id, on=workspace):
                return await on.execute(
                    TOOL, arguments, request_id=request_id, source=bridge.source
                )

            async def manager_receipt(source_key, destination, request_id):
                return await client.execute(
                    "workspace_copy_result",
                    {"source_key": source_key, "destination": destination},
                    request_id=request_id,
                )

            artifacts = {}
            for name in sources:
                with (device_root / name).open("rb") as stream:
                    artifacts[name] = store.snapshot(
                        stream.fileno(), name, source_key=f"send:phase2:{name}"
                    )["artifact_id"]

            # E7. Source computer offline: nothing reaches the Manager.
            with pytest.raises(WorkspaceError) as offline:
                await service(
                    {
                        "path": str(device_root / "report.md"),
                        "device": device_id,
                        "destination": "in/offline.md",
                    },
                    "phase2-offline",
                )
            assert str(offline.value) == "device_file_read_unavailable"
            assert not (root / "in" / "offline.md").exists()
            assert (
                await manager_receipt(
                    f"device:{device_id}:{device_root / 'report.md'}",
                    "in/offline.md",
                    "phase2-offline",
                )
            ) == {"found": False}
            record("source_computer_offline", error=str(offline.value), manager_receipt=False)

            async with actual_connector(bridge, tmp_path, monkeypatch) as connector:
                # E14. Artifact -> environment and computer -> environment, all sizes,
                # through the declared tool and the direct dispatcher. E16 computer ->
                # computer through the same device reader and the original upload.
                for name, data in sources.items():
                    for kind, arguments in (
                        (
                            "artifact",
                            {"artifact_id": artifacts[name], "destination": f"in/artifact-{name}"},
                        ),
                        (
                            "computer",
                            {
                                "path": str(device_root / name),
                                "device": device_id,
                                "destination": f"in/computer-{name}",
                            },
                        ),
                    ):
                        request_id = f"phase2-{kind}-{name}"
                        started = time.perf_counter()
                        result = await call(arguments, request_id)
                        elapsed = time.perf_counter() - started
                        target = root / arguments["destination"]
                        assert result["error"] is None, result
                        assert result["version"] == sha(data) and result["size"] == len(data)
                        assert target.read_bytes() == data
                        record(
                            f"{kind}_to_yuki_environment",
                            source=arguments.get("artifact_id") or arguments.get("path"),
                            destination=result["path"],
                            source_sha256=sha(data),
                            target_sha256=sha(target.read_bytes()),
                            size=len(data),
                            transfer_id=request_id,
                            receipt=result,
                            seconds=round(elapsed, 4),
                        )
                    target = device_root / f"copied-{name}"
                    result = await call(
                        {
                            "path": str(device_root / name),
                            "device": device_id,
                            "destination": str(target),
                            "to_device": device_id,
                        },
                        f"phase2-computer-to-computer-{name}",
                    )
                    assert result["error"] is None and result["status"] == "succeeded", result
                    assert target.read_bytes() == data and result["sha256"] == sha(data)
                    record(
                        "computer_to_computer",
                        source=str(device_root / name),
                        destination=str(target),
                        source_sha256=sha(data),
                        target_sha256=sha(target.read_bytes()),
                        size=len(data),
                        transfer_id=f"phase2-computer-to-computer-{name}",
                        receipt=result,
                    )
                assert residue(root / "in") == []

                # E1. Replay after success with the sources gone reads no source.
                report = sources["report.md"]
                inodes = {
                    kind: (root / "in" / f"{kind}-report.md").stat().st_ino
                    for kind in ("artifact", "computer")
                }
                (device_root / "report.md").unlink()
                copied_inode = (device_root / "copied-report.md").stat().st_ino
                with monkeypatch.context() as patch:
                    patch.setattr(store, "open_blob", no_source)
                    patch.setattr(devices, "open_file", no_source)
                    replays = [
                        await service(
                            {
                                "artifact_id": artifacts["report.md"],
                                "destination": "in/artifact-report.md",
                            },
                            "phase2-artifact-report.md",
                        ),
                        await service(
                            {
                                "path": str(device_root / "report.md"),
                                "device": device_id,
                                "destination": "in/computer-report.md",
                            },
                            "phase2-computer-report.md",
                        ),
                        await service(
                            {
                                "path": str(device_root / "report.md"),
                                "device": device_id,
                                "destination": str(device_root / "copied-report.md"),
                                "to_device": device_id,
                            },
                            "phase2-computer-to-computer-report.md",
                        ),
                    ]
                assert replays[0]["version"] == replays[1]["version"] == sha(report)
                assert replays[2]["status"] == "succeeded"
                assert {
                    kind: (root / "in" / f"{kind}-report.md").stat().st_ino
                    for kind in ("artifact", "computer")
                } == inodes
                assert (device_root / "copied-report.md").stat().st_ino == copied_inode
                record(
                    "replay_after_source_deleted", receipts=replays, target_inodes_unchanged=True
                )
                (device_root / "report.md").write_bytes(report)

                # E12. The same request with another destination is a conflict.
                conflict = await service(
                    {"artifact_id": artifacts["report.md"], "destination": "in/elsewhere.md"},
                    "phase2-artifact-report.md",
                )
                assert conflict["error"] == "idempotency_conflict"
                assert not (root / "in" / "elsewhere.md").exists()
                record("same_request_other_arguments", receipt=conflict)

                large = sources["one-mib-plus-one.bin"]
                large_path = str(device_root / "one-mib-plus-one.bin")

                # E2. The Manager restarts mid-transfer: the durable receipt is unknown.
                gate, reached = asyncio.Event(), asyncio.Event()

                async def hold_second_part(device, actor, action, arguments, *, identity):
                    if action == "read" and arguments["offset"] > 0 and not gate.is_set():
                        reached.set()
                        await gate.wait()
                        raise WorkspaceError("device_file_read_unavailable")
                    return await original_request(
                        device, actor, action, arguments, identity=identity
                    )

                monkeypatch.setattr(devices, "file_request", hold_second_part)
                in_flight = asyncio.create_task(
                    service(
                        {"path": large_path, "device": device_id, "destination": "in/restart.bin"},
                        "phase2-manager-restart",
                    )
                )
                async with asyncio.timeout(15):
                    await reached.wait()
                    while not residue(root / "in"):  # noqa: ASYNC110
                        await asyncio.sleep(0.01)
                at_restart = residue(root / "in")
                restarted = PersistentManager(
                    tmp_path / "manager",
                    store,
                    "test-image",
                    "test-network",
                    "",
                    tmp_path / "home",
                    testing=True,
                )
                restart_dir = Path(tempfile.mkdtemp(prefix="yk-"))
                restart_server = await asyncio.start_unix_server(
                    restarted.serve, str(restart_dir / "m.sock")
                )
                try:
                    after_restart = SandboxClient(restart_dir / "m.sock")
                    after_restart.local_devices = devices
                    recovered_workspace = WorkspaceService(
                        store,
                        sandbox=after_restart,
                        visual_inspector=WorkspaceInspector(store, ImagePreprocessor()),
                        conversation_media=media,
                    )
                    with monkeypatch.context() as patch:
                        patch.setattr(devices, "open_file", no_source)
                        recovered = await service(
                            {
                                "path": large_path,
                                "device": device_id,
                                "destination": "in/restart.bin",
                            },
                            "phase2-manager-restart",
                            on=recovered_workspace,
                        )
                finally:
                    restart_server.close()
                    await restart_server.wait_closed()
                    await restarted.close()
                    shutil.rmtree(restart_dir, ignore_errors=True)
                assert recovered["error"] == "file_mutation_unknown_read_current_version"
                assert recovered["uncertain"] is True and ".yuki-write-" in recovered["message"]
                assert not (root / "in" / "restart.bin").exists()
                record(
                    "manager_restart_mid_transfer",
                    transfer_id="phase2-manager-restart",
                    recovery=recovered,
                    residue_at_restart=at_restart,
                    target_exists=False,
                )

                # E3. The Bot stops pushing mid-transfer (here: the source read fails):
                # the Manager keeps a definite failure and no residue.
                gate.set()
                with pytest.raises(WorkspaceError) as stopped:
                    await in_flight
                monkeypatch.setattr(devices, "file_request", original_request)
                assert str(stopped.value) == "device_file_read_unavailable"
                with monkeypatch.context() as patch:
                    patch.setattr(devices, "open_file", no_source)
                    replay = await service(
                        {"path": large_path, "device": device_id, "destination": "in/restart.bin"},
                        "phase2-manager-restart",
                    )
                assert replay["error"] == "source_stream_incomplete"
                assert not replay.get("uncertain")
                assert not (root / "in" / "restart.bin").exists() and residue(root / "in") == []
                record(
                    "sender_stopped_mid_transfer",
                    error=str(stopped.value),
                    recovery=replay,
                    residue=residue(root / "in"),
                )

                # E4. All bytes landed but the answer is lost: unknown, then the
                # recovery reads the original success without reading the source.
                real_write = manager.workspace_write

                async def lose_answer(request, reader, writer):
                    await real_write(request, reader, LostAnswer(writer))

                monkeypatch.setattr(manager, "workspace_write", lose_answer)
                lost = await service(
                    {"path": large_path, "device": device_id, "destination": "in/lost.bin"},
                    "phase2-lost-answer",
                )
                monkeypatch.setattr(manager, "workspace_write", real_write)
                assert lost["error"] == "sandbox_unavailable" and lost["uncertain"] is True
                assert lost["source_sha256"] == sha(large) and ".yuki-write-" in lost["message"]
                inode = (root / "in" / "lost.bin").stat().st_ino
                with monkeypatch.context() as patch:
                    patch.setattr(devices, "open_file", no_source)
                    recovered = await service(
                        {"path": large_path, "device": device_id, "destination": "in/lost.bin"},
                        "phase2-lost-answer",
                    )
                assert recovered["version"] == sha(large)
                assert (root / "in" / "lost.bin").stat().st_ino == inode
                record("answer_lost_recovers_success", first=lost, recovery=recovered)

                # E5. Bytes that do not match the declared sha256 never replace a target.
                (root / "in" / "kept.bin").write_bytes(b"kept")
                payload = io.BytesIO(b"x" * 70000)

                async def bytes_of(wanted):
                    return payload.read(wanted)

                mismatch = await client.write(
                    {
                        "destination": "in/kept.bin",
                        "source_key": "test:mismatch",
                        "size": 70000,
                        "sha256": sha(b"other"),
                    },
                    bytes_of,
                    request_id="phase2-mismatch",
                )
                assert mismatch["error"] == "version_conflict"
                assert (root / "in" / "kept.bin").read_bytes() == b"kept"
                assert residue(root / "in") == []
                record("declared_sha256_mismatch", receipt=mismatch)

                # E6. The computer file changes during the read.
                changing = device_root / "changing.bin"
                changing.write_bytes(large)
                changed = {"done": False}

                async def change_source(device, actor, action, arguments, *, identity):
                    if action == "read" and arguments["offset"] > 0 and not changed["done"]:
                        changed["done"] = True
                        changing.write_bytes(b"rewritten during transfer")
                    return await original_request(
                        device, actor, action, arguments, identity=identity
                    )

                monkeypatch.setattr(devices, "file_request", change_source)
                with pytest.raises(WorkspaceError) as moved:
                    await service(
                        {
                            "path": str(changing),
                            "device": device_id,
                            "destination": "in/changing.bin",
                        },
                        "phase2-source-changes",
                    )
                monkeypatch.setattr(devices, "file_request", original_request)
                assert str(moved.value) == "file_changed"
                assert not (root / "in" / "changing.bin").exists()
                receipt = await manager_receipt(
                    f"device:{device_id}:{changing}", "in/changing.bin", "phase2-source-changes"
                )
                assert receipt["result"]["error"] == "source_stream_incomplete"
                record("computer_source_changed", error=str(moved.value), manager=receipt)

                # E8. Full access off on the source computer.
                connector.config = replace(connector.config, full_access=False)
                try:
                    with pytest.raises(WorkspaceError) as closed:
                        await service(
                            {
                                "path": str(device_root / "report.md"),
                                "device": device_id,
                                "destination": "in/closed.md",
                            },
                            "phase2-full-access-closed",
                        )
                finally:
                    connector.config = replace(connector.config, full_access=True)
                assert str(closed.value) == "full_access_disabled"
                assert not (root / "in" / "closed.md").exists()
                record("source_full_access_closed", error=str(closed.value))

                # E9 / E10. Mutable and unknown artifacts reach no Manager write.
                mutable = str(uuid.uuid4())
                blob = f"{uuid.uuid4()}.blob"
                (store.root / blob).write_bytes(report)
                with sqlite3.connect(store.root / "manifest.sqlite3") as db:
                    db.execute(
                        "INSERT INTO artifacts VALUES (?,?,?,?,?,1,0,0)",
                        (mutable, "legacy.md", blob, sha(report), len(report)),
                    )
                for case, artifact_id, expected in (
                    ("mutable_artifact", mutable, "artifact_not_immutable"),
                    ("unknown_artifact", str(uuid.uuid4()), "artifact_not_found"),
                ):
                    with pytest.raises(WorkspaceError) as rejected:
                        await service(
                            {"artifact_id": artifact_id, "destination": f"in/{case}.md"},
                            f"phase2-{case}",
                        )
                    assert str(rejected.value) == expected
                    assert (
                        await manager_receipt(
                            f"artifact:{artifact_id}", f"in/{case}.md", f"phase2-{case}"
                        )
                    ) == {"found": False}
                    record(case, error=expected, manager_receipt=False)

                # E11. Destinations: outside the workspace (a 1 MiB+1 source, so the
                # Manager answers after taking the pushed bytes), an existing
                # directory, a missing parent, and an existing file.
                outside = await service(
                    {"artifact_id": artifacts["one-mib-plus-one.bin"], "destination": "/etc/x"},
                    "phase2-outside",
                )
                assert outside["error"] == "path_outside_workspace"
                assert not outside.get("uncertain")
                (root / "in" / "folder").mkdir()
                into_folder = await service(
                    {"artifact_id": artifacts["report.md"], "destination": "in/folder"},
                    "phase2-into-folder",
                )
                assert into_folder["error"] == "IsADirectoryError"
                assert not into_folder.get("uncertain") and (root / "in" / "folder").is_dir()
                assert residue(root / "in") == []
                deep = await service(
                    {"artifact_id": artifacts["report.md"], "destination": "in/a/b/c/report.md"},
                    "phase2-missing-parent",
                )
                assert (root / "in" / "a" / "b" / "c" / "report.md").read_bytes() == report
                overwrite = await service(
                    {"artifact_id": artifacts["report.md"], "destination": "in/kept.bin"},
                    "phase2-overwrite",
                )
                assert (root / "in" / "kept.bin").read_bytes() == report
                record(
                    "destinations",
                    outside_workspace=outside,
                    existing_directory=into_folder,
                    missing_parent=deep,
                    existing_file_replaced=overwrite,
                )

                # E13. Two copies into one environment target at once.
                first, second = await asyncio.gather(
                    service(
                        {
                            "artifact_id": artifacts["one-mib-plus-one.bin"],
                            "destination": "in/shared.bin",
                        },
                        "phase2-concurrent-a",
                    ),
                    service(
                        {"path": large_path, "device": device_id, "destination": "in/shared.bin"},
                        "phase2-concurrent-b",
                    ),
                )
                assert first["version"] == second["version"] == sha(large)
                assert sha((root / "in" / "shared.bin").read_bytes()) == sha(large)
                assert residue(root / "in") == []
                record("concurrent_copies_same_target", receipts=[first, second])

                # E17. ``device`` names the source computer of a path only.
                with pytest.raises(WorkspaceError) as ambiguous:
                    await service(
                        {
                            "artifact_id": artifacts["report.md"],
                            "device": device_id,
                            "destination": "in/both.md",
                        },
                        "phase2-two-sources",
                    )
                assert str(ambiguous.value) == "choose_one_source"
                record("device_with_artifact", error=str(ambiguous.value))

                # E18. The Bot restarts while reading the source, before the Manager
                # saw the request: the same request reads and writes once.
                stat_started = asyncio.Event()

                async def stall_stat(device, actor, action, arguments, *, identity):
                    if action == "stat":
                        stat_started.set()
                        await asyncio.Event().wait()
                    return await original_request(
                        device, actor, action, arguments, identity=identity
                    )

                monkeypatch.setattr(devices, "file_request", stall_stat)
                interrupted = asyncio.create_task(
                    service(
                        {"path": large_path, "device": device_id, "destination": "in/resumed.bin"},
                        "phase2-bot-restart-reading",
                    )
                )
                async with asyncio.timeout(15):
                    await stat_started.wait()
                interrupted.cancel()
                await asyncio.gather(interrupted, return_exceptions=True)
                monkeypatch.setattr(devices, "file_request", original_request)
                resumed = await service(
                    {"path": large_path, "device": device_id, "destination": "in/resumed.bin"},
                    "phase2-bot-restart-reading",
                )
                assert resumed["version"] == sha(large)
                assert (root / "in" / "resumed.bin").read_bytes() == large
                record("bot_restart_while_reading_source", receipt=resumed)

                # E19. 64 MiB through each new path.
                big = random.Random(64).randbytes(64 * 1024 * 1024)
                (device_root / "big.bin").write_bytes(big)
                with (device_root / "big.bin").open("rb") as stream:
                    big_artifact = store.snapshot(stream.fileno(), "big.bin")["artifact_id"]
                for kind, arguments in (
                    ("artifact", {"artifact_id": big_artifact, "destination": "in/big-a.bin"}),
                    (
                        "computer",
                        {
                            "path": str(device_root / "big.bin"),
                            "device": device_id,
                            "destination": "in/big-c.bin",
                        },
                    ),
                ):
                    started = time.perf_counter()
                    result = await service(arguments, f"phase2-64-mib-{kind}")
                    elapsed = time.perf_counter() - started
                    assert result["version"] == sha(big)
                    assert sha((root / arguments["destination"]).read_bytes()) == sha(big)
                    record(
                        f"throughput_64_mib_{kind}",
                        size=len(big),
                        source_sha256=sha(big),
                        target_sha256=result["version"],
                        transfer_id=f"phase2-64-mib-{kind}",
                        seconds=round(elapsed, 3),
                        mib_per_second=round(len(big) / 1024 / 1024 / elapsed, 3),
                    )
    finally:
        output = Path("test-results") / f"file-copy-phase2-{revision[:12]}.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
