"""Stable structured composition of core and plugin prompt fragments."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class PromptStage(StrEnum):
    PLUGIN_CONTEXT = "plugin_context"
    TOOL_GUIDANCE = "tool_guidance"


class PromptTarget(StrEnum):
    AGENT = "agent"


class TrustedLevel(StrEnum):
    UNTRUSTED = "untrusted"


_STAGE_ORDER = {stage: index for index, stage in enumerate(PromptStage)}
_PLUGIN_STAGES = frozenset({PromptStage.PLUGIN_CONTEXT, PromptStage.TOOL_GUIDANCE})


@dataclass(frozen=True, slots=True)
class PromptFragment:
    id: str
    stage: PromptStage
    content: str
    plugin_id: str | None = None
    priority: int = 0
    trusted_level: TrustedLevel = TrustedLevel.UNTRUSTED
    target: PromptTarget = PromptTarget.AGENT
    source: str = "core"

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("prompt fragment id must not be empty")
        if not self.content.strip():
            raise ValueError("prompt fragment content must not be empty")
        if self.plugin_id is not None:
            if self.stage not in _PLUGIN_STAGES:
                raise ValueError("third-party fragments may only use plugin prompt stages")
            if self.trusted_level is not TrustedLevel.UNTRUSTED:
                raise ValueError("third-party prompt fragments must be untrusted")


class PromptRegistry:
    """Register immutable fragments and render them in one testable order."""

    def __init__(self) -> None:
        self._fragments: dict[str, PromptFragment] = {}

    def register(self, fragment: PromptFragment) -> None:
        if fragment.id in self._fragments:
            raise ValueError(f"duplicate prompt fragment: {fragment.id}")
        self._fragments[fragment.id] = fragment

    def unregister_plugin(self, plugin_id: str) -> int:
        selected = [
            fragment_id
            for fragment_id, fragment in self._fragments.items()
            if fragment.plugin_id == plugin_id
        ]
        for fragment_id in selected:
            self._fragments.pop(fragment_id)
        return len(selected)

    def render(
        self,
        dynamic: tuple[PromptFragment, ...] = (),
        *,
        target: PromptTarget = PromptTarget.AGENT,
    ) -> tuple[dict[str, str], ...]:
        fragments = tuple(self._fragments.values()) + dynamic
        ids: set[str] = set()
        for fragment in fragments:
            if fragment.id in ids:
                raise ValueError(f"duplicate prompt fragment: {fragment.id}")
            ids.add(fragment.id)
        ordered = sorted(
            (fragment for fragment in fragments if fragment.target is target),
            key=lambda item: (_STAGE_ORDER[item.stage], -item.priority, item.id),
        )
        rendered: list[dict[str, str]] = []
        for fragment in ordered:
            content = fragment.content.strip()
            item = {"fragment_id": fragment.id, "content": content}
            if fragment.plugin_id is not None:
                item["plugin_id"] = fragment.plugin_id
            rendered.append(item)
        return tuple(rendered)


__all__ = [
    "PromptFragment",
    "PromptRegistry",
    "PromptStage",
    "PromptTarget",
    "TrustedLevel",
]
