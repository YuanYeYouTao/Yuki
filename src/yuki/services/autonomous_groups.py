"""Debounced group observation scored locally, then one Main Agent turn."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from sqlalchemy.exc import SQLAlchemyError

from yuki.admin.config_service import RuntimeConfigService
from yuki.admin.models import ConversationRuntimeConfig, RuntimeConfigSnapshot
from yuki.conversation.features import AdmissionFeatureBuilder
from yuki.conversation.participation import (
    AdmissionSignalHint,
    LocalAutonomousParticipationPolicy,
)
from yuki.conversation.scope import runtime_conversation_key
from yuki.domain.messages import InboundMessage
from yuki.llm.base import LLMError
from yuki.plugin_host.admission_adapter import PluginAdmissionSignalAdapter
from yuki.runtime.observability import (
    RuntimeTurnCorrelation,
    bind_runtime_turn,
    new_runtime_turn_id,
)
from yuki.runtime.origin import TurnOrigin
from yuki.services.chat import ChatService
from yuki.services.plugin_events import content_free_turn_payload, publish_notification
from yuki.services.turn_coordinator import (
    ConversationTurnCoordinator,
    TurnInterruptedError,
    TurnSupersededError,
)
from yuki_plugin_sdk.events import EventName

if TYPE_CHECKING:
    from yuki.services.semantic_participation import SemanticParticipationService

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _GroupState:
    # Admission uses the latest observation; durable history already lives in the ledger.
    message: InboundMessage
    # Host-owned ordinary admission for the latest implicit human source.
    # None keeps the original local-scoring path. It returns sent messages.
    ordinary_attempt: Callable[[], Awaitable[int]] | None = None
    revision: int = 0
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None


class AutonomousGroupService:
    """Debounce one group; the latest source gets one ordinary or legacy attempt."""

    def __init__(
        self,
        *,
        chat: ChatService,
        admission_features: AdmissionFeatureBuilder,
        runtime_config: RuntimeConfigService | None = None,
        turn_coordinator: ConversationTurnCoordinator | None = None,
        admission_signals: PluginAdmissionSignalAdapter | None = None,
        participation: SemanticParticipationService | None = None,
    ) -> None:
        self._participation = participation
        self._chat = chat
        self._runtime_config = runtime_config or chat._runtime_config
        self._admission_features = admission_features
        self._coordinator = turn_coordinator or chat._turn_coordinator
        self._admission_signals = admission_signals
        self._states: dict[str, _GroupState] = {}
        self._closed = False

    def observe(
        self,
        message: InboundMessage,
        *,
        ordinary_attempt: Callable[[], Awaitable[int]] | None = None,
    ) -> None:
        group_id = message.group_id
        if self._closed or group_id is None:
            return
        scope_key = runtime_conversation_key(identity=message.scope(), inbound=message)
        state = self._states.get(scope_key)
        if state is None:
            state = _GroupState(message=message, ordinary_attempt=ordinary_attempt)
            self._states[scope_key] = state
        else:
            # Classification may finish out of order. An older source
            # must neither replace the newer slot nor extend its quiet window.
            latest = state.message.source_event_id
            source = message.source_event_id
            if latest is not None and source is not None and source < latest:
                return
            if (
                source is not None
                and source == latest
                and ordinary_attempt is not None
                and state.ordinary_attempt is not None
            ):
                state.message = message
                state.ordinary_attempt = ordinary_attempt
                self._ensure_task(scope_key, state)
                return
            if ordinary_attempt is not None or state.ordinary_attempt is None:
                state.message = message
                state.ordinary_attempt = ordinary_attempt
        state.revision += 1
        state.changed.set()
        self._ensure_task(scope_key, state)

    def is_current_ordinary_input(self, message: InboundMessage) -> bool:
        scope_key = runtime_conversation_key(identity=message.scope(), inbound=message)
        state = self._states.get(scope_key)
        return (
            state is not None
            and state.ordinary_attempt is not None
            and state.message.source_event_id == message.source_event_id
            and not state.changed.is_set()
        )

    def _ensure_task(self, scope_key: str, state: _GroupState) -> None:
        """Keep exactly one coalescing worker alive for one group."""

        if self._closed:
            return
        if state.task is not None and not state.task.done():
            return
        task = asyncio.create_task(
            self._after_silence(scope_key),
            name="conversation-scope-autonomous",
        )
        state.task = task

        def task_done(completed: asyncio.Task[None]) -> None:
            self._task_done(scope_key, completed)

        task.add_done_callback(task_done)

    def _task_done(self, scope_key: str, completed: asyncio.Task[None]) -> None:
        """Own a detached task, consume its outcome, and release its state reference."""

        try:
            completed.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.exception(
                "autonomous_group_task_failed exception_category=%s",
                type(exc).__name__,
            )
        state = self._states.get(scope_key)
        if state is None or state.task is not completed:
            return
        state.task = None
        # A message can arrive between the worker's final revision check and this
        # callback. Its event is the hand-off that prevents that update being lost.
        if state.changed.is_set() and not self._closed:
            self._ensure_task(scope_key, state)
        else:
            self._states.pop(scope_key, None)

    async def _after_silence(self, scope_key: str) -> None:
        while True:
            revision = -1
            try:
                state = self._states.get(scope_key)
                if state is None:
                    return
                # Consume this update before the first await, including disabled/error
                # exits. Only a newer observation may request another worker.
                revision = state.revision
                state.changed.clear()
                group_id = state.message.group_id
                if group_id is None:
                    return
                ordinary = state.ordinary_attempt is not None
                runtime = await self._runtime_config.snapshot(group_id=state.message.space_id)
                policy = _conversation_policy(runtime)
                # Ordinary admission is Host policy; only legacy scoring needs autonomy.
                if not ordinary and not policy.autonomous_enabled:
                    return
                # One worker absorbs every update until the group has remained
                # quiet for a full debounce interval.
                try:
                    await asyncio.wait_for(
                        state.changed.wait(),
                        timeout=policy.autonomous_debounce_seconds,
                    )
                except TimeoutError:
                    pass
                if not self._is_latest(scope_key, revision):
                    continue
                await self._run_latest(scope_key, revision, runtime)
            except asyncio.CancelledError:
                raise
            except (
                TurnInterruptedError,
                TurnSupersededError,
            ):
                pass
            except SQLAlchemyError as exc:
                logger.warning(
                    "autonomous_group_task_failed exception_category=%s",
                    type(exc).__name__,
                )
            except (LLMError, OSError, RuntimeError, ValueError, TypeError) as exc:
                logger.warning(
                    "autonomous_group_task_failed exception_category=%s",
                    type(exc).__name__,
                )
            if revision >= 0 and not self._is_latest(scope_key, revision):
                continue
            return

    def _is_latest(self, scope_key: str, revision: int) -> bool:
        state = self._states.get(scope_key)
        return state is not None and state.revision == revision

    async def _run_latest(
        self,
        scope_key: str,
        revision: int,
        runtime: RuntimeConfigSnapshot,
    ) -> None:
        """Bind one fresh runtime turn correlation per autonomous attempt."""

        state = self._states.get(scope_key)
        ordinary = state is not None and state.ordinary_attempt is not None
        correlation = RuntimeTurnCorrelation(
            turn_id=new_runtime_turn_id(),
            origin=TurnOrigin.USER_MESSAGE if ordinary else TurnOrigin.AUTONOMOUS_GROUP,
        )
        with bind_runtime_turn(correlation):
            await self._admit_latest(scope_key, revision, runtime)

    async def _admit_latest(
        self,
        scope_key: str,
        revision: int,
        runtime: RuntimeConfigSnapshot,
    ) -> int:
        state = self._states.get(scope_key)
        if state is None:
            return 0
        attempt = state.ordinary_attempt
        if attempt is not None:
            # Frozen before the first await; a newer slot gets its own window.
            return await attempt()
        last = state.message
        if self._participation is None or not await self._participation.legacy_allowed(last):
            return 0
        plugin_signals = (
            await self._admission_signals.collect(
                message=last,
                origin=TurnOrigin.AUTONOMOUS_GROUP,
                runtime=runtime,
            )
            if self._admission_signals is not None
            else ()
        )
        policy = _conversation_policy(runtime)
        features = await self._admission_features.admission_features(
            inbound=last,
            content=last.text,
            runtime=runtime,
            plugin_signals=cast(tuple[AdmissionSignalHint, ...], plugin_signals),
        )
        snapshot = LocalAutonomousParticipationPolicy(
            threshold=policy.autonomous_admission_threshold,
            bot_aliases=self._chat._settings.character_identity.aliases,
        ).evaluate(features)
        conversation_key = scope_key
        publisher = getattr(self._chat, "_event_publisher", None)
        if not snapshot.should_participate:
            await publish_notification(
                publisher,
                EventName.AUTONOMOUS_DECLINED,
                content_free_turn_payload(
                    origin=TurnOrigin.AUTONOMOUS_GROUP.value,
                    scope_type="group",
                    conversation_key=conversation_key,
                    score=snapshot.score,
                    threshold=snapshot.threshold,
                    reasons=list(snapshot.reasons),
                ),
            )
            return 0
        if not self._is_latest(scope_key, revision):
            return 0
        await self._participation.accept_legacy(last)
        return 0

    def observe_context(self, message: InboundMessage, *, direct: bool) -> None:
        if self._participation is not None:
            self._participation.observe_context(message, direct=direct)

    async def close(self) -> None:
        self._closed = True
        tasks = [
            state.task
            for state in self._states.values()
            if state.task is not None and not state.task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._states.clear()


def _conversation_policy(runtime: object) -> ConversationRuntimeConfig:
    getter = getattr(runtime, "conversation_policy", None)
    if callable(getter):
        policy = getter()
        if isinstance(policy, ConversationRuntimeConfig):
            return policy
    conversation = getattr(runtime, "conversation", None)
    if isinstance(conversation, ConversationRuntimeConfig):
        return conversation
    raise TypeError("runtime snapshot is missing conversation policy")
