"""Durable, one-shot signals for an existing Work activation.

Every mutation is made beside the Work mailbox in one SQLite writer transaction.
The publisher supplies an internal event ID; model-authored payloads never confer authority.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.plugin_host.db_models import (
    PluginBackgroundTargetGrantModel,
    PluginInstallationModel,
)
from qq_ai_bot.runtime.work_repository import WorkConflict, WorkLease, WorkRepository, bounded_json
from qq_ai_bot.runtime.work_schema_v1 import inputs, work
from qq_ai_bot.runtime.work_wait_schema import waits


def _timestamp(raw: str) -> float:
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid_wait_time") from exc
    if value.tzinfo is None:
        raise ValueError("wait_time_requires_timezone")
    return value.timestamp()


def normalize_conditions(raw: Any, now: float) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= 8:
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
                    or not 1 <= seconds <= 31_536_000
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
            if (
                not isinstance(run_id, str)
                or not 1 <= len(run_id) <= 128
                or set(item) != {"kind", "run_id"}
            ):
                raise ValueError("invalid_wait_run")
            result.append({"kind": kind, "run_id": run_id, "matched": None})
        else:
            raise ValueError("unknown_wait_condition")
    return result


class WorkWaitRepository:
    def __init__(self, repository: WorkRepository) -> None:
        self.repository = repository

    async def is_active(self, work_id: str) -> bool:
        async with self.repository.database.sessions() as session:
            return (
                await session.scalar(
                    select(waits.c.id).where(waits.c.work_id == work_id, waits.c.status == "active")
                )
                is not None
            )

    async def describe(self, work_id: str) -> dict[str, Any] | None:
        async with self.repository.database.sessions() as session:
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
            return {
                "wait_id": row["id"],
                "status": row["status"],
                "mode": row["mode"],
                "registered_at": row["created"],
                "deadline": row["deadline"],
                "conditions": conditions,
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
    ) -> dict[str, Any]:
        if mode not in {"any", "all"} or not 1 <= len(call_key) <= 256:
            raise ValueError("invalid_wait_registration")
        now = time.time()
        normalized = normalize_conditions(conditions, now)
        request_json = bounded_json(
            {"mode": mode, "conditions": conditions, "deadline_at": deadline_at}, 8192
        )
        deadline = _timestamp(deadline_at) if deadline_at is not None else None
        if deadline is not None and deadline <= now:
            raise ValueError("wait_deadline_in_past")
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
                        select(work).where(
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
            if row is None or json.loads(row["source_json"]) != source:
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
                    conditions_json=bounded_json(normalized, 8192),
                    status="active",
                    deadline=deadline,
                    created=now,
                    updated=now,
                )
            )
            return {
                "id": identity,
                "work_id": work_id,
                "status": "active",
                "mode": mode,
                "conditions": normalized,
                "deadline": deadline,
            }

    @staticmethod
    async def _deliver(
        session: AsyncSession,
        binding: dict[str, Any],
        conditions: list[dict[str, Any]],
        reason: str,
        now: float,
    ) -> int | None:
        original = (
            (await session.execute(select(work).where(work.c.id == binding["work_id"])))
            .mappings()
            .first()
        )
        conversation = await session.get(CanonicalConversationModel, binding["conversation_id"])
        if (
            original is None
            or conversation is None
            or conversation.generation != binding["generation"]
            or original["generation"] != binding["generation"]
            or original["state"] in {"completed", "failed", "cancelled"}
        ):
            await session.execute(
                update(waits)
                .where(waits.c.id == binding["id"], waits.c.status == "active")
                .values(status="invalidated", updated=now)
            )
            return None
        source = json.loads(original["source_json"])
        kind = source.get("principal_kind", "person")
        principal = "self" if kind == "self" else source.get("actor_person_id")
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
                    conditions_json=bounded_json(conditions, 8192),
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
                    event_id=max(event_ids) if event_ids else None,
                    ready=True,
                    payload_json=bounded_json(
                        {"text": json.dumps(payload, ensure_ascii=False), "signal": True}, 32768
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
        if source.get("owner") == "automation" and isinstance(source.get("automation_id"), int):
            from qq_ai_bot.persistence.models import AutomationModel

            await session.execute(
                update(AutomationModel)
                .where(
                    AutomationModel.id == source["automation_id"],
                    AutomationModel.status == "active",
                )
                .values(claimed_until=None)
            )
        return int(entry[0]) if entry else None

    async def match_event(
        self, *, event_id: int, kind: str, session: AsyncSession | None = None
    ) -> int | None:
        if kind not in {"conversation", "plugin_event"}:
            raise ValueError("invalid_wait_event_kind")
        if session is None:
            async with self.repository.database.sessions() as observer:
                event = await observer.get(ChatEventModel, event_id)
                if event is None or event.canonical_conversation_id is None:
                    return None
                active = await observer.scalar(
                    select(waits.c.id)
                    .where(
                        waits.c.conversation_id == event.canonical_conversation_id,
                        waits.c.status == "active",
                    )
                    .limit(1)
                )
                if active is None:
                    return None
            async with self.repository.database.immediate_session() as owned:
                return await self.match_event(event_id=event_id, kind=kind, session=owned)
        event = await session.get(ChatEventModel, event_id)
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
        rows = (
            (
                await session.execute(
                    select(waits)
                    .where(
                        waits.c.conversation_id == event.canonical_conversation_id,
                        waits.c.generation == conversation.generation,
                        waits.c.status == "active",
                    )
                    .order_by(waits.c.created)
                )
            )
            .mappings()
            .all()
        )
        now = time.time()
        matched_any = False
        delivered_input: int | None = None
        for binding in rows:
            conditions = json.loads(binding["conditions_json"])
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
                    "text": event.content[:7000],
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
                    await self._deliver(session, dict(binding), conditions, "signal", now)
                    or delivered_input
                )
                continue
            await session.execute(
                update(waits)
                .where(waits.c.id == binding["id"], waits.c.status == "active")
                .values(conditions_json=bounded_json(conditions, 8192), updated=now)
            )
        return (
            delivered_input
            if delivered_input is not None
            else (0 if matched_any and kind == "plugin_event" else None)
        )

    async def match_user_reply(self, event_id: int) -> int | None:
        """A reply to the Work's own question can resume waiting_user."""
        from qq_ai_bot.runtime.execution_receipts import PROCESS_ID
        from qq_ai_bot.social.db_models import SocialOperationModel

        async with self.repository.database.sessions() as observer:
            event = await observer.get(ChatEventModel, event_id)
            if event is None or event.reply_to_event_id is None:
                return None
        async with self.repository.database.immediate_session() as session:
            event = await session.get(ChatEventModel, event_id)
            if (
                event is None
                or event.direction != "inbound"
                or event.event_kind != "message"
                or event.reply_to_event_id is None
                or event.author_person_id is None
                or event.canonical_conversation_id is None
            ):
                return None
            conversation = await session.get(
                CanonicalConversationModel, event.canonical_conversation_id
            )
            if conversation is None:
                return None
            candidates = (
                (
                    await session.execute(
                        select(work)
                        .where(
                            work.c.conversation_id == conversation.id,
                            work.c.generation == conversation.generation,
                            work.c.state == "waiting_user",
                        )
                        .order_by(work.c.updated.desc())
                    )
                )
                .mappings()
                .all()
            )
            for candidate in candidates:
                source = json.loads(candidate["source_json"])
                if (
                    source.get("origin") != "user_message"
                    or source.get("actor_person_id") != event.author_person_id
                    or not isinstance(source.get("trigger_event_id"), int)
                ):
                    continue
                question = await session.scalar(
                    select(SocialOperationModel.id).where(
                        SocialOperationModel.source_conversation_id == conversation.id,
                        SocialOperationModel.source_turn_id
                        == f"{conversation.id}:event:{source['trigger_event_id']}",
                        SocialOperationModel.event_id == event.reply_to_event_id,
                        SocialOperationModel.status == "succeeded",
                    )
                )
                if question is None:
                    continue
                source_key = f"user-reply:{event.id}:{candidate['id']}"
                await session.execute(
                    insert(inputs)
                    .values(
                        conversation_id=conversation.id,
                        generation=conversation.generation,
                        source_key=source_key,
                        event_id=event.id,
                        work_id=candidate["id"],
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

    async def deliver_due(self, now: float | None = None) -> int:
        """Called by the automation clock; no separate timer loop or model polling."""
        from qq_ai_bot.runtime.subagent_schema import children
        from qq_ai_bot.sandbox.db_models import SandboxTaskRunModel

        count, now = 0, time.time() if now is None else now
        async with self.repository.database.immediate_session() as session:
            rows = (
                (
                    await session.execute(
                        select(waits).where(waits.c.status == "active").order_by(waits.c.created)
                    )
                )
                .mappings()
                .all()
            )
            for binding in rows:
                conversation = await session.get(
                    CanonicalConversationModel, binding["conversation_id"]
                )
                original_state = await session.scalar(
                    select(work.c.state).where(work.c.id == binding["work_id"])
                )
                if (
                    conversation is None
                    or conversation.generation != binding["generation"]
                    or original_state in {None, "completed", "failed", "cancelled"}
                ):
                    await session.execute(
                        update(waits)
                        .where(waits.c.id == binding["id"], waits.c.status == "active")
                        .values(status="invalidated", updated=now)
                    )
                    continue
                conditions = json.loads(binding["conditions_json"])
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
                        child = (
                            await session.execute(
                                select(children.c.state).where(
                                    children.c.root_id == binding["work_id"],
                                    children.c.work_id == condition["run_id"],
                                )
                            )
                        ).first()
                        state = child[0] if child else None
                        if state is None:
                            task = await session.scalar(
                                select(SandboxTaskRunModel).where(
                                    SandboxTaskRunModel.run_id == condition["run_id"]
                                )
                            )
                            if (
                                task
                                and json.loads(task.source_json).get("work_id")
                                == binding["work_id"]
                            ):
                                state = task.status
                        if state in {"completed", "succeeded", "failed", "cancelled", "uncertain"}:
                            condition["matched"] = {
                                "kind": "owned_run",
                                "run_id": condition["run_id"],
                                "status": state,
                            }
                            changed = True
                done = (
                    any(c["matched"] is not None for c in conditions)
                    if binding["mode"] == "any"
                    else all(c["matched"] is not None for c in conditions)
                )
                member_failed = any(
                    isinstance(c.get("matched"), dict)
                    and c["matched"].get("status") in {"failed", "cancelled", "uncertain"}
                    for c in conditions
                )
                expired = binding["deadline"] is not None and binding["deadline"] <= now
                if done or expired or member_failed:
                    if await self._deliver(
                        session,
                        dict(binding),
                        conditions,
                        "member_failed"
                        if member_failed
                        else ("deadline" if expired and not done else "signal"),
                        now,
                    ):
                        count += 1
                elif changed:
                    await session.execute(
                        update(waits)
                        .where(waits.c.id == binding["id"], waits.c.status == "active")
                        .values(conditions_json=bounded_json(conditions, 8192), updated=now)
                    )
        return count
