"""Safe personal ledger and activity projections from original sources."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.client_api.errors import ClientError
from yuki.client_api.identity import ClientIdentityService, ClientPrincipal
from yuki.client_api.works import ClientWorks, work_access
from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.execution_trace.db_models import ExecutionTraceEntryModel
from yuki.execution_trace.recorder import TraceRecorder
from yuki.identity.db_models import IdentityBindingModel
from yuki.identity.person_identity import person_family_condition
from yuki.persistence.models import ChatEventModel
from yuki.runtime.work_schema_v1 import work
from yuki.social.db_models import SocialOperationModel


async def personal_conversation(
    session: AsyncSession, person: str, identity: str
) -> CanonicalConversationModel:
    row = await session.scalar(
        select(CanonicalConversationModel).where(
            CanonicalConversationModel.id == identity,
            CanonicalConversationModel.kind == "private",
            person_family_condition(CanonicalConversationModel.person_id, person),
        )
    )
    if row is None:
        raise ClientError("not_found")
    return row


def public_event(row: ChatEventModel, binding: IdentityBindingModel) -> dict[str, Any]:
    attachments = []
    for segment in json.loads(row.segments_json):
        if segment["type"] in {"image", "audio", "record", "video", "file"}:
            data = segment.get("data", {})
            attachments.append(
                {
                    "kind": segment["type"],
                    **{
                        key: data[key]
                        for key in (
                            "media_id",
                            "artifact_id",
                            "version",
                            "filename",
                            "mime_type",
                            "size",
                        )
                        if key in data
                    },
                }
            )
    return {
        "event_id": row.id,
        "conversation_id": row.canonical_conversation_id,
        "binding_id": binding.id,
        "platform": binding.platform,
        "direction": row.direction,
        "author_kind": row.author_kind,
        "text": row.content,
        "origin": row.origin,
        "created_at": row.observed_at.isoformat(),
        "attachments": attachments,
    }


class ClientViews:
    def __init__(
        self,
        identity: ClientIdentityService,
        works: ClientWorks,
        traces: TraceRecorder | None = None,
    ) -> None:
        self.identity, self.works, self.traces = identity, works, traces
        self.database = identity.database

    async def _entries(
        self, session: AsyncSession, principal: ClientPrincipal
    ) -> list[dict[str, Any]]:
        rows = (
            await session.execute(
                select(CanonicalConversationModel, IdentityBindingModel)
                .join(
                    IdentityBindingModel,
                    CanonicalConversationModel.identity_binding_id == IdentityBindingModel.id,
                )
                .where(
                    CanonicalConversationModel.kind == "private",
                    person_family_condition(
                        CanonicalConversationModel.person_id, principal.person_id
                    ),
                )
                .order_by(IdentityBindingModel.created_at, IdentityBindingModel.id)
            )
        ).all()
        return [
            {
                "conversation_id": row.id,
                "binding_id": binding.id,
                "platform": binding.platform,
                "display_name": binding.display_name,
                "status": binding.status,
                "generation": row.generation,
                "latest_event_id": row.last_event_id,
                "revision": row.prompt_source_revision,
            }
            for row, binding in rows
        ]

    async def home(self, principal: ClientPrincipal) -> dict[str, Any]:
        async with self.database.sessions() as session:
            await session.execute(text("BEGIN"))
            principal = await self.identity.require_principal(session, principal)
            entries = await self._entries(session, principal)
            rows = (
                (
                    await session.execute(
                        select(work)
                        .join(
                            CanonicalConversationModel,
                            CanonicalConversationModel.id == work.c.conversation_id,
                        )
                        .where(work_access(principal.person_id))
                        .order_by(work.c.updated.desc(), work.c.id)
                    )
                )
                .mappings()
                .all()
            )
            return {
                "main": next(
                    item for item in entries if item["conversation_id"] == principal.conversation_id
                ),
                "entries": [
                    item for item in entries if item["conversation_id"] != principal.conversation_id
                ],
                "works": await self.works.public_many(session, [dict(row) for row in rows]),
                "snapshot_at": datetime.now(UTC).isoformat(),
            }

    async def events(
        self,
        principal: ClientPrincipal,
        identity: str,
        *,
        after: int = 0,
        before: int | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 50 or after < 0 or (before is not None and before < 1):
            raise ClientError("validation_error")
        async with self.database.sessions() as session:
            await session.execute(text("BEGIN"))
            principal = await self.identity.require_principal(session, principal)
            conversation = await personal_conversation(session, principal.person_id, identity)
            binding = await session.get(IdentityBindingModel, conversation.identity_binding_id)
            assert binding is not None
            query = select(ChatEventModel).where(
                ChatEventModel.canonical_conversation_id == identity,
                ChatEventModel.suppression_status == "keeper",
                ChatEventModel.id > after,
            )
            if binding.platform == "acp":
                query = query.where(
                    or_(
                        ChatEventModel.direction == "inbound",
                        select(SocialOperationModel.id)
                        .where(
                            SocialOperationModel.event_id == ChatEventModel.id,
                            SocialOperationModel.status == "succeeded",
                            ChatEventModel.direction == "outbound",
                        )
                        .exists(),
                    )
                )
            if before is not None:
                query = query.where(ChatEventModel.id < before).order_by(ChatEventModel.id.desc())
            else:
                query = query.order_by(ChatEventModel.id)
            rows = list(await session.scalars(query.limit(limit + 1)))
            selected = rows[:limit]
            if before is not None:
                selected.reverse()
            return {
                "items": [public_event(row, binding) for row in selected],
                "has_more": len(rows) > limit,
                "next_after": selected[-1].id if selected else after,
                "next_before": selected[0].id if selected else before,
                "latest_event_id": conversation.last_event_id,
                "generation": conversation.generation,
                "revision": conversation.prompt_source_revision,
            }

    async def activity(self, principal: ClientPrincipal, identity: str) -> dict[str, Any]:
        async with self.database.sessions() as session:
            principal = await self.identity.require_principal(session, principal)
            await personal_conversation(session, principal.person_id, identity)
            return (await self._activities(session, (identity,)))[0]

    async def _activities(
        self, session: AsyncSession, identities: tuple[str, ...]
    ) -> list[dict[str, Any]]:
        requested = func.json_each(json.dumps(identities)).table_valued("value").alias("entries")
        latest = (
            select(ExecutionTraceEntryModel.id)
            .where(
                ExecutionTraceEntryModel.conversation_id == requested.c.value,
                ExecutionTraceEntryModel.expires_at > datetime.now(UTC),
            )
            .order_by(ExecutionTraceEntryModel.id.desc())
            .limit(1)
            .correlate(requested)
            .scalar_subquery()
        )
        rows = (
            await session.execute(
                select(
                    requested.c.value, ExecutionTraceEntryModel.id, ExecutionTraceEntryModel.kind
                )
                .select_from(requested)
                .outerjoin(ExecutionTraceEntryModel, ExecutionTraceEntryModel.id == latest)
            )
        ).all()
        result = []
        for identity, revision, kind in rows:
            live = self.traces.live_spans(identity) if self.traces else ()
            category = (
                "model"
                if kind and kind.startswith(("provider_", "model_"))
                else "tool"
                if kind and kind.startswith("tool_")
                else "delivery"
                if kind and kind.startswith("delivery_")
                else "turn"
            )
            result.append(
                {
                    "conversation_id": identity,
                    "evidence": "executing" if live else "insufficient",
                    "sources": [
                        {
                            "origin": span.origin,
                            "started_at": span.started_at.isoformat(),
                            "kind": span.family,
                        }
                        for span in live
                    ],
                    "step_kind": category if live else None,
                    "revision": revision or 0,
                }
            )
        return result

    async def sync(self, principal: ClientPrincipal) -> dict[str, Any]:
        async with self.database.sessions() as session:
            await session.execute(text("BEGIN"))
            principal = await self.identity.require_principal(session, principal)
            entries = await self._entries(session, principal)
            rows = (
                await session.execute(
                    select(work.c.id, work.c.revision)
                    .join(
                        CanonicalConversationModel,
                        CanonicalConversationModel.id == work.c.conversation_id,
                    )
                    .where(work_access(principal.person_id))
                )
            ).all()
            return {
                "conversations": entries,
                "works": [{"work_id": row.id, "revision": row.revision} for row in rows],
                "activity": await self._activities(
                    session, tuple(item["conversation_id"] for item in entries)
                ),
            }

    async def stream(self, principal: ClientPrincipal) -> AsyncIterator[str]:
        queue = self.database.commit_hints.subscribe()
        sequence = 0

        def frame(kind: str, data: Any) -> str:
            nonlocal sequence
            sequence += 1
            return (
                f"id: {sequence}\nevent: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            )

        try:
            previous = await self.sync(principal)
            yield frame("sync", previous)
            while True:
                try:
                    available = await asyncio.wait_for(queue.get(), 15)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if not available:
                    break
                while not queue.empty():
                    if not queue.get_nowait():
                        return
                current = await self.sync(principal)
                for category, key, event_kind in (
                    ("conversations", "conversation_id", "conversation"),
                    ("works", "work_id", "work"),
                    ("activity", "conversation_id", "activity"),
                ):
                    old = {item[key]: item for item in previous[category]}
                    for item in current[category]:
                        if item != old.get(item[key]):
                            yield frame(event_kind, item)
                    if old.keys() - {item[key] for item in current[category]}:
                        yield frame("sync", current)
                previous = current
        except ClientError:
            return
        finally:
            self.database.commit_hints.unsubscribe(queue)
