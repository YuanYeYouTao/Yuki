"""Media upload authorization and exact-version artifact downloads."""

from __future__ import annotations

import asyncio
import json
import mimetypes
from pathlib import Path
from typing import Any

from sqlalchemy import func, or_, select, text, tuple_

from yuki.client_api.errors import ClientError
from yuki.client_api.identity import ClientPrincipal
from yuki.client_api.result_events import confirmed_outbound, visible_outbound, visible_result_work
from yuki.client_api.views import ClientViews, personal_conversation
from yuki.client_api.work_projection import cursor, decode_cursor
from yuki.conversation.media_service import ConversationMediaError
from yuki.conversation.uploads import ConversationUploads, owned_upload
from yuki.domain.messages import AttachmentKind, MessageAttachment
from yuki.persistence.models import ChatEventModel, ConversationMediaItemModel
from yuki.runtime.work_schema_v1 import effects, work
from yuki.time.formatting import utc_iso
from yuki.workspace.store import WorkspaceError, WorkspaceStore


class ClientFiles:
    def __init__(
        self, uploads: ConversationUploads, views: ClientViews, store: WorkspaceStore
    ) -> None:
        self.uploads, self.views, self.store = uploads, views, store
        self.identity = uploads.identity

    def _references(self, person: str) -> Any:
        parts = func.json_each(ChatEventModel.segments_json).table_valued(
            "key", "value", joins_implicitly=True
        )
        artifact = func.json_extract(parts.c.value, "$.data.artifact_id")
        version = func.json_extract(parts.c.value, "$.data.version")
        query = (
            select(
                ChatEventModel.id.label("event_id"),
                parts.c.key.label("part_index"),
                parts.c.value.label("segment"),
                ChatEventModel.canonical_conversation_id.label("conversation_id"),
                ChatEventModel.observed_at.label("created_at"),
                visible_result_work(person)
                .order_by(work.c.id)
                .limit(1)
                .scalar_subquery()
                .label("work_id"),
            )
            .select_from(ChatEventModel, parts)
            .where(
                *confirmed_outbound(),
                visible_outbound(person),
                artifact.is_not(None),
                version.is_not(None),
            )
        )
        return query, artifact, version, parts

    async def list(
        self, principal: ClientPrincipal, before: str | None, limit: int, kind: str
    ) -> dict[str, Any]:
        position = decode_cursor(before) if before else None
        if position is not None and (
            not isinstance(position, list)
            or len(position) != 2
            or any(type(value) is not int for value in position)
        ):
            raise ClientError("validation_error")
        items: list[dict[str, Any]] = []
        next_cursor = None
        while len(items) < limit:
            async with self.identity.database.sessions() as session:
                await session.execute(text("BEGIN"))
                current = await self.identity.require_principal(session, principal)
                query, _, _, parts = self._references(current.person_id)
                if position is not None:
                    query = query.where(tuple_(ChatEventModel.id, parts.c.key) < tuple(position))
                rows = (
                    (
                        await session.execute(
                            query.order_by(ChatEventModel.id.desc(), parts.c.key.desc()).limit(
                                limit + 1
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
                paths = {}
                for row in rows[:limit]:
                    segment = json.loads(row["segment"])["data"]
                    published = await session.scalar(
                        select(effects.c.receipt_json)
                        .where(
                            func.json_extract(effects.c.receipt_json, "$.invocation.tool_id")
                            == "workspace_publish",
                            or_(
                                func.json_extract(
                                    effects.c.receipt_json, "$.result.data.artifact_id"
                                )
                                == segment["artifact_id"],
                                func.json_extract(effects.c.receipt_json, "$.result.artifact_id")
                                == segment["artifact_id"],
                            ),
                        )
                        .order_by(effects.c.created)
                        .limit(1)
                    )
                    if published:
                        result = json.loads(published).get("result", {})
                        paths[(row["event_id"], row["part_index"])] = result.get(
                            "data", result
                        ).get("path")
            if not rows:
                next_cursor = None
                break
            consumed = 0
            for row in rows[:limit]:
                consumed += 1
                position = [row["event_id"], row["part_index"]]
                segment = json.loads(row["segment"])["data"]
                try:
                    metadata = await asyncio.to_thread(self.store.metadata, segment["artifact_id"])
                except (WorkspaceError, OSError):
                    metadata = {}
                name = segment.get("filename") or metadata.get("name") or segment["artifact_id"]
                media_type = (
                    segment.get("mime_type")
                    or mimetypes.guess_type(name)[0]
                    or "application/octet-stream"
                )
                suffix = Path(name).suffix.lower()
                is_code = suffix in {
                    ".py",
                    ".js",
                    ".ts",
                    ".tsx",
                    ".jsx",
                    ".rs",
                    ".go",
                    ".sh",
                    ".json",
                    ".yaml",
                    ".yml",
                    ".html",
                    ".css",
                    ".sql",
                    ".java",
                    ".c",
                    ".cpp",
                }
                if (
                    (kind == "image" and not media_type.startswith("image/"))
                    or (kind == "code" and not is_code)
                    or (kind == "document" and (media_type.startswith("image/") or is_code))
                ):
                    continue
                items.append(
                    {
                        "ref": {"event_id": row["event_id"], "part_index": row["part_index"]},
                        "artifact_id": segment["artifact_id"],
                        "version": segment["version"],
                        "name": name,
                        "size": segment.get("size", segment.get("file_size", metadata.get("size"))),
                        "media_type": media_type,
                        "conversation_id": row["conversation_id"],
                        "work_id": row["work_id"],
                        "published_path": paths.get((row["event_id"], row["part_index"])),
                        "created_at": utc_iso(row["created_at"]),
                    }
                )
                if len(items) == limit:
                    break
            has_more = consumed < len(rows)
            next_cursor = cursor(position) if has_more else None
            if not has_more:
                break
        if items:
            async with self.identity.database.sessions() as session:
                current = await self.identity.require_principal(session, principal)
                query, _, _, parts = self._references(current.person_id)
                refs = [(item["ref"]["event_id"], item["ref"]["part_index"]) for item in items]
                live = (
                    (
                        await session.execute(
                            query.where(tuple_(ChatEventModel.id, parts.c.key).in_(refs))
                        )
                    )
                    .mappings()
                    .all()
                )
                valid = {
                    (
                        row["event_id"],
                        row["part_index"],
                        json.loads(row["segment"])["data"]["artifact_id"],
                        json.loads(row["segment"])["data"]["version"],
                    )
                    for row in live
                }
                items = [
                    item
                    for item in items
                    if (
                        item["ref"]["event_id"],
                        item["ref"]["part_index"],
                        item["artifact_id"],
                        item["version"],
                    )
                    in valid
                ]
        return {"items": items, "next_cursor": next_cursor}

    async def _attachment_event(
        self, principal: ClientPrincipal, conversation: str, event_id: int
    ) -> None:
        async with self.identity.database.sessions() as session:
            current = await self.identity.require_principal(session, principal)
            await personal_conversation(session, current.person_id, conversation)
            event = await session.get(ChatEventModel, event_id)
            if (
                event is None
                or event.canonical_conversation_id != conversation
                or event.suppression_status != "keeper"
                or event.direction != "inbound"
            ):
                raise ClientError("not_found")

    async def attachment_content(
        self, principal: ClientPrincipal, conversation: str, event_id: int, index: int
    ) -> tuple[ConversationMediaItemModel, Path]:
        await self._attachment_event(principal, conversation, event_id)
        try:
            item, path = await self.uploads.media.personal_path(event_id, index)
        except ConversationMediaError as exc:
            code = str(exc)
            if code == "attachment_not_found":
                raise ClientError("not_found") from None
            reason = (
                "source_changed"
                if code in {"attachment_source_changed", "attachment_source_invalid"}
                else "expired"
                if code == "attachment_expired"
                else "cleaned"
                if code == "cache_missing"
                else "source_unavailable"
            )
            raise ClientError("media_unavailable", details={"reason": reason}) from None
        except OSError:
            raise ClientError(
                "media_unavailable", details={"reason": "source_unavailable"}
            ) from None
        await self._attachment_event(principal, conversation, event_id)
        return item, path

    async def attachments(
        self, principal: ClientPrincipal, references: tuple[str, ...]
    ) -> tuple[tuple[dict[str, Any], ...], tuple[MessageAttachment, ...]]:
        segments, attachments = [], []
        async with self.identity.database.sessions() as session:
            principal = await self.identity.require_principal(session, principal)
            for index, identity in enumerate(references):
                item = await owned_upload(session, principal.person_id, identity)
                kind = item["kind"]
                segments.append(
                    {
                        "type": kind,
                        "data": {
                            "media_id": identity,
                            "filename": item["filename"],
                            "file_size": item["size"],
                        },
                    }
                )
                attachments.append(
                    MessageAttachment(
                        kind=AttachmentKind.AUDIO if kind == "record" else AttachmentKind(kind),
                        label=item["filename"],
                        segment_index=index + 1,
                        filename=item["filename"],
                        file_size=item["size"],
                        local_source=(self.uploads.path(item["sha256"]), item["sha256"]),
                    )
                )
        return tuple(segments), tuple(attachments)

    async def _authorized(self, principal: ClientPrincipal, identity: str, version: str) -> None:
        async with self.identity.database.sessions() as session:
            await session.execute(text("BEGIN"))
            principal = await self.identity.require_principal(session, principal)
            query, artifact, content_version, _parts = self._references(principal.person_id)
            found = await session.scalar(
                query.where(artifact == identity, content_version == version)
                .with_only_columns(ChatEventModel.id)
                .limit(1)
            )
            if found is not None:
                return
        raise ClientError("not_found")

    async def artifact(
        self, principal: ClientPrincipal, identity: str, version: str
    ) -> tuple[dict[str, Any], bytes]:
        await self._authorized(principal, identity, version)
        try:
            metadata, data = await asyncio.to_thread(self.store.read_bytes, identity)
        except (WorkspaceError, OSError):
            raise ClientError("not_found") from None
        if metadata["sha256"] != version or not metadata["immutable"]:
            raise ClientError("not_found")
        await self._authorized(principal, identity, version)
        return metadata, data
