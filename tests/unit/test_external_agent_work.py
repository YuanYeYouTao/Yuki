"""Original ACP operations, Work ownership and callback waits against real SQLite."""

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update
from tests.support.social_identity_cases import social_env

from yuki.capabilities.results import ToolExecutionResult, process_receipt
from yuki.identity.db_models import CanonicalPersonModel
from yuki.persistence.models import ChatEventModel
from yuki.runtime.effect_outcomes import execution_evidence
from yuki.runtime.work_activation import current_work_control
from yuki.runtime.work_control import WorkControl
from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_schema_v1 import effects, inputs, work
from yuki.runtime.work_wait import WorkWaitRepository
from yuki.runtime.work_wait_schema import waits
from yuki.sandbox.client import SandboxClient
from yuki.sandbox.completion_receiver import CompletionReceiver
from yuki.sandbox.db_models import SandboxTaskRunModel
from yuki.sandbox.environment_tools import is_execution
from yuki.sandbox.task_repository import SandboxTaskRepository


class ManagerClient(SandboxClient):
    def __init__(self, tasks, path):
        super().__init__(path, tasks=tasks)
        self.calls = []
        self.opened = {}
        self.runs = {}
        self.events = []
        self.before_ack = None

    async def execute(self, name, args, *, request_id, source=None):
        if name == "external_agent":
            return await super().execute(name, args, request_id=request_id, source=source)
        self.calls.append((name, args, request_id))
        if name == "acp_agents":
            return {"agents": [{"id": "configured", "description": "fixture"}]}
        if name == "acp_peer_open":
            return self.opened.setdefault(request_id, {"peer_id": str(uuid4()), "pending": False})
        if name == "acp_prompt_submit":
            return self.runs.setdefault(
                request_id,
                {
                    "kind": "acp_prompt",
                    "request_id": request_id,
                    "run_id": str(uuid4()),
                    "peer_id": args["peer_id"],
                    "session_id": "agent-session",
                    "pending": True,
                    "status": "running",
                },
            )
        if name == "acp_prompt_query":
            return next(view for view in self.runs.values() if view["run_id"] == args["run_id"])
        if name == "acp_peer_close":
            return {
                "admitted": True,
                "pending": True,
                "closing": True,
                "operation_id": request_id,
                **args,
            }
        if name in {"acp_permission_reply", "acp_prompt_cancel"}:
            return {"admitted": True, "pending": True, **args}
        if name == "acp_peer_events":
            if "ack_cursor" in args:
                if self.before_ack:
                    await self.before_ack()
                self.events = []
            return {"events": self.events, "has_more": False, "next_cursor": len(self.events)}
        raise AssertionError(name)


@pytest_asyncio.fixture
async def case(database, tmp_path):
    env = await social_env(database, tmp_path)
    repo = WorkRepository(database)
    lease = await repo.acquire(env.context.conversation_id, 1)
    async with database.sessions() as session:
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
    client = ManagerClient(tasks, tmp_path / "manager.sock")
    token = current_work_control.set(control)
    try:
        yield SimpleNamespace(
            db=database, control=control, client=client, tasks=tasks, source=source
        )
    finally:
        current_work_control.reset(token)
        await repo.release(lease)


async def prompt(case, identity="original", **args):
    return await case.client.execute(
        "external_agent",
        {"action": "prompt", "agent_id": "configured", "prompt": "work", **args},
        request_id=identity,
    )


def permission(view):
    return {
        "peer_id": view["peer_id"],
        "run_id": view["run_id"],
        "request_id": view["request_id"],
        "session_id": view["session_id"],
        "type": "permission_request",
        "cursor": 99,
        "event_id": str(uuid4()),
        "permission_id": str(uuid4()),
        "payload": {"options": [{"optionId": "allow", "kind": "allow_once"}]},
    }


async def test_original_prompt_recovery_and_management_do_not_create_more_tasks(case):
    original = await prompt(case)
    assert await prompt(case) == original
    assert len(case.client.opened) == len(case.client.runs) == 1
    case.control.current = None
    assert (
        await case.client.execute(
            "external_agent", {"action": "get", "run_id": original["run_id"]}, request_id="read"
        )
        == original
    )
    await case.client.execute(
        "external_agent",
        {
            "action": "respond",
            "run_id": original["run_id"],
            "permission_id": "original-permission",
            "option_id": "allow",
        },
        request_id="answer",
    )
    await case.client.execute(
        "external_agent", {"action": "cancel", "run_id": original["run_id"]}, request_id="cancel"
    )
    async with case.db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(SandboxTaskRunModel)) == 1
    assert not is_execution("external_agent", {"action": "respond"})
    with pytest.raises(ValueError, match="accept_work"):
        await prompt(case, "new")


async def test_same_work_reuses_peer_but_new_work_cannot_borrow_its_history(case):
    original = await prompt(case)
    second = await prompt(case, "second", peer_id=original["peer_id"])
    assert second["peer_id"] == original["peer_id"] and second["run_id"] != original["run_id"]
    assert len(case.client.opened) == 1
    case.control.current = await case.control.repository.accept(
        case.control.lease, source_key="different-work", source=case.source, goal="independent"
    )
    with pytest.raises(ValueError, match="peer_not_owned"):
        await prompt(case, "foreign", peer_id=original["peer_id"])
    assert len(case.client.runs) == 2


@pytest.mark.parametrize("phase", ["acp_peer_open", "acp_prompt_submit"])
async def test_confirmed_pre_admission_rejection_is_preserved_without_new_submission(
    case, monkeypatch, phase
):
    execute = case.client.execute
    attempts = []

    async def reject(name, args, *, request_id, source=None):
        if name == phase:
            attempts.append(request_id)
            return {"error": "confirmed_rejection", "executed": False, "retryable": False}
        return await execute(name, args, request_id=request_id, source=source)

    monkeypatch.setattr(case.client, "execute", reject)
    first = await prompt(case)
    assert first["executed"] is False
    original = await case.tasks.get("original")
    assert original.status == "completed" and original.run_id is None
    assert await prompt(case) == first
    assert len(attempts) == 1


@pytest.mark.parametrize("mode", ["any", "all"])
@pytest.mark.parametrize("event_first", [True, False])
async def test_permission_interrupts_completion_wait_without_completing_runs(
    case, mode, event_first
):
    first = await prompt(case)
    second = await prompt(case, "second")
    event = permission(first)
    if event_first:
        await case.tasks.receive_permission(event)
    async with case.db.sessions() as session, session.begin():
        await session.execute(
            update(work)
            .where(work.c.id == case.control.current["id"])
            .values(state="waiting_external")
        )
    waiting = await WorkWaitRepository(case.control.repository).register(
        case.control.lease,
        work_id=case.control.current["id"],
        source=case.source,
        call_key="original-wait",
        mode=mode,
        conditions=[{"kind": "owned_run", "run_id": v["run_id"]} for v in [first, second]],
        deadline_at=None,
    )
    if not event_first:
        await case.tasks.receive_permission(event)
    async with case.db.sessions() as session:
        row = (
            (await session.execute(select(waits).where(waits.c.id == waiting["id"])))
            .mappings()
            .one()
        )
        assert row["status"] == "delivered"
        assert all(c["matched"] is None for c in json.loads(row["conditions_json"]))
        assert (
            await session.scalar(
                select(work.c.state).where(work.c.id == case.control.current["id"])
            )
            == "queued"
        )
        count = await session.scalar(select(func.count()).select_from(inputs))
    await case.tasks.receive_permission(event)
    async with case.db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(inputs)) == count
    assert (await case.tasks.by_run(first["run_id"])).status == "waiting"


@pytest.mark.parametrize("state", ["suspended", "waiting_user", "completed", "cancelled"])
async def test_permission_does_not_revive_explicit_pause_or_terminal(case, state):
    original = await prompt(case)
    async with case.db.sessions() as session, session.begin():
        await session.execute(
            update(work).where(work.c.id == case.control.current["id"]).values(state=state)
        )
    await case.tasks.receive_permission(permission(original))
    async with case.db.sessions() as session:
        assert (
            await session.scalar(
                select(work.c.state).where(work.c.id == case.control.current["id"])
            )
            == state
        )


async def test_callback_is_committed_before_transport_ack(case):
    original = await prompt(case)
    from acp.schema import PermissionOption, ToolCallUpdate

    from yuki.sandbox.acp_peer import Peer, atomic_json, read_events

    root = case.client.socket.parent / "actual-peer"
    peer = Peer(original["peer_id"], root, {"permission_mode": "ask"})
    peer.session_id = original["session_id"]
    peer.active = {"run_id": original["run_id"], "operation_id": original["request_id"]}
    atomic_json(root / "peer_metadata.json", {"session_id": peer.session_id})
    pending = asyncio.create_task(
        peer.request_permission(
            peer.session_id,
            ToolCallUpdate(tool_call_id="actual-call", raw_input={"large": "正文" * 160000}),
            [PermissionOption(option_id="allow", name="Allow", kind="allow_once")],
        )
    )
    await asyncio.sleep(0)
    case.client.events = read_events(root)["events"]
    assert case.client.events[0]["type"] == "permission_request"

    async def committed():
        async with case.db.sessions() as session:
            assert await session.scalar(
                select(inputs.c.id).where(inputs.c.source_key.like("acp-permission:%"))
            )

    case.client.before_ack = committed
    await CompletionReceiver(case.client, case.tasks).drain_peer_events()
    assert not case.client.events
    next(iter(peer.permissions.values()))[1].set_result("allow")
    assert (await pending).outcome.option_id == "allow"


async def test_disabled_original_source_cannot_start_or_answer_through_read_permission(case):
    original = await prompt(case)
    async with case.db.sessions() as session, session.begin():
        await session.execute(
            update(CanonicalPersonModel)
            .where(CanonicalPersonModel.id == case.source["actor_person_id"])
            .values(enabled=False)
        )
    assert (
        await case.client.execute(
            "external_agent", {"action": "get", "run_id": original["run_id"]}, request_id="read"
        )
    )["run_id"] == original["run_id"]
    with pytest.raises(ValueError, match="actor_disabled"):
        await case.client.execute(
            "external_agent",
            {
                "action": "respond",
                "run_id": original["run_id"],
                "permission_id": "question",
                "option_id": "allow",
            },
            request_id="answer",
        )
    with pytest.raises(ValueError, match="actor_disabled"):
        await prompt(case, "new-after-disabled")
    assert len(case.client.opened) == len(case.client.runs) == 1
    assert not any(name == "acp_permission_reply" for name, _, _ in case.client.calls)


@pytest.mark.parametrize("method", ["acp_agents", "terminal_read", "get_code_run_by_request"])
async def test_manager_client_reads_full_protocol_lines_beyond_stream_buffer(
    tmp_path, monkeypatch, method
):
    from yuki.sandbox.acp_peer import read_protocol_line

    content = "正文" * 160000

    async def handle(reader, writer):
        request = json.loads(await read_protocol_line(reader))
        assert request["method"] == method
        writer.write(json.dumps({"body": content}).encode() + b"\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, limit=65536)
    port = server.sockets[0].getsockname()[1]

    async def connect(_path, *, limit):
        return await asyncio.open_connection("127.0.0.1", port, limit=limit)

    monkeypatch.setattr(asyncio, "open_unix_connection", connect, raising=False)
    async with server:
        result = await SandboxClient(tmp_path / "socket").execute(
            method, {}, request_id="original-large-request"
        )
    assert result == {"body": content}


async def test_privacy_closes_unconfirmed_resource_intent_and_skips_plain_terminal(case):
    await case.tasks.prepare(
        "before-open",
        {"tool": "external_agent"},
        {
            **case.source,
            "work_id": case.control.current["id"],
            "conversation_id": case.control.lease.conversation_id,
            "generation": 1,
        },
    )
    await case.tasks.checkpoint(
        "before-open", {"kind": "acp_prompt", "resource_request_id": "before-open:peer"}
    )
    await case.tasks.prepare(
        "terminal",
        {"tool": "terminal_exec"},
        {
            **case.source,
            "work_id": case.control.current["id"],
            "conversation_id": case.control.lease.conversation_id,
            "generation": 1,
        },
    )
    admitted = await case.client.close_peers_for_requests(
        ["before-open", "terminal"], "forget-original"
    )
    assert len(admitted) == 1 and admitted[0]["resource_request_id"] == "before-open:peer"
    assert [name for name, _, _ in case.client.calls] == ["acp_peer_close"]


async def test_cleanup_cannot_confirm_without_original_task_repository(tmp_path):
    with pytest.raises(ValueError, match="cleanup_unconfirmed"):
        await SandboxClient(tmp_path / "socket").close_peers_for_requests(["original"], "forget")


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


@pytest.mark.parametrize("action", ["prompt", "get", "respond", "cancel", "close", "list", None])
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
    async with case.db.sessions() as session:
        stored = json.loads(await session.scalar(select(effects.c.receipt_json)))
    assert stored["outcome"]["pending"] is (action != "prompt")
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
        for entry in ("platform", "app"):
            full = await app.main_agent_contract.definitions(entry)
            direct = await app.main_agent_contract.model_definitions(entry)
            external = next(tool for tool in direct if tool.name == "external_agent")
            assert external.parameters["properties"]["action"]["enum"] == [
                "list",
                "prompt",
                "get",
                "respond",
                "cancel",
                "close",
            ]
            assert not {"command", "argv", "env", "cwd"} & external.parameters["properties"].keys()
            print(
                f"mode={'code' if code_enabled else 'direct'} entry={entry} "
                f"full={len(full)} direct={len(direct)}"
            )
    finally:
        assert not await app.lifecycle._close_entries(app.lifecycle._entries)
