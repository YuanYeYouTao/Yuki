"""History reports original Social facts separately from Work reservations."""

import json
import time
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import insert, select, update
from tests.integration.test_automation_lifecycle import calls
from tests.support.automation_unified_delivery_helpers import setup_run

from yuki.persistence.models import AutomationRunModel, AutomationVersionModel
from yuki.runtime.work_recovery_schema import deliveries, invocations
from yuki.runtime.work_schema_v1 import work
from yuki.social.db_models import SocialOperationModel
from yuki.social.repository import sequence_prefix
from yuki.social.source_keys import social_source_key


@pytest.mark.parametrize("state", ["succeeded", "failed", "uncertain", "executing"])
async def test_history_uses_original_send_receipt_without_changing_lifecycle(
    database, tmp_path, state
):
    case = await setup_run(database, tmp_path)
    case.clock.advance(2)
    original = await case.dispatch()
    async with database.immediate_session() as writer:
        await writer.execute(update(SocialOperationModel).values(status=state))
    _, (observed,) = await case.service.history(case.row.id, actor=case.actor)
    assert observed.status == original.status
    assert observed.messages_sent == original.messages_sent == 1
    assert observed.delivery["confirmed_parts"] == int(state == "succeeded")
    assert observed.delivery["failed_parts"] == int(state == "failed")
    assert observed.delivery["unconfirmed_parts"] == int(state in {"uncertain", "executing"})
    assert await case.repository.run_history(case.row.id) == (original,)


async def test_history_preserves_unknown_work_reservation_and_root_budget(database, tmp_path):
    case = await setup_run(database, tmp_path, mode="silent")
    case.provider._responder = lambda _: calls(
        ("accept-work", {"action": "accept", "goal": "继续原工作"}),
        (
            "wait-work",
            {"action": "wait", "conditions": [{"kind": "time_due", "after_seconds": 600}]},
        ),
    )
    case.clock.advance(2)
    original = await case.dispatch()
    async with database.immediate_session() as writer:
        accepted = (await writer.execute(select(work))).mappings().one()
        identity = str(uuid4())
        await writer.execute(
            insert(deliveries).values(
                id=identity,
                work_id=accepted["id"],
                kind="message",
                message_count=3,
                state="unknown",
                payload_json="{}",
                receipt_json="{}",
                created=time.time(),
                updated=time.time(),
            )
        )
        await writer.execute(update(work).values(sent_messages=3))
    _, (observed,) = await case.service.history(case.row.id, actor=case.actor)
    assert observed.delivery["confirmed_parts"] == 0
    assert observed.delivery["work_reservations"] == [
        {"id": identity, "work_id": accepted["id"], "state": "unknown", "message_count": 3}
    ]
    assert observed.status == original.status
    assert (await case.works.get(accepted["id"]))["sent_messages"] == 3


async def test_history_distinguishes_known_no_send_from_unlinked_legacy_counter(database, tmp_path):
    case = await setup_run(database, tmp_path, mode="silent")
    case.clock.advance(2)
    await case.dispatch()
    _, (known,) = await case.service.history(case.row.id, actor=case.actor)
    assert known.delivery["evidence"] == "no_send_recorded"
    assert known.delivery["confirmed_parts"] == 0
    async with database.immediate_session() as writer:
        await writer.execute(update(AutomationRunModel).values(messages_sent=2))
        await writer.execute(update(AutomationVersionModel).values(authority_snapshot_json=None))
    # No current definition or current scene may repair the old run's missing source.
    from yuki.automation.run_delivery import read_run_deliveries

    (old,) = await read_run_deliveries(database, await case.repository.run_history(case.row.id))
    assert old.messages_sent == 2
    assert old.delivery == {"evidence": "legacy_counter_only"}


def original_social_part(case, source_key, call_id, status, *, action="send_message", planned=None):
    now = datetime.now(UTC)
    return SocialOperationModel(
        id=str(uuid4()),
        source_turn_id=social_source_key(source_key),
        tool_call_id=call_id,
        source_conversation_id=case.env.context.conversation_id,
        action=action,
        payload_hash="original-payload-hash",
        target_kind="space",
        target_id=case.env.space,
        presence_id=case.env.presence,
        status=status,
        created_at=now,
        updated_at=now,
        planned_parts=planned,
    )


@pytest.mark.parametrize("valid_hash,valid_owner", [(True, True), (False, True), (True, False)])
async def test_legacy_cursor_and_step_receipts_keep_exact_original_ownership(
    database, tmp_path, valid_hash, valid_owner
):
    case = await setup_run(database, tmp_path, mode="silent")
    case.provider._responder = lambda _: calls(
        ("accept-original-work", {"action": "accept", "goal": "原付费工作"}),
        (
            "wait-original-work",
            {"action": "wait", "conditions": [{"kind": "time_due", "after_seconds": 600}]},
        ),
    )
    case.clock.advance(2)
    original = await case.dispatch()
    async with database.immediate_session() as writer:
        accepted = (await writer.execute(select(work))).mappings().one()
        version = await writer.get(AutomationVersionModel, original.version_id)
        version.script_json = json.dumps(
            {
                "name": "legacy",
                "timezone": "UTC",
                "schedule": {"type": "interval", "seconds": 60},
                "steps": [{"id": "original-agent", "call": "yuki.agent"}],
            }
        )
        source = json.loads(accepted["source_json"])
        if not valid_owner:
            source["automation_run_id"] = original.id + 999
        await writer.execute(
            update(work)
            .where(work.c.id == accepted["id"])
            .values(
                source_key="original-legacy-work-key",
                source_json=json.dumps(source),
                sent_messages=5,
            )
        )
        await writer.execute(
            insert(invocations).values(
                run_id=original.id,
                script_hash=version.script_hash if valid_hash else "another-definition-hash",
                phase="original-paused",
                payload_json=json.dumps({"work_id": accepted["id"]}),
                updated=time.time(),
            )
        )
        reservation_id = str(uuid4())
        await writer.execute(
            insert(deliveries).values(
                id=reservation_id,
                work_id=accepted["id"],
                kind="message",
                message_count=5,
                state="unknown",
                payload_json="{}",
                receipt_json="{}",
                created=time.time(),
                updated=time.time(),
            )
        )
        step_source = (
            f"{case.env.context.conversation_id}:execution:automation:"
            f"{original.id}:original-agent:{version.script_hash}"
        )
        writer.add(original_social_part(case, step_source, "original-step-send", "succeeded"))
    _, (observed,) = await case.service.history(case.row.id, actor=case.actor)
    assert observed.delivery["confirmed_parts"] == 1
    assert observed.delivery["work_reservations"] == (
        [
            {
                "id": reservation_id,
                "work_id": accepted["id"],
                "state": "unknown",
                "message_count": 5,
            }
        ]
        if valid_hash and valid_owner
        else []
    )
    assert (await case.works.get(accepted["id"]))["sent_messages"] == 5


async def test_split_parent_is_not_an_extra_sent_part(database, tmp_path):
    case = await setup_run(database, tmp_path, mode="silent")
    case.clock.advance(2)
    original = await case.dispatch()
    source = (
        f"{case.env.context.conversation_id}:execution:automation:"
        f"{original.id}:{case.row.script_hash}"
    )
    async with database.immediate_session() as writer:
        parent = original_social_part(
            case, source, "split-original", "uncertain", action="send_message_sequence", planned=3
        )
        writer.add(parent)
        writer.add(
            original_social_part(
                case, source, f"{sequence_prefix(parent.tool_call_id)}0", "succeeded"
            )
        )
        writer.add(
            original_social_part(
                case, source, f"{sequence_prefix(parent.tool_call_id)}1", "uncertain"
            )
        )
    _, (observed,) = await case.service.history(case.row.id, actor=case.actor)
    assert observed.delivery["confirmed_parts"] == 1
    assert observed.delivery["unconfirmed_parts"] == 1
    assert observed.delivery["not_sent_parts"] == 1
    assert observed.delivery["sequences"] == [
        {
            "operation_id": parent.id,
            "status": "uncertain",
            "planned_parts": 3,
        }
    ]
