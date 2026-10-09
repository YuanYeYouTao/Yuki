"""Legacy frozen delivery plans are reconciled offline; nothing is ever re-sent."""

import base64
import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select, update
from tests.support.work_session import WorkSession
from tests.unit.test_runtime_recovery import setup

from qq_ai_bot.domain.messages import AttachmentKind
from qq_ai_bot.runtime.delivery_intents import reserve
from qq_ai_bot.runtime.work_delivery import LEGACY_DELIVERY_PAUSE, import_legacy_deliveries
from qq_ai_bot.runtime.work_recovery_schema import deliveries
from qq_ai_bot.runtime.work_schema_v1 import effects
from qq_ai_bot.services.turn_transcript import TurnTranscript


def old_message(*, kind="image", text="original"):
    # Exact pre-retirement DTO field order and defaults, independent of the new DTO.
    media = {
        "kind": AttachmentKind(kind),
        "content": base64.b64encode(b"pixels").decode(),
        "mime_type": "image/png",
        "summary": "original image",
        "emoji_id": None,
        "animated": False,
        "local_path": None,
        "spoken_text": "",
        "generation_id": None,
        "voice_profile_id": None,
        "voice_reference_key": None,
        "voice_language": None,
        "duration_milliseconds": None,
    }
    return {"text": text, "reply_to_message_id": None, "media": (media,)}


async def freeze(control, values):
    original_hash = hashlib.sha256(repr(values).encode()).hexdigest()
    control.session.progress["delivery_plan"] = values
    await control.session.save("delivery")
    await reserve(
        control,
        control.session.call_key("final-plan"),
        "final",
        {"plan_hash": original_hash},
        count=len(values),
    )
    control.session = WorkSession(control, "contract")
    await control.session.restore(TurnTranscript(()))
    return deepcopy(control.session.progress["delivery_plan"]), original_hash


async def accept(control, index, *, state="accepted"):
    key = control.session.call_key(f"final-{index}")
    assert await control.repository.prepare_effect(
        control.lease, control.current["id"], key, "final"
    )
    if state != "prepared":
        await control.repository.record_effect(
            key,
            state,
            {"transport_accepted": True, "message_id": "original-confirmation"}
            if state == "accepted"
            else {},
        )


class Ledger:
    """Records ledger appends; there is no gateway on this path at all."""

    def __init__(self, event=None):
        self.event, self.appended = event, []

    async def get_event(self, _identity):
        return self.event

    async def find_by_platform_message(self, **_):
        return None

    async def append(self, **values):
        self.appended.append(values)


async def run(control, *, dry_run=False, ledger=None):
    await control.repository.release(control.lease)
    return await import_legacy_deliveries(
        control.repository.database, ledger or Ledger(), dry_run=dry_run
    )


async def work_row(control):
    return await control.repository.get(control.current["id"])


async def snapshot(control):
    async with control.repository.database.sessions() as session:
        return (
            tuple(
                (
                    await session.execute(
                        select(deliveries).where(deliveries.c.work_id == control.current["id"])
                    )
                ).mappings()
            ),
            tuple(
                (
                    await session.execute(
                        select(effects).where(effects.c.work_id == control.current["id"])
                    )
                ).mappings()
            ),
        )


@pytest.mark.asyncio
async def test_unsent_image_plan_is_recorded_not_sent_and_pauses(database, tmp_path):
    control = await setup(database, tmp_path)
    await freeze(control, [old_message()])
    budget = control.current["sent_messages"]
    report = await run(control)
    assert (report.plans, report.paused, report.not_sent_recorded) == (1, 1, 1)
    row = await work_row(control)
    assert (row["state"], row["reason"]) == ("suspended", LEGACY_DELIVERY_PAUSE)
    assert row["sent_messages"] == budget
    effect = (await snapshot(control))[1][0]
    assert effect["state"] == "failed"
    assert json.loads(effect["receipt_json"])["error"] == "delivery_not_dispatched"
    # The original plan reservation and hash are untouched.
    assert json.loads((await snapshot(control))[0][0]["payload_json"])["plan_hash"]


@pytest.mark.asyncio
async def test_dry_run_writes_nothing_and_rerun_is_idempotent(database, tmp_path):
    control = await setup(database, tmp_path)
    await freeze(control, [old_message(), old_message(text="second")])
    before = await snapshot(control)
    state = (await work_row(control))["state"]
    dry = await run(control, dry_run=True)
    assert dry.paused == 1 and await snapshot(control) == before
    assert (await work_row(control))["state"] == state
    await import_legacy_deliveries(control.repository.database, Ledger())
    after = await snapshot(control)
    # The journal is no longer executable, so a second run finds nothing.
    again = await import_legacy_deliveries(control.repository.database, Ledger())
    assert again.plans == 0 and await snapshot(control) == after


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", [False, True])
async def test_confirmed_audio_is_only_reconciled_never_resent(database, tmp_path, partial):
    control = await setup(database, tmp_path)
    audio = old_message(kind="audio")
    audio["media"][0].update(content="not-base64", local_path="deleted.wav", generation_id=19)
    values = [audio, old_message(text="remaining")] if partial else [audio]
    await freeze(control, values)
    await accept(control, 1)
    report = await run(control)
    row = await work_row(control)
    if partial:
        assert report.paused == 1 and row["reason"] == LEGACY_DELIVERY_PAUSE
    else:
        assert report.completed == 1 and row["state"] == "completed"
    receipts = {item["effect_key"]: item for item in (await snapshot(control))[1]}
    assert receipts[control.session.call_key("final-1")]["state"] == "accepted"


@pytest.mark.asyncio
async def test_unsubmitted_audio_is_never_sent(database, tmp_path):
    control = await setup(database, tmp_path)
    await freeze(control, [old_message(), old_message(kind="audio")])
    report = await run(control)
    assert report.not_sent_recorded == 2 and report.paused == 1
    assert {item["state"] for item in (await snapshot(control))[1]} == {"failed"}


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["prepared", "unknown"])
async def test_unknown_or_prepared_shard_stays_unresolved(database, tmp_path, state):
    control = await setup(database, tmp_path)
    await freeze(control, [old_message(), old_message(kind="audio")])
    await accept(control, 2, state=state)
    before = await snapshot(control)
    report = await run(control)
    assert report.unresolved == 1 and await snapshot(control) == before


@pytest.mark.asyncio
async def test_saved_plan_before_reservation_is_definitely_not_sent(database, tmp_path):
    control = await setup(database, tmp_path)
    control.session.progress["delivery_plan"] = [old_message()]
    await control.session.save("delivery")
    budget = control.current["sent_messages"]
    report = await run(control)
    assert report.paused == 1 and report.not_sent_recorded == 1
    assert (await work_row(control))["sent_messages"] == budget
    assert (await snapshot(control))[0] == ()  # No reservation is created.


@pytest.mark.asyncio
async def test_cancelled_owner_is_not_imported(database, tmp_path):
    control = await setup(database, tmp_path)
    await freeze(control, [old_message(kind="audio"), old_message()])
    await control.repository.cancel(control.lease.conversation_id)
    before = await snapshot(control)
    report = await run(control)
    assert report.plans == 0 and await snapshot(control) == before


@pytest.mark.asyncio
async def test_tampered_frozen_plan_cannot_reuse_original_intent_hash(database, tmp_path):
    control = await setup(database, tmp_path)
    await freeze(control, [old_message()])
    control.session.progress["delivery_plan"][0]["text"] = "changed after reservation"
    await control.session.save("delivery")
    before = await snapshot(control)
    report = await run(control)
    assert report.invalid == 1 and await snapshot(control) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change,outcome",
    [
        ({"kind": "progress"}, "invalid"),
        ({"message_count": 3}, "invalid"),
        ({"payload_json": '{"plan_hash":"wrong"}'}, "invalid"),
        ({"state": "unknown"}, "unresolved"),
        ({"state": "dispatching"}, "unresolved"),
        ({"state": "accepted"}, "unresolved"),
    ],
)
async def test_original_plan_intent_count_hash_and_state_fence_import(
    database, tmp_path, change, outcome
):
    control = await setup(database, tmp_path)
    await freeze(control, [old_message()])
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(deliveries)
            .where(deliveries.c.id == control.session.call_key("final-plan"))
            .values(**change)
        )
    before = await snapshot(control)
    report = await run(control)
    assert getattr(report, outcome) == 1 and await snapshot(control) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("remaining", [False, True])
async def test_missing_plan_intent_with_accepted_audio_never_recharges_budget(
    database, tmp_path, remaining
):
    control = await setup(database, tmp_path)
    values = [old_message(kind="audio")]
    if remaining:
        values.append(old_message())
    await freeze(control, values)
    await accept(control, 1)
    async with database.sessions() as session, session.begin():
        await session.execute(
            delete(deliveries).where(deliveries.c.id == control.session.call_key("final-plan"))
        )
    budget = control.current["sent_messages"]
    report = await run(control)
    if remaining:
        assert report.unresolved == 1
    else:
        assert report.completed == 1
    assert (await work_row(control))["sent_messages"] == budget
    assert (await snapshot(control))[0] == ()


@pytest.mark.asyncio
async def test_accepted_shards_repair_every_missing_ledger_row(database, tmp_path):
    control = await setup(database, tmp_path)
    values = [old_message(text=f"part-{index}") for index in range(130)]
    await freeze(control, values)
    for index in range(1, 131):
        key = control.session.call_key(f"final-{index}")
        assert await control.repository.prepare_effect(
            control.lease, control.current["id"], key, "final"
        )
        await control.repository.record_effect(
            key,
            "accepted",
            {"transport_accepted": True, "message_id": f"m{index}", "text": f"part-{index}"},
        )
    event = SimpleNamespace(
        id=1, bot_user_id="80001", scope_type="group", group_id="20001", sender_user_id="1"
    )
    ledger = Ledger(event)
    report = await run(control, ledger=ledger)
    assert report.completed == 1 and report.ledger_repaired == 130
    assert len(ledger.appended) == 130
    assert all(
        json.loads(item["receipt_json"]).get("ledger_recorded")
        for item in (await snapshot(control))[1]
    )
