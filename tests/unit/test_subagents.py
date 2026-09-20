"""Real SQLite worker isolation, recovery and shared-budget contracts."""

import asyncio
import json
from itertools import pairwise

import pytest
from sqlalchemy import select, update
from tests.support.social_identity_cases import social_env

from qq_ai_bot.runtime.subagent_repository import SubagentRepository
from qq_ai_bot.runtime.subagent_schema import budgets, children
from qq_ai_bot.runtime.work_budget import WorkBudgetExceeded
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import work


async def stack(database, tmp_path):
    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    lease = await repository.acquire(env.context.conversation_id, 1)
    parent = await repository.accept(
        lease, source_key="parent", source={}, goal="draw", output_kind="answer"
    )
    workers = SubagentRepository(repository)
    identity = await workers.start(
        lease, parent["id"], "spawn", {"goal": "draw", "output_kind": "answer"}
    )
    return repository, workers, lease, parent, identity


@pytest.mark.asyncio
async def test_foreground_answer_and_finalization_keep_worker_alive(database, tmp_path):
    from qq_ai_bot.runtime.work_control import WorkControl

    repo, workers, lease, parent, identity = await stack(database, tmp_path)

    async def valid():
        assert await repo.valid(lease)

    control = WorkControl(repo, lease, "parent", {}, valid)
    control.current = parent
    with pytest.raises(ValueError, match="unfinished_subagents"):
        await control._control({"action": "fail", "reason": "继续聊天"}, "fail")
    result = await control._control({"action": "answer", "text": "好，继续聊。"}, "answer")
    assert result["background_state"] == "waiting_external"
    assert control.chat_answer == "好，继续聊。"
    # Also defend against a runner falling through to implicit failure.
    control.ending = "failed"
    await control.settle(delivered=True, pending_inputs=False)
    assert (await repo.get(parent["id"]))["state"] == "waiting_external"
    child_lease = await workers.acquire(identity)
    assert child_lease is not None
    child = await repo.get(identity)
    await repo.transition(child_lease, identity, child["revision"], "suspended", reason="error")
    assert await control.background_state() == "suspended"
    await repo.release(child_lease)
    await workers.cancel(lease, parent["id"], identity)
    assert await control.background_state() is None
    await control._control({"action": "fail", "reason": "已明确取消子任务"}, "cancelled")
    control.settled = False  # A distinct activation owns the explicit cancellation.
    await control.settle(delivered=True, pending_inputs=False)
    assert (await repo.get(parent["id"]))["state"] == "failed"


@pytest.mark.asyncio
async def test_worker_busy_retry_keeps_journal_evidence_and_budget(database, tmp_path):
    import sqlite3

    from sqlalchemy.exc import OperationalError

    from qq_ai_bot.runtime.work_control import WorkControl
    from qq_ai_bot.runtime.work_recovery_schema import recovery

    repo, workers, parent_lease, _parent, identity = await stack(database, tmp_path)
    lease = await workers.acquire(identity)
    evidence = {"execution_evidence": [{"run_id": "already-dispatched", "uncertain": True}]}
    await repo.checkpoint(lease, identity, evidence, models=2, tools=1)

    async def valid():
        assert await repo.valid(lease)

    control = WorkControl(repo, lease, "worker", {}, valid)
    error = OperationalError("UPDATE", {}, sqlite3.OperationalError("database is locked"))
    for count in range(1, 4):
        control.current = await repo.get(identity)
        control.settled = False
        await control.recover_failure(error)
        row = await repo.get(identity)
        assert row["state"] == "queued"
        checkpoint = json.loads(row["checkpoint_json"])
        async with database.sessions() as session:
            saved = (
                (await session.execute(select(recovery).where(recovery.c.work_id == identity)))
                .mappings()
                .one()
            )
        assert saved["attempts"] == count and saved["not_before"] > 0
        assert checkpoint["execution_evidence"] == evidence["execution_evidence"]
        assert row["model_requests"] == 2 and row["tool_calls"] == 1
    control.settled = False
    await control.recover_failure(error)
    assert control.current["state"] == "suspended"
    from qq_ai_bot.runtime.activation_outcome import classify_failure

    assert not classify_failure(
        OperationalError("SELECT", {}, sqlite3.OperationalError("no such table: missing"))
    ).retryable
    await repo.release(lease)
    await repo.release(parent_lease)


@pytest.mark.asyncio
async def test_worker_independent_lease_messages_and_dormant_resume(database, tmp_path):
    repo, workers, parent_lease, parent, identity = await stack(database, tmp_path)
    assert (
        await workers.start(
            parent_lease, parent["id"], "spawn", {"goal": "draw", "output_kind": "answer"}
        )
        == identity
    )
    child_lease = await workers.acquire(identity)
    assert child_lease and await repo.valid(parent_lease)
    with pytest.raises(ValueError, match="recursive"):
        await workers.start(child_lease, identity, "recursive", {"goal": "nested"})
    for index in range(3):
        await workers.start(
            parent_lease,
            parent["id"],
            f"extra-{index}",
            {"goal": "independent", "output_kind": "answer"},
        )
    with pytest.raises(ValueError, match="capacity"):
        await workers.start(
            parent_lease, parent["id"], "overflow", {"goal": "extra", "output_kind": "answer"}
        )
    assert not await workers.acquire(identity)
    assert [r["id"] for r in await repo.active(parent_lease.conversation_id, 1)] == [parent["id"]]
    await workers.message(child_lease, parent["id"], identity, "question", "Which color?", ask=True)
    await workers.message(child_lease, parent["id"], identity, "question", "Which color?", ask=True)
    pending = await repo.pending(parent_lease, work_id=parent["id"])
    assert len(pending) == 1
    assert "Which color?" in pending[0]["payload_json"]
    child = await repo.get(identity)
    await repo.transition(child_lease, identity, child["revision"], "completed")
    await workers.finish(child_lease, "done")
    await repo.release(child_lease)
    await workers.message(
        parent_lease, parent["id"], identity, "answer", "Use blue", reply_to="question"
    )
    assert (await repo.get(identity))["state"] == "queued"
    resumed = await workers.acquire(identity)
    assert resumed and resumed.fence > child_lease.fence
    mail = await repo.pending(resumed, work_id=identity)
    assert len(mail) == 1 and "Use blue" in mail[0]["payload_json"]
    await repo.release(resumed)
    await repo.release(parent_lease)
    # Feature-off installations need not configure every worker dependency.
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from qq_ai_bot.runtime.subagent_scheduler import SubagentScheduler

    database.subagents_enabled = False
    scheduler = SubagentScheduler(
        SimpleNamespace(
            database=database,
            settings=SimpleNamespace(runtime_work_enabled=True, global_llm_concurrency=4),
            main_agent_contract=SimpleNamespace(definitions=AsyncMock(return_value=())),
        )
    )
    await scheduler.start()
    assert (await scheduler.health())["running"]
    await scheduler.close()


@pytest.mark.asyncio
async def test_shared_budget_reserves_parent_capacity_atomically(database, tmp_path):
    repo, workers, parent_lease, parent, identity = await stack(database, tmp_path)
    child_lease = await workers.acquire(identity)
    await repo.checkpoint(parent_lease, parent["id"], None, models=110, tools=151)
    outcomes = await asyncio.gather(
        *(repo.checkpoint(child_lease, identity, None, models=1, tools=1) for _ in range(3)),
        return_exceptions=True,
    )
    assert sum(isinstance(item, WorkBudgetExceeded) for item in outcomes) == 2
    await repo.checkpoint(parent_lease, parent["id"], None, models=9, tools=8)
    with pytest.raises(WorkBudgetExceeded):
        await repo.checkpoint(parent_lease, parent["id"], None, models=1)
    # Exhaustion must not prevent heartbeats, durable receipt settlement or cancellation.
    await repo.checkpoint(child_lease, identity, None, active_seconds=1)
    assert await repo.renew(child_lease)
    async with database.sessions() as session:
        row = (await session.execute(select(budgets))).mappings().one()
        assert row["models"] == 120 and row["tools"] == 160
    await repo.release(child_lease)
    await repo.release(parent_lease)


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat_completions", "responses", "responses_native"])
@pytest.mark.parametrize("scenario", ["complete", "segment", "question", "compaction", "business"])
async def test_worker_scheduler_uses_fixed_tools_and_recovers_history(
    database, tmp_path, protocol, scenario, monkeypatch
):
    from types import SimpleNamespace

    from tests.conftest import build_harness, make_settings

    from qq_ai_bot.domain.messages import ChatResponse, ToolCall, ToolFunction
    from qq_ai_bot.identity.db_models import CanonicalSpaceModel
    from qq_ai_bot.llm.fake import FakeLLMProvider
    from qq_ai_bot.persistence.models import ChatEventModel
    from qq_ai_bot.runtime.subagent_scheduler import SubagentScheduler
    from qq_ai_bot.sandbox.task_repository import SandboxTaskRepository
    from qq_ai_bot.services.main_agent_contract import MainAgentContract
    from qq_ai_bot.workspace.short_state import ShortState
    from qq_ai_bot.workspace.store import WorkspaceStore

    env = await social_env(database, tmp_path)
    async with database.sessions() as session, session.begin():
        event_id = await session.scalar(select(ChatEventModel.id))
        space = await session.get(CanonicalSpaceModel, env.space)
        space.enabled = True
    source = {
        "origin": "user_message",
        "conversation_id": env.context.conversation_id,
        "actor_user_id": "10001",
        "trigger_id": "inbound",
        "trigger_event_id": event_id,
        "bot_user_id": "80001",
        "presence_id": env.presence,
        "generation": 1,
    }
    repo = WorkRepository(database)
    lease = await repo.acquire(env.context.conversation_id, 1)
    parent = await repo.accept(
        lease, source_key="real-parent", source=source, goal="research", output_kind="answer"
    )
    workers = SubagentRepository(repo)
    identity = await workers.start(
        lease, parent["id"], "real-spawn", {"goal": "research", "output_kind": "answer"}
    )
    leading = []
    if scenario == "segment":
        leading = [("subagent_message", {"text": f"Checked item {index}"}) for index in range(24)]
    elif scenario == "question":
        leading = [("subagent_message", {"text": "Which color?", "ask": True})]
    elif scenario == "compaction":
        from unittest.mock import AsyncMock

        from qq_ai_bot.runtime.work_session import WorkSession

        monkeypatch.setattr(
            WorkSession, "needs_compaction", AsyncMock(side_effect=[True] + [False] * 8)
        )
        leading = [(None, None)]
    elif scenario == "business":
        leading = [("batch", None)]
    steps = iter(leading + [("task_control", {"action": "complete"}), (None, None)] * 2)

    def respond(request):
        name, args = next(steps)
        if name == "batch":
            return ChatResponse(
                "",
                0,
                tool_calls=tuple(
                    ToolCall(
                        f"exec-{index}",
                        ToolFunction("terminal_exec", json.dumps({"command": f"printf {index}"})),
                    )
                    for index in range(40)
                ),
            )
        return (
            ChatResponse("Verified result", 0)
            if name is None
            else ChatResponse(
                "",
                0,
                tool_calls=(
                    ToolCall(str(len(provider.requests)), ToolFunction(name, json.dumps(args))),
                ),
            )
        )

    provider = FakeLLMProvider(respond)
    settings = make_settings(
        database.url,
        runtime_work_enabled=True,
        enabled_groups_csv="20001",
        web_enabled=True,
        web_mode="native",
    )
    harness = build_harness(database, settings, provider)
    chat = harness.processor._chat
    from qq_ai_bot.mcp.repository import ToolArtifactRepository

    chat._tool_artifacts = ToolArtifactRepository(
        database, tmp_path / "tool-results", retention_seconds=86400
    )
    sandbox_calls = []

    async def sandbox_execute(name, arguments, **kwargs):
        from uuid import uuid4

        sandbox_calls.append((name, arguments, kwargs))
        return {
            "ok": True,
            "run_id": str(uuid4()),
            "status": "succeeded",
            "pending": False,
            "exit_code": 0,
        }

    chat._tools.sandbox_client = SimpleNamespace(execute=sandbox_execute)
    from tests.support.runtime_wire import install_wire

    native = protocol == "responses_native"
    protocol = "responses" if native else protocol
    client, wire = install_wire(chat, provider, protocol, native=native)
    chat._agent_runner.main_contract = MainAgentContract(
        chat, ShortState(WorkspaceStore(tmp_path / "state"))
    )
    app = SimpleNamespace(
        database=database,
        settings=settings,
        chat=chat,
        ledger=chat._ledger,
        runtime_config=chat._runtime_config,
        sandbox_tasks=SandboxTaskRepository(database),
    )
    scheduler = SubagentScheduler(app)
    await scheduler.run(identity)
    if scenario == "segment":
        assert (await repo.get(identity))["state"] == "queued"
        assert (await repo.get(identity))["model_requests"] == 24
        await scheduler.run(identity)
    elif scenario == "business":
        assert (await repo.get(identity))["state"] == "queued"
        assert (await repo.get(identity))["tool_calls"] == 32
        assert len(sandbox_calls) == 32
        assert all(call[2]["source"]["work_id"] == identity for call in sandbox_calls)
        assert all(call[2]["source"]["parent_work_id"] == parent["id"] for call in sandbox_calls)
        await scheduler.run(identity)
    elif scenario == "question":
        assert (await repo.get(identity))["state"] == "waiting_user"
        await workers.message(
            lease, parent["id"], identity, "answer-color", "Blue", reply_to="question"
        )
        await scheduler.run(identity)
    assert (await repo.get(identity))["state"] == "completed"
    first_tools = provider.requests[0].tools
    names = {t.name for t in first_tools}
    from qq_ai_bot.runtime.subagent_tools import WORKER_NAMES

    assert names == WORKER_NAMES
    assert "subagent_message" in names and "get_person_memories" in names
    assert not names & {"send_group_message", "memory_change", "subagent_start", "report_progress"}
    await workers.message(lease, parent["id"], identity, "continue", "Check again")
    await scheduler.run(identity)
    assert (await repo.get(identity))["state"] == "completed"
    assert len(provider.requests) == 4 + len(leading)
    assert all(r.tools == first_tools for r in provider.requests)
    before, after = provider.requests[1], provider.requests[2]
    assert after.messages[: len(before.messages)] == before.messages
    field = "input" if protocol == "responses" else "messages"
    assert len(wire) == 4 + len(leading)
    assert all(payload["tools"] == wire[0]["tools"] for payload in wire)
    if native:
        assert any(t["type"] == "web_search" for t in wire[0]["tools"])
    compared = wire[1:] if scenario == "compaction" else wire
    for previous, following in pairwise(compared):
        assert following[field][: len(previous[field])] == previous[field]
    assert (await repo.get(identity))["model_requests"] == len(wire)
    if scenario == "compaction":
        assert provider.requests[0].request_chain_id != provider.requests[1].request_chain_id
        assert wire[0]["tools"] == wire[1]["tools"]
        assert "explicit_context_compaction" in json.dumps(wire[1])
        assert provider.requests[0].messages[:2] == provider.requests[1].messages[:2]
    await client.aclose()
    await repo.release(lease)


@pytest.mark.asyncio
async def test_cancel_fences_media_recovery_and_privacy_cleanup(database, tmp_path):
    from qq_ai_bot.domain.messages import ChatImage, ChatMessage, ProviderContinuation
    from qq_ai_bot.runtime.subagent_schema import media
    from qq_ai_bot.runtime.work_control import WorkControl
    from qq_ai_bot.runtime.work_journal import WorkJournal
    from qq_ai_bot.runtime.work_repository import WorkConflict
    from qq_ai_bot.runtime.work_schema_v1 import journal
    from qq_ai_bot.runtime.work_session import WorkSession
    from qq_ai_bot.services.turn_transcript import TurnTranscript

    repo, workers, _parent_lease, _parent, identity = await stack(database, tmp_path)
    lease = await workers.acquire(identity)

    async def valid():
        assert await repo.valid(lease)

    control = WorkControl(repo, lease, "worker", {}, valid)
    control.current = await repo.get(identity)
    image = ChatImage(
        data_url="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
    )
    original = TurnTranscript(
        (ChatMessage("system", "fixed"), ChatMessage("user", "inspect", images=(image,)))
    )
    session = WorkSession(control, "fixed")
    transcript = await session.restore(original)
    transcript.accept(
        ProviderContinuation(
            provider="deepseek", protocol="responses", payload={"image_url": image.data_url}
        )
    )
    await session.save("paired")
    async with database.sessions() as db:
        payload = await db.scalar(
            select(journal.c.payload_json).where(journal.c.work_id == identity)
        )
        assert "data:image" not in payload and "$work_media" in payload
        assert len((await db.execute(select(media))).all()) == 1
    recovered = WorkSession(control, "fixed")
    restored = await recovered.restore(
        TurnTranscript((ChatMessage("user", "changed dynamic data"),))
    )
    assert restored.request() == transcript.request()
    from qq_ai_bot.runtime.work_repository import WorkCapacityError

    restored.append(ChatMessage("user", "x" * (4 * 1024 * 1024)))
    with pytest.raises(WorkCapacityError):
        await recovered.save("paired")
    intact = await WorkSession(control, "fixed").restore(TurnTranscript(()))
    assert intact.request() == transcript.request()
    async with database.immediate_session() as db:
        await repo.cancel_in_session(db, lease.conversation_id)
    assert not await repo.valid(lease)
    with pytest.raises(WorkConflict):
        await workers.finish(lease, "late output must not notify")
    with pytest.raises(WorkConflict):
        await WorkJournal(repo).save(
            lease,
            identity,
            "fixed",
            intact,
            phase="paired",
            pending=[],
            source_revision=0,
            metadata={},
        )
    async with database.immediate_session() as db:
        await repo.purge_scope(db, lease.conversation_id)
    async with database.sessions() as db:
        assert not (await db.execute(select(media))).all()
        assert not (await db.execute(select(children))).all()


@pytest.mark.asyncio
async def test_finish_repair_root_resume_and_seven_day_archive(database, tmp_path):
    import time

    repo, workers, lease, parent, identity = await stack(database, tmp_path)
    child_lease = await workers.acquire(identity)
    row = await repo.get(identity)
    await repo.checkpoint(child_lease, identity, None, models=3)
    await repo.transition(child_lease, identity, row["revision"], "completed")
    await repo.release(child_lease)
    # Simulate a crash between settlement and publishing the parent's durable receipt.
    await workers.maintain()
    await workers.maintain()
    assert len(await repo.pending(lease, work_id=parent["id"])) == 1
    from qq_ai_bot.runtime.work_schema_v1 import inputs

    # The parent must consume its completion before it can become dormant.
    async with database.immediate_session() as db:
        await db.execute(
            update(inputs).where(inputs.c.work_id == parent["id"]).values(state="consumed")
        )
    parent = await repo.get(parent["id"])
    await repo.transition(lease, parent["id"], parent["revision"], "completed")
    reopened = await workers.reopen_parent(lease, identity, models=2)
    assert reopened["id"] == parent["id"]
    async with database.sessions() as db:
        assert (await db.execute(select(budgets.c.models))).scalar_one() == 5
    async with database.immediate_session() as db:
        await db.execute(
            update(work).where(work.c.id == parent["id"]).values(updated=time.time() - 8 * 86400)
        )
    await workers.maintain()
    assert (await workers.related(parent["id"], identity))["archived_at"] is None
    reopened = await repo.get(parent["id"])
    await repo.transition(lease, parent["id"], reopened["revision"], "completed")
    async with database.immediate_session() as db:
        await db.execute(
            update(work).where(work.c.id == parent["id"]).values(updated=time.time() - 8 * 86400)
        )
    await workers.maintain()
    assert (await workers.related(parent["id"], identity))["archived_at"] is not None
    with pytest.raises(ValueError, match="archived"):
        await workers.reopen_parent(lease, identity, models=1)
    await repo.release(lease)
