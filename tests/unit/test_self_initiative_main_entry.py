"""Actual SELF composition and provider wires remain the shared Main Agent contract."""

import json
from datetime import UTC, datetime
from itertools import pairwise
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from tests.conftest import build_harness, make_settings
from tests.support.runtime_wire import install_wire
from tests.unit.test_self_initiative_runtime import self_source

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import ChatMessage, ChatResponse, ToolCall, ToolFunction
from qq_ai_bot.gateway.providers import builtin_provider_catalog
from qq_ai_bot.gateway.registry import GatewayConnectionRegistry
from qq_ai_bot.identity.routing import PresenceRouter
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.mcp.repository import MCPRepository, ToolArtifactRepository
from qq_ai_bot.persistence.models import MemoryToolReceiptModel
from qq_ai_bot.persistence.repository_records import EventRecord
from qq_ai_bot.runtime.subagent_repository import SubagentRepository
from qq_ai_bot.runtime.subagent_scheduler import SubagentScheduler
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.runtime.work_scheduler import WorkScheduler
from qq_ai_bot.runtime.work_schema_v1 import journal
from qq_ai_bot.services.main_agent_contract import MainAgentContract
from qq_ai_bot.workspace.short_state import ShortState
from qq_ai_bot.workspace.store import WorkspaceStore


@pytest.mark.parametrize(
    ("protocol", "native"),
    [
        ("responses", False),
        ("chat_completions", False),
        ("responses", True),
    ],
)
async def test_self_main_segment_resume_preserves_real_wire_prefix_and_silent_completion(
    database,
    tmp_path,
    protocol,
    native,
    monkeypatch,
):
    from qq_ai_bot.runtime.work_control import WorkControl

    failures = []
    recover = WorkControl.recover_failure

    async def capture(control, exc):
        import traceback

        failures.append("".join(traceback.format_exception(exc)))
        return await recover(control, exc)

    monkeypatch.setattr(WorkControl, "recover_failure", capture)
    source, _, _ = await self_source(database)
    steps = iter(range(25))

    def respond(_request):
        step = next(steps)
        if step == 24:
            return ChatResponse('NO_REPLY\n<yuki-state>{"engage":"quiet"}</yuki-state>', 0)
        return ChatResponse(
            "",
            0,
            tool_calls=(
                ToolCall(
                    f"call-{step}",
                    ToolFunction(
                        "update_short_state",
                        json.dumps(
                            {
                                "slot": 1,
                                "text": f"step-{step}",
                                "expected_revision": step,
                            }
                        ),
                    ),
                ),
            ),
        )

    provider = FakeLLMProvider(respond)
    harness = build_harness(
        database,
        make_settings(
            database.url,
            runtime_work_enabled=True,
            enabled_groups_csv="2001",
            agent_max_model_requests=24,
            agent_max_tool_calls=32,
            web_mode="native" if native else "disabled",
        ),
        provider,
    )
    chat = harness.processor._chat
    state = ShortState(WorkspaceStore(tmp_path / "state"))
    chat._tools.short_state = state
    contract = MainAgentContract(chat, state)
    chat._agent_runner.main_contract = contract
    client, captured = install_wire(chat, provider, protocol, native=native)
    registry = GatewayConnectionRegistry(providers=builtin_provider_catalog())
    bot = SimpleNamespace(self_id="8000")
    registry.connect(bot, provider_id="snowluma", presence_id=source["presence_id"])
    repo = WorkRepository(database)
    lease = await repo.acquire(source["conversation_id"], 1)
    item = await repo.accept(
        lease,
        source_key=f"initiative:{source['initiative_run_id']}",
        source=source,
        goal=source["instruction"],
        output_kind="answer",
        deliver_artifacts=False,
    )
    await repo.release(lease)
    app = SimpleNamespace(
        database=database,
        chat=chat,
        settings=harness.settings,
        presence_router=PresenceRouter(database, registry),
        conversation_scopes=chat._conversation_scopes,
        runtime_config=chat._runtime_config,
        turn_coordinator=chat._turn_coordinator,
    )
    try:
        scheduler = WorkScheduler(app)
        await scheduler._resume_self(item, source)
        first = await repo.get(item["id"])
        assert first["state"] == "queued", failures or first
        assert first["model_requests"] == 24
        await scheduler._resume_self(first, source)
        final = await repo.get(item["id"])
        assert final["state"] == "completed", (final, scheduler._last_error)
        assert final["model_requests"] == 25
        assert len(captured) == 25
        field = "input" if protocol == "responses" else "messages"
        for previous, following in pairwise(captured):
            assert following[field][: len(previous[field])] == previous[field]
            assert following["tools"] == previous["tools"]
        assert provider.requests[0].request_chain_id == provider.requests[-1].request_chain_id
        assert {tool.name for tool in await contract.definitions()} == {
            tool["name"] if protocol == "responses" else tool["function"]["name"]
            for tool in captured[0]["tools"]
            if tool["type"] == "function"
        }
        if native:
            assert {"type": "web_search"} in captured[0]["tools"]
        assert not final["source_json"].count("trigger_event_id")
        async with database.sessions() as db:
            payload = json.loads(
                await db.scalar(
                    select(journal.c.payload_json).where(journal.c.work_id == item["id"])
                )
            )
        reports = payload["metadata"]["progress"]["self_reports"]
        assert len(reports) == 1 and reports[0]["sequence"] == 25
        assert reports[0]["run_ref"] == source["initiative_run_id"]
    finally:
        await client.aclose()


@pytest.mark.parametrize("repeat_tool_ids", [False, True], ids=["plain", "repeated-call-id"])
async def test_self_worker_returns_internal_result_without_group_delivery(
    database,
    tmp_path,
    monkeypatch,
    repeat_tool_ids,
):
    from qq_ai_bot.runtime.work_control import WorkControl

    failures = []
    recover = WorkControl.recover_failure

    async def capture(control, exc):
        import traceback

        failures.append("".join(traceback.format_exception(exc)))
        return await recover(control, exc)

    monkeypatch.setattr(WorkControl, "recover_failure", capture)
    source, _, _ = await self_source(database)
    responses = iter(
        [
            *(
                ChatResponse(
                    "",
                    0,
                    tool_calls=(
                        ToolCall(
                            "call_0",
                            ToolFunction("search_chat_history", json.dumps({"keyword": query})),
                        ),
                    ),
                )
                for query in ("first", "second")
                if repeat_tool_ids
            ),
            ChatResponse("检查完成，文件不需要修改。", 0),
        ]
    )
    provider = FakeLLMProvider(lambda _: next(responses))
    harness = build_harness(
        database,
        make_settings(
            database.url,
            runtime_work_enabled=True,
            enabled_groups_csv="2001",
            web_mode="native",
        ),
        provider,
    )
    chat = harness.processor._chat
    if repeat_tool_ids:
        chat._tool_invocations = MCPRepository(database)
    chat._tool_artifacts = ToolArtifactRepository(
        database,
        tmp_path / "artifacts",
        retention_seconds=60,
    )
    state = ShortState(WorkspaceStore(tmp_path / "state"))
    chat._tools.short_state = state
    contract = MainAgentContract(chat, state)
    chat._agent_runner.main_contract = contract
    repo = WorkRepository(database)
    lease = await repo.acquire(source["conversation_id"], 1)
    parent = await repo.accept(
        lease,
        source_key=f"initiative:{source['initiative_run_id']}",
        source=source,
        goal=source["instruction"],
        output_kind="answer",
        deliver_artifacts=False,
    )
    workers = SubagentRepository(repo)
    child_id = await workers.start(
        lease,
        parent["id"],
        "inspect",
        {
            "goal": "检查环境",
            "output_kind": "answer",
        },
    )
    await repo.release(lease)
    scheduler = SubagentScheduler(
        SimpleNamespace(
            database=database,
            chat=chat,
            settings=harness.settings,
            runtime_config=chat._runtime_config,
        )
    )
    from qq_ai_bot.runtime.subagent_tools import WORKER_NAMES

    assert WORKER_NAMES <= {tool.name for tool in await contract.definitions()}
    await scheduler.run(child_id)
    child = await repo.get(child_id)
    assert child["state"] == "completed", failures or (child, scheduler.last_error)
    assert len(provider.requests) == (3 if repeat_tool_ids else 1)
    result = await workers.related(parent["id"], child_id)
    assert "检查完成" in result["result_json"]
    assert (await repo.get(parent["id"]))["state"] == "running"
    if repeat_tool_ids:
        async with database.sessions() as session:
            receipts = list(
                await session.scalars(
                    select(MemoryToolReceiptModel).where(
                        MemoryToolReceiptModel.initiative_run_id == source["initiative_run_id"],
                    )
                )
            )
        assert len(receipts) == 2, [
            message.content for message in provider.requests[-1].messages if message.role == "tool"
        ]
        assert all(receipt.execution_id == child_id for receipt in receipts)
        assert {receipt.tool_call_id.rsplit(":", 2)[-2] for receipt in receipts} == {"1", "2"}
        assert all(receipt.tool_call_id.endswith(":call_0") for receipt in receipts)


async def test_actorless_budget_counts_full_visible_history_and_current_instruction(database):
    chat = build_harness(database, make_settings(database.url)).processor._chat
    event = EventRecord(
        id=1,
        bot_user_id="8000",
        platform_message_id="original",
        scope_type=ScopeType.GROUP,
        sender_user_id="1001",
        direction="inbound",
        content="历史内容" * 50,
        visual_summary="",
        segments=(),
        occurred_at=datetime.now(UTC),
        group_id="2001",
    )
    assembler = chat._context_assembler
    current = ChatMessage(role="user", content="actorless instruction")
    view = assembler._uncovered_prompt_view(
        (event,),
        current_event_id=None,
        content="",
        yuki_account_ids=frozenset({"8000"}),
        current_message_override=current,
        current_event=None,
    )
    assert view is not None
    assert view.history_rows == (event,) and view.record is None
    assert view.current_characters == len(current.content)
    assert view.rendered_characters >= len(event.content)
    assert not assembler._uncovered_fits_window(view, event_limit=4, character_budget=32)
    assert assembler._uncovered_fits_window(view, event_limit=4, character_budget=4000)
