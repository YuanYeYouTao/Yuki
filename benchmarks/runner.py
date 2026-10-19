"""Run one isolated task through Yuki's real Main contract and Work execution."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import time
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from yuki.admin.config_service import RuntimeConfigService
from yuki.application.lifecycle import LifecycleRegistry
from yuki.application.modules.model_runtime import ModelRuntimeModule
from yuki.config import Settings
from yuki.domain.conversations import ScopeType
from yuki.domain.messages import ChatMessage, InboundMessage, SenderIdentity
from yuki.execution_trace.db_models import ExecutionTraceEntryModel
from yuki.execution_trace.payload import decode_payload
from yuki.gateway.providers import builtin_provider_catalog
from yuki.gateway.providers.app import AppHandle
from yuki.gateway.registry import GatewayConnectionRegistry
from yuki.identity.canonical_repository import create_person_binding, create_presence
from yuki.identity.canonical_uow import CanonicalIngressUnitOfWork
from yuki.identity.ingress import CanonicalIngressResolver
from yuki.identity.routing import PresenceRouter
from yuki.identity.write_settings import configure_identity_write_settings
from yuki.memory.claim_processor import MemoryClaimProcessor
from yuki.memory.mutation.service import MemoryMutationService
from yuki.memory.repository import MemoryFactRepository
from yuki.memory.runtime.partition_lookup import DatabaseMemoryPartitionLookup
from yuki.memory.service import MemoryFactService
from yuki.model_runtime.db_models import ModelInvocationModel
from yuki.persistence.database import Database
from yuki.persistence.event_repository import EventLedgerRepository
from yuki.persistence.models import ChatEventModel
from yuki.persistence.people_repository import PeopleRepository
from yuki.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork
from yuki.runtime.work_activation import activate_work
from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_wait import WorkWaitRepository
from yuki.sandbox.continuation_worker import SandboxContinuationWorker
from yuki.sandbox.db_models import SandboxTaskRunModel
from yuki.sandbox.task_repository import SandboxTaskRepository
from yuki.services.agent_tools import AgentToolService, ToolRuntime
from yuki.services.chat import ChatService
from yuki.services.concurrency import ConcurrencyManager
from yuki.services.context_assembler import AssembledContext, ContextMetrics
from yuki.services.image_preprocessor import ImagePreprocessor
from yuki.services.invocation_context import InvocationContextFactory
from yuki.services.main_agent_backend import MainAgentBackend
from yuki.services.main_agent_contract import MainAgentContract
from yuki.social.service import SocialService
from yuki.tool_results.artifacts import ToolArtifactRepository
from yuki.tool_results.recorder import ToolReceiptRepository
from yuki.workspace.inspect import WorkspaceInspector
from yuki.workspace.service import WorkspaceService
from yuki.workspace.short_state import ShortState
from yuki.workspace.store import WorkspaceStore

ROOT = Path(__file__).resolve().parents[1]


async def _initialize_database(database: Database) -> None:
    # Alembic's environment deliberately loads Settings; isolate its environment
    # rather than temporarily changing this process's DATABASE_URL.
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "alembic",
        "-c",
        str(ROOT / "alembic.ini"),
        "upgrade",
        "head",
        cwd=ROOT,
        env={
            **os.environ,
            "DATABASE_URL": database.url,
            "PYTHONPATH": str(ROOT / "src") + os.pathsep + os.environ.get("PYTHONPATH", ""),
        },
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await process.communicate()
    except BaseException:
        process.kill()
        await process.wait()
        raise
    path = Path(database.url.removeprefix("sqlite+aiosqlite:///"))
    await asyncio.to_thread((path.parent / "migration.log").write_bytes, output)
    if process.returncode:
        raise RuntimeError("benchmark_database_initialization_failed")


class ReceiptEnvironment:
    """Keep original task receipts while the executor owns only container processes."""

    def __init__(self, environment: Any, tasks: SandboxTaskRepository) -> None:
        self.environment, self.tasks = environment, tasks

    async def execute(
        self,
        name: str,
        args: dict[str, Any],
        *,
        request_id: str,
        source: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if name == "terminal_exec":
            if source is None:
                raise ValueError("missing_task_source")
            prior = await self.tasks.prepare(request_id, {"tool": name, "arguments": args}, source)
            if prior is not None and prior.run_id:
                return await self.execute(
                    "terminal_read", {"run_id": prior.run_id}, request_id=request_id
                )
        result = await self.environment.execute(name, args, request_id=request_id, source=source)
        if name == "terminal_exec" and result.get("run_id"):
            await self.tasks.bind_run(request_id, result["run_id"])
        completion = result.get("completion", result)
        if (
            isinstance(completion, dict)
            and completion.get("status") in {"succeeded", "failed", "cancelled"}
            and completion.get("pending") is False
            and completion.get("run_id")
        ):
            task = await self.tasks.by_run(completion["run_id"])
            if task is not None and task.status != "completed":
                await self.tasks.receive(
                    {
                        "request_id": task.request_id,
                        "run_id": task.run_id,
                        "result": completion,
                    }
                )
        return result

    async def drain(self) -> None:
        async with self.tasks.database.sessions() as session:
            identities = list(
                await session.execute(
                    select(SandboxTaskRunModel.request_id, SandboxTaskRunModel.run_id).where(
                        SandboxTaskRunModel.status == "waiting",
                        SandboxTaskRunModel.run_id.is_not(None),
                    )
                )
            )
        for request_id, identity in identities:
            if hasattr(self.environment, "runs"):
                self.environment.runs[request_id] = identity
            await self.execute("terminal_read", {"run_id": identity}, request_id="bench-observe")


async def _source(
    database: Database,
    registry: GatewayConnectionRegistry,
    router: PresenceRouter,
    instruction: str,
    task_id: str,
) -> tuple[InboundMessage, AppHandle]:
    now = datetime.now(UTC)
    account = f"benchmark-{uuid4().hex}"
    async with database.sessions() as session, session.begin():
        await create_person_binding(
            session, external_account_id=account, display_name="Benchmark", now=now, platform="app"
        )
        presence = await create_presence(
            session, external_account_id="yuki-app", now=now, platform="app"
        )
    handle = AppHandle()
    registry.connect(handle, provider_id="app", presence_id=presence.id)
    message = InboundMessage(
        message_id=str(uuid4()),
        event_type="message:app",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(account, nickname="Benchmark"),
        text=instruction,
        bot_user_id=handle.self_id,
        platform="app",
    )
    admitted = await CanonicalIngressResolver(database, registry, router).pre_admit(handle, message)
    if admitted.dropped:
        raise RuntimeError(f"benchmark_ingress_rejected:{admitted.reason}")
    appended = await CanonicalIngressUnitOfWork(database, router).append_inbound(
        admitted.message, admitted
    )
    return replace(
        admitted.message,
        source_event_id=appended.event.id,
        conversation_id=appended.event.canonical_conversation_id,
    ), handle


async def _export(database: Database, output_dir: Path) -> dict[str, Any]:
    async with database.sessions() as session:
        traces = list(
            await session.scalars(
                select(ExecutionTraceEntryModel).order_by(ExecutionTraceEntryModel.id)
            )
        )
        models = list(await session.scalars(select(ModelInvocationModel)))
        messages = list(
            await session.scalars(
                select(ChatEventModel)
                .where(ChatEventModel.direction == "outbound")
                .order_by(ChatEventModel.id)
            )
        )
    with (output_dir / "trajectory.jsonl").open("w", encoding="utf-8") as stream:
        for row in traces:
            payload = (
                decode_payload(row.payload_gzip, size=row.payload_bytes, digest=row.payload_sha256)
                if row.payload_gzip is not None
                else None
            )
            stream.write(
                json.dumps(
                    {
                        "kind": row.kind,
                        "turn_id": row.turn_id,
                        "work_id": row.work_id,
                        "execution_id": row.execution_id,
                        "payload_status": row.payload_status,
                        "payload": payload,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    fields = {
        "input_tokens": "prompt_tokens",
        "output_tokens": "completion_tokens",
        "cached_input_tokens": "cached_prompt_tokens",
        "total_tokens": "total_tokens",
        "physical_request_count": "physical_request_count",
        "unknown_usage_request_count": "unknown_usage_request_count",
    }
    usage = {
        field: sum(getattr(row, field) for row in models)
        if models and all(getattr(row, field) is not None for row in models)
        else None
        for field in fields.values()
    }
    usage = {name: usage[field] for name, field in fields.items()}
    return {
        "usage": usage,
        "model_invocations": len(models),
        "messages": [{"event_id": row.id, "text": row.content} for row in messages],
    }


async def run_task(
    instruction: str,
    environment: Any,
    settings: Settings,
    output_dir: Path,
    *,
    task_id: str,
    timeout_seconds: float | None,
    resume: bool = False,
) -> dict[str, Any]:
    """Execute the original single-agent direct loop, continuing only this task's Work."""
    started_at = time.time()
    output_dir = await asyncio.to_thread(Path(output_dir).resolve)
    output_dir.mkdir(parents=True, exist_ok=True)
    state = output_dir / "state"
    state.mkdir(mode=0o700, exist_ok=resume)
    manifest_path = state / "task.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if resume else None
    fingerprint = hashlib.sha256(instruction.encode()).hexdigest()
    if manifest is not None and (
        manifest["task_id"] != task_id or manifest["instruction_sha256"] != fingerprint
    ):
        raise ValueError("benchmark_resume_task_mismatch")
    settings = Settings(
        _env_file=None,
        **{
            **settings.model_dump(),
            "database_url": f"sqlite+aiosqlite:///{state / 'yuki.db'}",
            "workspace_directory": state / "workspace",
            "code_mode_enabled": False,
            "subagents_enabled": False,
            "runtime_work_enabled": True,
        },
    )
    from benchmarks.config import public_settings

    configuration = await asyncio.to_thread(public_settings, settings)
    if manifest is not None and (
        manifest["configuration"]["fingerprint"] != configuration["fingerprint"]
    ):
        raise ValueError("benchmark_resume_configuration_mismatch")
    started_at = manifest["started_at"] if manifest else started_at
    deadline_at = (
        manifest["deadline_at"]
        if manifest
        else (started_at + timeout_seconds if timeout_seconds is not None else None)
    )
    prior_result = output_dir / "result.json"
    configure_identity_write_settings(settings)
    cleanup_errors: list[dict[str, str]] = []
    environment_close_attempted = False

    async def cleanup(operation: str, action: Callable[[], Awaitable[None]]) -> None:
        try:
            await action()
        except Exception as exc:
            cleanup_errors.append(
                {
                    "operation": operation,
                    "error": type(exc).__name__,
                    "detail": str(exc),
                }
            )

    async def close_environment() -> None:
        nonlocal environment_close_attempted
        if not environment_close_attempted:
            environment_close_attempted = True
            await cleanup("environment.close", environment.close)

    async with AsyncExitStack() as resources:
        resources.push_async_callback(close_environment)
        # The native helper is task-local. Reinstalling it on resume observes
        # original run directories; setup never starts a replacement command.
        await environment.setup()
        if resume and prior_result.is_file():
            saved_result = json.loads(prior_result.read_text(encoding="utf-8"))
            if saved_result["status"] in {"completed", "failed", "waiting_user", "suspended"}:
                return saved_result
        database = Database(settings.database_url)
        resources.push_async_callback(database.close)
        await _initialize_database(database)
        config = RuntimeConfigService(settings=settings, database=database)
        await config.initialize()
        lifecycle = LifecycleRegistry()
        models = ModelRuntimeModule(
            settings.model_runtime, database, lifecycle=lifecycle, web_settings=settings.web
        ).build()
        await lifecycle.start()
        resources.push_async_callback(lifecycle.close)
        ledger = EventLedgerRepository(database)
        writer = ScopedEventLedgerUnitOfWork(database)
        ledger.set_scoped_writer(writer)
        memories = MemoryFactService(MemoryFactRepository(database))
        mutations = MemoryMutationService(
            settings=settings,
            facts=memories,
            processor=MemoryClaimProcessor(settings=settings, facts=memories),
            ledger=ledger,
        )
        tools = AgentToolService(
            settings=settings,
            ledger=ledger,
            memories=memories,
            runtime_config=config,
            memory_mutations=mutations,
        )
        artifacts = ToolArtifactRepository(
            database,
            state / "tool-artifacts",
            retention_seconds=settings.tooling_result_artifact_retention_seconds,
            max_media_bytes=settings.vision_max_prepared_bytes,
            max_media_frames=settings.vision_max_frames_per_turn,
        )
        chat = ChatService(
            settings=settings,
            model_executor=models.executor,
            concurrency=ConcurrencyManager(),
            ledger=ledger,
            people=PeopleRepository(database),
            memories=memories,
            tools=tools,
            runtime_config=config,
            memory_partition_lookup=DatabaseMemoryPartitionLookup(database),
            tool_artifacts=artifacts,
            tool_receipts=ToolReceiptRepository(database),
        )
        store = WorkspaceStore(settings.workspace_directory)
        environment.workspace_store = store
        workspace = WorkspaceService(store)
        workspace.visual_inspector = WorkspaceInspector(store, ImagePreprocessor())
        tasks = SandboxTaskRepository(database, settings=settings)
        sandbox = ReceiptEnvironment(environment, tasks)
        database.sandbox_client = sandbox
        workspace.sandbox = sandbox
        tools.sandbox_client, tools.workspace_service = sandbox, workspace
        registry = GatewayConnectionRegistry(providers=builtin_provider_catalog())
        router = PresenceRouter(database, registry)
        social = SocialService(database, router, writer)
        social.workspace = store
        tools.social_service = social
        contract = MainAgentContract(chat, ShortState(store))
        chat.runtime.runner.main_contract = contract
        await contract.definitions()
        if manifest is None:
            message, handle = await _source(database, registry, router, instruction, task_id)
        else:
            original = manifest["message"]
            message = InboundMessage(
                **original,
                event_type="message:app",
                scope_type=ScopeType.PRIVATE,
                sender=SenderIdentity(manifest["source"]["actor_user_id"], nickname="Benchmark"),
                text=instruction,
                platform="app",
                bot_user_id="yuki-app",
            )
            handle = AppHandle()
            registry.connect(handle, provider_id="app", presence_id=message.presence_id)
        resources.callback(registry.disconnect, handle)
        runtime_config = await config.snapshot(user_id=message.person_id)
        runtime_config = replace(
            runtime_config,
            agent=replace(runtime_config.agent, max_model_requests=None, max_tool_calls=None),
        )
        source = {
            "origin": "user_message",
            "principal_kind": "person",
            "actor_user_id": message.sender.user_id,
            "actor_person_id": message.person_id,
            "bot_user_id": message.bot_user_id,
            "presence_id": message.presence_id,
            "conversation_id": message.conversation_id,
            "trigger_event_id": message.source_event_id,
            "generation": 1,
            "actor_is_superuser": False,
            "delivery_contract": "return_to_caller",
        }
        repository = WorkRepository(database)
        waits = WorkWaitRepository(repository)
        continuations = SandboxContinuationWorker(repository)
        source_key = f"event:{message.conversation_id}:{message.source_event_id}"
        existing = await repository.by_source(source_key)
        task_work_id = existing["id"] if existing else None
        activations = manifest.get("activations", 0) if manifest else 0
        outcome, error = "unknown", None
        cancelled = False

        def save_manifest() -> None:
            content = {
                "task_id": task_id,
                "instruction_sha256": fingerprint,
                "work_id": task_work_id,
                "source": source,
                "source_key": source_key,
                "activations": activations,
                "started_at": started_at,
                "deadline_at": deadline_at,
                "configuration": configuration,
                "message": {
                    key: getattr(message, key)
                    for key in (
                        "message_id",
                        "person_id",
                        "conversation_id",
                        "presence_id",
                        "source_event_id",
                    )
                },
            }
            pending = manifest_path.with_suffix(".pending")
            pending.write_text(json.dumps(content), encoding="utf-8")
            pending.replace(manifest_path)

        save_manifest()

        async def validate() -> None:
            # No external source or identity can mutate this isolated task's database.
            if message.conversation_id is None or message.person_id is None:
                raise ValueError("benchmark_source_unavailable")

        async def child(run_id: str) -> dict[str, Any] | None:
            task = await tasks.by_run(run_id)
            return (
                None
                if task is None
                else await sandbox.execute(
                    "terminal_read", {"run_id": run_id}, request_id=f"work-check:{run_id}"
                )
            )

        async def execute() -> None:
            nonlocal task_work_id, activations, outcome
            while True:
                await sandbox.drain()
                await continuations.drain_once()
                await waits.deliver_due()
                work = await repository.get(task_work_id) if task_work_id else None
                if work and work["state"] not in {"queued", "running"}:
                    outcome = work["state"]
                    if outcome != "waiting_external":
                        return
                    await asyncio.sleep(0.1)
                    continue
                async with activate_work(
                    repository,
                    message.conversation_id,
                    1,
                    source_key,
                    source,
                    validate,
                    child,
                    work_id=task_work_id,
                    bindings=chat.runtime.bindings,
                ) as control:
                    if control.current is None:
                        control.current = await repository.accept(
                            control.lease,
                            source_key=source_key,
                            source=source,
                            goal=instruction,
                            reporting="quiet",
                        )
                    task_work_id = control.current["id"]
                    save_manifest()
                    context = AssembledContext(
                        metadata_payload={
                            "benchmark_task": task_id,
                            "task_environment": {
                                "cwd": getattr(environment, "cwd", None),
                                "home": getattr(environment, "home", None),
                                "terminal": (
                                    "Terminal tools execute directly inside this task container. "
                                    "Use its actual cwd/home and the paths required by the task."
                                ),
                            },
                        },
                        history_messages=(),
                        current_message=ChatMessage("user", instruction),
                        metrics=ContextMetrics(0, 0, 0, len(instruction), False),
                        current_event_id=message.source_event_id,
                        visible_event_ids=frozenset({message.source_event_id}),
                    )
                    composition = await chat.runtime.main_turns.compose(
                        inbound=message,
                        context=context,
                        runtime=runtime_config,
                    )
                    tool_runtime = ToolRuntime(
                        inbound=message,
                        runtime_config=runtime_config,
                        conversation_key=f"conversation:{message.conversation_id}",
                        memory_session=chat.open_memory_session(autonomous=False),
                        visible_event_ids=composition.visible_event_ids,
                        sandbox_source=source,
                    )
                    agent_runtime = replace(
                        InvocationContextFactory.from_tools(
                            tool_runtime,
                            allowed_capabilities=chat.web_capabilities(runtime_config),
                            max_tool_calls=runtime_config.agent.max_tool_calls,
                            max_model_requests=runtime_config.agent.max_model_requests,
                        ),
                        work_control=control,
                    )
                    await chat.runtime.main_turns.run(
                        composition.messages, agent_runtime, MainAgentBackend(chat, tool_runtime)
                    )
                    activations += 1
                    save_manifest()

        try:
            remaining = None if deadline_at is None else max(0, deadline_at - time.time())
            async with asyncio.timeout(remaining):
                await execute()
        except TimeoutError:
            outcome, error = "timeout", "task_deadline_exceeded"
        except asyncio.CancelledError:
            outcome, error, cancelled = "cancelled", "task_cancelled", True
        except Exception as exc:
            outcome, error = "error", type(exc).__name__
        if outcome in {"timeout", "cancelled", "error"}:
            await cleanup("environment.cancel_runs", environment.cancel_runs)
        # Successful task services stay alive for the official verifier.
        await close_environment()
        await cleanup("terminal_receipts.drain", sandbox.drain)
        await cleanup("model_resources.close", lifecycle.close)
        exported = await _export(database, output_dir)
        exported["diagnostics"] = await lifecycle.health()
        exported["configuration"] = configuration
        work = await repository.get(task_work_id) if task_work_id else None
        revision = await asyncio.create_subprocess_exec(
            "git",
            "rev-parse",
            "HEAD",
            cwd=ROOT,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        revision_stdout, _ = await revision.communicate()
        dirty = await asyncio.create_subprocess_exec(
            "git",
            "status",
            "--porcelain",
            "--untracked-files=all",
            cwd=ROOT,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        dirty_stdout, _ = await dirty.communicate()
        result = {
            "task_id": task_id,
            "started_at": started_at,
            "deadline_at": deadline_at,
            "status": outcome,
            "git_commit": revision_stdout.decode().strip() if revision.returncode == 0 else None,
            "git_dirty": bool(dirty_stdout) if dirty.returncode == 0 else None,
            "error": error,
            "cleanup_errors": cleanup_errors,
            "work_id": task_work_id,
            "work_state": work["state"] if work else None,
            "conversation_id": message.conversation_id,
            "person_id": message.person_id,
            "source_event_id": message.source_event_id,
            "activations": activations,
            "tool_contract_revision": contract.app_revision,
            "work_model_requests": work["model_requests"] if work else None,
            "work_tool_calls": work["tool_calls"] if work else None,
            **exported,
        }
        (output_dir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if cancelled:
            raise asyncio.CancelledError
        return result
