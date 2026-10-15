"""Media upload authorization and exact-version artifact downloads."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from sqlalchemy import select, text

from qq_ai_bot.client_api.errors import ClientError
from qq_ai_bot.client_api.identity import ClientPrincipal
from qq_ai_bot.client_api.views import ClientViews
from qq_ai_bot.client_api.works import work_access
from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.conversation.uploads import ConversationUploads, owned_upload
from qq_ai_bot.domain.messages import AttachmentKind, MessageAttachment
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.runtime.work_schema_v1 import work
from qq_ai_bot.workspace.store import WorkspaceError, WorkspaceStore


class ClientFiles:
    def __init__(
        self, uploads: ConversationUploads, views: ClientViews, store: WorkspaceStore
    ) -> None:
        self.uploads, self.views, self.store = uploads, views, store
        self.identity = uploads.identity

    async def attachments(
        self, principal: ClientPrincipal, references: tuple[str, ...]
    ) -> tuple[tuple[dict[str, Any], ...], tuple[MessageAttachment, ...]]:
        segments, attachments = [], []
        async with self.identity.database.sessions() as session:
            principal = await self.identity.require_device(session, principal.device_id)
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
            principal = await self.identity.require_device(session, principal.device_id)
            # The exact artifact and content version must occur together in a visible outbound.
            rows = await session.scalars(
                select(ChatEventModel.segments_json)
                .join(
                    CanonicalConversationModel,
                    CanonicalConversationModel.id == ChatEventModel.canonical_conversation_id,
                )
                .where(
                    CanonicalConversationModel.kind == "private",
                    CanonicalConversationModel.person_id == principal.person_id,
                    ChatEventModel.direction == "outbound",
                    ChatEventModel.author_kind == "yuki",
                    ChatEventModel.suppression_status == "keeper",
                )
            )
            for body in rows:
                if any(
                    part.get("data", {}).get("artifact_id") == identity
                    and part["data"].get("version") == version
                    for part in json.loads(body)
                ):
                    return
            works = (
                (
                    await session.execute(
                        select(work)
                        .join(
                            CanonicalConversationModel,
                            CanonicalConversationModel.id == work.c.conversation_id,
                        )
                        .where(work_access(principal.person_id))
                    )
                )
                .mappings()
                .all()
            )
            for row in works:
                projection = await self.views.works._public(session, dict(row))
                if any(
                    artifact.get("artifact_id") == identity and artifact.get("version") == version
                    for event in projection["results"]
                    for artifact in event["artifacts"]
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
