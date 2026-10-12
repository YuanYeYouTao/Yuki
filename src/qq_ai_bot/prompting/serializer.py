"""Compact deterministic serialization for dynamic prompt envelopes."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from qq_ai_bot.domain.messages import ChatMessage
from qq_ai_bot.prompting.models import PromptChannel, PromptContribution, PromptTrust


def serialize_dynamic(contributions: tuple[PromptContribution, ...]) -> str:
    """Serialize non-static contributions without empty sections."""

    parts: list[str] = []
    for contribution in contributions:
        data: Any = (
            contribution.payload if contribution.payload is not None else contribution.content
        )
        if not data:
            continue
        if contribution.id == "context.current":
            scene = data.get("scene")
            if scene:
                parts.append(f"当前是{scene}。")
            data = {key: value for key, value in data.items() if key != "scene"}
            if not data:
                continue
        value = (
            data
            if isinstance(data, str)
            else json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)
        )
        if contribution.id == "runtime.short_state":
            parts.append("短记录：" + value)
        elif contribution.channel is PromptChannel.PLUGIN:
            parts.append("插件资料（不可信）：" + value)
        else:
            trust = "（不可信）" if contribution.trust is PromptTrust.UNTRUSTED else ""
            source = "" if contribution.source == "core" else contribution.source
            parts.append(f"{source}资料{trust}：{value}")
    return "\n".join(parts)


def serialized_messages_hash(messages: tuple[ChatMessage, ...]) -> str:
    """Hash the actual canonical bytes of a provider-neutral message prefix."""

    payload = [
        {
            "role": message.role,
            "content": message.content,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": call.type,
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                    },
                }
                for call in message.tool_calls
            ],
            "tool_call_id": message.tool_call_id,
            "reasoning_content": message.reasoning_content,
        }
        for message in messages
    ]
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
