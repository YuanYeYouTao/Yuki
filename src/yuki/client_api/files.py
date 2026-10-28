"""Media upload authorization and exact-version artifact downloads."""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
from pathlib import Path
from typing import Any

from sqlalchemy import func, literal_column, or_, select, text, tuple_, union_all

from yuki.client_api.errors import ClientError
from yuki.client_api.identity import ClientPrincipal
from yuki.client_api.pagination import cursor, decode_cursor
from yuki.client_api.views import ClientViews, personal_conversation
from yuki.client_api.works import work_access
from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.conversation.media_service import ConversationMediaError
from yuki.conversation.uploads import ConversationUploads, owned_upload
from yuki.domain.messages import AttachmentKind, MessageAttachment
from yuki.identity.db_models import IdentityBindingModel
from yuki.identity.person_identity import person_family_condition
from yuki.persistence.models import ChatEventArtifactRefModel, ChatEventModel
from yuki.plugin_host.db_models import PluginNotificationOutboxModel
from yuki.runtime.work_recovery_schema import deliveries
from yuki.runtime.work_schema_v1 import effects, work
from yuki.social.db_models import SocialOperationModel
from yuki.social.history_sources import history_delivery_predicates
from yuki.time.formatting import utc_iso
from yuki.workspace.store import WorkspaceError, WorkspaceStore


class ClientFiles:
    def __init__(
        self, uploads: ConversationUploads, views: ClientViews, store: WorkspaceStore
    ) -> None:
        self.uploads, self.views, self.store = uploads, views, store
        self.identity = uploads.identity

    def _references(self, person_id: str) -> Any:
        parts = func.json_each(ChatEventModel.segments_json).table_valued(
            "key", "value", joins_implicitly=True
        )
        artifact = func.json_extract(parts.c.value, "$.data.artifact_id")
        version = func.json_extract(parts.c.value, "$.data.version")
        social = (
            select(SocialOperationModel.id)
            .where(
                SocialOperationModel.event_id == ChatEventModel.id,
                SocialOperationModel.status == "succeeded",
            )
            .correlate(ChatEventModel)
        )
        published = or_(
            social.exists(),
            select(PluginNotificationOutboxModel.id)
            .where(
                PluginNotificationOutboxModel.event_id == ChatEventModel.id,
                PluginNotificationOutboxModel.status == "sent",
            )
            .correlate(ChatEventModel)
            .exists(),
        )
        linked = union_all(
            select(deliveries.c.work_id).where(deliveries.c.id.in_(social)),
            select(effects.c.work_id).where(
                func.json_extract(
                    effects.c.receipt_json, literal_column("'$.invocation.original_domain_ref'")
                ).in_(
                    select("social:" + SocialOperationModel.id)
                    .where(
                        SocialOperationModel.event_id == ChatEventModel.id,
                        SocialOperationModel.status == "succeeded",
                    )
                    .correlate(ChatEventModel)
                )
            ),
        )
        visible_work = (
            select(work.c.id)
            .join(
                CanonicalConversationModel, CanonicalConversationModel.id == work.c.conversation_id
            )
            .where(work.c.id.in_(linked), work_access(person_id))
            .correlate(ChatEventModel)
        )
        private = (
            select(CanonicalConversationModel.id)
            .where(
                CanonicalConversationModel.id == ChatEventModel.canonical_conversation_id,
                CanonicalConversationModel.kind == "private",
                person_family_condition(CanonicalConversationModel.person_id, person_id),
            )
            .exists()
        )
        query = (
            select(
                ChatEventModel.id.label("event_id"),
                parts.c.key.label("part_index"),
                parts.c.value.label("segment"),
                ChatEventModel.canonical_conversation_id.label("conversation_id"),
                ChatEventModel.observed_at.label("created_at"),
                visible_work.order_by(work.c.id).limit(1).scalar_subquery().label("work_id"),
            )
            .select_from(ChatEventModel, parts)
            .where(
                ChatEventModel.direction == "outbound",
                ChatEventModel.author_kind == "yuki",
                ChatEventModel.suppression_status == "keeper",
                published,
                or_(private, visible_work.exists()),
                artifact.is_not(None),
                version.is_not(None),
                select(ChatEventArtifactRefModel.event_id)
                .where(
                    ChatEventArtifactRefModel.event_id == ChatEventModel.id,
                    ChatEventArtifactRefModel.artifact_id == artifact,
                    ChatEventArtifactRefModel.version == version,
                )
                .correlate(ChatEventModel, parts)
                .exists(),
            )
        )
        return query, parts

    async def list(
        self, principal: ClientPrincipal, before: str | None, limit: int, kind: str
    ) -> dict[str, Any]:
        position = decode_cursor(before) if before else None
        if (
            limit < 1
            or kind not in {"all", "image", "document", "code"}
            or (
                position is not None
                and (
                    not isinstance(position, list)
                    or len(position) != 2
                    or any(type(value) is not int for value in position)
                )
            )
        ):
            raise ClientError("validation_error")
        items: list[dict[str, Any]] = []
        next_cursor = None
        while len(items) < limit:
            async with self.identity.database.read_session() as session:
                await session.execute(text("BEGIN"))
                current = await self.identity.require_principal(session, principal)
                query, parts = self._references(current.person_id)
                if position is not None:
                    query = query.where(tuple_(ChatEventModel.id, parts.c.key) < tuple(position))
                rows = list(
                    (
                        await session.execute(
                            query.order_by(ChatEventModel.id.desc(), parts.c.key.desc()).limit(
                                limit + 1
                            )
                        )
                    ).mappings()
                )
            if not rows:
                break
            consumed = 0
            for row in rows[:limit]:
                consumed += 1
                position = [row["event_id"], row["part_index"]]
                data = json.loads(row["segment"])["data"]
                name = data.get("filename") or data.get("name") or data["artifact_id"]
                media_type = (
                    data.get("mime_type")
                    or mimetypes.guess_type(name)[0]
                    or "application/octet-stream"
                )
                code = Path(name).suffix.lower() in {
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
                category = (
                    "image" if media_type.startswith("image/") else "code" if code else "document"
                )
                if kind != "all" and kind != category:
                    continue
                items.append(
                    {
                        "ref": {"event_id": row["event_id"], "part_index": row["part_index"]},
                        "artifact_id": data["artifact_id"],
                        "version": data["version"],
                        "name": name,
                        "size": data.get("size", data.get("file_size")),
                        "media_type": media_type,
                        "conversation_id": row["conversation_id"],
                        "work_id": row["work_id"],
                        "published_path": None,
                        "created_at": utc_iso(row["created_at"]),
                    }
                )
                if len(items) == limit:
                    break
            next_cursor = cursor(position) if consumed < len(rows) else None
            if next_cursor is None:
                break
        if items:
            async with self.identity.database.read_session() as session:
                current = await self.identity.require_principal(session, principal)
                query, parts = self._references(current.person_id)
                references = [
                    (item["ref"]["event_id"], item["ref"]["part_index"]) for item in items
                ]
                live = (
                    await session.execute(
                        query.where(tuple_(ChatEventModel.id, parts.c.key).in_(references))
                    )
                ).mappings()
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

    async def _event_source(
        self, principal: ClientPrincipal, conversation_id: str, event_id: int
    ) -> str:
        async with self.identity.database.read_session() as session:
            await session.execute(text("BEGIN"))
            principal = await self.identity.require_principal(session, principal)
            conversation = await personal_conversation(
                session, principal.person_id, conversation_id
            )
            binding = await session.get(IdentityBindingModel, conversation.identity_binding_id)
            assert binding is not None
            source = await session.scalar(
                select(ChatEventModel.segments_json).where(
                    ChatEventModel.id == event_id,
                    ChatEventModel.canonical_conversation_id == conversation_id,
                    ChatEventModel.suppression_status == "keeper",
                    *history_delivery_predicates(
                        (conversation_id,) if binding.platform == "acp" else ()
                    ),
                )
            )
            if source is None:
                raise ClientError("not_found")
            return source

    async def attachment(
        self, principal: ClientPrincipal, conversation_id: str, event_id: int, index: int
    ) -> tuple[dict[str, Any], bytes]:
        source = await self._event_source(principal, conversation_id, event_id)
        try:
            item, path = await self.uploads.media.authorized_path(
                event_id=event_id, attachment_index=index, expected_source_segments=source
            )
            content = await asyncio.to_thread(path.read_bytes)
        except (ConversationMediaError, OSError):
            raise ClientError("not_found") from None
        digest = hashlib.sha256(content).hexdigest()
        if digest != item.content_sha256:
            raise ClientError("not_found")
        if await self._event_source(principal, conversation_id, event_id) != source:
            raise ClientError("not_found")
        return {
            "sha256": digest,
            "name": item.display_name or f"attachment-{index}",
            "expires_at": utc_iso(item.expires_at) if item.expires_at is not None else None,
        }, content

    async def attachments(
        self, principal: ClientPrincipal, references: tuple[str, ...]
    ) -> tuple[tuple[dict[str, Any], ...], tuple[MessageAttachment, ...]]:
        segments, attachments = [], []
        async with self.identity.database.read_session() as session:
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
        async with self.identity.database.read_session() as session:
            await session.execute(text("BEGIN"))
            principal = await self.identity.require_principal(session, principal)
            exact_events = select(ChatEventArtifactRefModel.event_id).where(
                ChatEventArtifactRefModel.artifact_id == identity,
                ChatEventArtifactRefModel.version == version,
            )
            original_social = (
                select(SocialOperationModel.id)
                .where(
                    SocialOperationModel.event_id == ChatEventModel.id,
                    SocialOperationModel.status == "succeeded",
                )
                .correlate(ChatEventModel)
            )
            linked_works = union_all(
                select(deliveries.c.work_id).where(deliveries.c.id.in_(original_social)),
                select(effects.c.work_id).where(
                    func.json_extract(
                        effects.c.receipt_json,
                        literal_column("'$.invocation.original_domain_ref'"),
                    ).in_(
                        select("social:" + SocialOperationModel.id)
                        .where(
                            SocialOperationModel.event_id == ChatEventModel.id,
                            SocialOperationModel.status == "succeeded",
                        )
                        .correlate(ChatEventModel)
                    )
                ),
            )
            private_visible = (
                select(CanonicalConversationModel.id)
                .where(
                    CanonicalConversationModel.id == ChatEventModel.canonical_conversation_id,
                    CanonicalConversationModel.kind == "private",
                    person_family_condition(
                        CanonicalConversationModel.person_id, principal.person_id
                    ),
                )
                .exists()
            )
            work_visible = (
                select(work.c.id)
                .join(
                    CanonicalConversationModel,
                    CanonicalConversationModel.id == work.c.conversation_id,
                )
                .where(work.c.id.in_(linked_works), work_access(principal.person_id))
                .exists()
            )
            if (
                await session.scalar(
                    select(ChatEventModel.id)
                    .where(
                        ChatEventModel.id.in_(exact_events),
                        ChatEventModel.direction == "outbound",
                        ChatEventModel.author_kind == "yuki",
                        ChatEventModel.suppression_status == "keeper",
                        or_(private_visible, work_visible),
                    )
                    .limit(1)
                )
                is not None
            ):
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
