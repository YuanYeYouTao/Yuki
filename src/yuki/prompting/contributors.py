"""Small helpers for constructing common prompt contributions."""

from __future__ import annotations

from yuki.prompting.models import (
    PromptChannel,
    PromptContribution,
    PromptStability,
)


def static_text(
    contribution_id: str,
    content: str,
    *,
    channel: PromptChannel,
    priority: int,
) -> PromptContribution:
    return PromptContribution(
        id=contribution_id,
        channel=channel,
        priority=priority,
        stability=PromptStability.STATIC,
        content=content,
    )
