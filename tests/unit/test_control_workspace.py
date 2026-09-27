"""Real store/CAS and Control receipts; offline Manager protocol, no executions."""

import base64
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from tests.unit.test_control_plane_foundation import context

from qq_ai_bot.control_plane import (
    ControlCommand,
    ControlCommandError,
    ControlCommandService,
    ControlQueryError,
    ControlQueryService,
    ProblemCode,
)
from qq_ai_bot.control_plane.json_types import freeze_json_object
from qq_ai_bot.domain.identity import RequestId
from qq_ai_bot.persistence.control_command import ControlCommandAdapter
from qq_ai_bot.persistence.control_query import ControlQueryAdapter
from qq_ai_bot.persistence.models import AdminOperationEventModel
from qq_ai_bot.sandbox.client import SandboxClient
from qq_ai_bot.sandbox.db_models import SandboxTaskContinuationModel
from qq_ai_bot.sandbox.task_repository import SandboxTaskRepository
from qq_ai_bot.workspace.service import WorkspaceService
from qq_ai_bot.workspace.store import WorkspaceStore


def command(ctx, revision, payload):
    return ControlCommand(request_id=ctx.request_id, expected_revision=revision, payload=payload)


async def test_artifact_upload_edit_conflict_delete_and_replay(database, tmp_path):
    store = WorkspaceStore(tmp_path / "files")
    service = WorkspaceService(store)
    commands = ControlCommandService(ControlCommandAdapter(database, workspace_service=service))
    ctx = context("control.workspace.mutate")
    create = command(
        ctx,
        0,
        {
            "action": "upload",
            "resource_id": "yuki",
            "spec": {"name": "hello.txt", "base64": base64.b64encode(b"original").decode()},
        },
    )
    original = await commands.mutate_workspace(ctx, create)
    assert original.success and original.revision == 1
    assert await commands.mutate_workspace(ctx, create) == original
    assert len(store.list()["items"]) == 1
    assert store.read_bytes(original.resource_id)[1] == b"original"
    ctx = replace(ctx, request_id=RequestId.new())
    changed = await commands.mutate_workspace(
        ctx,
        command(
            ctx,
            1,
            {
                "action": "edit",
                "resource_id": original.resource_id,
                "spec": {"name": "hello.txt", "text": "new"},
            },
        ),
    )
    assert changed.revision == 2 and store.read_bytes(original.resource_id)[1] == b"new"
    ctx = replace(ctx, request_id=RequestId.new())
    with pytest.raises(ControlCommandError) as conflict:
        await commands.mutate_workspace(
            ctx, command(ctx, 1, {"action": "delete", "resource_id": original.resource_id})
        )
    assert conflict.value.problem.code is ProblemCode.VERSION_CONFLICT
    ctx = replace(ctx, request_id=RequestId.new())
    deleted = await commands.mutate_workspace(
        ctx, command(ctx, 2, {"action": "delete", "resource_id": original.resource_id})
    )
    assert deleted.success and not store.list()["items"]


async def test_unknown_artifact_effect_keeps_original_fence(database, tmp_path):
    store = WorkspaceStore(tmp_path / "files")
    adapter = ControlCommandAdapter(database, workspace_service=WorkspaceService(store))
    commands = ControlCommandService(adapter)
    ctx = context("control.workspace.mutate")

    def fail():
        raise RuntimeError("final receipt unavailable")

    adapter._after_audit_flush = fail
    original = command(ctx, 0, {"action": "upload", "spec": {"name": "once.txt", "base64": "eA=="}})
    result = await commands.mutate_workspace(ctx, original)
    assert not result.success and result.operation.status.value == "unknown"
    adapter._after_audit_flush = None
    replay = await commands.mutate_workspace(ctx, original)
    assert not replay.success and replay.operation.status.value == "unknown"
    assert len(store.list()["items"]) == 1
    fresh = replace(ctx, request_id=RequestId.new())
    with pytest.raises(ControlCommandError) as fenced:
        await commands.mutate_workspace(fresh, command(fresh, 0, original.payload))
    assert fenced.value.problem.code is ProblemCode.PRECONDITION_FAILED


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "upload", "spec": {"name": "../bad", "base64": "eA=="}},
        {"action": "upload", "spec": {"name": "bad", "base64": "%%%"}},
        {"action": "upload", "spec": {"name": "bad", "base64": "eA==", "host_path": "x"}},
    ],
)
async def test_upload_validation_happens_before_store_effect(database, tmp_path, payload):
    store = WorkspaceStore(tmp_path / "files")
    commands = ControlCommandService(
        ControlCommandAdapter(database, workspace_service=WorkspaceService(store))
    )
    ctx = context("control.workspace.mutate")
    with pytest.raises(ControlCommandError) as bad:
        await commands.mutate_workspace(ctx, command(ctx, 0, payload))
    assert bad.value.problem.code is ProblemCode.VALIDATION_ERROR
    assert not store.root.exists()


async def test_terminal_original_request_once_and_completion_without_chat(
    database, tmp_path, monkeypatch
):
    run = str(uuid4())
    seen = []

    async def execute(self, name, args, *, request_id, source=None):
        # The real short Control writer has already committed before dispatch.
        async with database.immediate_session() as session:
            assert await session.scalar(select(func.count(AdminOperationEventModel.id))) >= 1
        seen.append((name, args, request_id, source))
        return {"run_id": run, "status": "running", "pending": True}

    monkeypatch.setattr(SandboxClient, "execute", execute)
    workspace = WorkspaceService(WorkspaceStore(tmp_path / "files"))
    workspace.sandbox = SandboxClient(Path("offline.sock"))
    commands = ControlCommandService(ControlCommandAdapter(database, workspace_service=workspace))
    ctx = context("control.terminal.mutate")
    original = command(
        ctx,
        0,
        {"resource_id": "environment", "action": "exec", "spec": {"command": "offline fixture"}},
    )
    accepted = await commands.mutate_environment_terminal(ctx, original)
    assert accepted.success and accepted.resource_id == run
    assert await commands.mutate_environment_terminal(ctx, original) == accepted
    assert len(seen) == 1 and seen[0][3] is None
    assert seen[0][2] == f"control:{ctx.principal.principal_id.text}:{ctx.request_id.text}"
    tasks = SandboxTaskRepository(database)
    completion = {
        "request_id": seen[0][2],
        "run_id": run,
        "result": {"run_id": run, "status": "succeeded", "pending": False, "stdout": "result"},
    }
    await tasks.receive(completion)
    await tasks.receive(completion)
    async with database.sessions() as session:
        assert (
            await session.scalar(select(func.count(SandboxTaskContinuationModel.request_id))) == 0
        )
        assert (
            await session.scalar(
                select(func.count(AdminOperationEventModel.id)).where(
                    AdminOperationEventModel.operation == "control.terminal.completion"
                )
            )
            == 1
        )
    completion["result"]["stdout"] = "different"
    with pytest.raises(ValueError, match="conflicting"):
        await tasks.receive(completion)
    completion["request_id"] = f"control:{ctx.principal.principal_id.text}:{uuid4()}"
    with pytest.raises(ValueError, match="intent missing"):
        await tasks.receive(completion)


async def test_environment_content_authorization_precedes_socket_reads(
    database, tmp_path, monkeypatch
):
    workspace = WorkspaceService(WorkspaceStore(tmp_path / "files"))
    workspace.sandbox = SandboxClient(Path("offline.sock"))
    called = 0

    async def execute(*args, **kwargs):
        nonlocal called
        called += 1
        return {"text": "private content"}

    monkeypatch.setattr(SandboxClient, "execute", execute)
    queries = ControlQueryService(ControlQueryAdapter(database, workspace_service=workspace))
    ctx = context("control.workspace.metadata.read")
    schema = await queries.read_environment(ctx, "schema", freeze_json_object({}))
    assert schema.fields["file_actions"]["write"] and called == 0
    for section, arguments in [("file", {"path": "a"}), ("terminal", {"run_id": str(uuid4())})]:
        with pytest.raises(ControlQueryError) as denied:
            await queries.read_environment(ctx, section, freeze_json_object(arguments))
        assert denied.value.problem.code is ProblemCode.CAPABILITY_DENIED
    assert called == 0
    with pytest.raises(ValueError):
        await queries.read_environment(
            context("control.workspace.metadata.read", "control.workspace.content.read"),
            "file",
            freeze_json_object({"path": "../../etc/passwd"}),
        )
    assert called == 0


async def test_terminal_submission_uses_authenticated_operator_and_denies_before_socket(
    database, tmp_path, monkeypatch
):
    from qq_ai_bot.domain.identity import RequestId

    workspace = WorkspaceService(WorkspaceStore(tmp_path / "files"))
    workspace.sandbox = SandboxClient(Path("offline.sock"))
    observed = []

    async def execute(self, name, args, **kwargs):
        observed.append((name, args))
        return {"error": "run_not_found"}

    monkeypatch.setattr(SandboxClient, "execute", execute)
    queries = ControlQueryService(ControlQueryAdapter(database, workspace_service=workspace))
    request = RequestId.new()
    with pytest.raises(ControlQueryError) as denied:
        await queries.read_terminal_submission(context(), request)
    assert denied.value.problem.code is ProblemCode.CAPABILITY_DENIED and not observed
    for _ in range(2):
        ctx = context("control.terminal.content.read")
        await queries.read_terminal_submission(ctx, request)
        assert observed[-1] == (
            "get_code_run_by_request",
            {"request_id": f"control:{ctx.principal.principal_id.text}:{request.text}"},
        )
    assert observed[0] != observed[1]
