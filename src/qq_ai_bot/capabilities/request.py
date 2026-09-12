"""Model-facing schema for loading omitted capabilities via local search."""

from __future__ import annotations

from qq_ai_bot.domain.messages import ChatTool

REQUEST_TOOLS_NAME = "request_tools"


def request_tools_definition() -> ChatTool:
    """Return the stable schema used to ask the Host for omitted tools."""

    return ChatTool(
        name=REQUEST_TOOLS_NAME,
        description=(
            "按自然语言查找本轮有权执行的能力及真实工具名。工具清单在部署内固定；"
            "本工具只返回可用性与使用信息，不添加 schema，也不能扩大权限。"
            "已有合适工具时直接调用，不必先查询。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 2,
                    "maxLength": 200,
                    "description": "所需能力，例如：搜索并发送网易云单曲",
                },
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 8,
                    "default": 4,
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    )


__all__ = [
    "REQUEST_TOOLS_NAME",
    "request_tools_definition",
]
