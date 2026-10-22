"""Durable, one-shot signals for an existing Work activation.

Every mutation is made beside the Work mailbox in one SQLite writer transaction.
The publisher supplies an internal event ID; model-authored payloads never confer authority.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

from sqlalchemy import (
    String,
    cast,
    column,
    func,
    literal,
    select,
    text,
    tuple_,
    update,
    values,
)
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.gateway.local_clients import LOCAL_CLIENT_PLATFORMS
from yuki.identity.db_models import PresenceModel
from yuki.identity.person_identity import same_person_condition, same_person_id
from yuki.persistence.models import ChatEventModel
from yuki.plugin_host.db_models import (
    PluginBackgroundTargetGrantModel,
    PluginInstallationModel,
)
from yuki.runtime.work_repository import WorkConflict, WorkLease, WorkRepository, encode_json
from yuki.runtime.work_schema_v1 import inputs, work
from yuki.runtime.work_wait_schema import waits


def _timestamp(raw: str) -> float:
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid_wait_time") from exc
    if value.tzinfo is None:
        raise ValueError("wait_time_requires_timezone")
    return value.timestamp()


def normalize_conditions(raw: Any, now: float) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise ValueError("wait_conditions_required")
    result: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("invalid_wait_condition")
        kind = item.get("kind")
        if kind == "time_due":
            if ("at" in item) == ("after_seconds" in item):
                raise ValueError("wait_time_requires_at_or_after")
            if "at" in item:
                if not isinstance(item["at"], str):
                    raise ValueError("invalid_wait_time")
                due = _timestamp(item["at"])
            else:
                seconds = item["after_seconds"]
                if (
                    not isinstance(seconds, (int, float))
                    or isinstance(seconds, bool)
                    or not math.isfinite(seconds)
                ):
                    raise ValueError("invalid_wait_delay")
                due = now + seconds
            result.append({"kind": kind, "due": due, "matched": None})
        elif kind == "conversation":
            if set(item) != {"kind"}:
                raise ValueError("invalid_wait_condition")
            result.append({"kind": kind, "matched": None})
        elif kind == "plugin_event":
            plugin_id, event_type = item.get("plugin_id"), item.get("event_type")
            source = item.get("external_source")
            if (
                not isinstance(plugin_id, str)
                or not 1 <= len(plugin_id) <= 128
                or not isinstance(event_type, str)
                or not 1 <= len(event_type) <= 128
                or (source is not None and (not isinstance(source, str) or len(source) > 128))
                or set(item) - {"kind", "plugin_id", "event_type", "external_source"}
            ):
                raise ValueError("invalid_wait_plugin_filter")
            result.append(
                {
                    "kind": kind,
                    "plugin_id": plugin_id,
                    "event_type": event_type,
                    "external_source": source,
                    "matched": None,
                }
            )
        elif kind == "owned_run":
            run_id = item.get("run_id")
            if not isinstance(run_id, str) or not run_id or set(item) != {"kind", "run_id"}:
                raise ValueError("invalid_wait_run")
            result.append({"kind": kind, "run_id": run_id, "matched": None})
        else:
            raise ValueError("unknown_wait_condition")
    return result


@dataclass(frozen=True, slots=True)
class WorkInputMatch:
    input_id: int
    kind: Literal["reply", "wait"]


@dataclass(frozen=True, slots=True)
class PreparedWaitAction:
    binding: dict[str, Any] | None
    conditions: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class PreparedEventWaits:
    bindings: tuple[dict[str, Any], ...]
    conditions: dict[str, list[dict[str, Any]]]


class WorkWaitRepository:
    def __init__(self, repository: WorkRepository) -> None:
        self.repository = repository

    async def describe(self, work_id: str) -> dict[str, Any] | None:
        async with self.repository.database.read_session() as session:
            row = (
                (
                    await session.execute(
                        select(waits)
                        .where(waits.c.work_id == work_id)
                        .order_by(waits.c.created.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            conditions = json.loads(row["conditions_json"])
            for condition in conditions:
                if isinstance(condition.get("matched"), dict):
                    condition["matched"].pop("text", None)
            from yuki.runtime.work_management import management_view

            # A delivered signal on a paused Work is held, not a new state:
            # show it with the original pause reason and resume/cancel actions.
            managed = await management_view(session, work_id)
            return {
                "wait_id": row["id"],
                "status": row["status"],
                "mode": row["mode"],
                "registered_at": row["created"],
                "deadline": row["deadline"],
                "conditions": conditions,
                **({"work": managed} if managed is not None else {}),
            }

    async def cancel(self, lease: WorkLease, work_id: str) -> bool:
        async with self.repository.database.immediate_session() as session:
            await self.repository._assert_lease(session, lease)
            changed = (
                await session.execute(
                    update(waits)
                    .where(
                        waits.c.work_id == work_id,
                        waits.c.conversation_id == lease.conversation_id,
                        waits.c.generation == lease.generation,
                        waits.c.status == "active",
                    )
                    .values(status="cancelled", updated=time.time())
                    .returning(waits.c.id)
                )
            ).first()
            return changed is not None

    async def register(
        self,
        lease: WorkLease,
        *,
        work_id: str,
        source: dict[str, Any],
        call_key: str,
        mode: str,
        conditions: list[dict[str, Any]],
        deadline_at: str | None,
        accepted: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if mode not in {"any", "all"} or not isinstance(call_key, str) or not call_key:
            raise ValueError("invalid_wait_registration")
        now = time.time()
        normalized = normalize_conditions(conditions, now)
        request_json = encode_json(
            {"mode": mode, "conditions": conditions, "deadline_at": deadline_at}
        )
        deadline = _timestamp(deadline_at) if deadline_at is not None else None
        principal_kind = source.get("principal_kind", "person")
        principal_id = "self" if principal_kind == "self" else source.get("actor_person_id")
        if (
            principal_kind not in {"person", "self"}
            or not isinstance(principal_id, str)
            or not principal_id
        ):
            raise ValueError("wait_principal_unavailable")
        async with self.repository.database.immediate_session() as session:
            await self.repository._assert_lease(session, lease)
            row = (
                (
                    await session.execute(
                        select(
                            work.c.id,
                            work.c.generation,
                            work.c.state,
                            func.coalesce(
                                func.json_extract(work.c.source_json, "$.principal_kind"), "person"
                            ).label("source_kind"),
                            func.json_extract(work.c.source_json, "$.actor_person_id").label(
                                "source_person_id"
                            ),
                        ).where(
                            work.c.id == work_id,
                            work.c.conversation_id == lease.conversation_id,
                            work.c.generation == lease.generation,
                            work.c.state.not_in(("completed", "failed", "cancelled")),
                        )
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise WorkConflict("wait_work_source_changed")
            original_kind = row["source_kind"]
            original_principal = "self" if original_kind == "self" else row["source_person_id"]
            if principal_kind != original_kind or (
                principal_id != original_principal
                and not (
                    principal_kind == "person"
                    and await same_person_id(session, principal_id, original_principal)
                )
            ):
                raise WorkConflict("wait_work_source_changed")
            conversation = await session.get(CanonicalConversationModel, lease.conversation_id)
            if conversation is None:
                raise WorkConflict("wait_conversation_missing")
            for condition in normalized:
                if condition["kind"] != "plugin_event":
                    continue
                plugin_id = condition["plugin_id"]
                target = (
                    PluginBackgroundTargetGrantModel.canonical_target_space_id
                    == conversation.space_id
                    if conversation.space_id is not None
                    else PluginBackgroundTargetGrantModel.canonical_target_person_id
                    == conversation.person_id
                )
                allowed = await session.scalar(
                    select(PluginBackgroundTargetGrantModel.id)
                    .join(
                        PluginInstallationModel,
                        PluginInstallationModel.plugin_id
                        == PluginBackgroundTargetGrantModel.plugin_id,
                    )
                    .where(
                        PluginBackgroundTargetGrantModel.plugin_id == plugin_id,
                        PluginBackgroundTargetGrantModel.enabled.is_(True),
                        PluginInstallationModel.enabled.is_(True),
                        target,
                    )
                )
                if allowed is None:
                    raise PermissionError("plugin_wait_target_not_granted")
            existing = (
                (await session.execute(select(waits).where(waits.c.call_key == call_key)))
                .mappings()
                .first()
            )
            if existing is not None:
                if existing["work_id"] != work_id or existing["request_json"] != request_json:
                    raise WorkConflict("wait_call_conflict")
                if existing["status"] != "active":
                    raise WorkConflict("wait_already_resolved")
                return dict(existing)
            if await session.scalar(
                select(waits.c.id).where(waits.c.work_id == work_id, waits.c.status == "active")
            ):
                raise WorkConflict("work_already_waiting_signal")
            watermark = int(
                await session.scalar(
                    select(func.max(ChatEventModel.id)).where(
                        ChatEventModel.canonical_conversation_id == lease.conversation_id,
                    )
                )
                or 0
            )
            for condition in normalized:
                if condition["kind"] in {"conversation", "plugin_event"}:
                    condition["after_event_id"] = watermark
            identity = str(uuid4())
            await session.execute(
                insert(waits).values(
                    id=identity,
                    work_id=work_id,
                    conversation_id=lease.conversation_id,
                    generation=lease.generation,
                    principal_kind=principal_kind,
                    principal_id=principal_id,
                    call_key=call_key,
                    request_json=request_json,
                    mode=mode,
                    conditions_json=encode_json(normalized),
                    status="active",
                    deadline=deadline,
                    created=now,
                    updated=now,
                )
            )
            if accepted is not None:
                # The binding and the accepted control that references it commit together.
                await self.repository.set_accepted_control(
                    session, lease, work_id, {**accepted, "wait_id": identity}
                )
            pending_runs = await session.scalars(
                select(func.json_extract(inputs.c.payload_json, "$.external_run_id")).where(
                    inputs.c.work_id == work_id,
                    inputs.c.state == "pending",
                    func.json_extract(inputs.c.payload_json, "$.external_run_id").is_not(None),
                )
            )
            for pending_run in pending_runs:
                await self.signal_external_action(
                    session,
                    work_id,
                    pending_run,
                    prepared=PreparedWaitAction(
                        {
                            "id": identity,
                            "work_id": work_id,
                            "conversation_id": lease.conversation_id,
                            "generation": lease.generation,
                            "principal_kind": principal_kind,
                            "principal_id": principal_id,
                        },
                        normalized,
                    ),
                )
            return {
                "id": identity,
                "work_id": work_id,
                "status": "active",
                "mode": mode,
                "conditions": normalized,
                "deadline": deadline,
            }

    async def prepare_external_action(
        self, reader: AsyncSession, work_id: str
    ) -> PreparedWaitAction:
        row = (
            (await reader.execute(self._due_query().where(waits.c.work_id == work_id)))
            .mappings()
            .first()
        )
        return PreparedWaitAction(
            dict(row) if row is not None else None,
            json.loads(row["conditions_json"]) if row is not None else [],
        )

    async def external_action_current(
        self,
        writer: AsyncSession,
        work_id: str,
        prepared: PreparedWaitAction,
    ) -> bool:
        row = (
            (await writer.execute(self._due_query().where(waits.c.work_id == work_id)))
            .mappings()
            .first()
        )
        return (dict(row) if row is not None else None) == prepared.binding

    async def prepare_event_waits(
        self, reader: AsyncSession, conversation_id: str
    ) -> PreparedEventWaits:
        rows = tuple(
            dict(row)
            for row in (
                await reader.execute(
                    self._due_query()
                    .where(waits.c.conversation_id == conversation_id)
                    .order_by(waits.c.created, waits.c.id)
                )
            ).mappings()
        )
        decoded = {row["id"]: json.loads(row["conditions_json"]) for row in rows}
        for conditions in decoded.values():
            for condition in conditions:
                if isinstance(condition.get("matched"), dict):
                    condition["matched"].pop("text", None)
        return PreparedEventWaits(rows, decoded)

    async def event_waits_current(
        self,
        writer: AsyncSession,
        conversation_id: str,
        prepared: PreparedEventWaits,
    ) -> bool:
        rows = tuple(
            dict(row)
            for row in (
                await writer.execute(
                    self._due_query()
                    .where(waits.c.conversation_id == conversation_id)
                    .order_by(waits.c.created, waits.c.id)
                )
            ).mappings()
        )
        return rows == prepared.bindings

    async def signal_external_action(
        self, session: AsyncSession, work_id: str, run_id: str, *, prepared: PreparedWaitAction
    ) -> None:
        """An original callback interrupts completion waits without satisfying their conditions."""
        owner = (
            (await session.execute(select(work.c.state).where(work.c.id == work_id)))
            .mappings()
            .first()
        )
        if owner is None or owner["state"] in {
            "completed",
            "failed",
            "cancelled",
            "suspended",
            "waiting_user",
        }:
            return
        binding = prepared.binding
        if binding is not None:
            conditions = prepared.conditions
            if any(c["kind"] == "owned_run" and c["run_id"] == run_id for c in conditions):
                await self._deliver(session, binding, conditions, "requires_action", time.time())
        elif owner["state"] == "waiting_external":
            await session.execute(
                update(work)
                .where(work.c.id == work_id, work.c.state == "waiting_external")
                .values(
                    state="queued",
                    reason="external_agent_requires_action",
                    revision=work.c.revision + 1,
                    updated=time.time(),
                )
            )

    @staticmethod
    async def _deliver(
        session: AsyncSession,
        binding: dict[str, Any],
        conditions: list[dict[str, Any]],
        reason: str,
        now: float,
        *,
        source_event_id: int | None = None,
    ) -> int | None:
        original: Any
        if "work_generation" in binding:
            original = {
                "generation": binding["work_generation"],
                "state": binding["work_state"],
                "source_kind": binding["work_source_kind"],
                "source_person_id": binding["work_source_person_id"],
            }
            current_generation = binding["current_generation"]
        else:
            original = (
                (
                    await session.execute(
                        select(
                            work.c.generation,
                            work.c.state,
                            func.coalesce(
                                func.json_extract(work.c.source_json, "$.principal_kind"), "person"
                            ).label("source_kind"),
                            func.json_extract(work.c.source_json, "$.actor_person_id").label(
                                "source_person_id"
                            ),
                        ).where(work.c.id == binding["work_id"])
                    )
                )
                .mappings()
                .first()
            )
            current_generation = await session.scalar(
                select(CanonicalConversationModel.generation).where(
                    CanonicalConversationModel.id == binding["conversation_id"],
                )
            )
        if (
            original is None
            or current_generation != binding["generation"]
            or original["generation"] != binding["generation"]
            or original["state"] in {"completed", "failed", "cancelled"}
        ):
            await session.execute(
                update(waits)
                .where(waits.c.id == binding["id"], waits.c.status == "active")
                .values(status="invalidated", updated=now)
            )
            return None
        kind = original["source_kind"]
        principal = "self" if kind == "self" else original["source_person_id"]
        if kind != binding["principal_kind"] or principal != binding["principal_id"]:
            await session.execute(
                update(waits)
                .where(waits.c.id == binding["id"], waits.c.status == "active")
                .values(status="invalidated", updated=now)
            )
            return None
        status = "expired" if reason == "deadline" else "delivered"
        changed = (
            await session.execute(
                update(waits)
                .where(waits.c.id == binding["id"], waits.c.status == "active")
                .values(
                    status=status,
                    conditions_json=encode_json(conditions),
                    updated=now,
                    delivered=now,
                )
                .returning(waits.c.id)
            )
        ).first()
        if changed is None:
            return None
        payload = {
            "kind": "work_signal",
            "wait_id": binding["id"],
            "reason": reason,
            "conditions": conditions,
            "at": now,
        }
        event_ids = [
            c["matched"]["event_id"]
            for c in conditions
            if isinstance(c.get("matched"), dict) and isinstance(c["matched"].get("event_id"), int)
        ]
        entry = (
            await session.execute(
                insert(inputs)
                .values(
                    conversation_id=binding["conversation_id"],
                    generation=binding["generation"],
                    source_key=f"wait:{binding['id']}",
                    kind="control",
                    work_id=binding["work_id"],
                    # The delivering message owns its admission; every matched
                    # ref stays in conditions for the Work signal itself.
                    event_id=(
                        source_event_id
                        if source_event_id is not None
                        else max(event_ids)
                        if event_ids
                        else None
                    ),
                    ready=True,
                    payload_json=encode_json(
                        {"text": json.dumps(payload, ensure_ascii=False), "signal": True}
                    ),
                    created=now,
                )
                .on_conflict_do_nothing(index_elements=[inputs.c.source_key])
                .returning(inputs.c.id)
            )
        ).first()
        await session.execute(
            update(work)
            .where(
                work.c.id == binding["work_id"],
                work.c.state.in_(("waiting_external", "waiting_user")),
            )
            .values(
                state="queued",
                reason="wait_signal_arrived",
                revision=work.c.revision + 1,
                updated=now,
            )
        )
        return int(entry[0]) if entry else None

    async def match_event(
        self,
        *,
        event_id: int,
        kind: str,
        on_delivery: Callable[[AsyncSession, int], Awaitable[None]] | None = None,
        prepare_source: Callable[[], Awaitable[bool]] | None = None,
    ) -> int | None:
        if kind not in {"conversation", "plugin_event"}:
            raise ValueError("invalid_wait_event_kind")
        async with self.repository.database.read_session() as reader:
            conversation_id = await reader.scalar(
                select(ChatEventModel.canonical_conversation_id).where(
                    ChatEventModel.id == event_id
                )
            )
            if conversation_id is None:
                return None
            prepared = await self.prepare_event_waits(reader, conversation_id)
        return await self._match_prepared_event(
            event_id=event_id,
            kind=kind,
            conversation_id=conversation_id,
            prepared=prepared,
            on_delivery=on_delivery,
            prepare_source=prepare_source,
        )

    async def _match_prepared_event(
        self,
        *,
        event_id: int,
        kind: str,
        conversation_id: str,
        prepared: PreparedEventWaits,
        on_delivery: Callable[[AsyncSession, int], Awaitable[None]] | None,
        prepare_source: Callable[[], Awaitable[bool]] | None,
    ) -> int | None:
        source_prepared = False
        while True:
            matching = any(
                c["kind"] == kind and c["matched"] is None and event_id > c["after_event_id"]
                for conditions in prepared.conditions.values()
                for c in conditions
            )
            if not matching:
                return None
            if prepare_source is not None and not source_prepared:
                if not await prepare_source():
                    return None
                source_prepared = True
            async with self.repository.database.immediate_session() as writer:
                if await self.event_waits_current(writer, conversation_id, prepared):
                    return await self.apply_event(
                        writer,
                        event_id=event_id,
                        kind=kind,
                        prepared=prepared,
                        on_delivery=on_delivery,
                    )
            async with self.repository.database.read_session() as reader:
                current_conversation = await reader.scalar(
                    select(ChatEventModel.canonical_conversation_id).where(
                        ChatEventModel.id == event_id
                    )
                )
                if current_conversation is None:
                    return None
                conversation_id = current_conversation
                prepared = await self.prepare_event_waits(reader, conversation_id)

    async def apply_event(
        self,
        session: AsyncSession,
        *,
        event_id: int,
        kind: str,
        prepared: PreparedEventWaits,
        on_delivery: Callable[[AsyncSession, int], Awaitable[None]] | None = None,
    ) -> int | None:
        event = (
            await session.execute(
                select(
                    ChatEventModel.id,
                    ChatEventModel.canonical_conversation_id,
                    ChatEventModel.suppression_status,
                    ChatEventModel.direction,
                    ChatEventModel.event_kind,
                    ChatEventModel.source_plugin_id,
                    ChatEventModel.external_resume_wait,
                    ChatEventModel.external_event_type,
                    ChatEventModel.external_source,
                ).where(ChatEventModel.id == event_id)
            )
        ).one_or_none()
        if event is None or not event.canonical_conversation_id:
            return None
        if event.suppression_status != "keeper":
            return None
        conversation = await session.get(
            CanonicalConversationModel, event.canonical_conversation_id
        )
        if conversation is None:
            return None
        if kind == "conversation" and (
            event.direction != "inbound" or event.event_kind != "message"
        ):
            return None
        if kind == "plugin_event" and (
            event.direction != "external"
            or event.event_kind != "external_event"
            or not event.source_plugin_id
            or not event.external_resume_wait
        ):
            return None
        rows = [
            row
            for row in prepared.bindings
            if row["conversation_id"] == event.canonical_conversation_id
            and row["generation"] == conversation.generation
        ]
        now = time.time()
        matched_any = False
        delivered_input: int | None = None
        for binding in rows:
            conditions = prepared.conditions[binding["id"]]
            changed = False
            for condition in conditions:
                if (
                    condition["kind"] != kind
                    or condition["matched"] is not None
                    or event.id <= condition["after_event_id"]
                ):
                    continue
                if kind == "plugin_event" and (
                    condition["plugin_id"] != event.source_plugin_id
                    or condition["event_type"] != event.external_event_type
                    or (
                        condition["external_source"] is not None
                        and condition["external_source"] != event.external_source
                    )
                ):
                    continue
                condition["matched"] = {
                    "event_id": event.id,
                    "kind": kind,
                    "at": now,
                }
                changed = True
            if not changed:
                continue
            matched_any = True
            done = (
                any(c["matched"] is not None for c in conditions)
                if binding["mode"] == "any"
                else all(c["matched"] is not None for c in conditions)
            )
            if done:
                delivered_input = (
                    await self._deliver(
                        session,
                        dict(binding),
                        conditions,
                        "signal",
                        now,
                        source_event_id=event.id if kind == "conversation" else None,
                    )
                    or delivered_input
                )
                continue
            await session.execute(
                update(waits)
                .where(waits.c.id == binding["id"], waits.c.status == "active")
                .values(conditions_json=encode_json(conditions), updated=now)
            )
        if delivered_input is not None and on_delivery is not None:
            await on_delivery(session, delivered_input)
        return delivered_input if delivered_input is not None else (0 if matched_any else None)

    async def match_input(
        self,
        event_id: int,
        *,
        on_delivery: Callable[[AsyncSession, int], Awaitable[None]] | None = None,
        prepare_source: Callable[[], Awaitable[bool]] | None = None,
    ) -> WorkInputMatch | None:
        """Prepare quoted/personal replies and conversation waits in one read view."""
        async with self.repository.database.read_session() as observer:
            await observer.execute(text("BEGIN"))
            source = (
                await observer.execute(
                    select(
                        ChatEventModel.canonical_conversation_id,
                        ChatEventModel.reply_to_event_id,
                        PresenceModel.platform,
                    )
                    .outerjoin(
                        PresenceModel, PresenceModel.id == ChatEventModel.ingress_presence_id
                    )
                    .where(ChatEventModel.id == event_id)
                )
            ).one_or_none()
            if source is None or source.canonical_conversation_id is None:
                return None
            conversation_id = source.canonical_conversation_id
            reply_candidate = (
                await observer.scalar(
                    select(work.c.id)
                    .where(
                        work.c.conversation_id == source.canonical_conversation_id,
                        work.c.state == "waiting_user",
                    )
                    .limit(1)
                )
                if source.reply_to_event_id is not None or source.platform in LOCAL_CLIENT_PLATFORMS
                else None
            )
            prepared = await self.prepare_event_waits(observer, conversation_id)
        if reply_candidate is not None:
            async with self.repository.database.immediate_session() as session:
                explicit = await self.match_user_reply(event_id, session=session)
            if explicit is not None:
                return WorkInputMatch(explicit, "reply")
        matched = await self._match_prepared_event(
            event_id=event_id,
            kind="conversation",
            conversation_id=conversation_id,
            prepared=prepared,
            on_delivery=on_delivery,
            prepare_source=prepare_source,
        )
        return WorkInputMatch(matched, "wait") if matched is not None else None

    async def match_user_reply(
        self, event_id: int, *, session: AsyncSession | None = None
    ) -> int | None:
        """Resume a quoted question, or the same Person's current personal waiting Work."""
        from yuki.runtime.execution_receipts import PROCESS_ID
        from yuki.social.db_models import SocialOperationModel

        if session is None:
            async with self.repository.database.read_session() as observer:
                source_event = await observer.get(ChatEventModel, event_id)
                if source_event is None:
                    return None
                if (
                    source_event.reply_to_event_id is None
                    and await observer.scalar(
                        select(PresenceModel.platform).where(
                            PresenceModel.id == source_event.ingress_presence_id
                        )
                    )
                    not in LOCAL_CLIENT_PLATFORMS
                ):
                    return None
        async with (
            self.repository.database.immediate_session()
            if session is None
            else nullcontext(session)
        ) as session:
            event = (
                await session.execute(
                    select(
                        ChatEventModel.id,
                        ChatEventModel.direction,
                        ChatEventModel.event_kind,
                        ChatEventModel.author_person_id,
                        ChatEventModel.canonical_conversation_id,
                        ChatEventModel.reply_to_event_id,
                        ChatEventModel.ingress_presence_id,
                    ).where(ChatEventModel.id == event_id)
                )
            ).one_or_none()
            if (
                event is None
                or event.direction != "inbound"
                or event.event_kind != "message"
                or event.author_person_id is None
                or event.canonical_conversation_id is None
            ):
                return None
            conversation = await session.get(
                CanonicalConversationModel, event.canonical_conversation_id
            )
            if conversation is None:
                return None
            if (
                event.reply_to_event_id is None
                and await session.scalar(
                    select(PresenceModel.platform).where(
                        PresenceModel.id == event.ingress_presence_id
                    )
                )
                not in LOCAL_CLIENT_PLATFORMS
            ):
                return None
            actor = func.json_extract(work.c.source_json, "$.actor_person_id")
            trigger = func.json_extract(work.c.source_json, "$.trigger_event_id")
            query = select(work.c.id).where(
                work.c.conversation_id == conversation.id,
                work.c.generation == conversation.generation,
                work.c.state == "waiting_user",
                func.json_extract(work.c.source_json, "$.origin") == "user_message",
                func.json_type(work.c.source_json, "$.trigger_event_id") == "integer",
                same_person_condition(actor, event.author_person_id),
            )
            if event.reply_to_event_id is not None:
                query = query.where(
                    select(SocialOperationModel.id)
                    .where(
                        SocialOperationModel.source_conversation_id == conversation.id,
                        SocialOperationModel.source_turn_id
                        == literal(f"{conversation.id}:event:") + cast(trigger, String),
                        SocialOperationModel.event_id == event.reply_to_event_id,
                        SocialOperationModel.status == "succeeded",
                    )
                    .exists()
                )
            candidate_id = await session.scalar(query.order_by(work.c.updated.desc()).limit(1))
            if candidate_id is not None:
                source_key = f"user-reply:{event.id}:{candidate_id}"
                await session.execute(
                    insert(inputs)
                    .values(
                        conversation_id=conversation.id,
                        generation=conversation.generation,
                        source_key=source_key,
                        event_id=event.id,
                        work_id=candidate_id,
                        kind="message",
                        ready=False,
                        prepare_owner=PROCESS_ID,
                        created=time.time(),
                    )
                    .on_conflict_do_nothing(index_elements=[inputs.c.source_key])
                )
                identity = await session.scalar(
                    select(inputs.c.id).where(inputs.c.source_key == source_key)
                )
                return int(identity) if identity is not None else None
        return None

    @staticmethod
    def _due_query() -> Any:
        return (
            select(
                waits.c.id,
                waits.c.work_id,
                waits.c.conversation_id,
                waits.c.generation,
                waits.c.principal_kind,
                waits.c.principal_id,
                waits.c.mode,
                waits.c.conditions_json,
                waits.c.deadline,
                waits.c.created,
                CanonicalConversationModel.generation.label("current_generation"),
                work.c.generation.label("work_generation"),
                work.c.state.label("work_state"),
                func.coalesce(
                    func.json_extract(work.c.source_json, "$.principal_kind"), "person"
                ).label("work_source_kind"),
                func.json_extract(work.c.source_json, "$.actor_person_id").label(
                    "work_source_person_id"
                ),
            )
            .select_from(
                waits.outerjoin(
                    CanonicalConversationModel,
                    CanonicalConversationModel.id == waits.c.conversation_id,
                ).outerjoin(work, work.c.id == waits.c.work_id)
            )
            .where(waits.c.status == "active")
        )

    @staticmethod
    def _observe_due(
        binding: Any,
        conditions: list[dict[str, Any]],
        states: dict[tuple[str, str], dict[str, Any]],
        now: float,
    ) -> tuple[list[dict[str, Any]], str] | None:
        if (
            binding["current_generation"] != binding["generation"]
            or binding["work_generation"] != binding["generation"]
            or binding["work_state"] in {None, "completed", "failed", "cancelled"}
        ):
            return [], "invalidated"
        for condition in conditions:
            if isinstance(condition.get("matched"), dict):
                condition["matched"].pop("text", None)
        changed = False
        for condition in conditions:
            if condition["matched"] is not None:
                continue
            if condition["kind"] == "time_due" and condition["due"] <= now:
                condition["matched"] = {
                    "kind": "time_due",
                    "due": condition["due"],
                    "observed_at": now,
                }
                changed = True
            elif condition["kind"] == "owned_run":
                state = states.get((binding["work_id"], condition["run_id"]))
                if state is not None and state["status"] in {
                    "completed",
                    "succeeded",
                    "failed",
                    "cancelled",
                    "detached",
                    "uncertain",
                    "unknown",
                }:
                    condition["matched"] = {
                        "kind": "owned_run",
                        "run_id": condition["run_id"],
                        **state,
                    }
                    changed = True
        done = (
            any(c["matched"] is not None for c in conditions)
            if binding["mode"] == "any"
            else all(c["matched"] is not None for c in conditions)
        )
        expired = binding["deadline"] is not None and binding["deadline"] <= now
        if done or expired:
            return conditions, "deadline" if expired and not done else "signal"
        return (conditions, "partial") if changed else None

    @staticmethod
    def _requested_runs(bindings: Any, conditions: dict[str, list[dict[str, Any]]]) -> str:
        pairs = {
            (row["work_id"], c["run_id"])
            for row in bindings
            for c in conditions[row["id"]]
            if c["kind"] == "owned_run" and c["matched"] is None
        }
        return encode_json([{"owner": owner, "run": run} for owner, run in sorted(pairs)])

    @staticmethod
    def _owned_runs_query(requested_json: str) -> Any:
        from yuki.runtime.work_tree import ancestors
        from yuki.sandbox.db_models import SandboxTaskRunModel

        records = func.json_each(requested_json).table_valued("value")
        requested = select(
            func.json_extract(records.c.value, "$.owner").label("owner_id"),
            func.json_extract(records.c.value, "$.run").label("run_id"),
        ).cte("requested_runs")
        owned_work = (
            select(
                requested.c.owner_id,
                requested.c.run_id,
                work.c.state,
                literal(None).label("completion_json"),
                literal(False).label("sandbox"),
            )
            .select_from(requested.join(work, work.c.id == requested.c.run_id))
            .where(requested.c.owner_id.in_(ancestors(requested.c.run_id)))
        )
        owned_sandbox = (
            select(
                requested.c.owner_id,
                requested.c.run_id,
                SandboxTaskRunModel.status,
                SandboxTaskRunModel.completion_json,
                literal(True),
            )
            .select_from(
                requested.join(
                    SandboxTaskRunModel, SandboxTaskRunModel.run_id == requested.c.run_id
                )
            )
            .where(
                requested.c.owner_id.in_(
                    ancestors(
                        func.json_extract(SandboxTaskRunModel.source_json, "$.work_id"),
                        include_self=True,
                    )
                )
            )
        )
        return owned_work.union_all(owned_sandbox)

    @staticmethod
    def _run_states(rows: Any) -> dict[tuple[str, str], dict[str, Any]]:
        states = {
            (owner, run): {"status": state} for owner, run, state, _, sandbox in rows if not sandbox
        }
        for owner, run, state, completion, sandbox in rows:
            if not sandbox or (owner, run) in states:
                continue
            if state == "completed" and completion:
                receipt = json.loads(completion)
                states[owner, run] = {
                    "status": receipt.get("operation_status", receipt.get("status", state)),
                    **{
                        name: receipt[name]
                        for name in ("operation_status", "native_status", "native_pending")
                        if name in receipt
                    },
                }
            else:
                states[owner, run] = {"status": state}
        return states

    @staticmethod
    async def _owned_runs_current(session: AsyncSession, query: Any, original: Any) -> bool:
        current = query.subquery()
        if not original:
            return not await session.scalar(select(current).exists().select())
        columns = tuple(current.c)
        if await session.scalar(select(func.count()).select_from(current)) != len(original):
            return False
        # Keep bind counts below SQLite's native budget even for many owned runs.
        for start in range(0, len(original), 32):
            page = original[start : start + 32]
            expected = (
                values(*(column(c.key, c.type) for c in columns))
                .data([tuple(row) for row in page])
                .cte("prepared_wait_runs")
            )
            matched = await session.scalar(
                select(func.count())
                .select_from(
                    current.join(
                        expected,
                        (current.c.owner_id == expected.c.owner_id)
                        & (current.c.run_id == expected.c.run_id)
                        & (current.c.sandbox == expected.c.sandbox),
                    )
                )
                .where(*(c.is_not_distinct_from(expected.c[c.key]) for c in columns))
            )
            if matched != len(page):
                return False
        return True

    async def deliver_due(self, now: float | None = None) -> int:
        """Prepare conditions and completion bodies, then prove their actual source rows."""
        from copy import deepcopy

        observed_at = time.time() if now is None else now
        prepared: dict[str, dict[str, Any]] = {}
        decoded: dict[str, list[dict[str, Any]]] = {}
        selected_runs: dict[tuple[str, str, bool], Any] = {}
        selected_states: dict[tuple[str, str], dict[str, Any]] = {}
        cursor: tuple[float, str] | None = None
        async with self.repository.database.read_session() as reader:
            while len(prepared) < 128:
                query = self._due_query().order_by(waits.c.created, waits.c.id).limit(128)
                if cursor is not None:
                    query = query.where(tuple_(waits.c.created, waits.c.id) > cursor)
                rows = (await reader.execute(query)).mappings().all()
                if not rows:
                    break
                page_conditions = {row["id"]: json.loads(row["conditions_json"]) for row in rows}
                run_query = self._owned_runs_query(self._requested_runs(rows, page_conditions))
                page_runs = (await reader.execute(run_query)).all()
                states = self._run_states(page_runs)
                by_owner: dict[str, list[Any]] = {}
                for run in page_runs:
                    by_owner.setdefault(run.owner_id, []).append(run)
                for binding in rows:
                    if (
                        self._observe_due(
                            binding, deepcopy(page_conditions[binding["id"]]), states, observed_at
                        )
                        is not None
                    ):
                        prepared[binding["id"]] = dict(binding)
                        decoded[binding["id"]] = page_conditions[binding["id"]]
                        for run in by_owner.get(binding["work_id"], ()):
                            selected_runs[run.owner_id, run.run_id, run.sandbox] = run
                            selected_states[run.owner_id, run.run_id] = states[
                                run.owner_id, run.run_id
                            ]
                        if len(prepared) == 128:
                            break
                cursor = rows[-1]["created"], rows[-1]["id"]
                if len(rows) < 128:
                    break
            if not prepared:
                return 0
        run_query = self._owned_runs_query(self._requested_runs(prepared.values(), decoded))
        runs = list(selected_runs.values())
        count = 0
        async with self.repository.database.immediate_session() as writer:
            if not await self._owned_runs_current(writer, run_query, runs):
                return 0  # Time and owned-run signals remain durable for the next poll.
            current = time.time() if now is None else now
            rows = (
                (
                    await writer.execute(
                        self._due_query()
                        .where(waits.c.id.in_(tuple(prepared)))
                        .order_by(waits.c.created, waits.c.id)
                    )
                )
                .mappings()
                .all()
            )
            for binding in rows:
                before = prepared[binding["id"]]
                if any(
                    binding[key] != before[key]
                    for key in (
                        "work_id",
                        "conversation_id",
                        "generation",
                        "principal_kind",
                        "principal_id",
                        "mode",
                        "conditions_json",
                        "deadline",
                    )
                ):
                    continue
                observation = self._observe_due(
                    binding, decoded[binding["id"]], selected_states, current
                )
                if observation is None:
                    continue
                conditions, reason = observation
                if reason in {"invalidated", "partial"}:
                    changes: dict[str, Any] = {"updated": current}
                    if reason == "invalidated":
                        changes["status"] = "invalidated"
                    else:
                        changes["conditions_json"] = encode_json(conditions)
                    await writer.execute(
                        update(waits)
                        .where(waits.c.id == binding["id"], waits.c.status == "active")
                        .values(**changes)
                    )
                elif await self._deliver(writer, dict(binding), conditions, reason, current):
                    count += 1
        return count
