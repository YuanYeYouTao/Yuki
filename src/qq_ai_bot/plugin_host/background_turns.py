"""Persistent main-conversation turns triggered by plugin external events."""

from __future__ import annotations

import asyncio
import logging
import time

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.conversation.rollup.errors import ConversationCoverageError
from qq_ai_bot.conversation.rollup.repository import ConversationScopeRepository
from qq_ai_bot.conversation.scope import ConversationTurnSnapshot
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.identity.routing import PresenceRouter, RouteSendError
from qq_ai_bot.persistence.event_repository import EventLedgerRepository
from qq_ai_bot.plugin_host.notification_repository import (
    BackgroundTurnFenceError,
    BackgroundTurnJobRecord,
    PluginNotificationRepository,
    queued_work_error_category,
)
from qq_ai_bot.plugin_host.ownership import PluginOwnershipError
from qq_ai_bot.runtime.observability import (
    RuntimeTurnCorrelation,
    TurnObservationRecorder,
    bind_runtime_turn,
    build_turn_observation,
    new_runtime_turn_id,
    record_observation_safely,
)
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.runtime.trigger import ExternalEventTurnTrigger
from qq_ai_bot.services.chat import ChatService
from qq_ai_bot.services.turn_coordinator import (
    ConversationTurnCoordinator,
    TurnInterruptedError,
    TurnSupersededError,
)

logger = logging.getLogger(__name__)


def _authoritative_plugin_observation_refs(
    job: BackgroundTurnJobRecord,
) -> tuple[str | None, str | None, str | None]:
    """Project persisted job canonicals. Never derive from raw QQ/group keys."""

    conversation_id = job.canonical_conversation_id or None
    if job.target_type == "private":
        return conversation_id, job.canonical_target_person_id or None, None
    if job.target_type == "group":
        return conversation_id, None, job.canonical_target_space_id or None
    return conversation_id, None, None


class PluginBackgroundTurnWorker:
    """Wake the normal Main Agent from a durable plugin event job."""

    def __init__(
        self,
        *,
        repository: PluginNotificationRepository,
        ledger: EventLedgerRepository,
        runtime_config: RuntimeConfigService,
        chat: ChatService,
        turns: ConversationTurnCoordinator,
        conversation_scopes: ConversationScopeRepository,
        turn_observations: TurnObservationRecorder | None = None,
        router: PresenceRouter | None = None,
    ) -> None:
        self._repository = repository
        self._ledger = ledger
        self._runtime_config = runtime_config
        self._chat = chat
        self._turns = turns
        self._conversation_scopes = conversation_scopes
        self._turn_observations = turn_observations
        self._router = router
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(
                self._run(),
                name="plugin-background-turns",
            )

    async def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._task is not None:
            await self._task
            self._task = None

    def wake(self) -> None:
        self._wake.set()

    async def _run(self) -> None:
        while not self._stop.is_set():
            job = await self._repository.claim_turn()
            if job is None:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=1.0)
                except TimeoutError:
                    pass
                continue
            await self._execute(job)

    async def _execute(self, job: BackgroundTurnJobRecord) -> None:
        """Bind one fresh runtime turn correlation per background job attempt."""

        started = time.perf_counter()
        correlation = RuntimeTurnCorrelation(
            turn_id=new_runtime_turn_id(),
            origin=TurnOrigin.PLUGIN_BACKGROUND,
        )
        error_category: str | None = None
        conversation_key = f"canonical-conversation:{job.canonical_conversation_id}"
        resolved_key = [conversation_key]
        canonical_conversation_id: str | None = None
        canonical_person_id: str | None = None
        canonical_space_id: str | None = None
        with bind_runtime_turn(correlation):
            try:
                (
                    canonical_conversation_id,
                    canonical_person_id,
                    canonical_space_id,
                ) = _authoritative_plugin_observation_refs(job)
                await self._execute_admitted(job, resolved_key)
            except BaseException as exc:
                error_category = type(exc).__name__
                raise
            finally:
                if correlation.touched or error_category is not None:
                    observation = build_turn_observation(
                        correlation,
                        scope_type=job.target_type,
                        conversation_key=resolved_key[0],
                        admission_outcome="plugin_background",
                        handled=error_category is None,
                        sent_messages=0,
                        error_category=error_category,
                        total_latency_ms=int((time.perf_counter() - started) * 1000),
                        canonical_conversation_id=canonical_conversation_id,
                        canonical_person_id=canonical_person_id,
                        canonical_space_id=canonical_space_id,
                    )
                    await record_observation_safely(self._turn_observations, observation)

    async def _execute_admitted(
        self,
        job: BackgroundTurnJobRecord,
        resolved_key: list[str] | None = None,
    ) -> None:
        """Execute from persisted Conversation + current Presence. No raw QQ fallback."""

        try:
            context = await self._repository.load_background_context(job)
        except BackgroundTurnFenceError:
            return
        except PluginOwnershipError as exc:
            await self._repository.fail_turn(
                job.id,
                attempt=job.attempts,
                error_category=queued_work_error_category(exc, job),
            )
            return
        if context.generation != job.generation:
            try:
                await self._repository.validate_turn_attempt(
                    job.id,
                    attempt=job.attempts,
                    generation=job.generation,
                )
            except BackgroundTurnFenceError:
                return
        event = await self._ledger.get_event(job.source_event_id)
        if (
            event is None
            or event.event_kind != "external_event"
            or event.source_plugin_id != job.plugin_id
            or event.canonical_conversation_id != context.conversation_id
        ):
            await self._repository.fail_turn(
                job.id,
                attempt=job.attempts,
                error_category="source_event_invalid",
            )
            return
        if self._router is None:
            await self._repository.fail_turn(
                job.id,
                attempt=job.attempts,
                error_category="none",
            )
            return
        try:
            if context.person_id and context.space_id:
                raise RouteSendError("none")
            if context.person_id:
                resolved = await self._router.resolve_send_for_person(context.person_id)
            elif context.space_id:
                resolved = await self._router.resolve_send_for_space(context.space_id)
            else:
                raise RouteSendError("none")
        except RouteSendError as exc:
            await self._repository.fail_turn(
                job.id,
                attempt=job.attempts,
                error_category=exc.category,
            )
            return
        if job.target_type == "group":
            transport = ConversationScope.group(
                resolved.sender_account_id, resolved.external_target_id
            )
        else:
            transport = ConversationScope.private(
                resolved.sender_account_id, resolved.external_target_id
            )
        try:
            await self._repository.ensure_resolved_transport_alias(
                conversation_id=context.conversation_id,
                transport_key=transport.key,
            )
        except PluginOwnershipError as exc:
            await self._repository.fail_turn(
                job.id,
                attempt=job.attempts,
                error_category=queued_work_error_category(exc, job),
            )
            return
        conversation_key = context.primary_alias
        if resolved_key is not None:
            resolved_key[0] = conversation_key
        token = await self._turns.begin_background(conversation_key)
        if token is None:
            try:
                await self._repository.validate_turn_attempt(
                    job.id,
                    attempt=job.attempts,
                    generation=job.generation,
                )
            except BackgroundTurnFenceError:
                return
            await self._repository.defer_turn(
                job.id,
                attempt=job.attempts,
                error_category="conversation_busy",
                delay_seconds=3,
                preserve_budget=True,
            )
            return
        runtime = await self._runtime_config.snapshot(
            user_id=context.creator_person_id,
            group_id=context.space_id,
        )
        self._chat.configure_runtime_controls(runtime)
        self._turns.configure_policy(
            cancel_replies_on_new_message=runtime.reply.cancel_on_new_message,
            interrupt_autonomous_on_new_message=(
                runtime.conversation_policy().interrupt_autonomous_on_new_message
            ),
        )
        turn_snapshot = ConversationTurnSnapshot(
            scope_id=context.scope_id,
            scope_key=conversation_key,
            generation=job.generation,
            trigger_event_id=event.id,
            coordinator_version=token.version,
            transport_scope_key=transport.key,
        )
        try:
            async with self._turns.track(token, "generation"):
                result = await self._chat.generate_main_agent_wakeup(
                    event=event,
                    trigger=ExternalEventTurnTrigger(
                        plugin_id=job.plugin_id,
                        source_event_id=event.id,
                        target_type=job.target_type,
                        target_id=resolved.external_target_id,
                        agent_intent=job.agent_intent,
                    ),
                    identity=transport,
                    runtime=runtime,
                    turn_token=token,
                    turn_snapshot=turn_snapshot,
                    gateway=(
                        resolved.connection.bot
                        if callable(getattr(resolved.connection.bot, "call_api", None))
                        else None
                    ),
                    person_id=context.person_id,
                    space_id=context.space_id,
                    presence_id=resolved.presence_id,
                    conversation_id=context.conversation_id,
                    before_model_request=lambda: self._repository.validate_turn_attempt(
                        job.id,
                        attempt=job.attempts,
                        generation=job.generation,
                    ),
                )
            completed = await self._repository.finish_turn(
                job.id,
                attempt=job.attempts,
                generation=job.generation,
                text=result.text,
                tool_calls_used=result.tool_calls_used,
                model_requests=result.model_requests,
            )
            if not completed:
                return
            logger.info(
                "plugin_background_turn_completed plugin_id=%s event_id=%d "
                "reply=%s model_requests=%d",
                job.plugin_id,
                event.id,
                bool(result.text),
                result.model_requests,
            )
        except (
            TurnInterruptedError,
            TurnSupersededError,
        ):
            try:
                await self._repository.validate_turn_attempt(
                    job.id,
                    attempt=job.attempts,
                    generation=job.generation,
                )
            except BackgroundTurnFenceError:
                return
            if job.attempts >= 2:
                await self._repository.abandon_turn(
                    job.id,
                    attempt=job.attempts,
                    error_category="interrupted_twice",
                )
            else:
                await self._repository.defer_turn(
                    job.id,
                    attempt=job.attempts,
                    error_category="interrupted_by_user",
                    delay_seconds=5,
                )
        except BackgroundTurnFenceError:
            return
        except ConversationCoverageError as exc:
            try:
                await self._repository.validate_turn_attempt(
                    job.id,
                    attempt=job.attempts,
                    generation=job.generation,
                )
            except BackgroundTurnFenceError:
                return
            await self._repository.fail_turn(
                job.id,
                attempt=job.attempts,
                error_category=type(exc).__name__,
            )
        except asyncio.CancelledError:
            await self._repository.defer_turn(
                job.id,
                attempt=job.attempts,
                error_category="worker_stopped",
                delay_seconds=5,
                preserve_budget=True,
            )
            raise
        except Exception as exc:
            logger.exception(
                "plugin_background_turn_failed plugin_id=%s event_id=%d error_category=%s",
                job.plugin_id,
                job.source_event_id,
                type(exc).__name__,
            )
            await self._repository.fail_turn(
                job.id,
                attempt=job.attempts,
                error_category=type(exc).__name__,
            )
