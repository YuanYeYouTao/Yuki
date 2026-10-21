"""Safe personal ledger and activity projections from original sources."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.character.repository import CharacterRepository, is_character_owner
from yuki.character.soul import CharacterSoul
from yuki.client_api.errors import ClientError
from yuki.client_api.identity import ClientIdentityService, ClientPrincipal
from yuki.client_api.works import ClientWorks, work_access
from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.execution_trace.db_models import ExecutionTraceEntryModel
from yuki.execution_trace.recorder import TraceRecorder
from yuki.identity.db_models import IdentityBindingModel
from yuki.identity.person_identity import person_family_condition
from yuki.persistence.event_view import event_view
from yuki.persistence.models import ChatEventModel
from yuki.persistence.repositories import EventLedgerRepository
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


class ClientViews:
    def __init__(
        self,
        identity: ClientIdentityService,
        works: ClientWorks,
        traces: TraceRecorder | None = None,
        *,
        character: CharacterRepository | None = None,
        soul: CharacterSoul | None = None,
    ) -> None:
        self.identity, self.works, self.traces = identity, works, traces
        self.character, self.soul = character, soul
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
        if type(limit) is not int or limit < 1 or after < 0 or (before is not None and before < 1):
            raise ClientError("validation_error")
        async with self.database.sessions() as session:
            await session.execute(text("BEGIN"))
            principal = await self.identity.require_principal(session, principal)
            conversation = await personal_conversation(session, principal.person_id, identity)
            binding = await session.get(IdentityBindingModel, conversation.identity_binding_id)
            assert binding is not None
            predicates: tuple[Any, ...] = ()
            if binding.platform == "acp":
                predicates = (
                    or_(
                        ChatEventModel.direction == "inbound",
                        select(SocialOperationModel.id)
                        .where(
                            SocialOperationModel.event_id == ChatEventModel.id,
                            SocialOperationModel.status == "succeeded",
                            ChatEventModel.direction == "outbound",
                        )
                        .exists(),
                    ),
                )
            page = await EventLedgerRepository(self.database).query_history(
                session=session,
                conversation_id=identity,
                event_kind="all",
                after_event_id=after,
                before_event_id=before,
                order="recent" if before is not None else "oldest",
                limit=limit,
                predicates=predicates,
            )
            selected = list(page.events)
            next_after = max((row.id for row in selected), default=after)
            next_before = min((row.id for row in selected), default=before)
            if before is not None:
                selected.reverse()
            facts = [event_view(row) for row in selected]
            return {
                "items": [
                    {
                        **fact,
                        "text": row.content,
                        "binding_id": binding.id,
                        "platform": binding.platform,
                        "created_at": fact["observed_at"],
                    }
                    for row, fact in zip(selected, facts, strict=True)
                ],
                "has_more": page.has_more,
                "next_after": next_after,
                "next_before": next_before,
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
            result: dict[str, Any] = {
                "conversations": entries,
                "works": [{"work_id": row.id, "revision": row.revision} for row in rows],
                "activity": await self._activities(
                    session, tuple(item["conversation_id"] for item in entries)
                ),
            }
            if self.character is not None:
                current = await self.character.current(session)
                owner = await is_character_owner(session, principal.person_id)
                hint: dict[str, Any] = {
                    "id": current.id,
                    "revision": current.revision,
                    "is_owner": owner,
                }
                if owner and self.soul is not None:
                    hint["soul"] = {
                        "revision": self.soul.revision,
                        "active_revision": self.soul.settings.active_soul_revision,
                    }
                result["character"] = hint
            return result

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
                if current.get("character") != previous.get("character"):
                    yield frame("character", current["character"])
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
