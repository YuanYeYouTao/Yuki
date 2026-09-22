"""Fenced delivery identities and receipts, without a send-frequency quota."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, true, update
from sqlalchemy.dialects.sqlite import insert

from qq_ai_bot.runtime.work_recovery_schema import deliveries
from qq_ai_bot.runtime.work_repository import WorkConflict, bounded_json
from qq_ai_bot.runtime.work_schema_v1 import work

if TYPE_CHECKING:
    from qq_ai_bot.runtime.work_control import WorkControl


async def reserve(
    control: WorkControl, key: str, kind: str, payload: dict[str, Any], *, count: int = 1
) -> bool:
    if control.current is None:
        return True
    now = time.time()
    identity = control.current["id"]
    target_value = payload.get("target")
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise WorkConflict("delivery_message_count_invalid")
    encoded_payload = bounded_json(payload)
    async with control.repository.database.immediate_session() as session:
        await control.repository._assert_lease(session, control.lease)
        if isinstance(target_value, dict) and target_value.get("kind") in {"person", "space"}:
            target = f"{target_value['kind']}:{target_value['id']}"
        else:
            from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel

            conversation = await session.get(
                CanonicalConversationModel, control.lease.conversation_id
            )
            if conversation is None:
                raise WorkConflict("delivery_conversation_missing")
            target = (
                f"space:{conversation.space_id}"
                if conversation.space_id
                else f"person:{conversation.person_id}"
                if conversation.person_id
                else f"conversation:{conversation.id}"
            )
        prior = (
            (await session.execute(select(deliveries).where(deliveries.c.id == key)))
            .mappings()
            .first()
        )
        if prior:
            if (
                prior["work_id"] != identity
                or prior["kind"] != kind
                or prior["message_count"] != count
                or prior["payload_json"] != encoded_payload
            ):
                raise WorkConflict("delivery_intent_conflict")
            if prior["state"] == "reserved":
                return True
            if prior["state"] in {"accepted", "unknown", "dispatching"}:
                raise WorkConflict("delivery_replay_forbidden")
        if prior:
            await session.execute(
                update(deliveries)
                .where(deliveries.c.id == key)
                .values(
                    state="reserved",
                    target_key=target,
                    created=now,
                    updated=now,
                    not_before=0,
                )
            )
        else:
            await session.execute(
                insert(deliveries).values(
                    id=key,
                    work_id=identity,
                    kind=kind,
                    target_key=target,
                    message_count=count,
                    not_before=0,
                    state="reserved",
                    payload_json=encoded_payload,
                    created=now,
                    updated=now,
                )
            )
        total = await session.scalar(
            update(work)
            .where(work.c.id == identity)
            .values(sent_messages=work.c.sent_messages + count)
            .returning(work.c.sent_messages)
        )
        control.current["sent_messages"] = total
    return True


async def record(control: WorkControl, key: str, state: str, receipt: dict[str, Any]) -> None:
    async with control.repository.database.sessions() as session, session.begin():
        await session.execute(
            update(deliveries)
            .where(
                deliveries.c.id == key,
                deliveries.c.state != "accepted",
                (deliveries.c.state != "unknown") if state != "accepted" else true(),
            )
            .values(state=state, receipt_json=bounded_json(receipt), updated=time.time())
        )
