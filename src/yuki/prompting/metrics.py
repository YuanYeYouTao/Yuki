"""Prompt and tool-schema size metrics."""

from __future__ import annotations

import json
import math
from collections import defaultdict

from pydantic import BaseModel, ConfigDict

from yuki.domain.messages import ChatTool


class ToolSchemaMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_count: int
    schema_characters: int
    estimated_tokens: int
    group_characters: dict[str, int]


def measure_tool_schemas(
    tools: tuple[ChatTool, ...],
    *,
    groups: dict[str, str] | None = None,
) -> ToolSchemaMetrics:
    total = 0
    per_group: dict[str, int] = defaultdict(int)
    for tool in tools:
        size = len(
            json.dumps(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        total += size
        per_group[(groups or {}).get(tool.name, "ungrouped")] += size
    return ToolSchemaMetrics(
        tool_count=len(tools),
        schema_characters=total,
        estimated_tokens=math.ceil(total / 4),
        group_characters=dict(sorted(per_group.items())),
    )
