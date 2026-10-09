"""Work inspection cannot leak recovery bodies, wake tasks or reset their budget."""

import json
import time
from uuid import uuid4

import pytest
from sqlalchemy import event, insert, update
from tests.unit.test_canonical_ingress import _message
from tests.unit.test_control_plane_foundation import context
from tests.unit.test_webui_activity import ingress

from qq_ai_bot.control_plane import ControlQueryError, ControlQueryService, PageRequest, ProblemCode
from qq_ai_bot.persistence.control_query import ControlQueryAdapter
from qq_ai_bot.runtime.subagent_schema import budgets, children
from qq_ai_bot.runtime.work_recovery_schema import deliveries, recovery
from qq_ai_bot.runtime.work_schema_v1 import effects, inputs, journal, work
from qq_ai_bot.runtime.work_wait_schema import waits


@pytest.fixture
async def detailed_work(database):
    resolver, uow, bot = await ingress(database)
    admitted = await resolver.pre_admit(bot, _message(message_id="work-detail-source"))
    stored = await uow.append_inbound(admitted.message, admitted)
    identity, child = str(uuid4()), str(uuid4())
    now = time.time()

    def row(identity, state):
        return {
            "id": identity,
            "conversation_id": admitted.conversation_id,
            "generation": 1,
            "source_key": f"offline:{identity}",
            "source_json": '{"private":"source-secret"}',
            "goal": "正文目标",
            "state": state,
            "model_requests": 7,
            "tool_calls": 8,
            "active_seconds": 12.5,
            "created": now,
            "updated": now,
            "checkpoint_json": '{"private":"checkpoint-secret"}',
        }

    async with database.immediate_session() as session:
        await session.execute(
            insert(work), [row(identity, "waiting_external"), row(child, "queued")]
        )
        await session.execute(
            insert(children).values(
                work_id=child,
                root_id=identity,
                source_key="child-source",
                brief_json='{"private":"brief-secret"}',
            )
        )
        await session.execute(insert(budgets).values(root_id=identity, models=9, tools=10))
        await session.execute(
            insert(journal).values(
                work_id=identity,
                chain_id=str(uuid4()),
                contract="main-v6",
                source_revision=1,
                phase="tool_pending",
                payload_json='{"private":"opaque-provider-state"}',
                updated=now,
            )
        )
        await session.execute(
            insert(recovery).values(
                work_id=identity,
                activation_id=str(uuid4()),
                exit_reason="waiting_external",
                stage="tool",
                attempts=2,
                not_before=now + 60,
                failure_json='{"private":"failure-secret"}',
                updated=now,
            )
        )
        await session.execute(
            insert(waits).values(
                id=str(uuid4()),
                work_id=identity,
                conversation_id=admitted.conversation_id,
                generation=1,
                principal_kind="self",
                principal_id="self",
                call_key="wait-call",
                request_json='{"private":"request-secret"}',
                mode="all",
                conditions_json=json.dumps(
                    [
                        {
                            "kind": "conversation",
                            "matched": {"event_id": stored.event.id, "private": "matched-secret"},
                        },
                        {"kind": "time_due", "due": now + 60, "matched": None},
                    ]
                ),
                status="active",
                created=now,
                updated=now,
            )
        )
        await session.execute(
            insert(inputs),
            [
                {
                    "conversation_id": admitted.conversation_id,
                    "generation": 1,
                    "source_key": f"input-{i}",
                    "event_id": stored.event.id,
                    "work_id": identity,
                    "kind": "message",
                    "state": "consumed",
                    "payload_json": '{"private":"input-secret"}',
                    "created": now + i,
                }
                for i in range(25)
            ],
        )
        await session.execute(
            insert(effects).values(
                effect_key="original-effect",
                work_id=identity,
                kind="tool",
                state="unknown",
                receipt_json='{"private":"effect-secret"}',
                created=now,
                updated=now,
            )
        )
        await session.execute(
            insert(deliveries).values(
                id="original-delivery",
                work_id=identity,
                kind="answer",
                target_key="transport-private-id",
                state="unknown",
                payload_json='{"private":"delivery-secret"}',
                receipt_json='{"private":"receipt-secret"}',
                created=now,
                updated=now,
            )
        )
    return identity, child


async def test_metadata_projection_does_not_read_private_payload_columns(database, detailed_work):
    identity, child = detailed_work
    statements = []

    def capture(_, __, sql, *args):
        statements.append(sql)

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        service = ControlQueryService(ControlQueryAdapter(database))
        ctx = context("control.execution.metadata.read")
        result = await service.read_work(ctx, identity)
        history = {
            section: await service.list_work_history(
                ctx, PageRequest(), work_id=identity, section=section
            )
            for section in ("children", "inputs", "effects", "deliveries", "waits")
        }
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    fields = result.fields
    assert fields["state"] == "waiting_external"
    assert fields["model_requests"] == 7 and fields["tool_calls"] == 8
    assert fields["shared_budget"]["models"] == 9
    assert history["children"].items[0].resource_id == child
    assert len(history["inputs"].items) == 20 and history["inputs"].next_cursor is not None
    assert history["effects"].items[0].fields["state"] == "unknown"
    assert history["deliveries"].items[0].resource_id == "original-delivery"
    assert "goal" not in fields and "conditions" not in history["waits"].items[0].fields
    from qq_ai_bot.runtime.work_management import CLASSIFICATION_KEYS

    # Management actions classify the original owner by fixed source paths only.
    joined = " ".join(statements)
    for key in CLASSIFICATION_KEYS:
        joined = joined.replace(f"json_extract(runtime_work.source_json, '$.{key}')", "")
    assert not any(
        token in joined
        for token in (
            "goal",
            "payload_json",
            "source_json",
            "checkpoint_json",
            "receipt_json",
            "brief_json",
            "result_json",
            "failure_json",
            "conditions_json",
            "target_key",
        )
    )
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)


async def test_content_and_child_lineage_remain_on_original_work(database, detailed_work):
    identity, child = detailed_work
    service = ControlQueryService(ControlQueryAdapter(database))
    ctx = context("control.execution.metadata.read", "control.execution.content.read")
    result = await service.read_work(ctx, identity, include_content=True)
    assert result.fields["goal"] == "正文目标"
    wait_history = await service.list_work_history(
        ctx, PageRequest(), work_id=identity, section="waits", include_content=True
    )
    conditions = wait_history.items[0].fields["conditions"]
    assert conditions[0]["matched"] is True and conditions[1]["matched"] is False
    assert "private" not in repr(result.fields) and "opaque-provider-state" not in repr(
        result.fields
    )
    child_detail = await service.read_work(ctx, child)
    assert child_detail.fields["root_id"] == identity
    assert child_detail.fields["shared_budget"]["tools"] == 10
    with pytest.raises(ControlQueryError) as exc:
        await service.read_work(
            context("control.execution.metadata.read"), identity, include_content=True
        )
    assert exc.value.problem.code is ProblemCode.CAPABILITY_DENIED


@pytest.mark.parametrize(
    "identity", [None, [], "platform-123", "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA", ""]
)
async def test_invalid_work_identity(database, identity):
    service = ControlQueryService(ControlQueryAdapter(database))
    with pytest.raises(ControlQueryError) as exc:
        await service.read_work(context("control.execution.metadata.read"), identity)
    assert exc.value.problem.code is ProblemCode.VALIDATION_ERROR


async def test_missing_work_is_not_reconstructed(database):
    service = ControlQueryService(ControlQueryAdapter(database))
    with pytest.raises(ControlQueryError) as exc:
        await service.read_work(context("control.execution.metadata.read"), str(uuid4()))
    assert exc.value.problem.code is ProblemCode.NOT_FOUND


async def test_input_keyset_reads_all_history_and_binds_scope(database, detailed_work):
    identity, child = detailed_work
    service = ControlQueryService(ControlQueryAdapter(database))
    ctx = context("control.execution.metadata.read")
    first = await service.list_work_history(
        ctx, PageRequest(limit=20), work_id=identity, section="inputs"
    )
    async with database.immediate_session() as session:
        await session.execute(update(inputs).values(state="pending"))
    second = await service.list_work_history(
        ctx, PageRequest(limit=20, cursor=first.next_cursor), work_id=identity, section="inputs"
    )
    ids = [int(row.resource_id) for row in (*first.items, *second.items)]
    assert len(ids) == 25 and len(set(ids)) == 25 and ids == sorted(ids, reverse=True)
    assert second.next_cursor is None
    for target, section in ((child, "inputs"), (identity, "effects")):
        with pytest.raises(ControlQueryError) as exc:
            await service.list_work_history(
                ctx, PageRequest(cursor=first.next_cursor), work_id=target, section=section
            )
        assert exc.value.problem.code is ProblemCode.VALIDATION_ERROR


async def test_equal_timestamp_and_full_length_effect_keys_paginate(database, detailed_work):
    identity, _ = detailed_work
    now = time.time()
    keys = [letter * 256 for letter in "abc"]
    async with database.immediate_session() as session:
        await session.execute(
            insert(effects),
            [
                {
                    "effect_key": key,
                    "work_id": identity,
                    "kind": "tool",
                    "state": "accepted",
                    "created": now,
                    "updated": now,
                }
                for key in keys
            ],
        )
    service = ControlQueryService(ControlQueryAdapter(database))
    ctx = context("control.execution.metadata.read")
    first = await service.list_work_history(
        ctx, PageRequest(limit=2), work_id=identity, section="effects"
    )
    second = await service.list_work_history(
        ctx, PageRequest(limit=2, cursor=first.next_cursor), work_id=identity, section="effects"
    )
    assert [row.resource_id for row in (*first.items, *second.items)] == [
        *reversed(keys),
        "original-effect",
    ]


async def test_wait_content_grant_cursor_and_invalid_sections(database, detailed_work):
    identity, _ = detailed_work
    service = ControlQueryService(ControlQueryAdapter(database))
    ctx = context("control.execution.metadata.read")
    with pytest.raises(ControlQueryError) as exc:
        await service.list_work_history(
            ctx, PageRequest(), work_id=identity, section="waits", include_content=True
        )
    assert exc.value.problem.code is ProblemCode.CAPABILITY_DENIED
    for section in ("source_json", "arbitrary_table", [], None):
        with pytest.raises(ControlQueryError) as exc:
            await service.list_work_history(ctx, PageRequest(), work_id=identity, section=section)
        assert exc.value.problem.code is ProblemCode.VALIDATION_ERROR
