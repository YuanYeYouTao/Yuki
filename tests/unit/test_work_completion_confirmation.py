"""An already observed terminal receipt must not masquerade as new steering."""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import event, select, update
from tests.support.social_identity_cases import social_env
from tests.support.work_session import WorkSession, invoke_tool

from qq_ai_bot.domain.messages import ChatMessage, ToolCall, ToolFunction
from qq_ai_bot.runtime.execution_receipts import ExecutionReceipts, current_receipts
from qq_ai_bot.runtime.work_activation import current_work_control
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import inputs
from qq_ai_bot.sandbox.client import SandboxClient
from qq_ai_bot.sandbox.continuations import SandboxContinuationRepository
from qq_ai_bot.sandbox.db_models import SandboxTaskContinuationModel, SandboxTaskRunModel
from qq_ai_bot.sandbox.task_repository import SandboxTaskRepository
from qq_ai_bot.services.turn_transcript import TurnTranscript


@pytest_asyncio.fixture
async def completion_case(database, tmp_path):
    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    lease = await repository.acquire(env.context.conversation_id, 1)
    assert lease is not None

    async def validate():
        assert await repository.valid(lease)

    control = WorkControl(repository, lease, "completion-fixture", {}, validate)
    control.current = await repository.accept(
        lease, source_key="completion-fixture", source={}, goal="inspect files"
    )
    tasks = SandboxTaskRepository(database)
    receipts = ExecutionReceipts()
    control_token = current_work_control.set(control)
    receipt_token = current_receipts.set(receipts)
    source = {
        "work_id": control.current["id"],
        "conversation_id": lease.conversation_id,
        "generation": lease.generation,
    }
    request_id, run_id = "terminal-fixture", str(uuid4())
    await tasks.prepare(request_id, {"command": "true"}, source)
    await tasks.bind_run(request_id, run_id)
    result = {"run_id": run_id, "pending": False, "status": "succeeded", "exit_code": 0}
    try:
        yield SimpleNamespace(
            database=database,
            repository=repository,
            lease=lease,
            control=control,
            tasks=tasks,
            receipts=receipts,
            source=source,
            request_id=request_id,
            result=result,
            client=SandboxClient(tmp_path / "unused.sock", tasks=tasks),
        )
    finally:
        current_receipts.reset(receipt_token)
        current_work_control.reset(control_token)
        await repository.release(lease)


async def input_rows(case):
    async with case.database.sessions() as session:
        return (await session.execute(select(inputs).order_by(inputs.c.id))).mappings().all()


@pytest.mark.asyncio
@pytest.mark.parametrize("notify_first", [True, False])
@pytest.mark.parametrize("tool", ["terminal_exec", "terminal_read", "get_code_run"])
async def test_confirmed_terminal_result_retires_only_duplicate_notification(
    completion_case, notify_first, tool
):
    case = completion_case
    await case.client._stage_result(
        tool,
        case.request_id,
        {"completion": case.result} if tool == "terminal_read" else case.result,
    )
    if notify_first:
        await case.repository.route_child_completion(case.request_id)
        assert len(await case.control.pending()) == 1
    await case.receipts.confirm()
    await case.repository.route_child_completion(case.request_id)
    assert await case.control.pending() == []
    rows = await input_rows(case)
    assert [row["state"] for row in rows] == (["cancelled"] if notify_first else [])
    # A later confirmation is a read-only no-op, and never recreates a wakeup.
    assert not await SandboxContinuationRepository(case.database).observed(case.request_id)
    await case.repository.route_child_completion(case.request_id)
    assert await case.control.pending() == []

    session = WorkSession(case.control, "fixture")
    session.transcript = TurnTranscript((ChatMessage(role="user", content="inspect"),))
    invoked = []

    async def invoke():
        invoked.append(True)
        return '{"ok":true,"data":{}}'

    result = await invoke_tool(
        session,
        ToolCall(
            id="next-read",
            type="function",
            function=ToolFunction(name="workspace_list", arguments="{}"),
        ),
        invoke,
        side_effecting=False,
    )
    assert json.loads(result)["ok"] is True
    assert invoked == [True]


@pytest.mark.asyncio
async def test_unseen_async_completion_still_wakes_and_enters_as_work_signal(completion_case):
    case = completion_case
    await case.tasks.receive(
        {"request_id": case.request_id, "run_id": case.result["run_id"], "result": case.result}
    )
    await case.repository.route_child_completion(case.request_id)
    await case.receipts.confirm()  # The original model has not received this result.
    pending = await case.control.pending()
    assert len(pending) == 1 and pending[0]["kind"] == "completion"
    messages = await case.control.take_inputs("consume-completion")
    assert len(messages) == 1
    assert "Work 信号" in messages[0].content
    assert "新增输入" not in messages[0].content
    assert case.result["run_id"] in messages[0].content


@pytest.mark.asyncio
async def test_confirmation_keeps_real_input_and_other_run_completion(completion_case):
    case = completion_case
    await case.client._stage_result("terminal_exec", case.request_id, case.result)
    await case.repository.route_child_completion(case.request_id)
    human = await case.repository.enqueue(
        case.lease.conversation_id,
        1,
        "actual-human-input",
        kind="message",
        work_id=case.control.current["id"],
    )
    await case.repository.prepare_input(human, {"text": "new requirement"})
    other_run = str(uuid4())
    await case.tasks.prepare("other-terminal", {"command": "true"}, case.source)
    await case.tasks.receive(
        {
            "request_id": "other-terminal",
            "run_id": other_run,
            "result": {**case.result, "run_id": other_run},
        }
    )
    await case.repository.route_child_completion("other-terminal")
    await case.receipts.confirm()
    pending = await case.control.pending()
    assert {row["source_key"] for row in pending} == {
        "actual-human-input",
        "completion:other-terminal",
    }
    assert any(row["id"] == human for row in pending)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value", [("state", "staged"), ("kind", "message"), ("generation", 2)]
)
async def test_confirmation_does_not_cancel_nonmatching_input(completion_case, field, value):
    case = completion_case
    await case.client._stage_result("terminal_exec", case.request_id, case.result)
    await case.repository.route_child_completion(case.request_id)
    async with case.database.sessions() as session, session.begin():
        await session.execute(update(inputs).values(**{field: value}))
    await case.receipts.confirm()
    row = (await input_rows(case))[0]
    assert row[field] == value and row["state"] != "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["uncertain", "claimed", "blocked", "finished"])
async def test_confirmation_does_not_clear_unknown_or_retired_receipt(completion_case, state):
    case = completion_case
    await case.client._stage_result("terminal_exec", case.request_id, case.result)
    await case.repository.route_child_completion(case.request_id)
    async with case.database.sessions() as session, session.begin():
        receipt = await session.get(SandboxTaskContinuationModel, case.request_id)
        receipt.state = state
    await case.receipts.confirm()
    assert len(await case.control.pending()) == 1
    async with case.database.sessions() as session:
        assert (await session.get(SandboxTaskContinuationModel, case.request_id)).state == state


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["conversation", "generation", "bool_generation", "work", "pending", "run", "waiting"]
)
async def test_confirmation_requires_original_terminal_source(completion_case, change):
    case = completion_case
    await case.client._stage_result("terminal_exec", case.request_id, case.result)
    await case.repository.route_child_completion(case.request_id)
    async with case.database.sessions() as session, session.begin():
        task = await session.get(SandboxTaskRunModel, case.request_id)
        source = json.loads(task.source_json)
        if change == "pending":
            task.completion_json = json.dumps({**case.result, "pending": True})
        elif change == "run":
            task.completion_json = json.dumps({**case.result, "run_id": str(uuid4())})
        elif change == "waiting":
            task.status = "waiting"
        else:
            source.update(
                {
                    "conversation": {"conversation_id": str(uuid4())},
                    "generation": {"generation": 2},
                    "bool_generation": {"generation": True},
                    "work": {"work_id": str(uuid4())},
                }[change]
            )
            task.source_json = json.dumps(source)
    await case.receipts.confirm()
    assert len(await case.control.pending()) == 1


@pytest.mark.asyncio
async def test_repeated_confirmation_without_pending_input_never_takes_writer(completion_case):
    case = completion_case
    await case.client._stage_result("terminal_exec", case.request_id, case.result)
    await case.repository.route_child_completion(case.request_id)
    await case.receipts.confirm()
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(case.database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        assert not await SandboxContinuationRepository(case.database).observed(case.request_id)
        assert not await case.repository.confirm_child_completion(case.request_id)
    finally:
        event.remove(case.database.engine.sync_engine, "before_cursor_execute", capture)
    assert statements
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    assert all("BEGIN IMMEDIATE" not in statement.upper() for statement in statements)


@pytest.mark.asyncio
async def test_unknown_missing_and_unfinished_confirmation_never_take_writer(completion_case):
    case = completion_case
    await case.client._stage_result("terminal_exec", case.request_id, case.result)
    await case.repository.route_child_completion(case.request_id)
    await case.tasks.prepare("unfinished", {"command": "true"}, case.source)
    async with case.database.sessions() as session, session.begin():
        receipt = await session.get(SandboxTaskContinuationModel, case.request_id)
        receipt.state = "uncertain"
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(case.database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        for request_id in (case.request_id, "missing", "unfinished"):
            assert not await case.repository.confirm_child_completion(request_id)
    finally:
        event.remove(case.database.engine.sync_engine, "before_cursor_execute", capture)
    assert len(statements) == 3
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    assert all("BEGIN IMMEDIATE" not in statement.upper() for statement in statements)
    assert len(await case.control.pending()) == 1
