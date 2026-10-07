"""Record final-reply transport intents before entering the gateway."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Awaitable, Callable
from copy import deepcopy
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
            control.session.delivery_call_key("final-plan"),
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
        await control.session.validate_delivery_source()
        if await control.pending():
            raise WorkConflict("new_input_before_final_delivery")
        self.index += 1
        key = control.session.delivery_call_key(f"final-{self.index}")
        if not self.planned:
            await self.plan([message])
        # The durable plan identifies the sequence; these receipts track individual sends.
        await control.session.save("delivery")
        if not await control.repository.prepare_effect(
            control.lease, control.current["id"], key, "final"
        ):
            raise WorkConflict("final_delivery_replay_forbidden")
        await record(control, key, "dispatching", {})
        dispatched = False
        try:
            # Preparation/receipt persistence can yield. Recheck the selected
            # source/privacy fence after those writers close, before the gateway.
            await control.validate()
            await control.session.validate_delivery_source()
            prepared_send = getattr(self.delegate, "send_prepared", None)
            dispatched = True
            receipt = (
                await prepared_send(message, key)
                if callable(prepared_send)
                else await self.delegate.send(message)
            )
            if not isinstance(receipt, OutboundSendReceipt):
                raise TypeError("work_sender_missing_receipt")
        except BaseException as exc:
            try:
                state = "unknown" if dispatched else "failed"
                failure = (
                    {"error": "delivery_outcome_unknown"}
                    if dispatched
                    else {
                        "error": "delivery_not_dispatched",
                        "executed": False,
                        "mutation_committed": False,
                    }
                )
                await record(control, key, state, failure)
                await control.repository.record_effect(key, state, failure)
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


def _persisted_message(value: dict[str, Any]) -> OutboundMessage:
    """Decode an executable copy, never upgrade the frozen delivery journal."""
    legacy_defaults = {
        "spoken_text": "",
        "generation_id": None,
        "voice_profile_id": None,
        "voice_reference_key": None,
        "voice_language": None,
    }
    decoded = deepcopy(value)
    try:
        if set(decoded) - {"text", "reply_to_message_id", "media"}:
            raise ValueError("unknown message field")
        if not isinstance(decoded["text"], str):
            raise ValueError("invalid message text")
        media_values = []
        for media in decoded["media"]:
            if AttachmentKind(media["kind"]) is AttachmentKind.AUDIO:
                raise WorkConflict("retired_speech_delivery")
            if AttachmentKind(media["kind"]) is not AttachmentKind.IMAGE:
                raise ValueError("unsupported media")
            for key, default in legacy_defaults.items():
                if key in media:
                    if type(media[key]) is not type(default) or media[key] != default:
                        raise ValueError("nondefault retired metadata")
                    del media[key]
            # These formerly shared DTO fields lost their last output consumer;
            # old images carried only None. Keep this separate from speech tags.
            for key in ("local_path", "duration_milliseconds"):
                if key in media:
                    if media[key] is not None:
                        raise ValueError("nondefault retired media metadata")
                    del media[key]
            media["kind"] = AttachmentKind.IMAGE
            media["content"] = base64.b64decode(media["content"], validate=True)
            media_values.append(OutboundMedia(**media))
        decoded["media"] = tuple(media_values)
        return OutboundMessage(**decoded)
    except (KeyError, TypeError, ValueError) as exc:
        raise WorkConflict("persisted_delivery_plan_invalid") from exc


def _frozen_plan_hash(values: list[dict[str, Any]]) -> str:
    # asdict originally retained tuple media and StrEnum kinds. JSON changes
    # those two representations; restore them without removing/reordering fields.
    original = deepcopy(values)
    for value in original:
        value["media"] = tuple(
            {**media, "kind": AttachmentKind(media["kind"])} for media in value["media"]
        )
    return hashlib.sha256(repr(original).encode()).hexdigest()


async def resume_delivery_plan(control: WorkControl, sender: Any) -> bool:
    """Only dispatch persisted, definitely unsubmitted parts; never regenerate an answer."""
    import json

    from sqlalchemy import select

    from qq_ai_bot.runtime.work_recovery_schema import deliveries
    from qq_ai_bot.runtime.work_schema_v1 import effects

    if control.session is None:
        return False
    values = control.session.progress.get("delivery_plan")
    if not values:
        return False
    if control.current is None or not isinstance(values, list):
        raise WorkConflict("persisted_delivery_plan_invalid")
    keys = [
        control.session.delivery_call_key(f"final-{index}") for index in range(1, len(values) + 1)
    ]
    # Read original receipts before attempting to decode obsolete AUDIO/metadata.
    async with control.repository.database.sessions() as session:
        rows = await session.execute(select(effects).where(effects.c.effect_key.in_(keys)))
        previous_by_key = {row["effect_key"]: row for row in rows.mappings()}
        intent = (
            (
                await session.execute(
                    select(deliveries).where(
                        deliveries.c.id == control.session.delivery_call_key("final-plan")
                    )
                )
            )
            .mappings()
            .first()
        )
        issued_final = await session.scalar(
            select(effects.c.effect_key)
            .where(effects.c.work_id == control.current["id"], effects.c.kind == "final")
            .limit(1)
        )
        existing_plan = await session.scalar(
            select(deliveries.c.id)
            .where(deliveries.c.work_id == control.current["id"], deliveries.c.kind == "final")
            .limit(1)
        )
    remaining = []
    for index, (key, value) in enumerate(zip(keys, values, strict=True), 1):
        previous = previous_by_key.get(key)
        if previous:
            if (
                previous["work_id"] == control.current["id"]
                and previous["state"] == "accepted"
                and json.loads(previous["receipt_json"]).get("transport_accepted") is True
            ):
                continue
            raise WorkConflict("delivery_outcome_requires_reconciliation")
        remaining.append((index, value))
    if intent:
        try:
            payload = json.loads(intent["payload_json"])
            if (
                intent["work_id"] != control.current["id"]
                or intent["kind"] != "final"
                or intent["message_count"] != len(values)
                or payload != {"plan_hash": _frozen_plan_hash(values)}
            ):
                raise WorkConflict("delivery_intent_conflict")
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkConflict("delivery_intent_conflict") from exc
        if intent["state"] in {"unknown", "dispatching"} or (
            remaining and intent["state"] == "accepted"
        ):
            raise WorkConflict("delivery_replay_forbidden")
    elif remaining and (issued_final is not None or existing_plan is not None):
        # Receipts prove an earlier dispatch but cannot reconstruct its missing
        # reservation/accounting. Re-reserving would grant another send budget.
        raise WorkConflict("delivery_outcome_requires_reconciliation")
    # Validate *all* unsubmitted fragments before reserving or sending any of them.
    messages = [(index, _persisted_message(value)) for index, value in remaining]
    if messages:
        payload = payload if intent else {"plan_hash": _frozen_plan_hash(values)}
        await reserve(
            control,
            control.session.delivery_call_key("final-plan"),
            "final",
            payload,
            count=len(values),
        )
    wrapped = WorkDeliverySender(sender, control)
    wrapped.planned = True
    for index, message in messages:
        wrapped.index = index - 1
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
    key = control.session.delivery_call_key("final-1")

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
