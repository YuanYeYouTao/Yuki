"""Retired speech cannot corrupt frozen image plans or authorize another send."""

import base64
import hashlib
import json
from copy import deepcopy

import pytest
from sqlalchemy import delete, select, update
from tests.support.work_session import WorkSession
from tests.unit.test_runtime_recovery import Sender, setup

from qq_ai_bot.domain.messages import AttachmentKind
from qq_ai_bot.runtime.delivery_intents import reserve
from qq_ai_bot.runtime.work_delivery import resume_delivery_plan
from qq_ai_bot.runtime.work_recovery_schema import deliveries
from qq_ai_bot.runtime.work_repository import WorkConflict
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
async def test_old_image_keeps_frozen_hash_and_original_budget_through_restart(database, tmp_path):
    control = await setup(database, tmp_path)
    frozen, digest = await freeze(control, [old_message()])
    budget = control.current["sent_messages"]
    sender = Sender()
    assert await resume_delivery_plan(control, sender)
    assert sender.messages == ["original"]
    assert control.session.progress["delivery_plan"] == frozen
    assert control.current["sent_messages"] == budget
    assert (await snapshot(control))[0][0]["payload_json"] == json.dumps(
        {"plan_hash": digest}, ensure_ascii=False, sort_keys=True, allow_nan=False
    )
    assert await resume_delivery_plan(control, sender)
    assert sender.messages == ["original"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,bad",
    [
        ("spoken_text", None),
        ("spoken_text", "spoken"),
        ("generation_id", 0),
        ("generation_id", False),
        ("voice_profile_id", ""),
        ("voice_reference_key", {}),
        ("voice_language", "zh"),
        ("local_path", "/old.wav"),
        ("duration_milliseconds", 0),
        ("unknown_media_field", None),
    ],
)
async def test_nondefault_metadata_rejected_before_any_partial_dispatch(
    database, tmp_path, field, bad
):
    control = await setup(database, tmp_path)
    bad_message = old_message()
    bad_message["media"][0][field] = bad
    frozen, _ = await freeze(control, [old_message(text="first"), bad_message])
    before = await snapshot(control)
    sender = Sender()
    with pytest.raises(WorkConflict, match="persisted_delivery_plan_invalid"):
        await resume_delivery_plan(control, sender)
    assert sender.messages == []
    assert await snapshot(control) == before
    assert control.session.progress["delivery_plan"] == frozen


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", [False, True])
async def test_confirmed_audio_is_read_without_dto_or_file_access(database, tmp_path, partial):
    control = await setup(database, tmp_path)
    audio = old_message(kind="audio")
    audio["media"][0].update(content="not-base64", local_path="deleted.wav", generation_id=19)
    values = [audio, old_message(text="remaining")] if partial else [audio]
    frozen, _ = await freeze(control, values)
    await accept(control, 1)
    sender = Sender()
    assert await resume_delivery_plan(control, sender)
    assert sender.messages == (["remaining"] if partial else [])
    assert control.session.progress["delivery_plan"] == frozen
    assert (await snapshot(control))[1][0]["state"] == "accepted"


@pytest.mark.asyncio
async def test_unsubmitted_audio_blocks_entire_remaining_plan_without_replacement(
    database, tmp_path
):
    control = await setup(database, tmp_path)
    frozen, _ = await freeze(control, [old_message(), old_message(kind="audio")])
    before = await snapshot(control)
    sender = Sender()
    with pytest.raises(WorkConflict, match="retired_speech_delivery"):
        await resume_delivery_plan(control, sender)
    assert sender.messages == [] and await snapshot(control) == before
    assert control.session.progress["delivery_plan"] == frozen


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["prepared", "unknown"])
async def test_unknown_or_prepared_audio_receipt_wins_over_retired_schema(
    database, tmp_path, state
):
    control = await setup(database, tmp_path)
    await freeze(control, [old_message(), old_message(kind="audio")])
    await accept(control, 2, state=state)
    before = await snapshot(control)
    sender = Sender()
    with pytest.raises(WorkConflict, match="requires_reconciliation"):
        await resume_delivery_plan(control, sender)
    assert sender.messages == [] and await snapshot(control) == before


@pytest.mark.asyncio
async def test_saved_plan_before_reservation_crash_retains_original_identity(database, tmp_path):
    control = await setup(database, tmp_path)
    values = [old_message()]
    digest = hashlib.sha256(repr(values).encode()).hexdigest()
    control.session.progress["delivery_plan"] = values
    await control.session.save("delivery")
    control.session = WorkSession(control, "contract")
    await control.session.restore(TurnTranscript(()))
    frozen = deepcopy(control.session.progress["delivery_plan"])
    sender = Sender()
    assert await resume_delivery_plan(control, sender)
    assert json.loads((await snapshot(control))[0][0]["payload_json"])["plan_hash"] == digest
    assert control.session.progress["delivery_plan"] == frozen


@pytest.mark.asyncio
async def test_cancelled_owner_cannot_send_but_late_confirmation_survives(database, tmp_path):
    control = await setup(database, tmp_path)
    await freeze(control, [old_message(kind="audio"), old_message()])
    await accept(control, 1, state="unknown")
    await control.repository.cancel(control.lease.conversation_id)
    key = control.session.call_key("final-1")
    await control.repository.record_effect(key, "accepted", {"transport_accepted": True})
    sender = Sender()
    with pytest.raises(WorkConflict):
        await resume_delivery_plan(control, sender)
    assert sender.messages == []
    assert (await snapshot(control))[1][0]["state"] == "accepted"


@pytest.mark.asyncio
async def test_tampered_frozen_plan_cannot_reuse_original_intent_hash(database, tmp_path):
    control = await setup(database, tmp_path)
    _, digest = await freeze(control, [old_message()])
    control.session.progress["delivery_plan"][0]["text"] = "changed after reservation"
    before = await snapshot(control)
    sender = Sender()
    with pytest.raises(WorkConflict, match="delivery_intent_conflict"):
        await resume_delivery_plan(control, sender)
    assert sender.messages == [] and await snapshot(control) == before
    assert json.loads(before[0][0]["payload_json"])["plan_hash"] == digest


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"work_id": "foreign-work"},
        {"kind": "progress"},
        {"message_count": 3},
        {"payload_json": '{"plan_hash":"wrong"}'},
        {"state": "unknown"},
        {"state": "dispatching"},
        {"state": "accepted"},
    ],
)
async def test_original_plan_intent_ownership_count_hash_and_state_fence_dispatch(
    database, tmp_path, change
):
    control = await setup(database, tmp_path)
    frozen, _ = await freeze(control, [old_message()])
    change = dict(change)
    if "work_id" in change:
        foreign = await control.repository.accept(
            control.lease, source_key="foreign-work", source={}, goal="unrelated owner"
        )
        change["work_id"] = foreign["id"]
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(deliveries)
            .where(deliveries.c.id == control.session.call_key("final-plan"))
            .values(**change)
        )
    before = await snapshot(control)
    sender = Sender()
    with pytest.raises(WorkConflict, match=r"delivery_(intent_conflict|replay_forbidden)"):
        await resume_delivery_plan(control, sender)
    assert sender.messages == [] and await snapshot(control) == before
    assert control.session.progress["delivery_plan"] == frozen


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
    before = await snapshot(control)
    budget = control.current["sent_messages"]
    sender = Sender()
    if remaining:
        with pytest.raises(WorkConflict, match="requires_reconciliation"):
            await resume_delivery_plan(control, sender)
    else:
        assert await resume_delivery_plan(control, sender)
    assert sender.messages == [] and await snapshot(control) == before
    assert control.current["sent_messages"] == budget


@pytest.mark.asyncio
async def test_image_without_legacy_fields_is_a_supported_reading_view(database, tmp_path):
    control = await setup(database, tmp_path)
    value = old_message()
    for field in (
        "spoken_text",
        "generation_id",
        "voice_profile_id",
        "voice_reference_key",
        "voice_language",
        "local_path",
        "duration_milliseconds",
    ):
        del value["media"][0][field]
    frozen, _ = await freeze(control, [value])
    sender = Sender()
    assert await resume_delivery_plan(control, sender)
    assert sender.messages == ["original"]
    assert control.session.progress["delivery_plan"] == frozen
