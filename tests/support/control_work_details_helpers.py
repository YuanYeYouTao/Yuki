"""Work inspection cannot leak recovery bodies, wake tasks or reset their budget."""

import json
import time
from uuid import uuid4

import pytest
from sqlalchemy import insert, update
from tests.support.webui_activity_helpers import ingress
from tests.unit.test_canonical_ingress import _message

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
            update(work).where(work.c.id == child).values(parent_work_id=identity)
        )
        await session.execute(
            insert(children).values(
                work_id=child,
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
                state="unknown",
                payload_json='{"private":"delivery-secret"}',
                receipt_json='{"private":"receipt-secret"}',
                created=now,
                updated=now,
            )
        )
    return identity, child
