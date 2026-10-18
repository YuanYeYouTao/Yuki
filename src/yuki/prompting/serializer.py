"""Compact deterministic serialization for dynamic prompt envelopes."""

from __future__ import annotations

import json
from typing import Any

from yuki.prompting.models import PromptChannel, PromptContribution, PromptTrust


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
