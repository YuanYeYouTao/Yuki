"""Real store/CAS and Control receipts; offline Manager protocol, no executions."""

import base64
import os
from pathlib import Path
from types import SimpleNamespace
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
from qq_ai_bot.sandbox.persistent import PersistentManager
from qq_ai_bot.sandbox.task_repository import SandboxTaskRepository
from qq_ai_bot.workspace.files import FileWorkspace
from qq_ai_bot.workspace.service import WorkspaceService
from qq_ai_bot.workspace.store import WorkspaceError, WorkspaceStore


def command(ctx, revision, payload):
    return ControlCommand(request_id=ctx.request_id, expected_revision=revision, payload=payload)


def test_retired_mutable_artifact_control_is_not_advertised():
    with pytest.raises(ValueError, match="forbidden capability"):
        context("control.workspace.mutate")
    assert not hasattr(ControlCommandService, "mutate_workspace")


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


async def test_binary_upload_writes_only_the_real_workspace_with_original_request(
    database, tmp_path, monkeypatch
):
    raw = b"\x00\xff\x89PNG\r\n"
    seen = []
    writes = []

    class FakeFiles:
        def write(self, path, content, expected_version):
            writes.append((path, content, expected_version))
            return {"path": path, "size": len(content), "version": "written-sha"}

    async def execute(self, name, args, *, request_id, source=None):
        seen.append((name, args, request_id))
        return PersistentManager.file_operation(SimpleNamespace(files=FakeFiles()), name, args)

    monkeypatch.setattr(SandboxClient, "execute", execute)
    store = WorkspaceStore(tmp_path / "artifacts")
    service = WorkspaceService(store)
    service.sandbox = SandboxClient(Path("offline.sock"))
    commands = ControlCommandService(ControlCommandAdapter(database, workspace_service=service))
    ctx = context("control.environment.file.mutate")
    upload = command(
        ctx,
        0,
        {
            "resource_id": "environment",
            "action": "upload",
            "spec": {
                "path": "/workspace/pictures/photo.png",
                "base64": base64.b64encode(raw).decode(),
                "expected_version": "missing",
            },
        },
    )
    result = await commands.mutate_environment_file(ctx, upload)
    assert result.success, (result.effective_state, result.operation, seen, writes)
    assert await commands.mutate_environment_file(ctx, upload) == result
    assert len(seen) == 1 and seen[0][0] == "workspace_upload"
    assert seen[0][2] == ctx.request_id.text
    assert writes == [("/workspace/pictures/photo.png", raw, "missing")]
    assert not store.root.exists()  # no artifact snapshot or indirect checkout


@pytest.mark.skipif(os.name != "posix", reason="FileWorkspace uses POSIX directory FDs")
def test_manager_binary_upload_writes_real_workspace_bytes(tmp_path):
    files = FileWorkspace(tmp_path / "persistent-workspace")
    files.root.mkdir()
    result = PersistentManager.file_operation(
        SimpleNamespace(files=files),
        "workspace_upload",
        {
            "path": "/workspace/pictures/photo.png",
            "base64": base64.b64encode(b"\x00PNG").decode(),
            "expected_version": "missing",
        },
    )
    assert result["size"] == 4
    assert (files.root / "pictures" / "photo.png").read_bytes() == b"\x00PNG"
    with pytest.raises(WorkspaceError, match="version_conflict"):
        PersistentManager.file_operation(
            SimpleNamespace(files=files),
            "workspace_upload",
            {
                "path": "/workspace/pictures/photo.png",
                "base64": base64.b64encode(b"changed").decode(),
                "expected_version": "missing",
            },
        )
    assert (files.root / "pictures" / "photo.png").read_bytes() == b"\x00PNG"


@pytest.mark.parametrize("invalid_kind", ["invalid_base64", "oversized"])
async def test_binary_upload_rejects_invalid_or_oversized_bytes_before_manager(
    database, tmp_path, monkeypatch, invalid_kind
):
    encoded = (
        "%%%"
        if invalid_kind == "invalid_base64"
        else base64.b64encode(b"x" * (4 * 1024 * 1024 + 1)).decode()
    )
    called = False

    async def execute(self, name, args, *, request_id, source=None):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(SandboxClient, "execute", execute)
    service = WorkspaceService(WorkspaceStore(tmp_path / "artifacts"))
    service.sandbox = SandboxClient(Path("offline.sock"))
    commands = ControlCommandService(ControlCommandAdapter(database, workspace_service=service))
    ctx = context("control.environment.file.mutate")
    with pytest.raises(ControlCommandError) as bad:
        await commands.mutate_environment_file(
            ctx,
            command(
                ctx,
                0,
                {
                    "resource_id": "environment",
                    "action": "upload",
                    "spec": {
                        "path": "/workspace/photo.png",
                        "base64": encoded,
                        "expected_version": "missing",
                    },
                },
            ),
        )
    assert bad.value.problem.code is ProblemCode.VALIDATION_ERROR
    assert not called
