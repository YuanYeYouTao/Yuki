"""Record final-reply transport intents before entering the gateway."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Any

from qq_ai_bot.domain.messages import (
    AttachmentKind,
    OutboundMedia,
    OutboundMessage,
    OutboundSendReceipt,
)
from qq_ai_bot.runtime.delivery_intents import record, reserve
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_repository import WorkConflict


class WorkDeliverySender:
    def __init__(self, delegate: Any, control: WorkControl) -> None:
        self.delegate, self.control = delegate, control
        self.index = 0
        self.planned = False

    async def plan(self, messages: list[OutboundMessage]) -> None:
        control = self.control
        if control.current is None or control.session is None or not messages:
            return
        values = []
        for message in messages:
            value = asdict(message)
            for media in value["media"]:
                media["content"] = base64.b64encode(media["content"]).decode("ascii")
            values.append(value)
        control.session.progress["delivery_plan"] = values
        # Persist the entire sequence before reserving or sending any fragment.
        await control.session.save("delivery")
        await reserve(
            control,
            control.session.call_key("final-plan"),
            "final",
            {"plan_hash": hashlib.sha256(repr(values).encode()).hexdigest()},
            count=len(values),
        )
        self.planned = True

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    async def send(self, message: OutboundMessage) -> OutboundSendReceipt:
        control = self.control
        if control.current is None or control.session is None:
            receipt = await self.delegate.send(message)
            if not isinstance(receipt, OutboundSendReceipt):
                raise TypeError("work_sender_missing_receipt")
            return receipt
        await control.validate()
        if await control.pending():
            raise WorkConflict("new_input_before_final_delivery")
        self.index += 1
        key = control.session.call_key(f"final-{self.index}")
        if not self.planned:
            await self.plan([message])
        # The whole plan already owns its slots; these receipts track individual sends.
        await control.session.save("delivery")
        if not await control.repository.prepare_effect(
            control.lease, control.current["id"], key, "final"
        ):
            raise WorkConflict("final_delivery_replay_forbidden")
        await record(control, key, "dispatching", {})
        try:
            prepared_send = getattr(self.delegate, "send_prepared", None)
            receipt = (
                await prepared_send(message, key)
                if callable(prepared_send)
                else await self.delegate.send(message)
            )
            if not isinstance(receipt, OutboundSendReceipt):
                raise TypeError("work_sender_missing_receipt")
        except BaseException as exc:
            try:
                await record(control, key, "unknown", {})
                await control.repository.record_effect(
                    key, "unknown", {"error": "delivery_outcome_unknown"}
                )
            except Exception as secondary:
                exc.add_note(f"delivery reconciliation deferred: {type(secondary).__name__}")
            raise
        await record(control, key, "accepted", {"message_id": receipt.platform_message_id})
        await control.repository.record_effect(
            key,
            "accepted",
            {
                "transport_accepted": True,
                "message_id": receipt.platform_message_id,
                "transport": receipt.transport,
                "text": message.text,
                "media": [
                    {
                        "kind": media.kind.value,
                        "summary": media.summary,
                        "sha256": hashlib.sha256(media.content).hexdigest(),
                    }
                    for media in message.media
                ],
            },
        )
        return receipt


async def resume_delivery_plan(control: WorkControl, sender: Any) -> bool:
    """Only dispatch persisted, definitely unsubmitted parts; never regenerate an answer."""
    import json

    from sqlalchemy import select

    from qq_ai_bot.runtime.work_schema_v1 import effects

    if control.session is None:
        return False
    values = control.session.progress.get("delivery_plan")
    if not values:
        return False
    messages = [
        OutboundMessage(
            text=value["text"],
            reply_to_message_id=value.get("reply_to_message_id"),
            media=tuple(
                OutboundMedia(
                    **{
                        **media,
                        "kind": AttachmentKind(media["kind"]),
                        "content": base64.b64decode(media["content"], validate=True),
                    }
                )
                for media in value["media"]
            ),
        )
        for value in values
    ]
    wrapped = WorkDeliverySender(sender, control)
    await wrapped.plan(messages)
    for index, message in enumerate(messages, 1):
        key = control.session.call_key(f"final-{index}")
        async with control.repository.database.sessions() as session:
            previous = (
                (await session.execute(select(effects).where(effects.c.effect_key == key)))
                .mappings()
                .first()
            )
        if previous:
            if previous["state"] == "accepted" and json.loads(previous["receipt_json"]).get(
                "transport_accepted"
            ):
                wrapped.index = index
                continue
            raise WorkConflict("delivery_outcome_requires_reconciliation")
        await wrapped.send(message)
    control.final_delivery = True
    control.ending = "completed"
    await control.session.save("delivered")
    return True


async def deliver_final_text(
    control: WorkControl,
    text: str,
    deliver: Callable[[str, str], Awaitable[dict[str, Any]]],
) -> None:
    if control.session is None:
        raise WorkConflict("final_delivery_requires_journal")
    key = control.session.call_key("final-1")

    class Sender:
        async def send(self, message: OutboundMessage) -> OutboundSendReceipt:
            outcome = await deliver(message.text, key)
            if not outcome.get("transport_accepted"):
                raise WorkConflict("final_delivery_unconfirmed")
            return OutboundSendReceipt(str(outcome["message_id"]))

    await WorkDeliverySender(Sender(), control).send(OutboundMessage(text=text))
    control.final_delivery = True
    await control.session.save("delivered")


async def repair_receipt_ledger(control: WorkControl, ledger: Any) -> None:
    """Repair local evidence for accepted transport receipts; never call a gateway."""
    import json

    from sqlalchemy import select

    from qq_ai_bot.runtime.work_schema_v1 import effects

    if control.current is None:
        return
    original = await ledger.get_event(control.source.get("trigger_event_id"))
    if original is None:
        return
    async with control.repository.database.sessions() as session:
        rows = (
            (
                await session.execute(
                    select(effects)
                    .where(
                        effects.c.work_id == control.current["id"],
                        effects.c.state == "accepted",
                        effects.c.kind.in_(("progress", "final")),
                    )
                    .order_by(effects.c.created)
                    .limit(128)
                )
            )
            .mappings()
            .all()
        )
    for row in rows:
        receipt = json.loads(row["receipt_json"])
        if receipt.get("ledger_recorded") or not receipt.get("message_id"):
            continue
        if not isinstance(receipt.get("text"), str):
            continue
        await control.validate()
        if not await control.repository.valid(control.lease):
            raise WorkConflict("work_receipt_repair_obsolete")
        existing = await ledger.find_by_platform_message(
            bot_user_id=original.bot_user_id, platform_message_id=receipt["message_id"]
        )
        if existing is None:
            await ledger.append(
                bot_user_id=original.bot_user_id,
                platform_message_id=receipt["message_id"],
                scope_type=original.scope_type,
                sender_user_id=original.bot_user_id,
                direction="outbound",
                content=receipt["text"],
                group_id=original.group_id,
                private_peer_user_id=None if original.group_id else original.sender_user_id,
                sender_is_bot=True,
                origin="system_task",
                caused_by_event_id=original.id,
            )
        await control.repository.record_effect(
            row["effect_key"],
            "accepted",
            {
                **receipt,
                "ledger_recorded": True,
            },
        )
