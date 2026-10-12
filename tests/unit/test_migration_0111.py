"""Execution sources preserve old Memory receipts and allow real SELF schedules."""

import asyncio
import json

from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from tests.support.automation_unified_delivery_helpers import setup_run
from tests.unit.test_self_initiative_memory import _service, record

from qq_ai_bot.automation.models import RunStatus
from qq_ai_bot.domain.messages import ChatResponse, ToolCall, ToolFunction
from qq_ai_bot.memory.mutation.models import (
    MemoryDecisionActorType,
    MemoryMutationContext,
    MemoryMutationRequest,
)
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    ChatEventModel,
    MemoryMutationReceiptModel,
    MemoryToolReceiptModel,
)
from qq_ai_bot.tool_results.recorder import ToolInvocationRepository


def test_0111_preserves_original_receipts_and_writes_real_scheduled_self_source(
    tmp_path, monkeypatch
):
    url = f"sqlite+aiosqlite:///{(tmp_path / 'memory-sources.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config("alembic.ini")
    command.upgrade(config, "0110")

    async def verify():
        database = Database(url)
        try:
            case = await setup_run(
                database,
                tmp_path,
                principal="self",
                delivery="none",
                mode="silent",
                self_memory_enabled=True,
            )
            mutations, _, ledger, _ = _service(database, self_memory_enabled=True)
            async with database.sessions() as session:
                event_id = await session.scalar(select(ChatEventModel.id))
            event = await ledger.get_event(event_id)
            await record(database, event, case.actor.initiative_run_id)
            saved = await mutations.mutate(
                MemoryMutationRequest(
                    operation="create",
                    target={"subject_ref": "self", "scope_type": "self"},
                    new_content="我用实际工具回执校验了曲线绘图。",
                    memory_key="original-paid-episode",
                    category="self_episode",
                    kind="episode",
                    evidence_quote="已生成并校验曲线绘图",
                ),
                MemoryMutationContext(
                    event=None,
                    conversation_key=event.scope.key,
                    turn_origin="self_initiative",
                    delegation_mode="main_agent",
                    trigger_actor_user_id="",
                    decision_actor_type=MemoryDecisionActorType.AGENT,
                    decision_actor_id="execution-1",
                    executed_by_bot_user_id=event.bot_user_id,
                    initiative_run_id=case.actor.initiative_run_id,
                    source_group_id=event.group_id,
                ),
            )
            assert saved.ok
            tables = (
                "memory_tool_receipts",
                "memory_mutation_receipts",
                "memory_facts",
                "memory_evidence",
                "memory_fact_state_events",
                "runtime_work",
                "runtime_work_effects",
                "runtime_automation_cursors",
            )

            async def snapshot():
                async with database.sessions() as session:
                    return {
                        name: [
                            tuple(row)
                            for row in await session.execute(text(f"SELECT * FROM {name}"))
                        ]
                        for name in tables
                    }

            original = await snapshot()
            await asyncio.to_thread(command.upgrade, config, "0111")
            assert await snapshot() == original
            async with database.sessions() as session:
                assert not list(await session.execute(text("PRAGMA foreign_key_check")))
                sql = await session.scalar(
                    text(
                        "SELECT group_concat(sql) FROM sqlite_master "
                        "WHERE name IN ('memory_tool_receipts','memory_mutation_receipts')"
                    )
                )
                assert "ck_memory_tool_receipts_source" not in sql
                assert "ck_memory_mutation_trigger_source" not in sql
            case.chat._tools._memory_mutations = mutations
            case.chat._tool_invocations = ToolInvocationRepository(database)
            responses = []

            def respond(request):
                index = len(case.provider.requests) - 1
                if index:
                    result = json.loads(request.messages[-1].content)
                    responses.append(result)
                    assert result["ok"], result
                calls = (
                    ("get_group_members", {}),
                    (
                        "memory_change",
                        {
                            "operation": "create",
                            "target": {"subject_ref": "self", "scope_type": "self"},
                            "new_content": "我查询了当前群实际成员 member 的资料。",
                            "memory_key": "scheduled-episode",
                            "category": "self_episode",
                            "kind": "episode",
                            "evidence_quote": "member",
                        },
                    ),
                )
                if index >= len(calls):
                    return "NO_REPLY"
                name, arguments = calls[index]
                return ChatResponse(
                    "",
                    0,
                    tool_calls=(
                        ToolCall(f"scheduled-{index}", ToolFunction(name, json.dumps(arguments))),
                    ),
                )

            case.provider._responder = respond
            case.clock.advance(2)
            result = await case.dispatch()
            assert result.status is RunStatus.SUCCEEDED
            assert responses[-1]["data"]["ok"]
            async with database.sessions() as session:
                receipts = list(await session.scalars(select(MemoryMutationReceiptModel)))
                assert len(receipts) == 2
                new = next(row for row in receipts if row.trigger_source_type == "execution")
                assert new.trigger_event_id is None and new.initiative_run_id is None
                tool = await session.scalar(
                    select(MemoryToolReceiptModel).where(
                        MemoryToolReceiptModel.execution_id == new.decision_actor_id,
                        MemoryToolReceiptModel.tool_name == "get_group_members",
                    )
                )
                assert tool.trigger_event_id is None and tool.initiative_run_id is None
            assert await case.dispatch() == result
            assert len(case.provider.requests) == 3
        finally:
            await database.close()

    asyncio.run(verify())
