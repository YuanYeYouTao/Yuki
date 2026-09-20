"""Bot inbox crash/retry semantics using actual database transactions."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.sandbox.client import SandboxClient
from qq_ai_bot.sandbox.completion_receiver import CompletionReceiver
from qq_ai_bot.sandbox.db_models import SandboxTaskRunModel
from qq_ai_bot.sandbox.task_repository import SandboxTaskRepository
from tests.support.social_identity_cases import social_env


async def task_receipt_cases(database, tmp_path):
    env = await social_env(database, tmp_path)
    tasks = SandboxTaskRepository(database)
    async with database.sessions() as session:
        event_id = await session.scalar(select(ChatEventModel.id))
    source = {
        "conversation_id": env.context.conversation_id,
        "origin": "user_message",
        "actor_user_id": "10001",
        "trigger_event_id": event_id,
        "generation": 1,
        "presence_id": env.presence,
        "bot_user_id": "80001",
    }
    arguments = {"code": "print(1)"}
    first, second = await asyncio.gather(
        tasks.prepare("request", arguments, source), tasks.prepare("request", arguments, source)
    )
    assert first.request_id == second.request_id
    for changed_args, changed_source in (
        ({"code": "print(2)"}, source),
        (arguments, {**source, "actor_user_id": "10002"}),
    ):
        with pytest.raises(ValueError, match="sandbox_task_idempotency_conflict"):
            await tasks.prepare("request", changed_args, changed_source)
    with pytest.raises(ValueError, match="scheduled_task_requires_work_lease"):
        await tasks.prepare("delegated", arguments, {**source, "origin": "scheduled_automation"})
    run_id = str(uuid4())
    event = {
        "request_id": "request",
        "run_id": run_id,
        "result": {"run_id": run_id, "status": "succeeded", "pending": False, "output": "1"},
    }

    class Transport:
        fail_ack = True

        def __init__(self):
            self.calls = []

        async def execute(self, name, args, *, request_id):
            self.calls.append(name)
            if name == "list_code_completions":
                return {"events": [event], "has_more": False}
            persisted = await tasks.get("request")
            assert persisted.completion_json == json.dumps(event["result"], sort_keys=True)
            return (
                {"error": "connection_lost"}
                if self.fail_ack
                else {"acknowledged": True, "run_id": run_id}
            )

    transport = Transport()
    receiver = CompletionReceiver(transport, tasks)
    with patch.object(tasks, "receive", side_effect=RuntimeError("database unavailable")):
        with pytest.raises(RuntimeError, match="database unavailable"):
            await receiver.drain_once()
    assert transport.calls == ["list_code_completions"]
    with pytest.raises(RuntimeError, match="sandbox_completion_ack_failed"):
        await receiver.drain_once()
    # A new repository/receiver simulates lost process state after the DB commit.
    transport.fail_ack = False
    tasks = SandboxTaskRepository(database)
    receiver = CompletionReceiver(transport, tasks)
    assert await receiver.drain_once() == 1
    assert await receiver.drain_once() == 1
    with pytest.raises(ValueError, match="conflicting_task_completion"):
        await tasks.receive({**event, "result": {**event["result"], "status": "cancelled"}})
    with pytest.raises(ValueError, match="unknown_task_completion"):
        await tasks.receive({**event, "request_id": "unknown"})
    row = await tasks.get("request")
    assert json.loads(row.source_json) == source
    assert row.status == "completed"
    from tests.support.sandbox_source_cases import message_source_cases

    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(SandboxTaskRunModel)) == 1
    from tests.support.sandbox_resume_cases import resume_cases

    await resume_cases(database, env, tasks, source)
    await message_source_cases(database, tasks, source, event)
    writer = SimpleNamespace(write=Mock(), drain=AsyncMock(), close=Mock(), wait_closed=AsyncMock())
    reader = SimpleNamespace(readline=AsyncMock(return_value=b'{"pending":true}\n'))

    async def connect(*args, **kwargs):
        assert (await tasks.get("before-send")).source_json == json.dumps(source, sort_keys=True)
        return reader, writer

    client = SandboxClient(tmp_path / "manager.sock", tasks=tasks)
    with patch.object(asyncio, "open_unix_connection", connect, create=True):
        assert (await client.execute("run_python", arguments, request_id="before-send"))[
            "error"
        ] == "missing_task_source"
        result = await client.execute(
            "run_python", arguments, request_id="before-send", source=source
        )
        assert result["pending"]
        wire = json.loads(writer.write.call_args.args[0])
        assert wire == {"method": "run_python", "args": arguments, "request_id": "before-send"}
        writer.write.reset_mock()
        with pytest.raises(ValueError, match="sandbox_task_idempotency_conflict"):
            await client.execute(
                "run_python", {"code": "different"}, request_id="before-send", source=source
            )
        writer.write.assert_not_called()
    # A lost submission response performs only an idempotency lookup, never a second run.
    rediscovered_run = str(uuid4())
    recovered_result = {"run_id": rediscovered_run, "status": "succeeded", "pending": False}
    recovery_reader = SimpleNamespace(
        readline=AsyncMock(
            side_effect=[OSError("lost response"), json.dumps(recovered_result).encode() + b"\n"]
        )
    )
    recovery_writer = SimpleNamespace(
        write=Mock(), drain=AsyncMock(), close=Mock(), wait_closed=AsyncMock()
    )
    with patch.object(
        asyncio,
        "open_unix_connection",
        AsyncMock(return_value=(recovery_reader, recovery_writer)),
        create=True,
    ):
        recovered = await client.execute(
            "run_python", arguments, request_id="uncertain-submit", source=source
        )
    assert recovered == recovered_result
    methods = [json.loads(call.args[0])["method"] for call in recovery_writer.write.call_args_list]
    assert methods == ["run_python", "get_code_run_by_request"]
    assert (await tasks.get("uncertain-submit")).run_id == rediscovered_run

    # Rejecting an unknown record must not starve a valid record in the same page.
    execute = transport.execute

    async def mixed_page(name, args, *, request_id):
        if name == "list_code_completions":
            return {"events": [{**event, "request_id": "unknown"}, event]}
        return await execute(name, args, request_id=request_id)

    with patch.object(transport, "execute", mixed_page):
        with pytest.raises(ValueError, match="unknown_task_completion"):
            await receiver.drain_once()
    assert transport.calls[-1] == "ack_code_completion"
    # Background receiver retries a failure without a second worker and closes
    # while an acknowledgement is in flight. The committed inbox remains usable.
    receiver = CompletionReceiver(transport, tasks, poll_seconds=0.001)
    ack_started = asyncio.Event()
    attempts = 0

    async def interrupted_ack(name, args, *, request_id):
        nonlocal attempts
        if name == "list_code_completions":
            attempts += 1
            if attempts == 1:
                return {"error": "sandbox_unavailable"}
            return {"events": [event]}
        ack_started.set()
        await asyncio.Event().wait()

    with patch.object(transport, "execute", interrupted_ack):
        await receiver.start()
        worker = receiver._worker
        await receiver.start()
        assert receiver._worker is worker
        await asyncio.wait_for(ack_started.wait(), timeout=2)
        assert (await tasks.get("request")).status == "completed"
        await receiver.close()
        assert worker.done()
        assert not (await receiver.health())["running"]
    assert await receiver.drain_once() == 1
    assert (await receiver.health())["acknowledged_this_process"] == 1
    cursors = []
    acknowledgements = []

    async def paginated(name, args, *, request_id):
        if name == "list_code_completions":
            cursors.append(args["after"])
            if args["after"] == 0:
                return {
                    "events": [{**event, "request_id": "unknown"}] * 20,
                    "has_more": True,
                    "next_cursor": 20,
                }
            return {"events": [event], "has_more": False, "next_cursor": 21}
        acknowledgements.append(args["run_id"])
        return {"acknowledged": True, "run_id": args["run_id"]}

    scanner = CompletionReceiver(transport, tasks)
    with patch.object(transport, "execute", paginated):
        with pytest.raises(ValueError, match="unknown_task_completion"):
            await scanner.drain_once()
        assert await scanner.drain_once() == 1
        with pytest.raises(ValueError, match="unknown_task_completion"):
            await scanner.drain_once()
    assert cursors == [0, 20, 0] and acknowledgements == [run_id]
    from qq_ai_bot.sandbox.continuations import SandboxContinuationRepository

    continuations = SandboxContinuationRepository(database)
    # Old detached jobs are retired and duplicate Manager delivery cannot revive them.
    assert (await continuations.get("request")).state == "blocked"
    await tasks.receive(event)
    restarted = SandboxContinuationRepository(database)
    assert (await restarted.get("request")).state == "blocked"
    observed_run = str(uuid4())
    await tasks.prepare("observed-request", arguments, source)
    await tasks.receive(
        {
            "request_id": "observed-request",
            "run_id": observed_run,
            "result": {**event["result"], "run_id": observed_run},
        }
    )
    assert await restarted.observed("observed-request")
    assert (await restarted.get("observed-request")).state == "observed"
