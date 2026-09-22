"""Automation delivery checks use durable whole-call receipts, not send counters."""

import json
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from tests.support.social_identity_cases import social_env

from qq_ai_bot.automation.agent_delivery import inspect_agent_delivery
from qq_ai_bot.runtime.work_recovery_schema import deliveries
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import effects, journal, work
from qq_ai_bot.social.models import OperationStatus, SocialTarget


async def setup_case(database, tmp_path):
    env = await social_env(database, tmp_path)
    execution = f"automation:23:execute:{'a' * 64}"
    source = f"{env.context.conversation_id}:execution:{execution}"
    repo = WorkRepository(database)
    lease = await repo.acquire(env.context.conversation_id, 1)
    row = await repo.accept(
        lease,
        source_key="agent-delivery-test",
        goal="deliver result",
        output_kind="answer",
        source={"owner": "automation", "parent_execution_id": execution},
    )
    row = await repo.transition(lease, row["id"], row["revision"], "completed")
    return SimpleNamespace(env=env, repo=repo, lease=lease, row=row, source=source, db=database)


async def inspect(case, **kwargs):
    return await inspect_agent_delivery(
        case.db,
        conversation_id=case.env.context.conversation_id,
        run_id=23,
        step_id="execute",
        script_hash="a" * 64,
        target_kind="space",
        target_id=case.env.space,
        **kwargs,
    )


async def social(case, call, *, status="succeeded", action="send_message", source=None):
    receipt = await case.env.service.receipts.prepare(
        source_turn_id=source or case.source,
        tool_call_id=call,
        source_conversation_id=case.env.context.conversation_id,
        action=action,
        target=SocialTarget(kind="space", id=case.env.space),
        payload={"text": call},
    )
    if status != "prepared":
        await case.env.service.receipts.claim(receipt.operation_id, presence_id=case.env.presence)
        async with case.db.sessions() as session, session.begin():
            await case.env.service.receipts.finish(
                receipt.operation_id,
                status=OperationStatus(status),
                session=session,
                platform_reference="42" if status == "succeeded" else None,
            )
    return (await case.env.service.receipts.get(receipt.operation_id)).model_dump(mode="json")


async def aggregate(case, call, body=None, *, ok=True, uncertain=False):
    key = f"chain:0:{call}"
    result = {"tool_name": "send_message", "ok": ok, "data": body, "uncertain": uncertain}
    await saved_effect(case, key, result)


async def saved_effect(case, key, result):
    # Seed historical committed receipts for the completed work under inspection.
    async with case.db.sessions() as session, session.begin():
        await session.execute(
            insert(effects).values(
                effect_key=key,
                work_id=case.row["id"],
                kind="tool",
                state="accepted",
                receipt_json=json.dumps({"result": json.dumps(result)}),
                created=time.time(),
                updated=time.time(),
            )
        )


@pytest.mark.asyncio
async def test_delivery_failure_survives_more_than_64_other_tools_and_can_be_repaired(
    database, tmp_path, monkeypatch
):
    case = await setup_case(database, tmp_path)
    assert (await inspect(case)).state == "none"
    await aggregate(case, "bad", ok=False)
    for index in range(66):
        key = f"chain:0:read-{index}"
        await saved_effect(case, key, {"tool_name": "workspace_read", "ok": True})
    assert (await inspect(case)).state == "failed"
    receipt = await social(case, "good")
    await aggregate(case, "good", receipt)
    assert (await inspect(case)).state == "succeeded"
    # Repeated result consumption does not depend on the old backend instance.
    assert (await inspect(case)).state == "succeeded"
    await aggregate(case, "new-failure", ok=False)
    assert (await inspect(case)).state == "failed"
    # Real reclamation retains the earlier successful Social receipt and intent,
    # but removes the later failed tool result. Those survivors cannot upgrade it.
    source = json.loads(case.row["source_json"])
    source["delivery_contract"] = "return_to_caller"
    async with database.sessions() as session, session.begin():
        await session.execute(
            insert(deliveries).values(
                id=receipt["operation_id"],
                work_id=case.row["id"],
                kind="message",
                state="accepted",
                created=1,
                updated=1,
                payload_json=json.dumps({"arguments": {"text": "earlier result"}}),
            )
        )
        await session.execute(
            update(work)
            .where(work.c.id == case.row["id"])
            .values(
                source_json=json.dumps(source),
                updated=time.time() - 8 * 86400,
            )
        )
        await session.execute(
            insert(work),
            [
                {
                    **case.row,
                    "id": str(uuid4()),
                    "source_key": f"archive-filler:{index}",
                    "source_json": "{}",
                    "updated": time.time(),
                    "created": time.time(),
                }
                for index in range(128)
            ],
        )
    execute = AsyncSession.execute
    reclaimed_during_read = False

    async def reclaim_after_work_read(session, statement, *args, **kwargs):
        nonlocal reclaimed_during_read
        result = await execute(session, statement, *args, **kwargs)
        if not reclaimed_during_read and str(statement).startswith(
            "SELECT runtime_work.id, runtime_work.state"
        ):
            reclaimed_during_read = True
            await case.repo.reclaim_terminal()
        return result

    monkeypatch.setattr(AsyncSession, "execute", reclaim_after_work_read)
    # Reclamation can commit while this reader holds its snapshot. The first
    # inspection must still see the failure; later inspections see the tombstone.
    assert (await inspect(case)).state == "failed"
    assert reclaimed_during_read
    async with database.sessions() as session:
        checkpoint = await session.scalar(
            select(work.c.checkpoint_json).where(work.c.id == case.row["id"])
        )
        assert json.loads(checkpoint)["archived"] is True
        assert not (
            await session.execute(select(effects).where(effects.c.work_id == case.row["id"]))
        ).first()
    outcome = await inspect(case)
    assert outcome.state == "uncertain"
    assert outcome.reason == "agent_delivery_archived"


@pytest.mark.asyncio
async def test_unknown_send_cannot_be_overwritten_by_later_success(database, tmp_path):
    case = await setup_case(database, tmp_path)
    await aggregate(case, "unknown", uncertain=True, ok=False)
    await aggregate(case, "good", await social(case, "good"))
    assert (await inspect(case)).state == "uncertain"


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["running", "waiting_external", "suspended", "failed"])
async def test_successful_send_does_not_finish_unfinished_work(database, tmp_path, state):
    case = await setup_case(database, tmp_path)
    await aggregate(case, "good", await social(case, "good"))
    async with database.sessions() as session, session.begin():
        await session.execute(update(work).where(work.c.id == case.row["id"]).values(state=state))
    assert (await inspect(case)).reason == "agent_work_not_completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("complete", [False, True])
async def test_split_send_requires_complete_aggregate_not_a_successful_part(
    database, tmp_path, complete
):
    import hashlib

    case = await setup_case(database, tmp_path)
    call = "split"
    await social(case, call, action="send_message_sequence", status="prepared")
    prefix = hashlib.sha256(call.encode()).hexdigest()[:24]
    first = await social(case, f"seq:{prefix}:0")
    second = await social(case, f"seq:{prefix}:1", status="succeeded" if complete else "failed")
    body = {
        "status": "succeeded" if complete else "failed",
        "target": first["target"],
        "planned_messages": 2,
        "sent_messages": 2 if complete else 1,
        "parts": [first, second],
    }
    await aggregate(case, call, body, ok=complete)
    assert (await inspect(case)).state == ("succeeded" if complete else "failed")


@pytest.mark.asyncio
@pytest.mark.parametrize("caption_status", ["succeeded", "failed", "uncertain"])
async def test_file_and_caption_must_both_be_confirmed(database, tmp_path, caption_status):
    case = await setup_case(database, tmp_path)
    file = await social(case, "file")
    caption = await social(
        case,
        "caption",
        source=f"social-caption:{file['operation_id']}",
        action="send_file_caption",
        status=caption_status,
    )
    body = await case.env.service._file_result(file["operation_id"])
    assert body["target"] == file["target"]
    assert body["caption"] == caption
    await aggregate(
        case,
        "file",
        body,
        ok=caption_status == "succeeded",
        uncertain=caption_status == "uncertain",
    )
    assert (await inspect(case)).state == caption_status


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,expected", [("text", "succeeded"), ("file", "uncertain"), ("sequence", "uncertain")]
)
async def test_missing_aggregate_only_accepts_proven_single_plain_send(
    database, tmp_path, kind, expected
):
    case = await setup_case(database, tmp_path)
    receipt = await social(
        case,
        "send",
        action="send_message_sequence" if kind == "sequence" else "send_message",
        status="prepared" if kind == "sequence" else "succeeded",
    )
    args = {"text": "result"}
    if kind == "file":
        args.update(artifact_id="file-id", attachment_kind="file")
    async with database.sessions() as session, session.begin():
        await session.execute(
            insert(deliveries).values(
                id=receipt["operation_id"],
                work_id=case.row["id"],
                kind="message",
                state="accepted",
                created=1,
                updated=1,
                payload_json=json.dumps({"arguments": args}),
            )
        )
    assert (await inspect(case)).state == expected


@pytest.mark.asyncio
async def test_unrelated_run_and_wrong_target_do_not_prove_delivery(database, tmp_path):
    case = await setup_case(database, tmp_path)
    await social(case, "another-run", source=case.source.replace(":23:", ":24:"))
    assert (await inspect(case)).state == "none"
    receipt = await social(case, "own")
    await aggregate(case, "own", receipt)
    outcome = await inspect_agent_delivery(
        database,
        conversation_id=case.env.context.conversation_id,
        run_id=23,
        step_id="execute",
        script_hash="a" * 64,
        target_kind="person",
        target_id=case.env.person,
    )
    assert outcome.state == "none"
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(effects).values(
                receipt_json=json.dumps(
                    {
                        "result": json.dumps(
                            {
                                "tool_name": "send_message",
                                "ok": True,
                                "data": {**receipt, "operation_id": "not-a-real-receipt"},
                            }
                        )
                    }
                )
            )
        )
    assert (await inspect(case)).state == "uncertain"


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmed", [False, True])
async def test_pending_tool_recovery_queries_original_single_send_receipt(
    database, tmp_path, confirmed
):
    case = await setup_case(database, tmp_path)
    async with database.sessions() as session, session.begin():
        await session.execute(
            insert(effects).values(
                effect_key="chain:0:pending",
                work_id=case.row["id"],
                kind="tool",
                state="unknown",
                receipt_json='{"error":"execution_interrupted"}',
                created=1,
                updated=1,
            )
        )
        await session.execute(
            insert(journal).values(
                work_id=case.row["id"],
                chain_id="chain",
                contract="contract",
                source_revision=0,
                phase="response",
                updated=1,
                payload_json=json.dumps(
                    {
                        "transcript": {"chain_id": "chain"},
                        "metadata": {"sequence": 0},
                        "pending": [{"id": "pending", "name": "send_message", "arguments": "{}"}],
                    }
                ),
            )
        )
    if confirmed:
        receipt = await social(case, "pending")
        async with database.sessions() as session, session.begin():
            await session.execute(
                insert(deliveries).values(
                    id=receipt["operation_id"],
                    work_id=case.row["id"],
                    kind="message",
                    state="accepted",
                    created=1,
                    updated=1,
                    payload_json=json.dumps({"arguments": {"text": "real result"}}),
                )
            )
    assert (await inspect(case)).state == ("succeeded" if confirmed else "uncertain")
