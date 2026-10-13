"""Small helpers for constructing common prompt contributions."""

from __future__ import annotations

from qq_ai_bot.prompting.models import (
    PromptChannel,
    PromptContribution,
    PromptStability,
    PromptTrust,
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
        trust=PromptTrust.CORE,
        priority=priority,
        stability=PromptStability.STATIC,
        content=content,
    )
