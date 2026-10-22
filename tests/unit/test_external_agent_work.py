"""Fixed external Agent declaration and original Work execution evidence."""

import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from tests.support.social_identity_cases import social_env

from yuki.capabilities.results import ToolExecutionResult, process_receipt
from yuki.persistence.models import ChatEventModel
from yuki.runtime.effect_outcomes import execution_evidence
from yuki.runtime.work_activation import current_work_control
from yuki.runtime.work_control import WorkControl
from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_schema_v1 import effects
from yuki.sandbox.task_repository import SandboxTaskRepository


@pytest_asyncio.fixture
async def case(database, tmp_path):
    env = await social_env(database, tmp_path)
    repo = WorkRepository(database)
    lease = await repo.acquire(env.context.conversation_id, 1)
    async with database.read_session() as session:
        event = await session.scalar(select(ChatEventModel))
    source = {
        "actor_person_id": env.person,
        "principal_kind": "person",
        "origin": "user_message",
        "generation": 1,
        "trigger_event_id": event.id,
        "bot_user_id": env.bot.self_id,
        "presence_id": env.presence,
    }

    async def valid():
        assert await repo.valid(lease)

    control = WorkControl(repo, lease, "acp-owner", source, valid)
    control.current = await repo.accept(
        lease, source_key="acp-owner", source=source, goal="delegate"
    )
    tasks = SandboxTaskRepository(database)
    client = SimpleNamespace(calls=[])
    token = current_work_control.set(control)
    try:
        yield SimpleNamespace(
            db=database, control=control, client=client, tasks=tasks, source=source
        )
    finally:
        current_work_control.reset(token)
        await repo.release(lease)


def test_status_read_is_observation_and_peer_close_is_not_prompt_completion():
    result = ToolExecutionResult(
        ok=True,
        provider_id="core",
        tool_name="external_agent",
        data={"kind": "acp_prompt", "run_id": "run", "status": "running", "pending": True},
    )
    evidence = execution_evidence(
        result, tool="external_agent", side_effecting=True, arguments='{"action":"get"}'
    )
    assert evidence["side_effecting"] is False and evidence["pending"]
    assert process_receipt(result)["run_id"] == "run"
    assert (
        process_receipt(
            ToolExecutionResult(
                ok=True,
                provider_id="core",
                tool_name="external_agent",
                data={
                    "kind": "acp_control",
                    "run_id": "control",
                    "peer_id": "peer",
                    "status": "succeeded",
                    "pending": False,
                },
            )
        )
        == {}
    )


@pytest.mark.parametrize(
    "action",
    ["prompt", "queue", "get", "watch", "steer", "respond", "cancel", "detach", "list", None],
)
@pytest.mark.parametrize("by_request", [False, True])
async def test_only_original_prompt_action_is_reconciled_as_execution(case, action, by_request):
    repo, lease, owner = case.control.repository, case.control.lease, case.control.current["id"]
    await repo.prepare_effect(
        lease,
        owner,
        "original-effect",
        "tool",
        invocation={"version": 1, "tool_id": "external_agent", "action": action, "revision": 0},
    )
    previous = {
        "tool": "external_agent",
        "side_effecting": True,
        "pending": True,
        "request_id": "original-request",
        "run_id": None if by_request else "original-run",
    }
    await repo.record_effect("original-effect", "accepted", {"outcome": previous})
    await repo.resolve_run_effects(
        lease,
        owner,
        "original-run",
        {
            "run_id": "original-run",
            "request_id": "original-request",
            "status": "succeeded",
            "pending": False,
            "uncertain": False,
            "ok": True,
        },
        **(
            {"effect_key": "original-effect", "request_id": "original-request"}
            if by_request
            else {}
        ),
    )
    async with case.db.read_session() as session:
        stored = json.loads(await session.scalar(select(effects.c.receipt_json)))
    assert stored["outcome"]["pending"] is (action not in {"prompt", "queue"})
    assert stored["invocation"]["action"] == action


@pytest.mark.parametrize("action", ["prompt", "respond"])
async def test_unknown_paid_receipt_keeps_original_action_without_replaying(case, action):
    from yuki.runtime.effect_outcomes import ResultCapture, current_result_capture
    from yuki.runtime.work_journal import WorkJournal

    repo, owner = case.control.repository, case.control.current["id"]
    await repo.prepare_effect(
        case.control.lease,
        owner,
        "unknown-original",
        "tool",
        invocation={"version": 1, "tool_id": "external_agent", "action": action, "revision": 0},
    )
    capture = ResultCapture(owner, "unknown-original")
    token = current_result_capture.set(capture)
    try:
        value = json.loads(await WorkJournal(repo).effect_result("unknown-original"))
    finally:
        current_result_capture.reset(token)
    assert value["replay_forbidden"] is True
    assert capture.evidence["tool"] == "external_agent"
    assert capture.evidence["action"] == action
    assert capture.evidence["uncertain"] is True
    assert not case.client.calls


def test_invocation_keeps_action_but_not_prompt_in_durable_metadata():
    from yuki.capabilities.invocation import direct_invocations
    from yuki.domain.messages import ToolCall, ToolFunction

    for arguments in ['{"action":"prompt","prompt":"private"}', "invalid JSON"]:
        invocation = direct_invocations(
            (
                ToolCall(
                    id="original", function=ToolFunction(name="external_agent", arguments=arguments)
                ),
            ),
            SimpleNamespace(),
        )[0]
        metadata = invocation.durable_metadata()
        assert metadata.get("action") == ("prompt" if arguments.startswith("{") else None)
        assert "private" not in json.dumps(metadata)


@pytest.mark.parametrize("action", ["respond", "cancel", "close"])
def test_management_intent_admission_is_separate_from_remote_prompt_completion(action):
    control = {"admitted": True, "pending": True, "run_id": "control-job"}
    view = {"kind": "acp_prompt", "run_id": "original", "pending": True, "status": "running"}
    if action == "respond":
        view["permission_response"] = control
    elif action == "close":
        view = {"close": control}
    else:
        view["cancel_requested"] = True
    evidence = execution_evidence(
        ToolExecutionResult(ok=True, data=view, provider_id="core", tool_name="external_agent"),
        tool="external_agent",
        side_effecting=True,
        arguments=json.dumps({"action": action}),
    )
    assert evidence["pending"] is evidence["uncertain"] is False
    assert evidence["status"] == "admitted" and evidence["mutation_committed"] is True
    assert evidence["ok"] is True
    assert view.get("status") in (None, "running")


@pytest.mark.parametrize("code_enabled", [False, True])
async def test_fixed_main_and_code_contract_exposes_generic_agent_without_operator_secrets(
    database, tmp_path, code_enabled
):
    from tests.integration.test_current_material_main_contract import _deployment

    from yuki.llm.fake import FakeLLMProvider

    app, *_ = await _deployment(database, tmp_path, FakeLLMProvider(), code_enabled=code_enabled)
    try:
        contract = app.main_agent_contract
        full = await contract.definitions()
        direct = await contract.model_definitions()
        external = next(tool for tool in direct if tool.name == "external_agent")
        assert external == next(tool for tool in full if tool.name == "external_agent")
        assert external.parameters["properties"]["action"]["enum"] == [
            "list",
            "get",
            "watch",
            "prompt",
            "steer",
            "queue",
            "respond",
            "cancel",
            "detach",
        ]
        assert (
            not {"command", "argv", "env", "peer_id", "agent_id"}
            & external.parameters["properties"].keys()
        )
        if code_enabled:
            assert contract.script_api.schemas["external_agent"] == external.parameters
        else:
            assert contract.script_api is None
    finally:
        assert not await app.lifecycle._close_entries(app.lifecycle._entries)


@pytest.mark.parametrize("ownership", ["observe", "owned", None])
async def test_observing_original_native_run_does_not_block_work_completion(case, ownership):
    from datetime import UTC, datetime
    from uuid import uuid4

    from yuki.sandbox.db_models import SandboxTaskRunModel

    repo, control = case.control.repository, case.control
    async with case.db.immediate_session() as writer:
        writer.add(
            SandboxTaskRunModel(
                request_id="original-observation",
                source_conversation_id=control.lease.conversation_id,
                source_json=json.dumps({"work_id": control.current["id"]}),
                payload_hash="a" * 64,
                run_id=str(uuid4()),
                status="waiting",
                progress_json=json.dumps({"kind": "external_agent", "ownership": ownership}),
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
    assert await repo.has_owned_execution(control.lease, control.current["id"]) is (
        ownership != "observe"
    )


@pytest.mark.parametrize("action", ["watch", "steer", "respond", "cancel", "detach"])
def test_new_control_receipt_does_not_claim_native_turn_completed(action):
    original = {
        "kind": "external_agent",
        "action": action,
        "run_id": "original-run",
        "operation_id": "original-operation",
        "session_id": "original-session",
        "admitted": True,
        "pending": True,
        "status": "running",
    }
    evidence = execution_evidence(
        ToolExecutionResult(ok=True, data=original, provider_id="core", tool_name="external_agent"),
        tool="external_agent",
        side_effecting=True,
        arguments=json.dumps({"action": action}),
    )
    assert evidence["status"] == "admitted" and evidence["pending"] is False
    assert evidence["operation_id"] == "original-operation"
    assert evidence["session_id"] == "original-session"
    assert original["status"] == "running" and original["pending"] is True


async def test_recovered_original_work_effect_keeps_internal_agent_references(case):
    from datetime import UTC, datetime
    from hashlib import sha256
    from uuid import uuid4

    from yuki.runtime.work_journal import WorkJournal
    from yuki.sandbox.db_models import SandboxTaskRunModel

    repo, control = case.control.repository, case.control
    key = "unknown-original-operation"
    await repo.prepare_effect(
        control.lease,
        control.current["id"],
        key,
        "tool",
        invocation={"version": 1, "tool_id": "external_agent", "action": "queue", "revision": 0},
    )
    run, operation, session_id = str(uuid4()), str(uuid4()), str(uuid4())
    async with case.db.immediate_session() as writer:
        writer.add(
            SandboxTaskRunModel(
                request_id=sha256(key.encode()).hexdigest(),
                source_conversation_id=control.lease.conversation_id,
                source_json=json.dumps({"work_id": control.current["id"]}),
                payload_hash="a" * 64,
                run_id=run,
                status="waiting",
                progress_json=json.dumps(
                    {
                        "kind": "external_agent",
                        "operation_id": operation,
                        "session_id": session_id,
                        "action": "queue",
                    }
                ),
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
    result = json.loads(await WorkJournal(repo).effect_result(key))
    assert result["data"]["operation_id"] == operation
    assert result["data"]["session_id"] == session_id
    assert result["data"]["run_id"] == run and result["data"]["pending"] is True
