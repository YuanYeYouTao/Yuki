"""Compose the current prompt program, conversation history and runtime context."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.config import Settings
from qq_ai_bot.conversation.rollup.renderer import render_rollup_message
from qq_ai_bot.domain.messages import ChatMessage, InboundMessage
from qq_ai_bot.memory.context import MEMORY_GROUNDING_RULE
from qq_ai_bot.persistence.event_repository import ConversationReadVersion
from qq_ai_bot.prompting import (
    PromptChannel,
    PromptCompiler,
    PromptContribution,
    PromptProgram,
    PromptTrust,
)
from qq_ai_bot.prompting.contracts import core_contract
from qq_ai_bot.prompting.contributors import static_text
from qq_ai_bot.prompting.models import CompiledPrompt, PromptMetrics
from qq_ai_bot.services.context_assembler import AssembledContext
from qq_ai_bot.services.context_boundary import ContextBoundaryReader
from qq_ai_bot.services.model_context_projection import project_short_state
from qq_ai_bot.services.prompt_registry import PromptRegistry, PromptTarget


@dataclass(frozen=True, slots=True)
class PromptComposition:
    messages: tuple[ChatMessage, ...]
    metrics: PromptMetrics
    visible_event_ids: frozenset[int] = frozenset()
    read_version: ConversationReadVersion | None = None
    commit_projection: Callable[[], Awaitable[None]] | None = None
    preparation_model_requests: int = 0
    observation_boundary: ContextBoundaryReader | None = None


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

    def _static_contributions(self) -> tuple[PromptContribution, ...]:
        return (
            static_text(
                "core.persona",
                self._settings.system_prompt,
                channel=PromptChannel.PERSONA,
                priority=100,
            ),
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

    def static_messages(self) -> tuple[ChatMessage, ...]:
        return self._compiler.compile(
            PromptProgram(contributions=self._static_contributions())
        ).messages

    def compose(
        self,
        *,
        inbound: InboundMessage | None,
        context: AssembledContext,
        runtime: RuntimeConfigSnapshot,
        include_plugin_context: bool = True,
        short_state: list[dict[str, Any]] | None = None,
    ) -> PromptComposition:
        contributions: list[PromptContribution] = [*self._static_contributions()]
        state = project_short_state(short_state or [])
        if state:
            contributions.append(
                PromptContribution(
                    id="runtime.short_state",
                    channel=PromptChannel.RUNTIME,
                    trust=PromptTrust.UNTRUSTED,
                    priority=-9_999,
                    payload=state,
                )
            )
        if context.metadata_payload:
            contributions.append(
                PromptContribution(
                    id="context.current",
                    channel=PromptChannel.CONTEXT,
                    trust=PromptTrust.UNTRUSTED,
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
                    trust=PromptTrust.UNTRUSTED,
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
        return self._finalize(context, compiled)

    @staticmethod
    def _conversation_history(context: AssembledContext) -> tuple[ChatMessage, ...]:
        if not context.rollup_text.strip():
            return context.history_messages
        return (
            render_rollup_message(
                context.rollup_text,
                kind=context.metrics.rollup_mode,
                covered_through_event_id=context.prompt_effective_coverage,
            ),
            *context.history_messages,
        )

    def _finalize(
        self,
        context: AssembledContext,
        compiled: CompiledPrompt,
    ) -> PromptComposition:
        snapshot = {
            "conversation_id": context.prompt_conversation_id,
            "scope_key": context.prompt_scope_key,
            "generation": context.prompt_generation,
            "coverage": context.prompt_effective_coverage,
            "rollup_revision": context.prompt_rollup_revision,
            "raw_tail_end_event_id": context.prompt_raw_tail_end_event_id,
            "conversation_prefix_hash": compiled.metrics.conversation_prefix_hash,
        }
        serialized = json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        metrics = compiled.metrics.model_copy(
            update={
                "prompt_snapshot_fingerprint": hashlib.sha256(
                    serialized.encode("utf-8")
                ).hexdigest()
            }
        )
        return PromptComposition(
            messages=compiled.messages,
            metrics=metrics,
            read_version=context.read_version,
            visible_event_ids=context.visible_event_ids,
        )
