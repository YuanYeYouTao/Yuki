"""Record final-reply transport intents before entering the gateway."""

from __future__ import annotations

import hashlib
from typing import Any

from qq_ai_bot.domain.messages import OutboundMessage, OutboundSendReceipt
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_repository import WorkConflict


class WorkDeliverySender:
    def __init__(self, delegate: Any, control: WorkControl) -> None:
        self.delegate, self.control = delegate, control
        self.index = 0

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
        await control.session.save("delivery")
        if not await control.repository.prepare_effect(
            control.lease, control.current["id"], key, "final"
        ):
            raise WorkConflict("final_delivery_replay_forbidden")
        if control.current["sent_messages"] >= 16:
            raise WorkConflict("work_message_budget_exhausted")
        await control.repository.checkpoint(control.lease, control.current["id"], None, messages=1)
        control.current["sent_messages"] += 1
        try:
            receipt = await self.delegate.send(message)
            if not isinstance(receipt, OutboundSendReceipt):
                raise TypeError("work_sender_missing_receipt")
        except BaseException:
            await control.repository.record_effect(
                key, "unknown", {"error": "delivery_outcome_unknown"}
            )
            raise
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
