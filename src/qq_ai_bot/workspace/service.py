"""Persistent global files and explicit event-bound attachment import."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

from qq_ai_bot.domain.messages import AttachmentKind, MessageAttachment
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.sandbox.client import SandboxClient
from qq_ai_bot.services.media_resolver import MediaResolver
from qq_ai_bot.vision.models import MediaReference
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
        if request_id is None:
            from hashlib import sha256

            from qq_ai_bot.capabilities.invocation import current_invocation

            invocation = current_invocation.get()
            execution = getattr(invocation.runtime, "execution_id", None) if invocation else None
            request_id = (
                sha256(
                    (
                        f"workspace:{invocation.conversation_key}:"
                        f"{execution or invocation.trigger_message_id}:"
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
        if name == "workspace_import_attachment":
            if self.resolver is None:
                raise WorkspaceError("event_attachment_unavailable")
            source = str(args.get("source", "current"))
            if args.get("event_id"):
                scope_id = conversation_id or (runtime.conversation_id if runtime else None)
                if not scope_id or self.database is None or source != "current":
                    raise WorkspaceError("event_attachment_unavailable")
                async with self.database.sessions() as session:
                    event = await session.get(ChatEventModel, int(args["event_id"]))
                    if (
                        event is None
                        or event.canonical_conversation_id != scope_id
                        or event.direction != "inbound"
                    ):
                        raise WorkspaceError("event_attachment_not_allowed")
                    segments = json.loads(event.segments_json)
                attachments = tuple(
                    MessageAttachment(
                        kind=AttachmentKind(
                            "audio" if segment["type"] == "record" else segment["type"]
                        ),
                        label=segment["type"],
                        file=segment["data"].get("file"),
                        url=segment["data"].get("url"),
                        filename=segment["data"].get("name"),
                    )
                    for segment in segments
                    if isinstance(segment, dict)
                    and segment.get("type") in {"image", "video", "file", "audio", "record"}
                    and isinstance(segment.get("data"), dict)
                )
            else:
                if runtime is None or runtime.inbound is None:
                    raise WorkspaceError("event_attachment_unavailable")
                attachments = (
                    runtime.inbound.attachments
                    if source == "current"
                    else runtime.inbound.reply_attachments
                )
            index = int(args["attachment_index"])
            if not 0 <= index < len(attachments):
                raise WorkspaceError("attachment_not_found")
            attachment = attachments[index]
            limit = (
                200 * 1024 * 1024 if attachment.kind is AttachmentKind.VIDEO else 20 * 1024 * 1024
            )
            with TemporaryDirectory(prefix="yuki-import-") as temporary:
                path = Path(temporary) / "attachment"
                await self.resolver.download_attachment(
                    MediaReference(
                        file=attachment.file,
                        url=attachment.url,
                        source="reply" if source == "reply" else "current",
                    ),
                    path,
                    max_download_bytes=limit,
                )
                data = await asyncio.to_thread(path.read_bytes)
            metadata = await asyncio.to_thread(
                self.store.write, attachment.filename or f"attachment-{index}.bin", data
            )
            return await self._checkout(metadata, request_id)
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
