"""Persistent files, selected media and internal delivery publication."""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from contextlib import AsyncExitStack
from typing import Any, BinaryIO
from uuid import uuid4

from yuki.conversation.media_service import ConversationMediaError, ConversationMediaService
from yuki.domain.messages import ChatImage
from yuki.sandbox.client import SandboxClient
from yuki.services.native_media import MAX_IMAGE_BYTES
from yuki.workspace.inspect import WorkspaceInspector
from yuki.workspace.store import WorkspaceError, WorkspaceStore
from yuki.workspace.tools import file_source
from yuki.workspace.tools import workspace_tools as workspace_tools


def _unconfirmed(result: dict[str, Any], sha256: str | None) -> dict[str, Any]:
    """Explain an unconfirmed computer write without turning it into a failure."""
    if not result.get("uncertain"):
        return result
    return {
        **result,
        **({"source_sha256": sha256} if sha256 is not None else {}),
        "message": "目标写入未确认，不要按失败重传同一调用。目标目录可能残留"
        " .yuki-transfer-*.part；可在该电脑终端核对目标文件的 sha256，"
        "确认未完成后再以新调用复制。",
    }


class WorkspaceService:
    def __init__(
        self,
        store: WorkspaceStore,
        *,
        sandbox: SandboxClient,
        visual_inspector: WorkspaceInspector,
        conversation_media: ConversationMediaService,
    ) -> None:
        self.store = store
        self.sandbox = sandbox
        self.visual_inspector = visual_inspector
        self.conversation_media = conversation_media
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await asyncio.to_thread(self.store.cleanup)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._cleanup(), name="workspace-cleanup")

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _cleanup(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                await asyncio.to_thread(self.store.cleanup)
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "workspace_cleanup_failed category=%s", type(exc).__name__
                )

    async def execute(
        self,
        name: str,
        args: dict[str, Any],
        *,
        runtime: Any = None,
        request_id: str | None = None,
        source: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if request_id is None:
            from hashlib import sha256

            from yuki.capabilities.invocation import current_invocation

            invocation = current_invocation.get()
            request_id = (
                sha256(
                    (
                        f"workspace:{invocation.runtime.conversation_key}:"
                        f"{invocation.execution_key}:"
                        f"{invocation.invocation_key}"
                    ).encode()
                ).hexdigest()
                if invocation
                else str(uuid4())
            )
        if name == "copy_file":
            chosen = file_source(args)
            return await self.copy_file(
                destination=str(args["destination"]),
                request_id=request_id,
                source=source,
                path=chosen.get("path"),
                artifact_id=chosen.get("artifact_id"),
                event_id=chosen.get("event_id"),
                attachment_index=chosen.get("attachment"),
                device=chosen.get("device"),
                to_device=args.get("to_device"),
            )
        if name != "view_file":
            raise WorkspaceError("unknown_tool")
        chosen = file_source(args)
        question = str(args.get("question") or "查看画面")
        vision = (
            runtime.runtime_config.vision
            if runtime is not None and runtime.runtime_config
            else None
        )
        if "event_id" in chosen:
            media = self.conversation_media
            try:
                item, cached = await media.authorized_path(
                    event_id=int(chosen["event_id"]),
                    attachment_index=int(chosen["attachment"]),
                )
                return await media.inspect(item, cached, question, runtime=vision)
            except ConversationMediaError as exc:
                raise WorkspaceError(str(exc)) from exc
        if "artifact_id" in chosen:
            return await self.visual_inspector(str(chosen["artifact_id"]), question, runtime=vision)
        path = str(chosen["path"])
        if "device" in chosen:
            device_id = str(chosen["device"])
            devices = self.sandbox.local_devices
            assert devices is not None
            async with devices.download(
                device_id,
                devices.actor(source),
                path,
                request_id=request_id,
                expected_version=None,
                max_bytes=MAX_IMAGE_BYTES,
            ) as (local, metadata):
                data = await asyncio.to_thread(local.read_bytes)
                prepared = await self.visual_inspector(
                    None,
                    question,
                    runtime=vision,
                    file_metadata={**metadata, "path": path, "version": metadata["sha256"]},
                    data=data,
                )
                prepared["device_id"] = device_id
                return prepared
        metadata = await self.sandbox.execute(
            "workspace_media_read",
            {"path": path, "expected_version": None},
            request_id=request_id or str(uuid4()),
        )
        if metadata.get("error"):
            raise WorkspaceError(str(metadata["error"]))
        encoded = metadata.pop("base64", "")
        if not isinstance(encoded, str) or len(encoded) > (20 * 1024 * 1024 + 2) // 3 * 4:
            raise WorkspaceError("workspace_media_too_large")
        try:
            data = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise WorkspaceError("workspace_media_invalid") from exc
        return await self.visual_inspector(
            None, question, runtime=vision, file_metadata=metadata, data=data
        )

    async def copy_file(
        self,
        *,
        destination: str,
        request_id: str,
        source: dict[str, Any] | None,
        path: str | None = None,
        artifact_id: str | None = None,
        event_id: int | None = None,
        attachment_index: int | None = None,
        device: str | None = None,
        to_device: str | None = None,
    ) -> dict[str, Any]:
        """Copy one original file byte-for-byte; its receipt is read before any source."""
        attachment = (
            (int(event_id), int(attachment_index))
            if event_id is not None and attachment_index is not None
            else None
        )
        if [path, artifact_id, attachment].count(None) != 2 or (
            attachment is None and (event_id is not None or attachment_index is not None)
        ):
            raise WorkspaceError("choose_one_source")
        # A source computer, and artifacts into the Yuki environment, need new
        # Manager methods; they are not served by another transfer path.
        if device is not None or (artifact_id is not None and to_device is None):
            raise WorkspaceError("source_not_supported_for_destination")
        media = self.conversation_media
        try:
            if to_device is None:
                if path is not None:
                    return await self.copy_from_cache(
                        path=path,
                        destination=destination,
                        request_id=request_id,
                        source_key=f"path:{path}",
                    )
                assert attachment is not None
                source_key = f"attachment:{attachment[0]}:{attachment[1]}"
                prior = await self.sandbox.execute(
                    "workspace_copy_result",
                    {"source_key": source_key, "destination": destination},
                    request_id=request_id,
                )
                if prior.get("error"):
                    return (
                        {**prior, "uncertain": True}
                        if prior["error"] == "sandbox_unavailable"
                        else prior
                    )
                if prior.get("found"):
                    result: dict[str, Any] = prior["result"]
                    return result
                item, cached = await media.authorized_path(
                    event_id=attachment[0], attachment_index=attachment[1]
                )
                return await self.copy_from_cache(
                    path="/inputs/media-cache/v1/" + cached.relative_to(media.root).as_posix(),
                    destination=destination,
                    expected_version=item.content_sha256,
                    request_id=request_id,
                    source_key=source_key,
                )
            devices = self.sandbox.local_devices
            assert devices is not None
            actor = devices.actor(source)
            if attachment is not None:
                await media._sources((attachment,))
            # transfer_id is the original invocation key: a recorded transfer is
            # returned as it stands, and only a transfer the computer never saw
            # reads its source.
            receipt = await devices.upload_result(
                to_device, actor, destination, request_id=request_id
            )
            if receipt is not None:
                return _unconfirmed(receipt, None)
            async with AsyncExitStack() as resources:
                if path is not None:
                    try:
                        header, reader, _ = await resources.enter_async_context(
                            self.sandbox.stream("files_read", {"path": path})
                        )
                    except OSError as exc:
                        raise WorkspaceError("sandbox_unavailable") from exc
                    except ValueError as exc:
                        raise WorkspaceError("source_file_unavailable") from exc
                    size, sha256 = int(header["size"]), str(header["sha256"])

                    async def stream(wanted: int) -> bytes:
                        try:
                            return await reader.readexactly(wanted)
                        except asyncio.IncompleteReadError as exc:
                            return exc.partial

                else:
                    opened: BinaryIO
                    if artifact_id is not None:
                        metadata, opened = await asyncio.to_thread(
                            self.store.open_blob, str(artifact_id)
                        )
                        resources.enter_context(opened)
                        if not metadata["immutable"]:
                            raise WorkspaceError("artifact_not_immutable")
                        size, sha256 = int(metadata["size"]), str(metadata["sha256"])
                    else:
                        assert attachment is not None
                        item, cached = await media.authorized_path(
                            event_id=attachment[0], attachment_index=attachment[1]
                        )
                        opened = resources.enter_context(cached.open("rb"))
                        size = os.fstat(opened.fileno()).st_size
                        sha256 = str(item.content_sha256)

                    async def stream(wanted: int) -> bytes:
                        return await asyncio.to_thread(opened.read, wanted)

                result = await devices.upload(
                    to_device,
                    actor,
                    stream,
                    size,
                    sha256,
                    destination,
                    request_id=request_id,
                )
            return _unconfirmed(result, sha256)
        except ConversationMediaError as exc:
            raise WorkspaceError(str(exc)) from exc

    async def validate_images(self, images: tuple[ChatImage, ...]) -> None:
        """Execution-time authority and current source versions, without fetching pixels."""
        if any(image.source == "history" for image in images):
            try:
                await self.conversation_media.validate_images(images)
            except ConversationMediaError as exc:
                raise WorkspaceError(str(exc)) from exc
        validated: set[tuple[str | None, str | None, str | None]] = set()
        for image in images:
            if image.source != "workspace":
                continue
            if image.content_hash != image.version:
                raise WorkspaceError("workspace_media_version_changed")
            dependency = (image.artifact_id, image.workspace_path, image.version)
            if dependency in validated:
                continue
            validated.add(dependency)
            if image.artifact_id:
                await self.visual_inspector.validate_artifact(image)
            elif image.workspace_path and image.version:
                result = await self.sandbox.execute(
                    "workspace_media_validate",
                    {
                        "path": image.workspace_path,
                        "expected_version": image.version,
                    },
                    request_id=str(uuid4()),
                )
                if result.get("error") or result.get("version") != image.version:
                    raise WorkspaceError("workspace_media_version_changed")
            else:
                raise WorkspaceError("workspace_source_missing")

    async def copy_from_cache(
        self,
        *,
        path: str,
        destination: str,
        request_id: str,
        source_key: str,
        expected_version: str | None = None,
    ) -> dict[str, Any]:
        result = await self.sandbox.execute(
            "workspace_copy",
            {
                "path": path,
                "destination": destination,
                "expected_version": expected_version,
                "source_key": source_key,
            },
            request_id=request_id,
        )
        return result

    async def publish_for_send(
        self,
        *,
        path: str,
        request_id: str,
        artifact_id: str,
        source_key: str,
        name: str | None = None,
        expected_version: str | None = None,
        device_id: str | None = None,
        actor_person_id: str | None = None,
    ) -> dict[str, Any]:
        """Freeze one original send through its selected original file owner."""
        if device_id is not None:
            devices = self.sandbox.local_devices
            assert devices is not None
            actor = devices.actor({"actor_person_id": actor_person_id})

            def frozen() -> dict[str, Any] | None:
                with self.store._transaction(write=False) as db:
                    row = db.execute(
                        "SELECT source_key FROM artifact_snapshots WHERE id=?",
                        (artifact_id,),
                    ).fetchone()
                    if row is None:
                        return None
                    if row[0] != source_key:
                        raise WorkspaceError("snapshot_identity_conflict")
                    return self.store._metadata(self.store._row(db, artifact_id))

            metadata = await asyncio.to_thread(frozen)
            if metadata is None:
                async with devices.download(
                    device_id,
                    actor,
                    path,
                    request_id=request_id,
                    expected_version=expected_version,
                ) as (selected, original):

                    def snapshot() -> dict[str, Any]:
                        with selected.open("rb") as stream:
                            return self.store.snapshot(
                                stream.fileno(),
                                name or original["name"],
                                artifact_id=artifact_id,
                                source_key=source_key,
                                expected_version=original["sha256"],
                            )

                    metadata = await asyncio.to_thread(snapshot)
            return {**metadata, "path": path, "version": metadata["sha256"], "device_id": device_id}
        result = await self.sandbox.execute(
            "workspace_publish",
            {
                "path": path,
                "artifact_id": artifact_id,
                "source_key": source_key,
                **({"name": name} if name is not None else {}),
                **({"expected_version": expected_version} if expected_version is not None else {}),
            },
            request_id=request_id,
        )
        return result
