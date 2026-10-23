"""Canonical Conversation history and temporary activation input for Main."""

from __future__ import annotations

import json
from collections.abc import Sequence, Set
from dataclasses import dataclass, replace
from datetime import UTC
from typing import TYPE_CHECKING, Any

from yuki.admin.models import RuntimeConfigSnapshot
from yuki.config import Settings
from yuki.conversation.frozen_fragments import FrozenFragments
from yuki.conversation.scope import (
    ConversationTurnSnapshot,
    turn_matches_hydrated_scope,
)
from yuki.conversation.scope_models import ConversationCoverageError, ConversationPromptSnapshot
from yuki.conversation.scope_repository import ConversationScopeRepository
from yuki.domain.conversations import ConversationScope, ScopeType
from yuki.domain.messages import ChatMessage, InboundMessage
from yuki.domain.profiles import UserProfileSnapshot
from yuki.event_prompt import ChatEventPromptRenderer
from yuki.persistence.event_repository import ConversationReadVersion
from yuki.persistence.repositories import (
    EventLedgerRepository,
    EventRecord,
)
from yuki.runtime.trigger import (
    ExternalEventTurnTrigger,
    SandboxTaskTurnTrigger,
    WorkResumeTrigger,
)

if TYPE_CHECKING:
    from yuki.conversation.projections import ProjectionSnapshot
    from yuki.conversation.prompt_history import PromptHistorySource
    from yuki.event_prompt import EventPromptReferences


@dataclass(frozen=True, slots=True)
class ContextMetrics:
    """Non-sensitive size diagnostics for one assembled context."""

    metadata_characters: int
    history_characters: int
    history_messages: int
    current_message_characters: int
    raw_history_window_shifted: bool
    checkpoint_characters: int = 0
    checkpoint_kind: str | None = None
    covered_to: int | None = None


@dataclass(frozen=True, slots=True)
class AssembledContext:
    """Trusted dynamic context and bounded chat history for one model request."""

    metadata_payload: dict[str, Any]
    history_messages: Sequence[ChatMessage]
    current_message: ChatMessage
    metrics: ContextMetrics
    visible_event_ids: Set[int] = frozenset()
    history_anchor_event_id: int | None = None
    checkpoint_text: str = ""
    prompt_effective_coverage: int = 0
    prompt_raw_tail_end_event_id: int = 0
    read_version: ConversationReadVersion | None = None
    history_fragments: tuple[tuple[tuple[int, ...], ChatMessage], ...] = ()
    history_event_fragments: tuple[tuple[tuple[int, ...], ChatMessage], ...] = ()
    current_event_id: int | None = None
    projection_scope: str = ""
    history_timezone: str = "Asia/Shanghai"
    projection_snapshot: ProjectionSnapshot | None = None
    history_incremental: bool = False
    prepared_fragments: FrozenFragments | None = None


@dataclass(frozen=True, slots=True)
class _HistoryPromptWindow:
    """One consistent public checkpoint plus its continuous uncovered suffix."""

    recent: tuple[EventRecord, ...]
    checkpoint_text: str
    coverage_end: int
    revision: int
    checkpoint_kind: str | None
    starts_after_event_id: int = 0
    read_version: ConversationReadVersion | None = None
    projection: ProjectionSnapshot | None = None
    source: PromptHistorySource | None = None
    references: EventPromptReferences | None = None
    current_event: EventRecord | None = None
    raw_tail_end_event_id: int = 0
    frozen_prefix: FrozenFragments | None = None


class ContextAssembler:
    """Load and bound the Conversation's history and current input."""

    def __init__(
        self,
        *,
        settings: Settings,
        ledger: EventLedgerRepository,
        conversation_scopes: ConversationScopeRepository,
    ) -> None:
        self._settings = settings
        self._ledger = ledger
        self._scopes = conversation_scopes

    async def _assemble_activation(
        self,
        *,
        identity: ConversationScope,
        turn: ConversationTurnSnapshot,
        content: str,
    ) -> AssembledContext:
        """Prepare the common history for a real execution without a chat event."""
        snapshot = await self._load_history_snapshot(identity, turn=turn, before_event_id=None)
        context = self._prepared_context(
            snapshot,
            identity=identity,
            content=content,
            metadata={
                "scene": "群聊" if identity.scope_type is ScopeType.GROUP else "私聊",
                "platform": identity.platform,
            },
            current_override=ChatMessage("user", content),
            projection_scope="main",
        )
        if snapshot.read_version is not None and not await self._ledger.read_version_matches(
            snapshot.read_version
        ):
            from yuki.services.turn_coordinator import HistorySourceChangedError

            raise HistorySourceChangedError(snapshot.read_version)
        return context

    def _prepared_context(
        self,
        snapshot: _HistoryPromptWindow,
        *,
        identity: ConversationScope,
        content: str,
        metadata: dict[str, Any],
        current_event: EventRecord | None = None,
        current_override: ChatMessage | None = None,
        projection_scope: str = "",
    ) -> AssembledContext:
        source, references = snapshot.source, snapshot.references
        assert source is not None and references is not None
        current_id = current_event.id if current_event is not None else None
        # The old bounded-history path includes this exact covered trigger as a
        # reference, not any of its older reply/mention targets. Actorless wakeups
        # keep their trigger private and do not enlarge the public reference table.
        if current_event is not None and current_event.id not in references.event_ids:
            references = references.with_events((current_event,))
        grouped, individual = source.render(
            snapshot.recent,
            references=references,
            through_event_id=references._through,
            current_event_id=current_id,
        )
        frozen = snapshot.frozen_prefix
        if frozen is None:
            frozen = FrozenFragments.load([])
        prepared = frozen.extend_history(grouped, individual)
        renderer = ChatEventPromptRenderer(
            references=references,
            timezone=self._settings.default_timezone,
        )
        current = current_override or (
            renderer.reference_message(
                current_event, current_event_id=current_id, current_content=content
            )
            if current_event is not None
            else ChatMessage("user", content)
        )
        visible = prepared.event_ids | ({current_id} if current_id is not None else set())
        return AssembledContext(
            metadata_payload=metadata,
            history_messages=prepared.messages_view(),
            current_message=current,
            metrics=ContextMetrics(
                len(json.dumps(metadata, ensure_ascii=False, separators=(",", ":"), default=str)),
                prepared.character_count,
                len(prepared),
                len(current.content or ""),
                False,
                len(snapshot.checkpoint_text),
                snapshot.checkpoint_kind,
                snapshot.coverage_end or None,
            ),
            visible_event_ids=visible,
            history_anchor_event_id=prepared.first_event_id or current_id,
            checkpoint_text=snapshot.checkpoint_text,
            prompt_effective_coverage=snapshot.coverage_end,
            prompt_raw_tail_end_event_id=snapshot.raw_tail_end_event_id,
            read_version=snapshot.read_version,
            history_fragments=grouped,
            history_event_fragments=individual,
            current_event_id=current_id,
            projection_scope=projection_scope,
            history_timezone=self._settings.default_timezone,
            projection_snapshot=snapshot.projection,
            history_incremental=True,
            prepared_fragments=prepared,
        )

    async def assemble_plugin(
        self,
        *,
        identity: ConversationScope,
        content: str,
        metadata: dict[str, Any],
        read_history: bool,
        runtime: RuntimeConfigSnapshot,
    ) -> AssembledContext:
        """Use the canonical checkpoint/raw-tail projection within the plugin read grant."""
        from yuki.runtime.work_activation import current_work_control

        control = current_work_control.get()
        recovery = control.protocol_recovery_preparation if control is not None else None
        scope = identity
        if recovery is not None:
            original = recovery.guard.version
            scope = replace(
                original.scope,
                conversation_id=original.conversation_id,
                key=f"conversation:{original.conversation_id}",
            )
        metadata = {**metadata, "platform": scope.platform}
        if read_history:
            loaded = await self._scopes.prepare_prompt_snapshot(
                scope,
                timezone=self._settings.default_timezone,
            )
            snapshot = self._history_window(scope, loaded)
            return self._prepared_context(
                snapshot,
                identity=identity,
                content=content,
                metadata=metadata,
                current_override=ChatMessage("user", content),
                projection_scope="main",
            )
        version, _ = await self._ledger.read_scope_context(scope, limit=0)
        return AssembledContext(
            metadata_payload=metadata,
            history_messages=(),
            current_message=ChatMessage("user", content),
            metrics=ContextMetrics(
                len(json.dumps(metadata, ensure_ascii=False)), 0, 0, len(content), False
            ),
            read_version=version,
            history_timezone=self._settings.default_timezone,
        )

    async def assemble(
        self,
        *,
        inbound: InboundMessage | None,
        identity: ConversationScope,
        profile: UserProfileSnapshot | None,
        turn: ConversationTurnSnapshot,
        content: str,
        runtime: RuntimeConfigSnapshot,
        external_event: EventRecord | None = None,
        external_trigger: ExternalEventTurnTrigger
        | SandboxTaskTurnTrigger
        | WorkResumeTrigger
        | None = None,
    ) -> AssembledContext:
        """Build one bounded snapshot without persisting model-only metadata."""

        if external_trigger is not None or external_event is not None:
            if inbound is not None or profile is not None:
                raise ConversationCoverageError("external wakeup must not invent a message actor")
            if external_trigger is None or external_event is None:
                raise ConversationCoverageError("external wakeup trigger is incomplete")
            return await self._assemble_actorless_turn(
                event=external_event,
                trigger=external_trigger,
                identity=identity,
                turn=turn,
                runtime=runtime,
            )
        if inbound is None and profile is None:
            return await self._assemble_activation(identity=identity, turn=turn, content=content)
        if inbound is None or profile is None:
            raise ConversationCoverageError("message turn requires a real inbound actor")
        if turn.trigger_event_id is None:
            raise ConversationCoverageError("message turn requires a real event anchor")

        snapshot = await self._load_history_snapshot(identity, turn=turn, before_event_id=None)
        current = snapshot.current_event
        if (
            current is None
            or current.canonical_conversation_id != turn.conversation_id
            or current.ingress_presence_id != inbound.presence_id
        ):
            raise ConversationCoverageError("turn trigger event does not match scope snapshot")
        context = self._prepared_context(
            snapshot,
            identity=identity,
            content=content,
            metadata={
                "scene": "群聊" if inbound.group_id else "私聊",
                "platform": inbound.platform,
            },
            current_event=current,
        )
        await self._validate_history_source(snapshot, current)
        return context

    async def _assemble_actorless_turn(
        self,
        *,
        event: EventRecord,
        trigger: ExternalEventTurnTrigger | SandboxTaskTurnTrigger | WorkResumeTrigger,
        identity: ConversationScope,
        turn: ConversationTurnSnapshot,
        runtime: RuntimeConfigSnapshot,
    ) -> AssembledContext:
        """Use common chat history while keeping the execution trigger temporary."""

        sandbox = isinstance(trigger, (SandboxTaskTurnTrigger, WorkResumeTrigger))
        if (
            event.id != trigger.source_event_id
            or event.canonical_conversation_id != turn.conversation_id
            or (sandbox and (event.direction != "inbound" or event.event_kind != "message"))
            or (
                not sandbox
                and (
                    event.source_plugin_id != trigger.plugin_id
                    or event.event_kind != "external_event"
                )
            )
        ):
            raise ConversationCoverageError("external wakeup source does not match trigger")
        snapshot = await self._load_history_snapshot(
            identity,
            turn=turn,
            before_event_id=None,
            trigger_event_id=event.id,
        )
        if event.id <= snapshot.starts_after_event_id:
            raise ConversationCoverageError("external trigger precedes the reset boundary")
        context = self._prepared_context(
            snapshot,
            identity=identity,
            content=event.content,
            metadata={
                "scene": "群聊" if event.group_id else "私聊",
                "platform": event.platform or identity.platform,
            },
            current_override=self._external_wakeup_message(event, trigger),
            projection_scope="main",
        )
        await self._validate_history_source(snapshot, event)
        return context

    @staticmethod
    def _external_wakeup_message(
        event: EventRecord,
        trigger: ExternalEventTurnTrigger | SandboxTaskTurnTrigger | WorkResumeTrigger,
    ) -> ChatMessage:
        payload: dict[str, object] = {
            "kind": "external_event_wakeup",
            "source": event.external_source or "external",
            "event_type": event.external_event_type or "event",
            "occurred_at": event.occurred_at.isoformat(),
            "summary": event.content,
            "agent_intent": trigger.agent_intent,
        }
        if isinstance(trigger, (SandboxTaskTurnTrigger, WorkResumeTrigger)):
            # This is a host execution trigger, not a new utterance. Original
            # user text is already in current history/goal/input sources.
            payload = {
                "kind": "sandbox_completion"
                if isinstance(trigger, SandboxTaskTurnTrigger)
                else "work_resume",
                "source_event_id": event.id,
                "agent_intent": trigger.agent_intent,
            }
        if isinstance(trigger, SandboxTaskTurnTrigger):
            payload["completion"] = trigger.completion_payload
        return ChatMessage(
            role="user",
            content=(
                "External event wakeup\n"
                + json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            ),
        )

    async def _validate_history_source(
        self,
        snapshot: _HistoryPromptWindow,
        event: EventRecord,
    ) -> None:
        if snapshot.read_version is None:
            return
        if not await self._ledger.read_version_matches(snapshot.read_version):
            from yuki.services.turn_coordinator import HistorySourceChangedError

            raise HistorySourceChangedError(snapshot.read_version)
        current = snapshot.current_event
        # SQLite reloads UTC timestamps without tzinfo; compare their instants
        # without treating that storage representation as a source mutation.
        if current is not None:
            current = replace(
                current,
                occurred_at=(
                    current.occurred_at.replace(tzinfo=UTC)
                    if current.occurred_at.tzinfo is None
                    else current.occurred_at
                ),
            )
        event = replace(
            event,
            occurred_at=(
                event.occurred_at.replace(tzinfo=UTC)
                if event.occurred_at.tzinfo is None
                else event.occurred_at
            ),
        )
        if current != event:
            raise ConversationCoverageError("trigger event changed while assembling context")

    async def _load_history_snapshot(
        self,
        scope: ConversationScope,
        *,
        turn: ConversationTurnSnapshot,
        before_event_id: int | None,
        trigger_event_id: int | None = None,
    ) -> _HistoryPromptWindow:
        loaded = await self._scopes.prepare_prompt_snapshot(
            scope,
            before_event_id=before_event_id,
            expected_turn=turn,
            timezone=self._settings.default_timezone,
            trigger_event_id=trigger_event_id,
        )
        if not turn_matches_hydrated_scope(
            turn,
            conversation_id=loaded.scope.id,
            generation=loaded.scope.generation,
            runtime_key=loaded.scope.runtime_scope_key,
        ):
            raise ConversationCoverageError("prompt snapshot generation changed")
        return self._history_window(scope, loaded)

    @staticmethod
    def _history_window(
        scope: ConversationScope, loaded: ConversationPromptSnapshot
    ) -> _HistoryPromptWindow:
        return _HistoryPromptWindow(
            recent=loaded.raw_events,
            checkpoint_text=loaded.checkpoint_text,
            coverage_end=loaded.effective_coverage,
            revision=loaded.checkpoint_revision,
            checkpoint_kind=loaded.checkpoint_kind,
            starts_after_event_id=loaded.scope.starts_after_event_id,
            read_version=ConversationReadVersion(
                scope,
                loaded.scope.id,
                loaded.scope.generation,
                loaded.scope.starts_after_event_id,
                loaded.prompt_source_revision,
                loaded.references.event_ids
                if loaded.references is not None
                else tuple(event.id for event in loaded.raw_events),
                selected_summary_text=loaded.checkpoint_text,
            ),
            projection=loaded.projection,
            source=loaded.source,
            references=loaded.references,
            current_event=loaded.current_event,
            raw_tail_end_event_id=loaded.raw_tail_end_event_id,
            frozen_prefix=loaded.frozen_prefix,
        )
