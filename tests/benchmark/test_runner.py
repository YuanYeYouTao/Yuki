"""Offline native-loop checks; no provider HTTP requests or production services."""

import asyncio
import json
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from benchmarks import runner
from sqlalchemy import text

from yuki.config import Settings
from yuki.domain.messages import ChatResponse, ToolCall, ToolFunction
from yuki.llm.fake import FakeLLMProvider
from yuki.model_runtime.models import ModelTask
from yuki.persistence.database import Database
from yuki.runtime.work_activation import current_work_control
from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_wait import WorkWaitRepository


def call(name, args):
    return ToolCall(str(uuid4()), ToolFunction(name, json.dumps(args)))


class Environment:
    def __init__(self):
        self.identity = str(uuid4())
        self.commands = []
        self.reads = 0
        self.closed = False
        self.cancelled = False
        self.setup_calls = 0

    async def setup(self):
        self.setup_calls += 1
        self.cwd, self.home = "/task", "/home/task-user"

    async def execute(self, name, args, *, request_id, source=None):
        if not self.setup_calls:
            raise AssertionError("native terminal helper was not installed")
        if name == "terminal_exec":
            self.commands.append((args, request_id, source))
            identity = self.identity if len(self.commands) == 1 else str(uuid4())
            return {"run_id": identity, "status": "running", "pending": True}
        if name == "terminal_read":
            self.reads += 1
            completion = {
                "run_id": args["run_id"],
                "status": "succeeded",
                "pending": False,
                "exit_code": 0,
                "output": "actual task output",
            }
            return {**completion, "completion": completion}
        raise AssertionError(f"unexpected environment operation: {name}")

    async def close(self):
        self.closed = True

    async def cancel_runs(self):
        self.cancelled = True


@pytest.fixture
def native(monkeypatch):
    captured = []

    class Responses(list):
        seen = asyncio.Event()

        def pop(self, index=-1):
            self.seen.set()
            return super().pop(index)

    responses = Responses()

    def respond(request):
        captured.append(request)
        return responses.pop(0)

    provider = FakeLLMProvider(respond)
    monkeypatch.setattr("yuki.model_runtime.pool.FakeLLMProvider", lambda: provider)
    monkeypatch.setattr(runner, "_initialize_database", lambda database: database.create_schema())
    return responses, captured


def settings(tmp_path, **values):
    profiles = tmp_path / "fake-profiles.toml"
    profiles.write_text(
        'schema_version = 3\n[profiles.main]\nprovider = "fake"\n'
        'protocol = "chat_completions"\nmodel = "fake-model"\n'
        'base_url = "https://test.invalid/v1"\napi_key_env = ""\n'
        "timeout_seconds = 60\nmax_retries = 0\n"
        "default_temperature = 0.7\ndefault_max_output_tokens = 2048\n"
        'capabilities = ["tools", "structured_output", "reasoning"]\n'
        "[routes]\n" + "".join(f'{task.value} = "main"\n' for task in ModelTask),
        encoding="utf-8",
    )
    return Settings(
        _env_file=None,
        llm_provider="fake",
        llm_model="fake-model",
        model_profiles_file=profiles,
        **values,
    )


async def test_native_tools_send_and_complete_keep_original_work_and_source(tmp_path, native):
    responses, captured = native
    environment = Environment()
    responses.extend(
        [
            ChatResponse("", 0, tool_calls=(call("terminal_exec", {"command": "echo task"}),)),
            ChatResponse(
                "", 0, tool_calls=(call("terminal_read", {"run_id": environment.identity}),)
            ),
            ChatResponse(
                "",
                0,
                tool_calls=(
                    call("send_message", {"text": "Completed task"}),
                    call("task_control", {"action": "complete", "result": "task result"}),
                ),
            ),
        ]
    )
    result = await runner.run_task(
        "Complete the task",
        environment,
        settings(tmp_path),
        tmp_path / "trial",
        task_id="native-core",
        timeout_seconds=20,
    )
    assert result["status"] == result["work_state"] == "completed"
    assert result["messages"][-1]["text"] == "Completed task"
    assert result["work_id"] == environment.commands[0][2]["work_id"]
    assert result["person_id"] == environment.commands[0][2]["actor_person_id"]
    assert result["source_event_id"] == environment.commands[0][2]["trigger_event_id"]
    assert len(environment.commands) == 1 and environment.closed
    assert environment.setup_calls == 1
    assert not environment.cancelled
    assert any(tool.name == "terminal_exec" for tool in captured[0].tools)
    assert any(message.role == "system" for message in captured[0].messages)
    assert (tmp_path / "trial" / "trajectory.jsonl").exists()
    assert result["usage"]["physical_request_count"] == 0
    assert result["usage"]["input_tokens"] is None


async def test_benchmark_removes_native_model_and_tool_segment_limits(tmp_path, native):
    responses, captured = native
    environment = Environment()
    responses.extend(
        [
            ChatResponse(
                "",
                0,
                tool_calls=(
                    call("send_message", {"text": "First result"}),
                    call("send_message", {"text": "Second result"}),
                ),
            ),
            ChatResponse("Finished", 0),
        ]
    )
    result = await runner.run_task(
        "Finish using the task's official deadline",
        environment,
        settings(tmp_path, agent_max_model_requests=1, agent_max_tool_calls=1),
        tmp_path / "segmented",
        task_id="segmented",
        timeout_seconds=20,
    )
    assert result["status"] == "completed"
    assert result["activations"] == 1
    assert [message["text"] for message in result["messages"]] == ["First result", "Second result"]
    assert len(captured) == 2
    assert result["work_model_requests"] == 2
    assert result["work_tool_calls"] == 2


async def test_unlimited_segment_keeps_auxiliary_request_accounting(tmp_path, native, monkeypatch):
    responses, captured = native
    responses.append(ChatResponse("Finished", 0))
    original = FakeLLMProvider._complete

    async def with_auxiliary_request(provider, request):
        control = current_work_control.get()
        assert control is not None and control.segment_model_limit is None
        await control.reserve_request(auxiliary=True)
        return await original(provider, request)

    monkeypatch.setattr(FakeLLMProvider, "_complete", with_auxiliary_request)
    result = await runner.run_task(
        "Finish with an auxiliary request",
        Environment(),
        settings(tmp_path, agent_max_model_requests=1),
        tmp_path / "auxiliary",
        task_id="auxiliary",
        timeout_seconds=20,
    )
    assert result["status"] == "completed"
    assert result["activations"] == len(captured) == 1
    assert result["work_model_requests"] == 2


@pytest.mark.parametrize("caller_limit", [None, 1])
async def test_plugin_continuation_keeps_caller_limit_with_unlimited_native_budget(
    database, tmp_path, monkeypatch, caller_limit
):
    from tests.integration.test_plugin_agent_domain_grants import source_host

    from yuki.plugin_host.main_turn import resume_plugin_work

    provider = FakeLLMProvider()
    _, harness, chat, source, host = await source_host(database, tmp_path, provider, ())
    chat._tools.sandbox_client = SimpleNamespace(execute=AsyncMock(return_value={"ready": True}))
    snapshot = chat._runtime_config.snapshot

    async def unlimited_snapshot(**kwargs):
        config = await snapshot(**kwargs)
        return replace(
            config, agent=replace(config.agent, max_model_requests=None, max_tool_calls=None)
        )

    monkeypatch.setattr(chat._runtime_config, "snapshot", unlimited_snapshot)
    responses = [
        ChatResponse("", 0, tool_calls=(call("environment_status", {}),)),
        ChatResponse(
            "", 0, tool_calls=(call("task_control", {"action": "complete", "result": "done"}),)
        ),
    ]
    provider._responder = lambda _: responses.pop(0)
    repository = WorkRepository(database)
    try:
        with host.invocation_scope(host.plugin_id, source):
            first = await host.agent.run("Inspect the environment", max_model_requests=caller_limit)
        original = await repository.get(first.data["work_id"])
        if caller_limit is not None:
            assert original["state"] == "queued" and original["model_requests"] == caller_limit
            await resume_plugin_work(
                lambda _: host, harness.ledger, original, json.loads(original["source_json"])
            )
        final = await repository.get(original["id"])
        assert final["state"] == "completed"
        assert final["model_requests"] == len(provider.requests) == 2
        chat._tools.sandbox_client.execute.assert_awaited_once()
    finally:
        await host.close_host_resources()


async def test_waiting_user_is_reported_without_fabricating_an_answer(tmp_path, native):
    responses, captured = native
    responses.append(
        ChatResponse(
            "",
            0,
            tool_calls=(
                call("task_control", {"action": "need_input", "reason": "Missing information"}),
            ),
        )
    )
    result = await runner.run_task(
        "Task needs a missing input",
        Environment(),
        settings(tmp_path),
        tmp_path / "waiting",
        task_id="needs-input",
        timeout_seconds=20,
    )
    assert result["status"] == result["work_state"] == "waiting_user"
    assert result["messages"] == [] and len(captured) == 1


async def test_owned_terminal_wait_resumes_original_work_from_real_receipt(tmp_path, native):
    responses, captured = native

    class DelayedEnvironment(Environment):
        async def execute(self, name, args, *, request_id, source=None):
            if name == "terminal_read" and self.reads < 2:
                self.reads += 1
                return {"run_id": self.identity, "status": "running", "pending": True}
            return await super().execute(name, args, request_id=request_id, source=source)

    environment = DelayedEnvironment()
    responses.extend(
        [
            ChatResponse("", 0, tool_calls=(call("terminal_exec", {"command": "long task"}),)),
            ChatResponse(
                "",
                0,
                tool_calls=(
                    call(
                        "task_control",
                        {
                            "action": "wait",
                            "conditions": [{"kind": "owned_run", "run_id": environment.identity}],
                        },
                    ),
                ),
            ),
            ChatResponse("Finished after original completion", 0),
        ]
    )
    result = await runner.run_task(
        "Wait for this command",
        environment,
        settings(tmp_path),
        tmp_path / "owned-wait",
        task_id="owned-wait",
        timeout_seconds=20,
    )
    assert result["status"] == "completed"
    assert result["activations"] == 2 and len(captured) == 3
    assert len(environment.commands) == 1
    assert environment.commands[0][2]["work_id"] == result["work_id"]
    assert environment.reads >= 3


async def test_deadline_preserves_waiting_work_and_artifacts(tmp_path, native):
    responses, captured = native
    responses.append(
        ChatResponse(
            "",
            0,
            tool_calls=(
                call(
                    "task_control",
                    {
                        "action": "wait",
                        "conditions": [{"kind": "time_due", "after_seconds": 60}],
                    },
                ),
            ),
        )
    )
    environment = Environment()
    result = await runner.run_task(
        "Wait for a future signal",
        environment,
        settings(tmp_path),
        tmp_path / "deadline",
        task_id="deadline",
        timeout_seconds=1.5,
    )
    assert result["status"] == "timeout"
    assert result["error"] == "task_deadline_exceeded"
    assert result["work_id"] and environment.closed and environment.cancelled
    assert len(captured) == 1
    assert (
        json.loads((tmp_path / "deadline" / "result.json").read_text(encoding="utf-8"))["status"]
        == "timeout"
    )
    original_requests = len(captured)
    resumed = await runner.run_task(
        "Wait for a future signal",
        Environment(),
        settings(tmp_path),
        tmp_path / "deadline",
        task_id="deadline",
        timeout_seconds=120,
        resume=True,
    )
    assert resumed["status"] == "timeout" and len(captured) == original_requests
    assert resumed["deadline_at"] == result["deadline_at"]


async def test_trial_state_is_isolated_and_reuse_is_not_silently_accepted(tmp_path, native):
    responses, _ = native
    results = []
    for name in ("first", "second"):
        responses.append(ChatResponse("Done", 0))
        results.append(
            await runner.run_task(
                "Same instruction",
                Environment(),
                settings(tmp_path),
                tmp_path / name,
                task_id=name,
                timeout_seconds=20,
            )
        )
    assert results[0]["person_id"] != results[1]["person_id"]
    assert results[0]["work_id"] != results[1]["work_id"]
    with pytest.raises(FileExistsError):
        await runner.run_task(
            "Same instruction",
            Environment(),
            settings(tmp_path),
            tmp_path / "first",
            task_id="first",
            timeout_seconds=20,
        )


async def test_explicit_resume_reuses_original_identity_work_and_cumulative_budget(
    tmp_path,
    native,
    monkeypatch,
):
    responses, captured = native
    responses.append(
        ChatResponse(
            "",
            0,
            tool_calls=(
                call(
                    "task_control",
                    {
                        "action": "wait",
                        "conditions": [{"kind": "time_due", "after_seconds": 60}],
                    },
                ),
            ),
        )
    )
    output = tmp_path / "resumed"
    boundary = asyncio.Event()
    release = asyncio.Event()
    original_drain = runner.SandboxContinuationWorker.drain_once

    async def pause_at_original_boundary(worker):
        await original_drain(worker)
        if captured:
            boundary.set()
            await release.wait()

    monkeypatch.setattr(runner.SandboxContinuationWorker, "drain_once", pause_at_original_boundary)
    initial_task = asyncio.create_task(
        runner.run_task(
            "Continue the original task",
            Environment(),
            settings(tmp_path),
            output,
            task_id="resumed",
            timeout_seconds=20,
        )
    )
    async with asyncio.timeout(10):
        await boundary.wait()
    initial_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await initial_task
    initial = json.loads((output / "result.json").read_text(encoding="utf-8"))
    assert initial["status"] == "cancelled" and initial["work_state"] == "waiting_external"
    monkeypatch.setattr(runner.SandboxContinuationWorker, "drain_once", original_drain)
    database = Database(f"sqlite+aiosqlite:///{output / 'state' / 'yuki.db'}")
    try:
        await WorkWaitRepository(WorkRepository(database)).deliver_due(now=time.time() + 120)
    finally:
        await database.close()
    responses.append(ChatResponse("Original task finished", 0))
    resumed_environment = Environment()
    resumed = await runner.run_task(
        "Continue the original task",
        resumed_environment,
        settings(tmp_path),
        output,
        task_id="resumed",
        timeout_seconds=20,
        resume=True,
    )
    assert resumed["status"] == "completed"
    assert resumed_environment.setup_calls == 1 and resumed_environment.closed
    for key in ("work_id", "person_id", "conversation_id", "source_event_id"):
        assert initial[key] == resumed[key]
    assert resumed["work_model_requests"] == 2
    assert resumed["activations"] == 2 and len(captured) == 2
    assert initial["deadline_at"] == resumed["deadline_at"]
    assert (
        await runner.run_task(
            "Continue the original task",
            Environment(),
            settings(tmp_path),
            output,
            task_id="resumed",
            timeout_seconds=20,
            resume=True,
        )
        == resumed
    )
    assert len(captured) == 2
    with pytest.raises(ValueError, match="resume_configuration_mismatch"):
        await runner.run_task(
            "Continue the original task",
            Environment(),
            settings(tmp_path, agent_max_tool_calls=9),
            output,
            task_id="resumed",
            timeout_seconds=20,
            resume=True,
        )
    with pytest.raises(ValueError, match="resume_task_mismatch"):
        await runner.run_task(
            "A different task",
            Environment(),
            settings(tmp_path),
            output,
            task_id="resumed",
            timeout_seconds=20,
            resume=True,
        )


async def test_external_cancellation_exports_evidence_then_propagates(tmp_path, native):
    responses, _ = native
    responses.append(
        ChatResponse(
            "",
            0,
            tool_calls=(
                call(
                    "task_control",
                    {
                        "action": "wait",
                        "conditions": [{"kind": "time_due", "after_seconds": 60}],
                    },
                ),
            ),
        )
    )
    environment = Environment()
    output = tmp_path / "cancelled"
    task = asyncio.create_task(
        runner.run_task(
            "Wait until cancelled",
            environment,
            settings(tmp_path),
            output,
            task_id="cancelled",
            timeout_seconds=None,
        )
    )
    async with asyncio.timeout(10):
        await responses.seen.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    result = json.loads((output / "result.json").read_text(encoding="utf-8"))
    assert result["status"] == "cancelled" and environment.closed and environment.cancelled
    assert (output / "trajectory.jsonl").exists()


async def test_cold_migration_uses_only_explicit_trial_database(tmp_path, monkeypatch):
    unrelated = tmp_path / "unrelated.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{unrelated}")
    path = tmp_path / "trial.db"
    database = Database(f"sqlite+aiosqlite:///{path}")
    try:
        await runner._initialize_database(database)
        async with database.sessions() as session:
            assert await session.scalar(text("SELECT version_num FROM alembic_version"))
            assert (
                await session.scalar(
                    text("SELECT count(*) FROM sqlite_master WHERE name='persons'")
                )
                == 1
            )
        assert not unrelated.exists()
        assert (tmp_path / "migration.log").exists()
    finally:
        await database.close()


async def test_failed_remote_cancellation_exports_original_model_evidence(tmp_path, native):
    responses, captured = native

    class LostContainer(Environment):
        def __init__(self):
            super().__init__()
            self.read_started = asyncio.Event()
            self.lost = False
            self.close_calls = 0

        async def execute(self, name, args, *, request_id, source=None):
            if name == "terminal_read":
                if self.lost:
                    raise ConnectionError("task container no longer exists")
                self.read_started.set()
                return {"run_id": self.identity, "status": "running", "pending": True}
            return await super().execute(name, args, request_id=request_id, source=source)

        async def cancel_runs(self):
            self.lost = True
            raise OSError("task daemon disconnected during cancellation")

        async def close(self):
            self.close_calls += 1
            raise RuntimeError("task transport close failed")

    environment = LostContainer()
    responses.extend(
        [
            ChatResponse("", 0, tool_calls=(call("terminal_exec", {"command": "long task"}),)),
            ChatResponse(
                "",
                0,
                tool_calls=(
                    call(
                        "task_control",
                        {
                            "action": "wait",
                            "conditions": [{"kind": "owned_run", "run_id": environment.identity}],
                        },
                    ),
                ),
            ),
        ]
    )
    output = tmp_path / "lost-container"
    task = asyncio.create_task(
        runner.run_task(
            "Wait for the original command",
            environment,
            settings(tmp_path),
            output,
            task_id="lost-container",
            timeout_seconds=20,
        )
    )
    async with asyncio.timeout(10):
        await environment.read_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    result = json.loads((output / "result.json").read_text(encoding="utf-8"))
    assert result["status"] == "cancelled" and result["error"] == "task_cancelled"
    assert len(captured) == result["model_invocations"] == 2
    assert result["work_model_requests"] == 2
    assert result["usage"]["physical_request_count"] == 0
    assert {item["operation"] for item in result["cleanup_errors"]} == {
        "environment.cancel_runs",
        "environment.close",
        "terminal_receipts.drain",
    }
    assert environment.close_calls == 1
    trajectory = (output / "trajectory.jsonl").read_text(encoding="utf-8")
    assert '"kind": "model_phases"' in trajectory
    database = Database(f"sqlite+aiosqlite:///{output / 'state' / 'yuki.db'}")
    try:
        assert (await WorkRepository(database).get(result["work_id"]))["model_requests"] == 2
    finally:
        await database.close()
