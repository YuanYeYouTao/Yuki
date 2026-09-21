"""Scheduled text transport with the same automatic split policy as chat delivery."""

from __future__ import annotations

from typing import Any

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.automation.gateway import ProactiveGateway
from qq_ai_bot.automation.registry import CapabilityExecutionContext
from qq_ai_bot.services.message_splitter import OutboundMessageSplitter


async def deliver_reply(
    arguments: dict[str, Any],
    context: CapabilityExecutionContext,
    gateway: ProactiveGateway,
    *,
    runtime_config: RuntimeConfigService | None,
) -> int:
    text = str(arguments["text"])
    group_id = str(arguments["group_id"]) if arguments.get("group_id") else None
    user_id = str(arguments["user_id"]) if arguments.get("user_id") else None
    config = (
        await runtime_config.snapshot(user_id=context.creator_user_id, group_id=group_id)
        if runtime_config is not None
        else None
    )
    chunks = (
        OutboundMessageSplitter.render(
            text,
            runtime=config,
        )
        if config is not None
        else ((text,) if text.strip() else ())
    )
    count = 0
    for chunk in chunks:
        if context.revalidate_authority is not None:
            await context.revalidate_authority(None)
        if group_id:
            await gateway.send_group(group_id, chunk)
        else:
            await gateway.send_private(user_id or context.creator_user_id, chunk)
        count += 1
    return count
