"""Copy Yuki environment files, artifacts and attachments to a real Connector computer.

Isolated SQLite, the real Connector package over TLS WSS, and a real testing
Manager on a Unix socket. The run writes ``test-results/file-copy-<commit>.json``.
"""

import asyncio
import hashlib
import io
import json
import os
import random
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from PIL import Image
from tests.conftest import make_settings
from tests.integration.test_external_agent_bridge import bridge as bridge
from tests.integration.test_local_device_transport import actual_connector
from tests.support.event_seed import seed_scoped_event
from tests.support.service_dependencies import tool_dependencies
from yuki_agent_connector import PROTOCOL_VERSION
from yuki_agent_connector import __version__ as connector_version

from yuki.capabilities.invocation import ToolInvocationContext, current_invocation
from yuki.capabilities.validation import JsonSchemaCapabilityValidator
from yuki.conversation.media_service import ConversationMediaService
from yuki.domain.conversations import ConversationScope, ScopeType
from yuki.domain.messages import AttachmentKind, InboundMessage, MessageAttachment, SenderIdentity
from yuki.memory.repository import MemoryFactRepository
from yuki.memory.service import MemoryFactService
from yuki.persistence.repositories import EventLedgerRepository
from yuki.sandbox.client import SandboxClient
from yuki.sandbox.persistent import PersistentManager
from yuki.services.agent_tools import AgentToolService, ToolRuntime
from yuki.services.image_preprocessor import ImagePreprocessor
from yuki.services.media_resolver import MediaResolver
from yuki.workspace.inspect import WorkspaceInspector
from yuki.workspace.service import WorkspaceService
from yuki.workspace.store import WorkspaceError
from yuki.workspace.tools import workspace_tools

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Manager safe FDs require POSIX")

TOOL = "copy_file"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def seeded_sources() -> dict[str, bytes]:
    rng = random.Random(20261107)
    words = "雪落在服务器与本机之间的文件桥上，字节原样抵达，不经过模型文本。"
    lines = ["# 文件复制报告\n\n"]
    while sum(len(line.encode()) for line in lines) < 12 * 1024:
        lines.append(f"- 第{len(lines)}行：" + "".join(rng.choice(words) for _ in range(40)) + "\n")
    return {
        "report.md": "".join(lines).encode(),
        "one-mib-plus-one.bin": rng.randbytes(1024 * 1024 + 1),
        "empty.bin": b"",
        "exact-128k.bin": rng.randbytes(128 * 1024),
    }


def source_revision() -> tuple[str, bool]:
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    status = subprocess.check_output(["git", "status", "--porcelain"], text=True)
    return revision, bool(status.strip())


@asynccontextmanager
async def manager_socket(root: Path, store, cache: Path):
    manager = PersistentManager(
        root / "manager",
        store,
        "test-image",
        "test-network",
        "",
        root / "home",
        testing=True,
        media_cache=cache,
    )
    manager.files.root.mkdir(parents=True, exist_ok=True)
    # macOS limits Unix socket paths, so the socket lives in a short directory.
    socket_dir = Path(tempfile.mkdtemp(prefix="yk-"))
    socket = socket_dir / "m.sock"
    server = await asyncio.start_unix_server(manager.serve, str(socket))
    try:
        yield manager, socket
    finally:
        server.close()
        await server.wait_closed()
        await manager.close()
        shutil.rmtree(socket_dir, ignore_errors=True)


@asynccontextmanager
async def cached_attachment(bridge, cache: Path, data: bytes):
    async def respond(_request):
        return httpx.Response(200, content=data, headers={"content-type": "image/png"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        resolver = MediaResolver(client=client, host_resolver=lambda *_args: ("8.8.8.8",))
        media = ConversationMediaService(bridge.database, cache, resolver, ImagePreprocessor())
        segment = {"type": "image", "data": {"url": "https://example.test/copy.png"}}
        appended = await seed_scoped_event(
            bridge.env.service.writer,
            scope=ConversationScope.group("80001", "20001"),
            origin_key="file-copy-attachment",
            sender_user_id="10001",
            direction="inbound",
            content="copy this",
            segments=(segment,),
        )
        event = appended.event
        message = InboundMessage(
            "file-copy-attachment",
            "file-copy-attachment",
            "message",
            ScopeType.GROUP,
            SenderIdentity("10001"),
            "copy this",
            bot_user_id="80001",
            group_id="20001",
            person_id=bridge.env.person,
            space_id=bridge.env.space,
            source_event_id=event.id,
            conversation_id=event.canonical_conversation_id,
            segments=(segment,),
            attachments=(
                MessageAttachment(
                    AttachmentKind.IMAGE,
                    "copy.png",
                    segment_index=0,
                    url="https://example.test/copy.png",
                ),
            ),
        )
        message = media.prepare_message(message, None, source_segments=event.segments)
        await media._cache_event(event.id, None, message)
        try:
            yield media, event.id
        finally:
            await media.close()


def direct_tools(bridge, workspace):
    settings = make_settings(bridge.database.url)
    tools = AgentToolService(
        settings=settings,
        ledger=EventLedgerRepository(bridge.database),
        memories=MemoryFactService(MemoryFactRepository(bridge.database)),
        **tool_dependencies(bridge.database, settings),
    )
    tools.workspace_service = workspace
    runtime = ToolRuntime(
        inbound=InboundMessage(
            "file-copy-trigger",
            "file-copy-trigger",
            "message:private:normal",
            ScopeType.PRIVATE,
            SenderIdentity("10001"),
            "copy the file",
            bot_user_id="80001",
            person_id=bridge.env.person,
        ),
        execution_id="file-copy-execution",
        conversation_id="file-copy-conversation",
        target_presence_id="presence-test",
    )
    validator = JsonSchemaCapabilityValidator()
    validator.admit_tools(workspace_tools())
    schema = validator.freeze()

    async def call(arguments, request_id):
        encoded = json.dumps(arguments, ensure_ascii=False)
        admitted = schema.validate(TOOL, encoded)
        assert admitted.ok, admitted
        token = current_invocation.set(ToolInvocationContext(runtime, request_id))
        try:
            result = await tools.execute(TOOL, encoded, runtime)
        finally:
            current_invocation.reset(token)
        return {"error": result.error_code, "uncertain": result.uncertain, **(result.data or {})}

    return call


async def test_file_copy_to_actual_connector(bridge, tmp_path, monkeypatch):
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
    cache = tmp_path / "media-cache"
    sources = seeded_sources()
    device_id = bridge.device["device_id"]
    store = bridge.env.store

    def record(case, **facts):
        evidence["cases"].append({"case": case, **facts})

    try:
        async with (
            manager_socket(tmp_path, store, cache) as (manager, socket),
            cached_attachment(bridge, cache, png_bytes()) as (media, event_id),
        ):
            client = SandboxClient(socket)
            client.local_devices = bridge.service.local_devices
            workspace = WorkspaceService(
                store,
                sandbox=client,
                visual_inspector=WorkspaceInspector(store, ImagePreprocessor()),
                conversation_media=media,
            )
            call = direct_tools(bridge, workspace)
            source = bridge.source
            for name, data in sources.items():
                (manager.files.root / "src").mkdir(exist_ok=True)
                (manager.files.root / "src" / name).write_bytes(data)

            async def service(arguments, request_id):
                return await workspace.execute(
                    TOOL, arguments, request_id=request_id, source=source
                )

            # 7a. The first call while the computer is offline is unknown, not failed.
            offline_target = device_root / "offline.md"
            offline = await service(
                {
                    "path": "src/report.md",
                    "destination": str(offline_target),
                    "to_device": device_id,
                },
                "copy-offline-first",
            )
            assert offline["status"] == "unknown" and offline["uncertain"] is True
            assert not offline_target.exists()
            record("device_offline_first_call", result=offline, target_exists=False)
            # The model reads the same unconfirmed copy as a failure with unknown status.
            offline_model = await call(
                {
                    "path": "src/report.md",
                    "destination": str(device_root / "offline-model.md"),
                    "to_device": device_id,
                },
                "copy-offline-model",
            )
            assert offline_model["error"] and offline_model["uncertain"] is True
            assert offline_model["status"] == "unknown" and offline_model["message"]
            record("device_offline_model_result", result=offline_model)

            async with actual_connector(bridge, tmp_path, monkeypatch) as connector:
                artifacts = {}
                for name in sources:
                    with (manager.files.root / "src" / name).open("rb") as stream:
                        artifacts[name] = store.snapshot(
                            stream.fileno(), name, source_key=f"send:file-copy:{name}"
                        )["artifact_id"]

                # 1. path / artifact -> computer, path -> Yuki environment, through the
                # declared tool and the direct tool dispatcher.
                for name, data in sources.items():
                    for kind in ("path", "artifact"):
                        target = device_root / f"{kind}-{name}"
                        request_id = f"copy-{kind}-{name}"
                        arguments = {
                            ("path" if kind == "path" else "artifact_id"): (
                                f"src/{name}" if kind == "path" else artifacts[name]
                            ),
                            "destination": str(target),
                            "to_device": device_id,
                        }
                        started = time.perf_counter()
                        result = await call(arguments, request_id)
                        elapsed = time.perf_counter() - started
                        assert result["error"] is None, result
                        assert result["status"] == "succeeded", result
                        assert target.read_bytes() == data
                        assert result["sha256"] == sha(data) and result["size"] == len(data)
                        record(
                            f"{kind}_to_computer",
                            source=arguments.get("path") or arguments.get("artifact_id"),
                            destination=str(target),
                            source_sha256=sha(data),
                            target_sha256=sha(target.read_bytes()),
                            size=len(data),
                            transfer_id=request_id,
                            receipt=result,
                            seconds=round(elapsed, 4),
                        )
                    request_id = f"copy-env-{name}"
                    started = time.perf_counter()
                    result = await call(
                        {"path": f"src/{name}", "destination": f"copies/{name}"}, request_id
                    )
                    elapsed = time.perf_counter() - started
                    copied = manager.files.root / "copies" / name
                    assert result["error"] is None, result
                    assert result["version"] == sha(data) and copied.read_bytes() == data
                    record(
                        "path_to_yuki_environment",
                        source=f"src/{name}",
                        destination=result["path"],
                        source_sha256=sha(data),
                        target_sha256=sha(copied.read_bytes()),
                        size=len(data),
                        transfer_id=request_id,
                        receipt=result,
                        seconds=round(elapsed, 4),
                    )

                # Attachment -> computer and -> Yuki environment keep the original branch.
                attachment = png_bytes()
                target = device_root / "attachment.png"
                result = await call(
                    {
                        "event_id": event_id,
                        "attachment": 0,
                        "destination": str(target),
                        "to_device": device_id,
                    },
                    "copy-attachment-computer",
                )
                assert result["status"] == "succeeded" and target.read_bytes() == attachment
                record(
                    "attachment_to_computer",
                    source=f"event:{event_id}:0",
                    destination=str(target),
                    source_sha256=sha(attachment),
                    target_sha256=sha(target.read_bytes()),
                    size=len(attachment),
                    transfer_id="copy-attachment-computer",
                    receipt=result,
                )
                result = await call(
                    {"event_id": event_id, "attachment": 0, "destination": "copies/a.png"},
                    "copy-attachment-env",
                )
                assert (manager.files.root / "copies" / "a.png").read_bytes() == attachment
                record(
                    "attachment_to_yuki_environment",
                    source=f"event:{event_id}:0",
                    destination=result["path"],
                    source_sha256=sha(attachment),
                    target_sha256=result["version"],
                    size=len(attachment),
                    transfer_id="copy-attachment-env",
                    receipt=result,
                )

                # 2. Replay after success reads no source and leaves the target alone.
                report = sources["report.md"]
                path_target = device_root / "path-report.md"
                artifact_target = device_root / "artifact-report.md"
                inodes = (path_target.stat().st_ino, artifact_target.stat().st_ino)
                (manager.files.root / "src" / "report.md").unlink()

                def no_source(*_args, **_kwargs):
                    raise AssertionError("a confirmed copy must not read its source again")

                with monkeypatch.context() as patch:
                    patch.setattr(client, "stream", no_source)
                    patch.setattr(store, "open_blob", no_source)
                    patch.setattr(media, "authorized_path", no_source)
                    replay_path = await service(
                        {
                            "path": "src/report.md",
                            "destination": str(path_target),
                            "to_device": device_id,
                        },
                        "copy-path-report.md",
                    )
                    replay_artifact = await service(
                        {
                            "artifact_id": artifacts["report.md"],
                            "destination": str(artifact_target),
                            "to_device": device_id,
                        },
                        "copy-artifact-report.md",
                    )
                    replay_env = await service(
                        {"path": "src/report.md", "destination": "copies/report.md"},
                        "copy-env-report.md",
                    )
                assert replay_path["status"] == replay_artifact["status"] == "succeeded"
                assert replay_path["sha256"] == replay_artifact["sha256"] == sha(report)
                assert replay_env["version"] == sha(report)
                assert (path_target.stat().st_ino, artifact_target.stat().st_ino) == inodes
                record(
                    "replay_after_source_deleted",
                    receipts=[replay_path, replay_artifact, replay_env],
                    target_inodes_unchanged=True,
                )
                (manager.files.root / "src" / "report.md").write_bytes(report)

                devices = bridge.service.local_devices
                original_request = devices.file_request

                async def wait_online():
                    async with asyncio.timeout(15):
                        while not bridge.service.connections.online(device_id):  # noqa: ASYNC110
                            await asyncio.sleep(0.02)

                # 3a. The socket closes before the last part: unknown, never rewritten.
                large = sources["one-mib-plus-one.bin"]
                cut_target = device_root / "cut-before-final.bin"
                cut = {"done": False}

                async def cut_before_final(device, actor, action, arguments, *, identity):
                    if action == "write" and arguments["final"] and not cut["done"]:
                        cut["done"] = True
                        await connector.stream.close()
                        async with asyncio.timeout(8):
                            while bridge.service.connections.online(device):  # noqa: ASYNC110
                                await asyncio.sleep(0.01)
                    return await original_request(
                        device, actor, action, arguments, identity=identity
                    )

                monkeypatch.setattr(devices, "file_request", cut_before_final)
                arguments = {
                    "path": "src/one-mib-plus-one.bin",
                    "destination": str(cut_target),
                    "to_device": device_id,
                }
                cut_result = await service(arguments, "copy-cut-before-final")
                monkeypatch.setattr(devices, "file_request", original_request)
                assert cut_result["status"] == "unknown" and cut_result["uncertain"] is True
                assert "source_sha256" in cut_result and ".part" in cut_result["message"]
                await wait_online()
                with monkeypatch.context() as patch:
                    patch.setattr(client, "stream", no_source)
                    recovered = await service(arguments, "copy-cut-before-final")
                assert recovered["status"] == "unknown" and not cut_target.exists()
                parts = sorted(p.name for p in device_root.glob(".yuki-transfer-*.part"))
                assert parts
                record(
                    "socket_closed_before_final_part",
                    transfer_id="copy-cut-before-final",
                    first=cut_result,
                    recovery=recovered,
                    target_exists=False,
                    part_files=parts,
                )

                # 3b. The last part lands but its acknowledgement is lost: recovery reads
                # the Connector's succeeded receipt without reading the source.
                lost_target = device_root / "lost-final-ack.bin"
                lost = {"done": False}

                async def lose_final_ack(device, actor, action, arguments, *, identity):
                    result = await original_request(
                        device, actor, action, arguments, identity=identity
                    )
                    if action == "write" and arguments["final"] and not lost["done"]:
                        lost["done"] = True
                        return {
                            "status": "unknown",
                            "pending": True,
                            "uncertain": True,
                            "error": "device_file_write_not_confirmed",
                            "request_id": identity,
                            "transfer_id": arguments["transfer_id"],
                            "path": arguments["path"],
                        }
                    return result

                monkeypatch.setattr(devices, "file_request", lose_final_ack)
                arguments = {
                    "path": "src/one-mib-plus-one.bin",
                    "destination": str(lost_target),
                    "to_device": device_id,
                }
                lost_result = await service(arguments, "copy-lost-final-ack")
                monkeypatch.setattr(devices, "file_request", original_request)
                assert lost_result["status"] == "unknown"
                inode = lost_target.stat().st_ino
                with monkeypatch.context() as patch:
                    patch.setattr(client, "stream", no_source)
                    recovered = await service(arguments, "copy-lost-final-ack")
                assert recovered["status"] == "succeeded" and recovered["sha256"] == sha(large)
                assert lost_target.stat().st_ino == inode and lost_target.read_bytes() == large
                record(
                    "final_ack_lost_recovers_succeeded",
                    transfer_id="copy-lost-final-ack",
                    first=lost_result,
                    recovery=recovered,
                    target_sha256=sha(lost_target.read_bytes()),
                    simulated="Bot-side lost acknowledgement after the real final write",
                )

                # 4. Full access off: new write rejected, target absent; get still works.
                connector.config = replace(connector.config, full_access=False)
                closed_target = device_root / "closed.md"
                try:
                    with pytest.raises(WorkspaceError) as closed:
                        await service(
                            {
                                "path": "src/report.md",
                                "destination": str(closed_target),
                                "to_device": device_id,
                            },
                            "copy-full-access-closed",
                        )
                    receipt = await service(
                        {
                            "path": "src/report.md",
                            "destination": str(path_target),
                            "to_device": device_id,
                        },
                        "copy-path-report.md",
                    )
                finally:
                    connector.config = replace(connector.config, full_access=True)
                assert str(closed.value) == "full_access_disabled" and not closed_target.exists()
                assert receipt["status"] == "succeeded"
                record(
                    "full_access_closed",
                    error=str(closed.value),
                    target_exists=False,
                    original_receipt_still_readable=receipt,
                )

                # 5. The source changes during transfer: the target is the snapshot from
                # the start of the read, never mixed bytes.
                changing = device_root / "changing.bin"
                changed = {"done": False}

                async def change_source(device, actor, action, arguments, *, identity):
                    if action == "write" and arguments["offset"] > 0 and not changed["done"]:
                        changed["done"] = True
                        (manager.files.root / "src" / "one-mib-plus-one.bin").write_bytes(
                            b"rewritten during transfer"
                        )
                    return await original_request(
                        device, actor, action, arguments, identity=identity
                    )

                monkeypatch.setattr(devices, "file_request", change_source)
                changing_result = await service(
                    {
                        "path": "src/one-mib-plus-one.bin",
                        "destination": str(changing),
                        "to_device": device_id,
                    },
                    "copy-source-changes",
                )
                monkeypatch.setattr(devices, "file_request", original_request)
                assert changed["done"] and changing.read_bytes() == large
                record(
                    "source_changed_during_transfer",
                    receipt=changing_result,
                    target_sha256=sha(changing.read_bytes()),
                    source_sha256_at_start=sha(large),
                )
                (manager.files.root / "src" / "one-mib-plus-one.bin").write_bytes(large)

                # 6. Existing target is replaced; existing directory and missing parent
                # are reported as Connector 0.4.3 reports them.
                existing = device_root / "existing.md"
                existing.write_bytes(b"older content")
                replaced = await service(
                    {"path": "src/report.md", "destination": str(existing), "to_device": device_id},
                    "copy-overwrite",
                )
                assert replaced["status"] == "succeeded" and existing.read_bytes() == report
                record("existing_target_replaced", receipt=replaced)
                directory = device_root / "Documents"
                directory.mkdir()
                into_directory = await service(
                    {
                        "path": "src/report.md",
                        "destination": str(directory),
                        "to_device": device_id,
                    },
                    "copy-into-directory",
                )
                assert into_directory["status"] == "unknown" and directory.is_dir()
                record(
                    "target_is_existing_directory",
                    receipt=into_directory,
                    note="Connector 0.4.3: os.replace fails, unknown with a .part left behind",
                )
                missing_parent = device_root / "missing" / "report.md"
                missing = await service(
                    {
                        "path": "src/report.md",
                        "destination": str(missing_parent),
                        "to_device": device_id,
                    },
                    "copy-missing-parent",
                )
                assert missing["status"] == "unknown" and not missing_parent.exists()
                record(
                    "target_parent_missing",
                    receipt=missing,
                    note="Connector 0.4.3: OSError is not a DriverError, reported as unknown",
                )

                # 7. Links, mutable artifacts and concurrent copies to one target.
                os.symlink("report.md", manager.files.root / "src" / "link.md")
                os.link(
                    manager.files.root / "src" / "report.md",
                    manager.files.root / "src" / "hard.md",
                )
                for linked in ("link.md", "hard.md"):
                    with pytest.raises(WorkspaceError) as rejected:
                        await service(
                            {
                                "path": f"src/{linked}",
                                "destination": str(device_root / linked),
                                "to_device": device_id,
                            },
                            f"copy-{linked}",
                        )
                    assert str(rejected.value) == "source_file_unavailable"
                    assert not (device_root / linked).exists()
                    record(f"rejected_{linked.split('.')[0]}_source", error=str(rejected.value))
                os.unlink(manager.files.root / "src" / "hard.md")
                mutable = str(uuid.uuid4())
                blob = f"{uuid.uuid4()}.blob"
                (store.root / blob).write_bytes(report)
                with sqlite3.connect(store.root / "manifest.sqlite3") as db:
                    db.execute(
                        "INSERT INTO artifacts VALUES (?,?,?,?,?,1,0,0)",
                        (mutable, "legacy.md", blob, sha(report), len(report)),
                    )
                with pytest.raises(WorkspaceError) as rejected:
                    await service(
                        {
                            "artifact_id": mutable,
                            "destination": str(device_root / "legacy.md"),
                            "to_device": device_id,
                        },
                        "copy-mutable-artifact",
                    )
                assert str(rejected.value) == "artifact_not_immutable"
                record("rejected_mutable_artifact", error=str(rejected.value))
                with pytest.raises(WorkspaceError) as ambiguous:
                    await service(
                        {
                            "path": "src/report.md",
                            "artifact_id": artifacts["report.md"],
                            "destination": str(device_root / "both.md"),
                            "to_device": device_id,
                        },
                        "copy-two-sources",
                    )
                assert str(ambiguous.value) == "choose_one_source"
                record("source_selection_errors", two_sources=str(ambiguous.value))
                shared = device_root / "shared.bin"
                first, second = await asyncio.gather(
                    service(
                        {
                            "path": "src/one-mib-plus-one.bin",
                            "destination": str(shared),
                            "to_device": device_id,
                        },
                        "copy-concurrent-a",
                    ),
                    service(
                        {
                            "path": "src/exact-128k.bin",
                            "destination": str(shared),
                            "to_device": device_id,
                        },
                        "copy-concurrent-b",
                    ),
                )
                assert first["status"] == second["status"] == "succeeded"
                assert sha(shared.read_bytes()) in {first["sha256"], second["sha256"]}
                record(
                    "concurrent_copies_same_target",
                    receipts=[first, second],
                    final_target_sha256=sha(shared.read_bytes()),
                )

                # 8. Throughput for 64 MiB through files_read and 64 KiB device parts.
                big = random.Random(64).randbytes(64 * 1024 * 1024)
                (manager.files.root / "src" / "big.bin").write_bytes(big)
                big_target = device_root / "big.bin"
                started = time.perf_counter()
                big_result = await service(
                    {"path": "src/big.bin", "destination": str(big_target), "to_device": device_id},
                    "copy-64-mib",
                )
                elapsed = time.perf_counter() - started
                assert big_result["status"] == "succeeded"
                assert big_result["sha256"] == sha(big) == sha(big_target.read_bytes())
                record(
                    "throughput_64_mib",
                    size=len(big),
                    source_sha256=sha(big),
                    target_sha256=big_result["sha256"],
                    transfer_id="copy-64-mib",
                    seconds=round(elapsed, 3),
                    mib_per_second=round(len(big) / 1024 / 1024 / elapsed, 3),
                )
    finally:
        output = Path("test-results") / f"file-copy-{revision[:12]}.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )


def png_bytes() -> bytes:
    rng = random.Random(566)
    image = Image.frombytes("RGB", (64, 64), rng.randbytes(64 * 64 * 3))
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()
