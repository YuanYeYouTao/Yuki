"""Compose the current prompt program, conversation history and runtime context."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Set
from dataclasses import dataclass
from typing import Any

from yuki.admin.models import RuntimeConfigSnapshot
from yuki.config import Settings
from yuki.domain.messages import ChatMessage, InboundMessage
from yuki.memory.context import MEMORY_GROUNDING_RULE
from yuki.persistence.event_repository import ConversationReadVersion
from yuki.prompting import (
    PromptChannel,
    PromptCompiler,
    PromptContribution,
    PromptProgram,
)
from yuki.prompting.contracts import PARTICIPATION_STATE_POLICY, core_contract
from yuki.prompting.contributors import static_text
from yuki.prompting.models import PromptMetrics
from yuki.services.context_assembler import AssembledContext
from yuki.services.context_boundary import ContextBoundaryReader
from yuki.services.context_compaction import PublicCompactContext, render_context_checkpoint
from yuki.services.model_context_projection import project_short_state
from yuki.services.prompt_registry import PromptRegistry, PromptTarget


@dataclass(frozen=True, slots=True)
class PromptComposition:
    messages: tuple[ChatMessage, ...]
    metrics: PromptMetrics
    visible_event_ids: Set[int] = frozenset()
    read_version: ConversationReadVersion | None = None
    commit_projection: Callable[[], Awaitable[None]] | None = None
    preparation_model_requests: int = 0
    observation_boundary: ContextBoundaryReader | None = None
    public_compaction: PublicCompactContext | None = None


class PromptComposer:
    """Translate existing chat inputs into one stable prefix and one turn envelope."""

    def __init__(
        self,
        settings: Settings,
        prompt_registry: PromptRegistry | None = None,
    ) -> None:
        self._settings = settings
        self._registry = prompt_registry or PromptRegistry()
        self._compiler = PromptCompiler()

    def static_contributions(self) -> tuple[PromptContribution, ...]:
        return (
            static_text(
                "core.persona",
                self._settings.character_prompt,
                channel=PromptChannel.PERSONA,
                priority=100,
            ),
            *self.invariant_contributions(),
        )

    def invariant_contributions(self) -> tuple[PromptContribution, ...]:
        return (
            static_text(
                "core.contract",
                core_contract(code_enabled=self._settings.code_mode_enabled),
                channel=PromptChannel.INVARIANT,
                priority=90,
            ),
            static_text(
                "memory.grounding_contract",
                MEMORY_GROUNDING_RULE,
                channel=PromptChannel.INVARIANT,
                priority=96,
            ),
        )

    def invariant_messages(self) -> tuple[ChatMessage, ...]:
        """Technical recovery compatibility; SOUL remains in the original transcript."""
        return self._compiler.compile(
            PromptProgram(contributions=self.invariant_contributions())
        ).messages

    def static_messages(self) -> tuple[ChatMessage, ...]:
        return self._compiler.compile(
            PromptProgram(contributions=self.static_contributions())
        ).messages

    def compose(
        self,
        *,
        inbound: InboundMessage | None,
        context: AssembledContext,
        runtime: RuntimeConfigSnapshot,
        include_plugin_context: bool = True,
        short_state: list[dict[str, Any]] | None = None,
        static: tuple[PromptContribution, ...] | None = None,
    ) -> PromptComposition:
        contributions = list(self.static_contributions() if static is None else static)
        if runtime.conversation_policy().semantic_participation_enabled:
            # Own join/stay/quiet reports have a consumer only in semantic participation.
            # It stays out of invariant_contributions(), so the recovery contract hash
            # does not follow this HOT switch.
            contributions.append(
                static_text(
                    "core.participation_state",
                    PARTICIPATION_STATE_POLICY,
                    channel=PromptChannel.INVARIANT,
                    priority=89,
                )
            )
        state = project_short_state(short_state or [])
        if state:
            contributions.append(
                PromptContribution(
                    id="runtime.short_state",
                    channel=PromptChannel.RUNTIME,
                    priority=-9_999,
                    payload=state,
                )
            )
        if context.resident_text:
            # Last part of the system message: stable per conversation (it changes at
            # most once per Dream batch), so it joins the cached prefix. It is kept out
            # of invariant_contributions(), so the recovery contract hash ignores it.
            contributions.append(
                static_text(
                    "memory.resident",
                    context.resident_text,
                    channel=PromptChannel.CONTEXT,
                    priority=10,
                )
            )
        if context.episode_index_text:
            # The episode index follows the resident segment as the very last part of
            # the system message: it changes at most once per Dream batch, so it joins
            # the cached prefix; like the resident segment it stays out of the
            # recovery contract hash.
            contributions.append(
                static_text(
                    "memory.episode_index",
                    context.episode_index_text,
                    channel=PromptChannel.CONTEXT,
                    priority=5,
                )
            )
        if context.episode_recent_text:
            # Episodes written after the watermark change every turn: they ride in the
            # current message right after the current scene material.
            contributions.append(
                PromptContribution(
                    id="memory.episodes_recent",
                    channel=PromptChannel.CONTEXT,
                    priority=84,
                    payload=context.episode_recent_text,
                )
            )
        if context.metadata_payload:
            contributions.append(
                PromptContribution(
                    id="context.current",
                    channel=PromptChannel.CONTEXT,
                    priority=85,
                    payload=context.metadata_payload,
                )
            )
        plugin_context = (
            self._registry.render(target=PromptTarget.AGENT) if include_plugin_context else ()
        )
        if plugin_context:
            contributions.append(
                PromptContribution(
                    id="context.plugins",
                    channel=PromptChannel.PLUGIN,
                    priority=30,
                    payload=list(plugin_context),
                    source="plugins",
                )
            )
        history = self._conversation_history(context)
        compiled = self._compiler.compile(
            PromptProgram(contributions=tuple(contributions)),
            history=history,
            current_message=context.current_message,
        )
        return PromptComposition(
            messages=compiled.messages,
            metrics=compiled.metrics,
            read_version=context.read_version,
            visible_event_ids=context.visible_event_ids,
        )

    @staticmethod
    def _conversation_history(context: AssembledContext) -> tuple[ChatMessage, ...]:
        if not context.checkpoint_text.strip():
            return tuple(context.history_messages)
        return (
            render_context_checkpoint(context.checkpoint_text, context.prompt_effective_coverage),
            *context.history_messages,
        )
