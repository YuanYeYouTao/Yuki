"""Persistent global files and explicit event-bound attachment import."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import uuid4

from qq_ai_bot.conversation.media_service import ConversationMediaError, ConversationMediaService
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.sandbox.client import SandboxClient
from qq_ai_bot.services.media_resolver import MediaResolver
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
        self.visual_inspector: Any = None
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
        conversation_id: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        if name == "workspace_inspect":
            if self.visual_inspector is None:
                raise WorkspaceError("visual_inspection_unavailable")
            return dict(
                await self.visual_inspector(str(args["artifact_id"]), str(args["question"]))
            )
        if name in {"inspect_conversation_attachment", "save_conversation_attachment_to_workspace"}:
            media = self.conversation_media
            scope_id = conversation_id or (runtime.conversation_id if runtime else None)
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
                    return await media.inspect(item, path, str(args["question"]))
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
            return await asyncio.to_thread(self.store.read, str(args["artifact_id"]))
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
