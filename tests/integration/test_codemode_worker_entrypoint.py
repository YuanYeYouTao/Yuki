"""Real persistent child entry → shared Pi loop → Monty → worker subset binding."""

import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from tests.conftest import build_harness, make_settings
from tests.support.codemode_cases import BINARY, requires_worker
from tests.support.runtime_execution import make_child_executor
from tests.support.social_identity_cases import social_env

from qq_ai_bot.domain.messages import ChatResponse, ToolCall, ToolFunction
from qq_ai_bot.identity.db_models import CanonicalSpaceModel
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.runtime.subagent_repository import SubagentRepository
from qq_ai_bot.runtime.subagent_tools import WORKER_REQUIRED_NAMES
from qq_ai_bot.runtime.work_budget_schema import budgets
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import effects
from qq_ai_bot.services.main_agent_contract import MainAgentContract
from qq_ai_bot.tool_results.artifacts import ToolArtifactRepository
from qq_ai_bot.workspace.short_state import ShortState

pytestmark = requires_worker


@pytest.mark.parametrize("scenario", ["normal", "refused", "cancelled", "resumed"])
async def test_persistent_worker_code_retains_subset_owner_and_root_budget(
    database, tmp_path, scenario, monkeypatch
):
    failures = []
    recover = WorkControl.recover_failure

    async def capture(control, error):
        failures.append(repr(error))
        return await recover(control, error)

    monkeypatch.setattr(WorkControl, "recover_failure", capture)
    env = await social_env(database, tmp_path)
    async with database.sessions() as writer, writer.begin():
        event_id = await writer.scalar(select(ChatEventModel.id))
        space = await writer.get(CanonicalSpaceModel, env.space)
        space.enabled = True
    repo = WorkRepository(database)
    lease = await repo.acquire(env.context.conversation_id, 1)
    source = {
        "origin": "user_message",
        "conversation_id": env.context.conversation_id,
        "actor_user_id": "10001",
        "trigger_event_id": event_id,
        "trigger_id": "inbound",
        "bot_user_id": "80001",
        "presence_id": env.presence,
        "generation": 1,
    }
    parent = await repo.accept(
        lease, source_key="worker-code-source", source=source, goal="inspect"
    )
    children = SubagentRepository(repo)
    identity = await children.start(
        lease, parent["id"], "worker-code-start", {"goal": "inspect", "output_kind": "answer"}
    )
    await repo.release(lease)
    count = 36 if scenario == "resumed" else 1
    code = (
        "await yuki_send_message({'text': 'forbidden'})"
        if scenario == "refused"
        else f"results = []\nfor i in range({count}):\n"
        "    results.append(await yuki_terminal_exec({'command': 'printf child'}))\n"
        "len(results)"
    )
    observed = []

    def respond(request):
        observed.append(request)
        if len(observed) == 1:
            return ChatResponse(
                "",
                0,
                tool_calls=(
                    ToolCall(
                        "worker-code", ToolFunction("execute_code", json.dumps({"code": code}))
                    ),
                ),
            )
        if len(observed) == 3:
            return ChatResponse("Verified result", 0)
        receipt = json.loads(
            next(m.content for m in request.messages if m.tool_call_id == "worker-code")
        )
        if scenario == "refused":
            assert receipt["status"] != "completed"
        else:
            assert receipt["status"] == "completed" and receipt["result"] == count
        return ChatResponse(
            "",
            0,
            tool_calls=(
                ToolCall("worker-complete", ToolFunction("task_control", '{"action":"complete"}')),
            ),
        )

    provider = FakeLLMProvider(respond)
    settings = make_settings(
        database.url,
        runtime_work_enabled=True,
        enabled_groups_csv="20001",
        web_enabled=True,
        web_mode="native",
        code_mode_worker_path=BINARY,
        code_mode_worker_sha256=hashlib.sha256(BINARY.read_bytes()).hexdigest(),
    )
    chat = build_harness(database, settings, provider).processor._chat
    chat._tool_artifacts = ToolArtifactRepository(
        database, tmp_path / "results", retention_seconds=60
    )
    chat.runtime.runner.code_mode_settings = settings
    chat.runtime.runner.main_contract = MainAgentContract(chat, ShortState(env.store))
    downstream = tmp_path / "worker-downstream.jsonl"

    async def sandbox_execute(name, arguments, **kwargs):
        assert name == "terminal_exec"
        assert kwargs["source"]["work_id"] == identity
        assert kwargs["source"]["parent_work_id"] == parent["id"]
        with downstream.open("a") as handle:
            handle.write(json.dumps({"tool": name, "command": arguments["command"]}) + "\n")
        if scenario == "cancelled":
            await repo.cancel(env.context.conversation_id)
        return {
            "ok": True,
            "run_id": str(uuid4()),
            "status": "succeeded",
            "pending": False,
            "exit_code": 0,
        }

    chat._tools.sandbox_client = SimpleNamespace(execute=sandbox_execute)
    executor = make_child_executor(
        repo,
        chat=chat,
        config=chat._runtime_config,
        runner=chat.runtime.runner,
        load_tools=chat.runtime.runner.main_contract.definitions,
        ledger=chat._ledger,
        sandbox_client=chat._tools.sandbox_client,
    )
    await executor.run(identity)
    api = executor.script_api
    assert api is not None and "yuki_terminal_exec" in api.names, (
        failures,
        WORKER_REQUIRED_NAMES - {tool.name for tool in executor.definitions or ()},
    )
    assert not api.names & {
        "yuki_send_message",
        "yuki_memory_change",
        "yuki_update_short_state",
        "yuki_subagent_start",
        "yuki_execute_code",
    }
    assert api.manifest_revision != chat.runtime.runner.main_contract.revision
    if scenario == "resumed":
        assert (await repo.get(identity))["state"] == "queued"
        assert len(observed) == 1
        assert len(downstream.read_text().splitlines()) == 32
        await executor.run(identity)
    if scenario == "cancelled":
        assert (await repo.get(identity))["state"] == "cancelled"
        await executor.run(identity)
        assert len(observed) == 1
    else:
        assert (await repo.get(identity))["state"] == "completed"
        # Complete's original receipt returns to the model before its final text.
        assert len(observed) == 3
    actual = len(downstream.read_text().splitlines()) if downstream.exists() else 0
    assert actual == (0 if scenario == "refused" else count)
    async with database.sessions() as reader:
        root_tools = await reader.scalar(
            select(budgets.c.tools).where(budgets.c.root_id == parent["id"])
        )
        receipts = list(
            await reader.scalars(
                select(effects.c.receipt_json).where(effects.c.work_id == identity)
            )
        )
    assert root_tools == actual
    decoded = [json.loads(row) for row in receipts]
    invocations = [row["invocation"] for row in decoded if "invocation" in row]
    # The outer control receipt uses composition metadata; only business children
    # consume an invocation and the shared root tool budget.
    assert len(invocations) == actual
    assert all(item["manifest_revision"] == api.manifest_revision for item in invocations)
    compositions = [row["composition"] for row in decoded if "composition" in row]
    assert len(compositions) == 1
    assert compositions[0]["version"] == 1
    assert compositions[0]["api_revision"] == api.digest()
    assert not any(action.startswith("send_") for action, _ in env.bot.calls)
