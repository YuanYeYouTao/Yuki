"""Safe personal ledger and activity projections from original sources."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from sqlalchemy import Integer, func, literal_column, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.client_api.errors import ClientError
from yuki.client_api.identity import ClientIdentityService, ClientPrincipal
from yuki.client_api.work_projection import steps_token
from yuki.client_api.works import ClientWorks, personal_work, root_work_query, work_access
from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.conversation.media_index import indexed_media_segments
from yuki.domain.audio import parse_transcripts
from yuki.execution_trace.db_models import ExecutionTraceEntryModel
from yuki.execution_trace.recorder import TraceRecorder
from yuki.identity.db_models import IdentityBindingModel
from yuki.identity.person_identity import person_family_condition
from yuki.persistence.commit_hints import CommittedHint
from yuki.persistence.models import ChatEventModel, ConversationMediaItemModel
from yuki.runtime.work_repository import TERMINAL
from yuki.runtime.work_schema_v1 import work
from yuki.social.db_models import SocialOperationModel
from yuki.time.formatting import utc_iso


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


if TYPE_CHECKING:
    from yuki.client_api.computer import ClientComputer


def public_event(
    row: ChatEventModel,
    binding: IdentityBindingModel,
    media: dict[tuple[int, int], ConversationMediaItemModel] | None = None,
) -> dict[str, Any]:
    attachments = []
    has_audio = False
    segments = json.loads(row.segments_json)
    payload_segments = [segment for segment in segments if segment.get("type") != "yuki_context"]
    for index, _position, kind, data in indexed_media_segments(segments):
        has_audio = has_audio or kind in {"record", "audio"}
        item = (media or {}).get((row.id, index))
        expires = item.expires_at if item else None
        expired = bool(
            item
            and (
                item.cache_status == "expired"
                or (expires and expires.replace(tzinfo=UTC) <= datetime.now(UTC))
            )
        )
        size = next(
            (
                value
                for value in (
                    data.get("size"),
                    data.get("file_size"),
                    item.declared_size if item else None,
                )
                if type(value) is int and value >= 0
            ),
            None,
        )
        attachments.append(
            {
                "kind": kind,
                "attachment_index": index,
                **({"size": size} if size is not None else {}),
                "availability": "not_readable"
                if kind in {"audio", "record"}
                else "expired"
                if expired
                else "available"
                if item
                else "not_readable",
                "expires_at": utc_iso(expires),
                **{
                    key: data[key]
                    for key in (
                        "media_id",
                        "artifact_id",
                        "version",
                        "filename",
                        "mime_type",
                    )
                    if key in data
                },
            }
        )
    result = {
        "event_id": row.id,
        "conversation_id": row.canonical_conversation_id,
        "binding_id": binding.id,
        "platform": binding.platform,
        "direction": row.direction,
        "author_kind": row.author_kind,
        "text": row.content,
        "origin": row.origin,
        "created_at": utc_iso(row.observed_at),
        "attachments": attachments,
        "display_text": (
            "".join(
                segment["data"]["text"] for segment in payload_segments if segment["type"] == "text"
            )
            if binding.platform == "app"
            and row.direction == "inbound"
            and payload_segments
            and all(
                segment.get("type") in {"text", "image", "file", "record", "audio", "video"}
                for segment in payload_segments
            )
            else None
        ),
    }
    if has_audio:
        transcript = "\n\n".join(
            item.text
            for item in parse_transcripts(row.audio_transcript)
            if item.source == "current"
        )
        result["transcript"] = (
            {"state": "available", "text": transcript} if transcript else {"state": "none"}
        )
    return result


async def public_events(
    session: AsyncSession, rows: list[ChatEventModel], binding: IdentityBindingModel
) -> list[dict[str, Any]]:
    items = (
        await session.scalars(
            select(ConversationMediaItemModel).where(
                ConversationMediaItemModel.source_event_id.in_([row.id for row in rows])
            )
        )
        if rows
        else ()
    )
    media = {(item.source_event_id, item.attachment_index): item for item in items}
    # Callers already authorized these private conversation rows. Keep links in
    # the same original conversation and include roots only, including ended Work.
    links: dict[int, list[str]] = {}
    if rows:
        trigger = literal_column(
            "json_extract(runtime_work.source_json, '$.trigger_event_id')", type_=Integer
        )
        for identity, event_id in await session.execute(
            select(work.c.id, trigger)
            .where(
                work.c.conversation_id.in_({row.canonical_conversation_id for row in rows}),
                work.c.parent_work_id.is_(None),
                trigger.in_([row.id for row in rows]),
            )
            .order_by(work.c.created, work.c.id)
        ):
            links.setdefault(event_id, []).append(identity)
    return [
        {**public_event(row, binding, media), "work_ids": links.get(row.id, [])} for row in rows
    ]


class ClientViews:
    def __init__(
        self,
        identity: ClientIdentityService,
        works: ClientWorks,
        traces: TraceRecorder | None = None,
        *,
        computer: ClientComputer | None = None,
    ) -> None:
        self.identity, self.works, self.traces = identity, works, traces
        self.computer = computer
        self.database = identity.database
        self._character_revision = 0
        self._character_facts: dict[str, str] = {}

    async def character_status(self, principal: ClientPrincipal) -> dict[str, Any]:
        from yuki.character.repository import CharacterRepository

        async with self.database.sessions() as session:
            await session.execute(text("BEGIN"))
            current = await self.identity.require_principal(session, principal)
            entries = await self._entries(session, current)
            activity = [
                await self._activity(session, entry["conversation_id"]) for entry in entries
            ]
            counts = (
                await session.execute(
                    root_work_query(current.person_id)
                    .with_only_columns(work.c.state, func.count())
                    .group_by(work.c.state)
                )
            ).all()
            mine: dict[str, Any] = {
                "live_conversations": [
                    entry["conversation_id"]
                    for entry in activity
                    if entry["evidence"] == "executing"
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
        environment: dict[str, Any] = {
            "state": "unavailable",
            "display": "unavailable",
            "generation": None,
            "control": None,
            "help_requested": None,
        }
        manager = self.database.sandbox_client
        if self.computer:
            environment = await self.computer.environment(current)
        elif manager:
            status = await manager.execute("environment_status", {}, request_id=str(uuid4()))
            if "error" not in status and type(status.get("ready")) is bool:
                environment["state"] = "running" if status.get("ready") else "stopped"
        profile = await CharacterRepository(self.identity).get(current)
        facts = json.dumps(
            {"mine": mine, "environment": environment, "profile": profile}, sort_keys=True
        )
        digest = hashlib.sha256(facts.encode()).hexdigest()
        if self._character_facts.get(current.person_id) != digest:
            self._character_facts[current.person_id] = digest
            self._character_revision += 1
        return {
            "observed_at": datetime.now(UTC).isoformat(),
            "environment": environment,
            "mine": mine,
            "revision": self._character_revision,
        }

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

    async def home(self, principal: ClientPrincipal, *, active: bool = False) -> dict[str, Any]:
        async with self.database.sessions() as session:
            await session.execute(text("BEGIN"))
            principal = await self.identity.require_principal(session, principal)
            entries = await self._entries(session, principal)
            query = (
                root_work_query(principal.person_id)
                if active
                else select(work)
                .join(
                    CanonicalConversationModel,
                    CanonicalConversationModel.id == work.c.conversation_id,
                )
                .where(work_access(principal.person_id))
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
                "works": [
                    await self.works._public(session, dict(row), detail=False) for row in rows
                ],
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
                "items": await public_events(session, selected, binding),
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
            return await self._activity(session, identity)

    async def _activity(self, session: AsyncSession, identity: str) -> dict[str, Any]:
        live = self.traces.live_spans(identity) if self.traces else ()
        latest = (
            await session.execute(
                select(ExecutionTraceEntryModel.id, ExecutionTraceEntryModel.kind)
                .where(
                    ExecutionTraceEntryModel.conversation_id == identity,
                    ExecutionTraceEntryModel.expires_at > datetime.now(UTC),
                )
                .order_by(ExecutionTraceEntryModel.id.desc())
                .limit(1)
            )
        ).first()
        kind = latest.kind if latest else ""
        category = (
            "model"
            if kind.startswith(("provider_", "model_"))
            else "tool"
            if kind.startswith("tool_")
            else "delivery"
            if kind.startswith("delivery_")
            else "turn"
        )
        return {
            "conversation_id": identity,
            "evidence": "executing" if live else "insufficient",
            "sources": [
                {
                    "origin": span.origin,
                    "started_at": utc_iso(span.started_at),
                    "kind": span.family,
                }
                for span in live
            ],
            "step_kind": category if live else None,
            "revision": latest.id if latest else 0,
        }

    async def sync(self, principal: ClientPrincipal, *, active: bool = False) -> dict[str, Any]:
        async with self.database.sessions() as session:
            await session.execute(text("BEGIN"))
            principal = await self.identity.require_principal(session, principal)
            entries = await self._entries(session, principal)
            query = (
                root_work_query(principal.person_id)
                if active
                else select(work)
                .join(
                    CanonicalConversationModel,
                    CanonicalConversationModel.id == work.c.conversation_id,
                )
                .where(work_access(principal.person_id))
            )
            rows = (
                await session.execute(
                    query.with_only_columns(work.c.id, work.c.revision, work.c.state)
                )
            ).all()
            hints = []
            for row in rows:
                hint: dict[str, Any] = {"work_id": row.id, "revision": row.revision}
                if row.state not in TERMINAL:
                    hint["steps_token"] = (await steps_token(session, row.id))[1]
                hints.append(hint)
            result: dict[str, Any] = {
                "conversations": entries,
                "works": hints,
                "activity": [
                    await self._activity(session, item["conversation_id"]) for item in entries
                ],
            }
        result["character_revision"] = (await self.character_status(principal))["revision"]
        if self.computer and principal.is_owner:
            result["computer_revision"] = self.computer.revision
        return result

    async def stream(
        self, principal: ClientPrincipal, *, active: bool = False
    ) -> AsyncIterator[str]:
        queue = self.database.commit_hints.subscribe(details=True)
        sequence = 0
        if self.computer:
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
                if isinstance(available, CommittedHint):
                    detail: tuple[str, dict[str, Any]] | None = None
                    async with self.database.sessions() as session:
                        current_principal = await self.identity.require_principal(
                            session, principal
                        )
                        if available.kind == "work_steps" and isinstance(available.reference, str):
                            try:
                                await personal_work(
                                    session, current_principal.person_id, available.reference
                                )
                            except ClientError:
                                continue
                            detail = ("work_steps", {"work_id": available.reference})
                        elif available.kind == "artifacts":
                            from yuki.client_api.result_events import (
                                confirmed_outbound,
                                visible_outbound,
                            )

                            parts = func.json_each(ChatEventModel.segments_json).table_valued(
                                "value", joins_implicitly=True
                            )
                            visible = await session.scalar(
                                select(ChatEventModel.id)
                                .select_from(ChatEventModel, parts)
                                .where(
                                    ChatEventModel.id == available.reference,
                                    *confirmed_outbound(),
                                    visible_outbound(current_principal.person_id),
                                    func.json_extract(parts.c.value, "$.data.artifact_id").is_not(
                                        None
                                    ),
                                )
                                .limit(1)
                            )
                            if visible is not None:
                                detail = ("artifacts", {})
                    if detail is not None:
                        yield frame(*detail)
                    continue
                current = await self.sync(principal, active=active)
                if current["character_revision"] != previous["character_revision"]:
                    yield frame("character", {"revision": current["character_revision"]})
                if current.get("computer_revision") != previous.get("computer_revision"):
                    yield frame("computer", {"revision": current.get("computer_revision", 0)})
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
            if self.computer:
                self.computer.device_connection(principal.device_id, -1)
