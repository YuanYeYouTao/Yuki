"""Persistent files, selected media and internal delivery publication."""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any
from uuid import uuid4

from yuki.conversation.media_service import ConversationMediaError, ConversationMediaService
from yuki.domain.messages import ChatImage
from yuki.sandbox.client import SandboxClient
from yuki.workspace.inspect import WorkspaceInspector
from yuki.workspace.store import WorkspaceError, WorkspaceStore
from yuki.workspace.tools import workspace_tools as workspace_tools


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
                        f"{invocation.call_id}"
                    ).encode()
                ).hexdigest()
                if invocation
                else str(uuid4())
            )
        if name == "workspace_inspect":
            if bool(args.get("artifact_id")) == bool(args.get("path")):
                raise WorkspaceError("choose_path_or_artifact_id")
            vision = (
                runtime.runtime_config.vision
                if runtime is not None and runtime.runtime_config
                else None
            )
            if args.get("artifact_id"):
                return await self.visual_inspector(
                    str(args["artifact_id"]), str(args["question"]), runtime=vision
                )
            metadata = await self.sandbox.execute(
                "workspace_media_read",
                {
                    "path": str(args["path"]),
                    "expected_version": args.get("expected_version"),
                },
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
                None, str(args["question"]), runtime=vision, file_metadata=metadata, data=data
            )
        if name in {"inspect_conversation_attachment", "save_conversation_attachment_to_workspace"}:
            source_key = f"attachment:{int(args['event_id'])}:{int(args['attachment_index'])}"
            if name == "save_conversation_attachment_to_workspace":
                prior = await self.sandbox.execute(
                    "workspace_copy_result",
                    {"source_key": source_key, "destination": str(args["destination"])},
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
            media = self.conversation_media
            try:
                item, path = await media.authorized_path(
                    event_id=int(args["event_id"]),
                    attachment_index=int(args["attachment_index"]),
                )
                if name == "inspect_conversation_attachment":
                    return await media.inspect(
                        item,
                        path,
                        str(args["question"]),
                        runtime=(
                            runtime.runtime_config.vision
                            if runtime is not None and runtime.runtime_config
                            else None
                        ),
                    )
                return await self.copy_from_cache(
                    path="/inputs/media-cache/v1/" + path.relative_to(media.root).as_posix(),
                    destination=str(args["destination"]),
                    expected_version=item.content_sha256,
                    request_id=request_id,
                    source_key=source_key,
                )
            except ConversationMediaError as exc:
                raise WorkspaceError(str(exc)) from exc
        raise WorkspaceError("unknown_tool")

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
    ) -> dict[str, Any]:
        """Freeze one original send through the Manager's durable file request."""
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
