"""Media upload authorization and exact-version artifact downloads."""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import func, literal_column, or_, select, text, union_all

from yuki.client_api.errors import ClientError
from yuki.client_api.identity import ClientPrincipal
from yuki.client_api.views import ClientViews
from yuki.client_api.works import work_access
from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.conversation.uploads import ConversationUploads, owned_upload
from yuki.domain.messages import AttachmentKind, MessageAttachment
from yuki.identity.person_identity import person_family_condition
from yuki.persistence.models import ChatEventArtifactRefModel, ChatEventModel
from yuki.runtime.work_recovery_schema import deliveries
from yuki.runtime.work_schema_v1 import effects, work
from yuki.social.db_models import SocialOperationModel
from yuki.workspace.store import WorkspaceError, WorkspaceStore


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
