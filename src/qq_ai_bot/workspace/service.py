"""Persistent global files and explicit event-bound attachment import."""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any
from uuid import uuid4

from qq_ai_bot.conversation.media_service import ConversationMediaError, ConversationMediaService
from qq_ai_bot.domain.messages import ChatImage
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.sandbox.client import SandboxClient
from qq_ai_bot.services.media_resolver import MediaResolver
from qq_ai_bot.workspace.inspect import WorkspaceInspector
from qq_ai_bot.workspace.store import WorkspaceError, WorkspaceStore
from qq_ai_bot.workspace.tools import workspace_tools as workspace_tools


class WorkspaceService:
    def __init__(
        self,
        store: WorkspaceStore,
        resolver: MediaResolver | None = None,
        database: Database | None = None,
    ) -> None:
        self.store, self.resolver = store, resolver
        self.database = database
        self.sandbox: SandboxClient | None = None
        self.visual_inspector: WorkspaceInspector | None = None
        self.conversation_media: ConversationMediaService | None = None
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

    async def upload_file(self, name: str, data: bytes, *, request_id: str) -> dict[str, Any]:
        """Publish operator bytes through the same store and environment checkout."""
        metadata = await asyncio.to_thread(self.store.write, name, data)
        return await self._checkout(metadata, request_id)

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
        if name == "workspace_inspect":
            if self.visual_inspector is None:
                raise WorkspaceError("visual_inspection_unavailable")
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
            if self.sandbox is None:
                raise WorkspaceError("environment_unavailable")
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
            media = self.conversation_media
            scope_id = runtime.effective_conversation_id if runtime is not None else None
            if media is None or scope_id is None or runtime is None:
                raise WorkspaceError("event_attachment_unavailable")
            try:
                item, path = await media.authorized_path(
                    event_id=int(args["event_id"]),
                    attachment_index=int(args["attachment_index"]),
                    conversation_id=scope_id,
                    generation=runtime.turn_snapshot.generation if runtime.turn_snapshot else None,
                    gateway=runtime.gateway,
                )
                if name == "inspect_conversation_attachment":
                    return await media.inspect(
                        item,
                        path,
                        str(args["question"]),
                        runtime=runtime.runtime_config.vision if runtime.runtime_config else None,
                    )
                if self.sandbox is None:
                    raise WorkspaceError("environment_unavailable")
                destination = str(args["destination"])
                data = await asyncio.to_thread(path.read_bytes)
                metadata = await asyncio.to_thread(self.store.write, destination, data)
                if request_id is None:
                    request_id = str(uuid4())
                return await self._checkout(metadata, request_id)
            except ConversationMediaError as exc:
                raise WorkspaceError(str(exc)) from exc
        if request_id is None:
            from hashlib import sha256

            from qq_ai_bot.capabilities.invocation import current_invocation

            invocation = current_invocation.get()
            request_id = (
                sha256(
                    (
                        f"workspace:{invocation.conversation_key}:"
                        f"{invocation.execution_key}:"
                        f"{invocation.call_id}"
                    ).encode()
                ).hexdigest()
                if invocation
                else str(uuid4())
            )
        if "path" in args and "artifact_id" in args:
            raise WorkspaceError("choose_path_or_artifact_id")
        if (
            name
            in {
                "workspace_mkdir",
                "workspace_move",
                "workspace_patch",
                "workspace_search",
                "workspace_publish",
            }
            or "path" in args
        ):
            if self.sandbox is None:
                raise WorkspaceError("environment_unavailable")
            return await self.sandbox.execute(name, args, request_id=request_id)

        if name == "workspace_list":
            return await asyncio.to_thread(
                self.store.list,
                cursor=str(args.get("cursor", "")),
                limit=int(args.get("limit", 20)),
            )
        if name == "workspace_read":
            return await asyncio.to_thread(
                self.store.read,
                str(args["artifact_id"]),
                offset=int(args.get("offset", 0)),
            )
        if name == "workspace_write":
            previous = (
                await asyncio.to_thread(self.store.read, args["artifact_id"])
                if args.get("artifact_id")
                else {}
            )
            metadata = await asyncio.to_thread(
                self.store.write,
                str(args["name"]),
                str(args["text"]).encode(),
                artifact_id=args.get("artifact_id"),
                expected_revision=args.get("expected_revision"),
            )
            return await self._checkout(metadata, request_id, previous.get("sha256"))
        if name == "workspace_delete":
            return await asyncio.to_thread(
                self.store.delete, str(args["artifact_id"]), int(args["expected_revision"])
            )
        raise WorkspaceError("unknown_tool")

    async def validate_images(self, images: tuple[ChatImage, ...]) -> None:
        """Execution-time authority and current source versions, without fetching pixels."""
        if any(image.source == "history" for image in images):
            if self.conversation_media is None:
                raise WorkspaceError("event_attachment_unavailable")
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
                if self.visual_inspector is None:
                    raise WorkspaceError("visual_inspection_unavailable")
                await self.visual_inspector.validate_artifact(image)
            elif image.workspace_path and image.version and self.sandbox:
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

    async def _checkout(
        self, metadata: dict[str, Any], request_id: str, previous_version: str | None = None
    ) -> dict[str, Any]:
        if self.sandbox is None:
            return metadata
        result = await self.sandbox.execute(
            "workspace_checkout",
            {
                "artifact_id": metadata["artifact_id"],
                "name": metadata["name"],
                "expected_version": previous_version,
            },
            request_id=request_id,
        )
        if result.get("error"):
            return {**metadata, "file_error": result["error"], "file_imported": False}
        return {**metadata, **result, "file_imported": True}
