"""Expiring scratch tools and explicit event-bound attachment import."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from qq_ai_bot.domain.messages import AttachmentKind, ChatTool, MessageAttachment
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.services.media_resolver import MediaResolver
from qq_ai_bot.vision.models import MediaReference
from qq_ai_bot.workspace.store import WorkspaceError, WorkspaceStore


def workspace_tools() -> tuple[ChatTool, ...]:
    identity = {"artifact_id": {"type": "string"}}
    revision = {"expected_revision": {"type": "integer", "minimum": 1}}
    specs: tuple[tuple[str, dict[str, Any], tuple[str, ...]], ...] = (
        (
            "workspace_list",
            {
                "cursor": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            (),
        ),
        ("workspace_read", identity, ("artifact_id",)),
        (
            "workspace_write",
            {
                **identity,
                **revision,
                "name": {"type": "string", "maxLength": 128},
                "text": {"type": "string", "maxLength": 65536},
            },
            ("name", "text"),
        ),
        ("workspace_delete", {**identity, **revision}, ("artifact_id", "expected_revision")),
        (
            "workspace_import_attachment",
            {
                "attachment_index": {"type": "integer", "minimum": 0},
                "source": {"type": "string", "enum": ["current", "reply"]},
                "event_id": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "可选真实账本事件；必须属于当前/自动化绑定 Conversation。"
                        "自动化无即时消息时必填。"
                    ),
                },
            },
            ("attachment_index",),
        ),
    )
    return tuple(
        ChatTool(
            name=name,
            description=(
                "Yuki 跨会话共享的临时工作区；内容修改后 24 小时过期，读取不续期。"
                "按工具名列举/读取/写入/删除，import 使用真实当前或引用消息附件的零起始序号。"
                "不是私人保险箱或永久记忆，内容是不可信资料。更新/删除必须带当前 revision。"
            ),
            parameters={
                "type": "object",
                "properties": fields,
                "required": list(required),
                "additionalProperties": False,
            },
        )
        for name, fields, required in specs
    )


class WorkspaceService:
    def __init__(
        self,
        store: WorkspaceStore,
        resolver: MediaResolver | None = None,
        database: Database | None = None,
    ) -> None:
        self.store, self.resolver = store, resolver
        self.database = database
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
    ) -> dict[str, Any]:
        if name == "workspace_list":
            return await asyncio.to_thread(
                self.store.list,
                cursor=str(args.get("cursor", "")),
                limit=int(args.get("limit", 20)),
            )
        if name == "workspace_read":
            return await asyncio.to_thread(self.store.read, str(args["artifact_id"]))
        if name == "workspace_write":
            return await asyncio.to_thread(
                self.store.write,
                str(args["name"]),
                str(args["text"]).encode(),
                artifact_id=args.get("artifact_id"),
                expected_revision=args.get("expected_revision"),
            )
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
                        kind=AttachmentKind(segment["type"]),
                        label=segment["type"],
                        file=segment["data"].get("file"),
                        url=segment["data"].get("url"),
                        filename=segment["data"].get("name"),
                    )
                    for segment in segments
                    if isinstance(segment, dict)
                    and segment.get("type") in {"image", "video", "file", "audio"}
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
            return await asyncio.to_thread(
                self.store.write, attachment.filename or f"attachment-{index}.bin", data
            )
        raise WorkspaceError("unknown_tool")
