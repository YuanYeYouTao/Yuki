"""Time scanning activates the ordinary Main Agent for each unique occurrence."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.automation.context import AutomationBindError, bind_automation_conversation
from qq_ai_bot.automation.control_context import resolve_execution_identity
from qq_ai_bot.automation.gateway import OneBotAutomationGateway
from qq_ai_bot.automation.models import AutomationRecord, RunStatus
from qq_ai_bot.automation.repository import AutomationRepository
from qq_ai_bot.automation.task_spec import TaskSpec
from qq_ai_bot.config import Settings
from qq_ai_bot.conversation.rollup.repository import ConversationScopeRepository
from qq_ai_bot.conversation.scope import ConversationTurnSnapshot
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.identity.routing import PresenceRouter
from qq_ai_bot.runtime.activation_bindings import ActiveWorkBindings
from qq_ai_bot.runtime.lease_heartbeat import supervise_lease
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.runtime.work_activation import bind_work_activation
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.services.agent_tools import ToolRuntime
from qq_ai_bot.services.execution_sources import recover_automation_source
from qq_ai_bot.services.turn_coordinator import ConversationTurnCoordinator
from qq_ai_bot.time.service import TimeContextService

logger = logging.getLogger(__name__)


class AutomationWorker:
    def __init__(
        self,
        *,
        settings: Settings,
        repository: AutomationRepository,
        time_service: TimeContextService,
        generate: Callable[..., Awaitable[Any]],
        runtime_config: RuntimeConfigService,
        conversation_scopes: ConversationScopeRepository,
        turns: ConversationTurnCoordinator,
        router: PresenceRouter,
        work_repository: WorkRepository,
        bindings: ActiveWorkBindings,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._time = time_service
        self._generate = generate
        self._config = runtime_config
        self._scopes = conversation_scopes
        self._turns = turns
        self._router = router
        self._works = work_repository
        self._bindings = bindings
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._running: set[asyncio.Task[None]] = set()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        # A previous process's non-Work occurrence has no recoverable Main journal.
        # Record its unknown result once; never repurchase that model/tool execution.
        await self._repository.settle_interrupted_occurrences()
        await self._repository.settle_work_occurrences()
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="automation-worker")

    async def close(self) -> None:
        self._stop.set()
        if self._task is not None:
            task, self._task = self._task, None
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for task in self._running:
            task.cancel()
        if self._running:
            await asyncio.gather(*self._running, return_exceptions=True)

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                rows = await self._repository.claim_due(
                    worker_id=uuid.uuid4().hex,
                    now=self._time.clock.now(),
                    lease_seconds=self._settings.automation_lease_seconds,
                    enabled=self._settings.automation_enabled,
                    clock=self._time.clock.now,
                )
                for row in rows:
                    task = asyncio.create_task(
                        self._process_guarded(row), name=f"automation-{row.id}"
                    )
                    self._running.add(task)
                    task.add_done_callback(self._running.discard)
            except Exception:
                logger.exception("automation_poll_failed")
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._settings.automation_poll_seconds
                )
            except TimeoutError:
                continue

    async def _process_guarded(self, automation: AutomationRecord) -> None:
        worker_id = automation.claimed_by
        assert worker_id is not None

        async def renew() -> bool:
            return await self._repository.renew_claim(
                automation.id,
                worker_id,
                self._time.clock.now() + timedelta(seconds=self._settings.automation_lease_seconds),
            )

        try:
            async with supervise_lease(
                renew,
                lambda: self._repository.claim_expiry(automation.id, worker_id),
                seconds=self._settings.automation_lease_seconds,
                interval=max(1, self._settings.automation_lease_seconds / 3),
                clock=lambda: self._time.clock.now().timestamp(),
            ):
                await self._process(automation)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("automation_process_failed automation_id=%d", automation.id)
        finally:
            await self._repository.release_claim(automation.id, worker_id=worker_id)

    async def _process(self, automation: AutomationRecord) -> None:
        try:
            async with self._repository._database.sessions() as session:
                key, conversation_id = await bind_automation_conversation(session, automation)
                actor_user_id = ""
                if automation.creator_kind != "self":
                    assert automation.canonical_creator_person_id is not None
                    actor_user_id, _ = await resolve_execution_identity(
                        session, self._settings, owner_id=automation.canonical_creator_person_id
                    )
            group_id = (
                automation.authority_snapshot["current_group_id"]
                if automation.canonical_target_space_id
                else None
            )
            identity = (
                ConversationScope.group(automation.bot_user_id, group_id)
                if group_id
                else ConversationScope.private(automation.bot_user_id, actor_user_id)
            )
            state = await self._scopes.get(identity)
            if state is None or state.id != conversation_id:
                raise ValueError("automation_conversation_changed")
        except (AutomationBindError, PermissionError, ValueError) as exc:
            assert automation.next_run_at is not None
            run = await self._repository.create_run(
                automation.id,
                scheduled_for=automation.next_run_at,
                actual_started_at=self._time.clock.now(),
                worker_id=automation.claimed_by,
                version_id=automation.version_id,
            )
            if run is not None:
                category = exc.category if isinstance(exc, AutomationBindError) else str(exc)
                await self._repository.finish_run(
                    run.id,
                    status=RunStatus.BLOCKED,
                    steps_completed=0,
                    llm_calls=0,
                    tool_calls=0,
                    messages_sent=0,
                    error_category=category,
                    summary={"error": category},
                    finished_at=self._time.clock.now(),
                    worker_id=automation.claimed_by,
                )
            return
        async with self._turns.background_turn(
            key, origin=TurnOrigin.SCHEDULED_AUTOMATION
        ) as token:
            if token is None:
                return
            lease = await self._works.acquire(state.id, state.generation)
            if lease is None:
                return
            run = None
            activation = None
            try:
                assert automation.next_run_at is not None
                run = await self._repository.create_run(
                    automation.id,
                    scheduled_for=automation.next_run_at,
                    actual_started_at=self._time.clock.now(),
                    worker_id=automation.claimed_by,
                    version_id=automation.version_id,
                )
                if run is None:
                    return
                original = await self._repository.get_for_run(run.id)
                if original is None:
                    raise ValueError("missing_original_run_version")
                instruction = TaskSpec.model_validate(original.script).instruction()
                execution_id = f"automation:{run.id}:{original.script_hash}"
                source = {
                    "owner": "automation",
                    "origin": TurnOrigin.SCHEDULED_AUTOMATION.value,
                    "automation_id": automation.id,
                    "automation_run_id": run.id,
                    "script_hash": original.script_hash,
                    "parent_execution_id": execution_id,
                    "conversation_id": state.id,
                    "generation": state.generation,
                    "principal_kind": original.creator_kind,
                    "actor_person_id": original.canonical_creator_person_id,
                    "actor_user_id": actor_user_id,
                    "presence_id": original.canonical_presence_id,
                    "space_id": original.canonical_target_space_id,
                    "bot_user_id": original.bot_user_id,
                    "current_group_id": group_id,
                    "instruction": instruction,
                }
                recovered = await recover_automation_source(
                    self._repository._database,
                    state.id,
                    source,
                    request_id=execution_id,
                    settings=self._settings,
                )
                resolved = await self._router.resolve_presence(recovered.presence_id)

                async def validate() -> None:
                    fresh = await recover_automation_source(
                        self._repository._database,
                        state.id,
                        source,
                        request_id=execution_id,
                        settings=self._settings,
                    )
                    if fresh != recovered:
                        raise ValueError("automation_source_changed")
                    current = await self._router.resolve_presence(recovered.presence_id)
                    if current.connection.snapshot != resolved.connection.snapshot:
                        raise ValueError("automation_connection_changed")

                snapshot = ConversationTurnSnapshot(
                    state.id,
                    key,
                    state.generation,
                    None,
                    token.version,
                    identity.key,
                    automation_run_id=run.id,
                )
                runtime = await self._config.snapshot(
                    user_id=recovered.actor_user_id or None,
                    group_id=group_id,
                )
                control = WorkControl(self._works, lease, execution_id, source, validate)
                activation = bind_work_activation(control, bindings=self._bindings, scope_key=key)
                async with activation:
                    await validate()
                    result = await self._generate(
                        content=instruction,
                        identity=identity,
                        runtime=runtime,
                        turn_token=token,
                        turn_snapshot=snapshot,
                        before_model_request=validate,
                        source_runtime=ToolRuntime(
                            inbound=None,
                            actor_context=recovered.actor(execution_id),
                            gateway=OneBotAutomationGateway(
                                bot_user_id=recovered.bot_user_id,
                                automation_id=automation.id,
                                automation_run_id=run.id,
                                router=self._router,
                            ),
                            actor_is_superuser=recovered.actor_is_superuser,
                            allow_generic_onebot=recovered.actor_is_superuser,
                            conversation_key=key,
                            execution_id=execution_id,
                            origin=TurnOrigin.SCHEDULED_AUTOMATION,
                            scope_type=identity.scope_type,
                            conversation_id=state.id,
                            person_id=recovered.actor_person_id,
                            space_id=recovered.target_space_id,
                            external_target_id=recovered.external_target_id,
                            sandbox_source=source,
                        ),
                    )
                if control.current is not None:
                    await self._repository.settle_work_occurrences()
                else:
                    failure = result.outcome.failure if result.outcome is not None else None
                    await self._repository.finish_run(
                        run.id,
                        status=RunStatus.FAILED if failure else RunStatus.SUCCEEDED,
                        steps_completed=0,
                        llm_calls=result.model_requests,
                        tool_calls=result.tool_calls_used,
                        messages_sent=result.messages_sent,
                        error_category=failure.code if failure else None,
                        summary={"result": result.text},
                        finished_at=self._time.clock.now(),
                    )
            except BaseException as exc:
                if run is not None:
                    # Existing Work owns its actual recovery; no outer retry or cursor.
                    await self._repository.settle_work_occurrences()
                    await self._repository.settle_interrupted_occurrences(
                        run_id=run.id, error_category=type(exc).__name__
                    )
                raise
            finally:
                if activation is None:
                    await self._works.release(lease)
