"""Reflection pages retain original event/receipt ranges and actual request usage."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from tests.support import control_memory_query_helpers as memory_fixtures

from qq_ai_bot.conversation.autonomy_db_models import AutonomyBindingModel, InitiativeRunModel
from qq_ai_bot.memory.self_reflection.db_models import (
    InitiativeReflectionCursorModel,
    InitiativeReflectionWindowModel,
)
from qq_ai_bot.persistence.models import MemorySelfReflectionCycleModel as Cycle
from qq_ai_bot.persistence.models import MemorySelfReflectionRequestModel as Request
from qq_ai_bot.persistence.models import MemorySelfReflectionResultModel as Result
from qq_ai_bot.persistence.models import MemorySelfReflectionRunModel as Run
from qq_ai_bot.persistence.models import MemorySelfReflectionStateModel as State
from qq_ai_bot.persistence.models import MemoryToolReceiptModel

memory_scene = memory_fixtures.memory_scene


@pytest.fixture
async def reflection_scene(database, memory_scene):
    env, facts, source, receipts = memory_scene
    now = datetime.now(UTC)
    initiative = str(uuid4())
    async with database.immediate_session() as session:
        session.add(
            AutonomyBindingModel(
                conversation_id=env.context.conversation_id,
                generation=1,
                master_enabled=True,
                external_enabled=True,
                effective_owner="semantic",
                controller_epoch=1,
                revision=1,
                updated_at=now,
            )
        )
        await session.flush()
        session.add(
            InitiativeRunModel(
                id=initiative,
                proposal_id="fixture",
                conversation_id=env.context.conversation_id,
                generation=1,
                owner="semantic",
                controller_epoch=1,
                space_id=env.space,
                presence_id=env.presence,
                payload_hash="a" * 64,
                sources_json="[]",
                support_refs_json="[]",
                state="completed",
                feedback_sequence=1,
                created_at=now,
                updated_at=now,
            )
        )
        for i in range(2):
            session.add(
                Cycle(
                    id=f"sr_fixture_{i}",
                    source_key=f"fixture_{i}",
                    trigger="scheduled",
                    status="completed",
                    created_at=now - timedelta(minutes=5),
                    started_at=now - timedelta(minutes=4),
                    completed_at=now,
                    report_json='{"before":{}}',
                    delivery_state="not_required",
                    delivery_receipt_json='{"secret":"private-platform-receipt"}',
                )
            )
        await session.flush()
        for index, receipt_id in enumerate(receipts):
            receipt = await session.get(MemoryToolReceiptModel, receipt_id)
            receipt.trigger_event_id = None
            receipt.initiative_run_id = initiative
            receipt.tool_call_id = f"call-{index}"
            receipt.source_call_key = f"{index + 1:064x}"
        runs = []
        for i in range(36):
            tools = i == 35
            run = Run(
                conversation_key_hash=("p" if i < 34 else "s") * 64,
                bot_user_id="80001",
                canonical_person_id=env.person if i < 34 else None,
                canonical_space_id=env.space if i >= 34 else None,
                scheduled_slot=f"fixture-{i}",
                trigger_reason="self_tool_receipts" if tools else "threshold",
                first_event_id=0 if tools else source,
                last_event_id=0 if tools else source,
                status="failed" if i == 33 else "completed",
                proposal_count=2,
                committed_count=1,
                started_at=now - timedelta(seconds=i),
                completed_at=now,
                cycle_id=f"sr_fixture_{int(i >= 34)}",
                attempt_count=3 if i == 33 else 1,
                retry_state="isolated" if i == 33 else None,
                processed_events=0 if tools else 1,
                processed_characters=10,
                checkpoint_json='{"private":"source-body"}',
            )
            session.add(run)
            runs.append(run)
        await session.flush()
        session.add(
            State(
                conversation_key_hash="p" * 64,
                bot_user_id="80001",
                canonical_person_id=env.person,
                last_event_id=0,
                latest_event_id=source,
                pending_events=12,
                pending_characters=100,
                pending_since=now - timedelta(hours=2),
                has_yuki_reply=True,
                updated_at=now,
            )
        )
        session.add(
            InitiativeReflectionWindowModel(
                reflection_run_id=runs[-1].id,
                initiative_run_id=initiative,
                first_receipt_id=receipts[0],
                last_receipt_id=receipts[-1],
            )
        )
        session.add(
            InitiativeReflectionCursorModel(
                initiative_run_id=initiative, last_receipt_id=receipts[-1]
            )
        )
        session.add(
            Request(
                run_id=runs[0].id,
                local_date=now.date().isoformat(),
                created_at=now,
                status="succeeded",
                attempt_kind="initial",
                output_tokens=None,
            )
        )
        session.add(
            Result(
                run_id=runs[0].id,
                fact_id=facts[0],
                result_kind="proposal",
                result_index=0,
                created_at=now,
            )
        )
    return env, [row.id for row in runs], initiative
