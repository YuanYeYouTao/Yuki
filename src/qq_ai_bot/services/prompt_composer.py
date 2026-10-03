"""Compose the current prompt program, conversation history and runtime context."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.config import Settings
from qq_ai_bot.conversation.rollup.renderer import render_rollup_message
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import ChatMessage, InboundMessage
from qq_ai_bot.domain.relationships import style_policy
from qq_ai_bot.memory.context import MEMORY_GROUNDING_RULE, entity_memory_rule
from qq_ai_bot.persistence.event_repository import ConversationReadVersion
from qq_ai_bot.prompting import (
    CORE_CONTRACT,
    PromptChannel,
    PromptCompiler,
    PromptContribution,
    PromptProgram,
    PromptTrust,
)
from qq_ai_bot.prompting.contributors import static_text
from qq_ai_bot.prompting.models import CompiledPrompt, PromptMetrics
from qq_ai_bot.prompting.serializer import serialized_messages_hash
from qq_ai_bot.services.context_assembler import AssembledContext
from qq_ai_bot.services.context_boundary import ContextBoundaryReader
from qq_ai_bot.services.model_context_projection import (
    project_people_and_scene,
    project_recent_delivery,
    project_short_state,
)
from qq_ai_bot.services.prompt_registry import PromptRegistry, PromptTarget
from qq_ai_bot.vision.models import VisualObservation


@dataclass(frozen=True, slots=True)
class PromptComposition:
    messages: tuple[ChatMessage, ...]
    metrics: PromptMetrics
    visible_event_ids: frozenset[int] = frozenset()
    read_version: ConversationReadVersion | None = None
    commit_projection: Callable[[], Awaitable[None]] | None = None
    preparation_model_requests: int = 0
    current_snapshot: dict[str, Any] | None = None
    observation_boundary: ContextBoundaryReader | None = None


class PromptComposer:
    """Translate existing chat inputs into one stable prefix and one turn envelope."""

    def __init__(
        self,
        settings: Settings,
        prompt_registry: PromptRegistry | None = None,
    ) -> None:
        self._settings = settings
        self._registry = prompt_registry or PromptRegistry(
            max_fragment_characters=settings.plugin_max_prompt_fragment_characters,
            max_characters_per_plugin=settings.plugin_max_prompt_characters_per_plugin,
            max_total_plugin_characters=settings.plugin_max_total_prompt_characters,
        )
        self._compiler = PromptCompiler()

    def configure_plugin_limits(self, runtime: RuntimeConfigSnapshot) -> None:
        self._registry.configure_limits(
            max_fragment_characters=runtime.plugins.max_prompt_fragment_characters,
            max_characters_per_plugin=runtime.plugins.max_prompt_characters_per_plugin,
            max_total_plugin_characters=runtime.plugins.max_total_prompt_characters,
        )

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
                CORE_CONTRACT,
                channel=PromptChannel.INVARIANT,
                priority=90,
            ),
            static_text(
                "memory.entity_contract",
                entity_memory_rule(self._settings.bot_display_name),
                channel=PromptChannel.INVARIANT,
                priority=95,
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
        visual_observation: VisualObservation | None,
        visual_failure: bool,
        scope_type: ScopeType | None = None,
        include_plugin_context: bool = True,
        short_state: list[dict[str, Any]] | None = None,
        memory_exclusive_write: bool = False,
    ) -> PromptComposition:
        contributions: list[PromptContribution] = [
            *self._static_contributions(),
            PromptContribution(
                id="runtime.time",
                channel=PromptChannel.RUNTIME,
                trust=PromptTrust.TRUSTED,
                priority=-10_000,
                payload=context.current_time.to_model_dict(),
                required=True,
            ),
        ]
        if short_state:
            contributions.append(
                PromptContribution(
                    id="runtime.short_state",
                    channel=PromptChannel.RUNTIME,
                    trust=PromptTrust.UNTRUSTED,
                    priority=-9_999,
                    payload=project_short_state(short_state),
                    required=True,
                )
            )
        for identity, enabled, data in (
            ("runtime.memory_mutation", memory_exclusive_write, {"exclusive_write": True}),
        ):
            if enabled:
                contributions.append(
                    PromptContribution(
                        id=identity,
                        channel=PromptChannel.RUNTIME,
                        trust=PromptTrust.TRUSTED,
                        priority=90,
                        payload=data,
                        required=True,
                    )
                )
        if inbound is not None and inbound.sender.user_id in self._settings.superusers:
            contributions.append(
                PromptContribution(
                    id="runtime.authority",
                    channel=PromptChannel.RUNTIME,
                    trust=PromptTrust.TRUSTED,
                    priority=95,
                    payload={
                        "authority": "superuser",
                        "source": "current_direct_event",
                    },
                    required=True,
                )
            )
        if context.current_relationship is not None:
            contributions.append(
                PromptContribution(
                    id="context.relationship",
                    channel=PromptChannel.CONTEXT,
                    trust=PromptTrust.TRUSTED,
                    priority=80,
                    payload={
                        "stage": context.current_relationship.stage.value,
                        "style": style_policy(
                            context.current_relationship.stage,
                            (
                                inbound.scope_type
                                if inbound is not None
                                else scope_type or ScopeType.PRIVATE
                            ),
                            self._settings.bot_display_name,
                        ),
                        "unverified_claim_gap": (runtime.relationship.conflict_preference_min_gap),
                    },
                )
            )
        if context.recent_delivery:
            contributions.append(
                PromptContribution(
                    id="runtime.recent_delivery",
                    channel=PromptChannel.RUNTIME,
                    trust=PromptTrust.TRUSTED,
                    priority=94,
                    payload={
                        "recent_delivery": project_recent_delivery(context.recent_delivery),
                        "purpose": "delivery_status_only",
                    },
                    required=True,
                )
            )
        if context.metadata_payload:
            contributions.append(
                PromptContribution(
                    id="context.people_and_scene",
                    channel=PromptChannel.CONTEXT,
                    trust=PromptTrust.UNTRUSTED,
                    priority=85,
                    payload=project_people_and_scene(context.metadata_payload),
                    required=True,
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
        if visual_observation is not None:
            contributions.append(
                PromptContribution(
                    id="modality.visual",
                    channel=PromptChannel.MODALITY,
                    trust=PromptTrust.UNTRUSTED,
                    priority=90,
                    payload=visual_observation.model_dump(
                        mode="json",
                        exclude={"provider", "model", "latency_seconds"},
                        exclude_none=True,
                    ),
                    required=True,
                )
            )
        elif visual_failure:
            contributions.append(
                PromptContribution(
                    id="modality.visual_failure",
                    channel=PromptChannel.MODALITY,
                    trust=PromptTrust.TRUSTED,
                    priority=90,
                    payload={"visual_status": "unavailable", "do_not_guess": True},
                    required=True,
                )
            )
        if runtime.speech.enabled:
            contributions.append(
                PromptContribution(
                    id="runtime.speech",
                    channel=PromptChannel.RUNTIME,
                    trust=PromptTrust.TRUSTED,
                    priority=40,
                    payload={"available": True},
                )
            )
        history = self._conversation_history(context)
        remaining = (
            runtime.context.window_tokens * 3
            + runtime.plugins.max_total_prompt_characters
            - sum(len(message.content or "") for message in history)
            - len(context.current_message.content or "")
            - (2 if context.current_message.content else 0)
        )
        compiled = self._compiler.compile(
            PromptProgram(contributions=tuple(contributions)),
            history=history,
            current_message=context.current_message,
            dynamic_character_budget=max(0, remaining),
        )
        composition = self._finalize(context, compiled)
        snapshot = {
            item.id: item.model_dump(mode="json", include={"payload", "content", "trust", "source"})
            for item in compiled.selected
            if item.channel in {PromptChannel.CONTEXT, PromptChannel.PLUGIN, PromptChannel.MODALITY}
            or item.id == "runtime.short_state"
        }
        from dataclasses import replace

        return replace(
            composition,
            current_snapshot={"kind": "dynamic_snapshot", "contributions": snapshot}
            if snapshot
            else None,
        )

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

    def with_history(
        self,
        composition: PromptComposition,
        original: AssembledContext,
        selected: AssembledContext,
    ) -> PromptComposition:
        """Replace selected history without rendering dynamic contributions twice."""
        history = self._conversation_history(original)
        end = len(composition.messages) - 1
        start = end - len(history)
        if composition.messages[start:end] != history:
            raise ValueError("compiled history slot does not match assembled history")
        selected_history = self._conversation_history(selected)
        messages = (
            *composition.messages[:start],
            *selected_history,
            composition.messages[-1],
        )
        total = sum(len(message.content or "") for message in messages)
        metrics = composition.metrics.model_copy(
            update={
                "history_characters": sum(
                    len(message.content or "") for message in selected_history
                ),
                "total_characters": total,
                "estimated_tokens": math.ceil(total / 4),
                "message_count": len(messages),
                "conversation_prefix_hash": serialized_messages_hash(messages[:-1]),
            }
        )
        from dataclasses import replace

        return replace(
            self._finalize(
                selected, CompiledPrompt(messages=messages, selected=(), metrics=metrics)
            ),
            current_snapshot=composition.current_snapshot,
        )

    def _finalize(
        self,
        context: AssembledContext,
        compiled: CompiledPrompt,
    ) -> PromptComposition:
        snapshot = {
            "scope_id": context.prompt_scope_id,
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
