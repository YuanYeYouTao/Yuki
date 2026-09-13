"""One background worker slot, using the existing Runner and execution backend."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from contextlib import ExitStack
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from qq_ai_bot.domain.messages import ChatMessage, InboundMessage, SenderIdentity
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.runtime.subagent_repository import SubagentRepository
from qq_ai_bot.runtime.subagent_schema import children
from qq_ai_bot.runtime.subagent_tools import WORKER_NAMES, WORKER_PROMPT
from qq_ai_bot.runtime.work_activation import current_work_control
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_repository import WorkConflict, WorkLease, WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import work
from qq_ai_bot.sandbox.source_recovery import recover_source
from qq_ai_bot.services.agent_runner import AgentRuntime
from qq_ai_bot.services.agent_tools import ToolRuntime
from qq_ai_bot.time.models import TimeContext

logger = logging.getLogger(__name__)


def _sqlite_busy(exc: BaseException) -> bool:
    if not isinstance(exc, OperationalError):
        return False
    code = getattr(exc.orig, "sqlite_errorcode", 0)
    return (
        isinstance(code, int) and code & 255 in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
    ) or str(exc.orig).lower() in {"database is locked", "database table is locked"}


class WorkerBackend:
    def __init__(self, delegate: Any, names: frozenset[str]) -> None:
        self.delegate, self.names = delegate, names

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    def work_control_allowed(self, name: str) -> bool:
        return name in self.names

    async def execute(self, name: str, arguments_json: str, runtime: AgentRuntime) -> str:
        if name not in self.names:
            return '{"ok":false,"error":"worker_tool_not_declared"}'
        return str(await self.delegate.execute(name, arguments_json, runtime))


class SubagentScheduler:
    def __init__(self, app: Any) -> None:
        self.app = app
        self.repository = WorkRepository(app.database)
        self.children = SubagentRepository(self.repository)
        self.task: asyncio.Task[None] | None = None
        self.last_error: str | None = None
        self.definitions: tuple[Any, ...] | None = None

    async def health(self) -> dict[str, Any]:
        return {
            "running": self.task is not None and not self.task.done(),
            "admission_enabled": self.app.database.subagents_enabled,
            "last_error_category": self.last_error,
            "tool_count": len(self.definitions or ()),
        }

    async def start(self) -> None:
        if self.app.database.subagents_enabled and self.app.settings.global_llm_concurrency < 2:
            raise ValueError("subagents_require_foreground_model_slot")
        if self.app.settings.runtime_work_enabled and self.task is None:
            self.definitions = tuple(
                t
                for t in await self.app.main_agent_contract.definitions()
                if t.name in WORKER_NAMES
            )
            if self.app.database.subagents_enabled and (
                frozenset(t.name for t in self.definitions) != WORKER_NAMES
            ):
                raise ValueError("incomplete_worker_tool_manifest")
            self.task = asyncio.create_task(self.loop(), name="subagent-scheduler")

    async def close(self) -> None:
        task, self.task = self.task, None
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def loop(self) -> None:
        while True:
            try:
                await self.children.maintain()
                await self.cancel_commands()
                async with self.app.database.sessions() as session:
                    ids = list(
                        await session.scalars(
                            select(children.c.work_id)
                            .join(work, work.c.id == children.c.work_id)
                            .where(
                                work.c.state.in_(("queued", "running")),
                                children.c.archived_at.is_(None),
                            )
                            .order_by(work.c.updated)
                            .limit(8)
                        )
                    )
                for identity in ids:
                    await asyncio.gather(self.run(identity), return_exceptions=True)
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = type(exc).__name__
                logger.warning("subagent_scheduler_failed category=%s", self.last_error)
            await asyncio.sleep(1)

    async def cancel_commands(self) -> None:
        """Reconcile cancellation outside the database transaction, by original run ID."""
        from qq_ai_bot.sandbox.db_models import SandboxTaskRunModel

        async with self.app.database.sessions() as session:
            ids = list(
                await session.scalars(
                    select(SandboxTaskRunModel.run_id)
                    .join(
                        children,
                        children.c.work_id
                        == func.json_extract(SandboxTaskRunModel.source_json, "$.work_id"),
                    )
                    .join(work, work.c.id == children.c.work_id)
                    .where(
                        work.c.state == "cancelled",
                        SandboxTaskRunModel.status == "waiting",
                        SandboxTaskRunModel.run_id.is_not(None),
                    )
                    .limit(8)
                )
            )
        for run_id in ids:
            await self.app.sandbox_client.execute("cancel_code_run", {"run_id": run_id})

    async def run(self, identity: str) -> None:
        lease = await self.children.acquire(identity)
        if lease is None:
            return
        pulse: asyncio.Task[None] | None = None
        memory = None
        token = None
        control = None
        bindings = ExitStack()
        try:
            row = await self.repository.get(identity)
            assert row is not None
            source = json.loads(row["source_json"])
            recovered = await recover_source(
                self.app.database, row["conversation_id"], source, request_id=identity
            )
            from qq_ai_bot.runtime.observability import RuntimeTurnCorrelation, bind_runtime_turn

            bindings.enter_context(
                bind_runtime_turn(
                    RuntimeTurnCorrelation(
                        turn_id=f"worker:{identity}",
                        origin=TurnOrigin(recovered.origin),
                    )
                )
            )
            original = await self.app.ledger.get_event(recovered.event_id)
            if original is None:
                raise WorkConflict("worker_source_deleted")
            child = await self.children.related(source["parent_work_id"], identity)

            async def validate() -> None:
                if not await self.repository.valid(lease):
                    raise WorkConflict("worker_lease_obsolete")
                if (
                    await recover_source(
                        self.app.database, row["conversation_id"], source, request_id=identity
                    )
                    != recovered
                ):
                    raise WorkConflict("worker_authority_changed")
                parent = await self.repository.get(source["parent_work_id"])
                if parent is None or parent["state"] in {"completed", "failed", "cancelled"}:
                    raise WorkConflict("worker_parent_obsolete")

            async def command(run_id: str) -> dict[str, Any] | None:
                record = await self.app.sandbox_tasks.by_run(run_id)
                if record is None or json.loads(record.source_json).get("work_id") != identity:
                    return None
                return {"run_id": run_id, "pending": record.status != "completed"}

            control = WorkControl(
                self.repository, lease, row["source_key"], source, validate, resolve_child=command
            )
            control.current = row
            token = current_work_control.set(control)
            activation = asyncio.current_task()

            async def heartbeat() -> None:
                while True:
                    await asyncio.sleep(15)
                    if not await self.repository.renew(lease):
                        if activation:
                            activation.cancel()
                        return
                    await control.meter_active_time()

            pulse = asyncio.create_task(heartbeat())
            config = await self.app.runtime_config.snapshot(
                user_id=recovered.actor_user_id, group_id=original.group_id
            )
            inbound = InboundMessage(
                message_id=original.platform_message_id,
                event_type="message",
                scope_type=original.scope_type,
                sender=SenderIdentity(recovered.actor_user_id),
                text=original.content,
                bot_user_id=recovered.bot_user_id,
                group_id=original.group_id,
                received_at=original.occurred_at,
                person_id=recovered.actor_person_id,
                space_id=recovered.target_space_id,
                conversation_id=recovered.conversation_id,
                presence_id=recovered.presence_id,
            )
            from qq_ai_bot.memory.runtime.resolver import MemoryStructuredCommand
            from qq_ai_bot.services.chat import _ChatAgentBackend

            memory = self.app.chat._open_memory_session(
                inbound,
                inbound.scope(),
                row["goal"],
                config,
                autonomous=recovered.origin == "autonomous_group",
                visual_input_present=False,
                structured_command=MemoryStructuredCommand.NONE,
            )
            tool_runtime = ToolRuntime(
                inbound=inbound,
                gateway=None,
                allow_generic_onebot=False,
                actor_user_id=recovered.actor_user_id,
                current_group_id=original.group_id,
                conversation_key=f"worker:{identity}",
                trigger_message_id=original.platform_message_id,
                trigger_event_id=original.id,
                runtime_config=config,
                origin=TurnOrigin(recovered.origin),
                execution_id=identity,
                sandbox_source={**source, "work_id": identity},
                memory_session=memory,
                conversation_id=recovered.conversation_id,
                presence_id=recovered.presence_id,
                person_id=recovered.actor_person_id,
                space_id=recovered.target_space_id,
            )
            runner = self.app.chat._agent_runner
            if self.definitions is None:
                self.definitions = tuple(
                    t for t in await runner.main_contract.definitions() if t.name in WORKER_NAMES
                )
            names = frozenset(t.name for t in self.definitions)
            if names != WORKER_NAMES:
                raise ValueError("incomplete_worker_tool_manifest")
            backend = WorkerBackend(_ChatAgentBackend(self.app.chat, tool_runtime), names)
            now = datetime.now(UTC)
            brief = json.loads(child["brief_json"])
            brief.update(child_id=identity, cwd=f"/workspace/tasks/{identity}", work_id=identity)
            result = await runner.run(
                (
                    ChatMessage(role="system", content=WORKER_PROMPT),
                    ChatMessage(role="user", content=json.dumps(brief, ensure_ascii=False)),
                ),
                AgentRuntime(
                    origin=TurnOrigin(recovered.origin),
                    actor_user_id=recovered.actor_user_id,
                    actor_is_superuser=False,
                    delegated_authority=None,
                    conversation_key=f"worker:{identity}",
                    current_group_id=original.group_id,
                    bot_user_id=recovered.bot_user_id,
                    gateway=None,
                    runtime_config=config,
                    current_time=TimeContext(now, now, "UTC"),
                    allowed_capabilities=self.app.chat._prefix_web_capabilities(config),
                    max_tool_calls=32,
                    max_model_requests=24,
                    before_model_request=validate,
                    canonical_conversation_id=recovered.conversation_id,
                    dynamic_context_prepared=True,
                    work_control=control,
                    execution_id=identity,
                    fixed_tools=self.definitions,
                    context_token_limit=self.app.settings.subagent_context_token_limit,
                ),
                backend,
            )
            await control.settle(delivered=True, pending_inputs=bool(await control.pending()))
            await self.children.finish(lease, result.text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if await self._retry_busy(lease, exc):
                return
            if await self.repository.valid(lease):
                current = await self.repository.get(identity)
                if current and current["state"] not in {"completed", "failed", "cancelled"}:
                    await self.repository.transition(
                        lease, identity, current["revision"], "suspended", reason=type(exc).__name__
                    )
                    await self.children.finish(
                        lease, "工作暂停，已保留执行记录。错误类别：" + type(exc).__name__
                    )
            logger.warning("subagent_run_failed category=%s", type(exc).__name__)
        finally:
            if pulse:
                pulse.cancel()
                await asyncio.gather(pulse, return_exceptions=True)
            if memory:
                await memory.close()
            if token is not None:
                current_work_control.reset(token)
            bindings.close()
            await self.repository.release(lease)

    async def _retry_busy(self, lease: WorkLease, exc: BaseException) -> bool:
        """Requeue the original journal, never resubmit commands or reset budgets."""
        if not _sqlite_busy(exc) or lease.work_id is None:
            return False
        for delay in (0.25, 0.75, 1.5):
            await asyncio.sleep(delay)
            try:
                if not await self.repository.valid(lease):
                    return True
                row = await self.repository.get(lease.work_id)
                if row is None or row["state"] in {"completed", "failed", "cancelled"}:
                    return True
                checkpoint = json.loads(row["checkpoint_json"])
                retries = int(checkpoint.get("sqlite_busy_retries", 0))
                if retries >= 3:
                    return False
                checkpoint["sqlite_busy_retries"] = retries + 1
                await self.repository.checkpoint(lease, row["id"], checkpoint)
                await self.repository.transition(
                    lease, row["id"], row["revision"], "queued", reason="sqlite_busy_retry"
                )
                logger.info("subagent_requeued reason=sqlite_busy retry=%s", retries + 1)
                return True
            except OperationalError as retry_exc:
                if not _sqlite_busy(retry_exc):
                    raise
            except WorkConflict:
                return True
        return False
