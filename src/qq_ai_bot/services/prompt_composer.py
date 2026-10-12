"""Compose the current prompt program, conversation history and runtime context."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
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
    PromptStability,
    PromptTrust,
)
from qq_ai_bot.prompting.contracts import core_contract
from qq_ai_bot.prompting.contributors import static_text
from qq_ai_bot.prompting.models import CompiledPrompt, PromptMetrics
from qq_ai_bot.services.context_assembler import AssembledContext
from qq_ai_bot.services.context_boundary import ContextBoundaryReader
from qq_ai_bot.services.model_context_projection import (
    project_people_and_scene,
    project_short_state,
)
from qq_ai_bot.services.prompt_registry import PromptRegistry, PromptTarget


@dataclass(frozen=True, slots=True)
class PromptComposition:
    messages: tuple[ChatMessage, ...]
    metrics: PromptMetrics
    visible_event_ids: frozenset[int] = frozenset()
    read_version: ConversationReadVersion | None = None
    commit_projection: Callable[[], Awaitable[None]] | None = None
    preparation_model_requests: int = 0
    current_snapshot: dict[str, Any] | None = None
    visible_material: dict[str, Any] = field(default_factory=dict)
    observation_boundary: ContextBoundaryReader | None = None


def _material_change(identity: str, old: Any, current: Any) -> Any:
    """Compare the existing slot, identity item and plugin fragment units."""
    if old is None:
        return current
    if old == current:
        return None
    if identity == "runtime.short_state":
        before = {item["slot"]: item for item in old}
        after = {item["slot"]: item for item in current}
        return (
            [item for key, item in after.items() if before.get(key) != item]
            + [{"slot": key, "text": ""} for key in before if key not in after]
        ) or None
    if identity == "context.people_and_scene":
        if "items" not in old or "items" not in current:
            return current
        before = {item["id"]: item["data"] for item in old["items"]}
        after = {item["id"]: item["data"] for item in current["items"]}
        changes = []
        for key, data in after.items():
            previous = before.get(key)
            if previous == data:
                continue
            if isinstance(previous, dict) and isinstance(data, dict):
                data = {
                    name: value for name, value in data.items() if previous.get(name) != value
                } | {name: None for name in previous if name not in data}
            changes.append({"id": key, "data": data})
        changes.extend({"id": key, "data": None} for key in before if key not in after)
        return {"items": changes} if changes else None
    if identity == "context.plugins":
        before = {item["fragment_id"]: item for item in old}
        after = {item["fragment_id"]: item for item in current}
        return (
            [item for key, item in after.items() if before.get(key) != item]
            + [{"fragment_id": key, "content": None} for key in before if key not in after]
        ) or None
    return current


def apply_material_delta(baseline: dict[str, Any], contributions: dict[str, Any]) -> dict[str, Any]:
    """Fold submitted changes into the discardable last-visible projection."""
    material = dict(baseline)
    for identity, contribution in contributions.items():
        data = contribution["payload"]
        if identity == "runtime.short_state":
            slots = {item["slot"]: item for item in material.get(identity, [])}
            for item in data:
                if item["text"]:
                    slots[item["slot"]] = item
                else:
                    slots.pop(item["slot"], None)
            material[identity] = list(slots.values())
        elif identity == "context.people_and_scene" and "items" in data:
            people = {
                item["id"]: item["data"] for item in material.get(identity, {"items": []})["items"]
            }
            for item in data["items"]:
                key, value = item["id"], item["data"]
                if value is None:
                    people.pop(key, None)
                elif isinstance(value, dict) and isinstance(people.get(key), dict):
                    people[key] = {**people[key], **value}
                    people[key] = {
                        name: field for name, field in people[key].items() if field is not None
                    }
                else:
                    people[key] = value
            material[identity] = {
                "items": [{"id": key, "data": value} for key, value in people.items()]
            }
        elif identity == "context.plugins":
            plugins = {item["fragment_id"]: item for item in material.get(identity, [])}
            for item in data:
                key = item["fragment_id"]
                if item["content"] is None:
                    plugins.pop(key, None)
                else:
                    plugins[key] = item
            material[identity] = list(plugins.values())
        else:
            material[identity] = data
    return material


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
        if state or context.visible_material.get("runtime.short_state"):
            contributions.append(
                PromptContribution(
                    id="runtime.short_state",
                    channel=PromptChannel.RUNTIME,
                    trust=PromptTrust.UNTRUSTED,
                    priority=-9_999,
                    payload=state,
                    required=True,
                )
            )
        if context.metadata_payload or context.visible_material.get("context.people_and_scene"):
            contributions.append(
                PromptContribution(
                    id="context.people_and_scene",
                    channel=PromptChannel.CONTEXT,
                    trust=PromptTrust.UNTRUSTED,
                    priority=85,
                    payload=project_people_and_scene(context.metadata_payload or {"items": []}),
                    required=True,
                )
            )
        plugin_context = (
            self._registry.render(target=PromptTarget.AGENT) if include_plugin_context else ()
        )
        if plugin_context or context.visible_material.get("context.plugins"):
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
        selected = []
        for item in contributions:
            if item.stability is PromptStability.STATIC:
                selected.append(item)
                continue
            data = item.payload if item.payload is not None else item.content
            old = context.visible_material.get(item.id)
            changed = _material_change(item.id, old, data)
            if changed is not None:
                selected.append(item.model_copy(update={"payload": changed}))
        compiled = self._compiler.compile(
            PromptProgram(contributions=tuple(selected)),
            history=history,
            current_message=context.current_message,
        )
        composition = self._finalize(context, compiled)
        snapshot = {
            item.id: {"payload": item.payload if item.payload is not None else item.content}
            for item in compiled.selected
            if item.channel in {PromptChannel.CONTEXT, PromptChannel.PLUGIN, PromptChannel.MODALITY}
            or item.id == "runtime.short_state"
        }
        return replace(
            composition,
            current_snapshot={"kind": "dynamic_snapshot", "contributions": snapshot}
            if snapshot
            else None,
            visible_material=apply_material_delta(context.visible_material, snapshot),
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
