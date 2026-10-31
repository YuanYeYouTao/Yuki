"""Safe personal ledger and activity projections from original sources."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.character.repository import CharacterRepository
from yuki.character.soul import CharacterSoul
from yuki.client_api.errors import ClientError
from yuki.client_api.identity import ClientIdentityService, ClientPrincipal
from yuki.client_api.works import ClientWorks, root_work_query, work_access
from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.execution_trace.db_models import ExecutionTraceEntryModel
from yuki.execution_trace.recorder import TraceRecorder
from yuki.identity.authority import person_is_superuser
from yuki.identity.db_models import IdentityBindingModel
from yuki.identity.person_identity import person_family_condition
from yuki.persistence.event_view import event_view
from yuki.persistence.repositories import EventLedgerRepository
from yuki.runtime.work_schema_v1 import effects, work
from yuki.social.history_sources import history_delivery_predicates


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
        computer: Any = None,
    ) -> None:
        self.identity, self.works, self.traces = identity, works, traces
        self.character, self.soul = character, soul
        self.computer = computer
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

    async def character_status(self, principal: ClientPrincipal) -> dict[str, Any]:
        async with self.database.read_session() as session:
            await session.execute(text("BEGIN"))
            current = await self.identity.require_principal(session, principal)
            entries = await self._entries(session, current)
            activities = await self._activities(
                session, tuple(item["conversation_id"] for item in entries)
            )
            counts = (
                await session.execute(
                    root_work_query(current.person_id)
                    .with_only_columns(work.c.state, func.count())
                    .group_by(work.c.state)
                )
            ).all()
            mine: dict[str, Any] = {
                "live_conversations": [
                    item["conversation_id"]
                    for item in activities
                    if item["evidence"] == "executing"
                ],
                "works": {state: count for state, count in counts},
            }
            mine["works"]["failed_recent"] = (
                await session.scalar(
                    root_work_query(current.person_id, "ended")
                    .with_only_columns(func.count())
                    .where(work.c.state == "failed", work.c.updated >= time.time() - 86400)
                )
                or 0
            )
            profile = await self.character.current(session) if self.character is not None else None
        environment = {
            "state": "unavailable",
            "display": "unavailable",
            "generation": None,
            "control": None,
            "help_requested": None,
        }
        if self.computer is not None:
            environment = await self.computer.environment(current)
        revision = int.from_bytes(
            hashlib.sha256(
                json.dumps(
                    {
                        "mine": mine,
                        "environment": environment,
                        "character": [profile.id, profile.revision, profile.avatar_revision]
                        if profile
                        else None,
                    },
                    sort_keys=True,
                ).encode()
            ).digest()[:6]
        )
        return {
            "observed_at": datetime.now(UTC).isoformat(),
            "environment": environment,
            "mine": mine,
            "revision": revision,
        }

    async def home(self, principal: ClientPrincipal, *, active: bool = False) -> dict[str, Any]:
        async with self.database.read_session() as session:
            await session.execute(text("BEGIN"))
            principal = await self.identity.require_principal(session, principal)
            entries = await self._entries(session, principal)
            query = (
                root_work_query(principal.person_id)
                if active
                else (
                    select(work)
                    .join(
                        CanonicalConversationModel,
                        CanonicalConversationModel.id == work.c.conversation_id,
                    )
                    .where(work_access(principal.person_id))
                )
            )
            rows = (
                (await session.execute(query.order_by(work.c.updated.desc(), work.c.id)))
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
        async with self.database.read_session() as session:
            await session.execute(text("BEGIN"))
            principal = await self.identity.require_principal(session, principal)
            conversation = await personal_conversation(session, principal.person_id, identity)
            binding = await session.get(IdentityBindingModel, conversation.identity_binding_id)
            assert binding is not None
            predicates = history_delivery_predicates(
                (identity,) if binding.platform == "acp" else ()
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
            linked = await self.works.linked_for_events(
                session, principal.person_id, (row.id for row in selected)
            )
            return {
                "items": [
                    {
                        **fact,
                        "text": row.content,
                        "binding_id": binding.id,
                        "platform": binding.platform,
                        "created_at": fact["observed_at"],
                        "work_ids": linked[row.id],
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
        async with self.database.read_session() as session:
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

    async def sync(self, principal: ClientPrincipal, *, active: bool = False) -> dict[str, Any]:
        async with self.database.read_session() as session:
            await session.execute(text("BEGIN"))
            principal = await self.identity.require_principal(session, principal)
            entries = await self._entries(session, principal)
            query = (
                root_work_query(principal.person_id)
                if active
                else (
                    select(work)
                    .join(
                        CanonicalConversationModel,
                        CanonicalConversationModel.id == work.c.conversation_id,
                    )
                    .where(work_access(principal.person_id))
                )
            )
            rows = (
                await session.execute(query.with_only_columns(work.c.id, work.c.revision))
            ).all()
            requested_works = func.json_each(json.dumps([row.id for row in rows])).table_valued(
                "value"
            )
            step_tokens = {
                row[0]: f"{row[1]}:{row[2]}"
                for row in await session.execute(
                    select(effects.c.work_id, func.count(), func.max(effects.c.updated))
                    .where(
                        effects.c.work_id.in_(select(requested_works.c.value)),
                        effects.c.kind == "tool",
                    )
                    .group_by(effects.c.work_id)
                )
            }
            result: dict[str, Any] = {
                "conversations": entries,
                "works": [
                    {
                        "work_id": row.id,
                        "revision": row.revision,
                        "steps_token": step_tokens.get(row.id, "0:0"),
                    }
                    for row in rows
                ],
                "activity": await self._activities(
                    session, tuple(item["conversation_id"] for item in entries)
                ),
            }
            if self.character is not None:
                current = await self.character.current(session)
                hint: dict[str, Any] = {
                    "id": current.id,
                    "revision": current.revision,
                    "avatar_revision": current.avatar_revision,
                }
                if self.soul is not None and await person_is_superuser(
                    session, principal.source_person_id
                ):
                    hint["soul"] = {
                        "revision": self.soul.revision,
                        "active_revision": self.soul.settings.active_soul_revision,
                    }
                result["character"] = hint
            if self.computer is not None:
                result["computer_revision"] = self.computer.revision
            return result

    async def stream(
        self, principal: ClientPrincipal, *, active: bool = False
    ) -> AsyncIterator[str]:
        queue = self.database.commit_hints.subscribe()
        sequence = 0
        if self.computer is not None:
            self.computer.device_connection(principal.device_id, 1)

        def frame(kind: str, data: Any) -> str:
            nonlocal sequence
            sequence += 1
            return (
                f"id: {sequence}\nevent: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            )

        try:
            previous = await self.sync(principal, active=active)
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
                current = await self.sync(principal, active=active)
                if current == previous:
                    # Management data can change outside this small snapshot.
                    yield frame("refresh", {})
                if current.get("computer_revision") != previous.get("computer_revision"):
                    yield frame("computer", {"revision": current["computer_revision"]})
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
            if self.computer is not None:
                self.computer.device_connection(principal.device_id, -1)
