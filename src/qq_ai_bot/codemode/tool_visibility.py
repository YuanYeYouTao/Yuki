"""Fixed model exposure over a separately frozen execution API."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from qq_ai_bot.domain.messages import ChatTool

if TYPE_CHECKING:
    from qq_ai_bot.codemode.api_projection import ScriptApi

TOOL_LOOKUP_NAME = "lookup_tools"
DIRECT_TOOL_NAMES = frozenset(
    {
        "send_message",
        "poke_person",
        "task_control",
        "subagent_start",
        "subagent_control",
        "subagent_message",
        "update_short_state",
        "search_memory",
        "get_memory_fact",
        "get_memory_evidence",
        "memory_change",
        "get_relationship",
        "find_contacts",
        "get_group_members",
        "get_my_capabilities",
        "get_recent_chat_history",
        "search_chat_history",
        "get_chat_history_around",
        "read_conversation_history",
        "inspect_conversation_attachment",
        "read_tool_artifact",
        "workspace_read",
        "workspace_write",
        "workspace_list",
        "workspace_search",
        "workspace_inspect",
        "workspace_patch",
        "workspace_mkdir",
        "workspace_move",
        "workspace_delete",
        "workspace_publish",
        "time_get_current",
        "time_get_timezone",
        "get_code_run",
        "cancel_code_run",
        "web_search",
        "read_webpage",
        "execute_code",
        TOOL_LOOKUP_NAME,
    }
)

LOOKUP_TOOLS = ChatTool(
    name=TOOL_LOOKUP_NAME,
    description=(
        "只读查询本部署冻结的工具用法，不执行工具或授予权限。query 按名称和说明搜索，"
        "为空时分页列出目录；name 精确读取单个工具的原参数 schema 和脚本调用名。"
        "未直接声明的工具只能在接纳 Work 后通过 execute_code 调用；查询不会追加声明。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "maxLength": 128},
            "name": {"type": "string", "minLength": 1, "maxLength": 256},
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10},
        },
        "additionalProperties": False,
    },
    tags=("code", "discovery"),
    result_cacheable=True,
)


def model_definitions(tools: tuple[ChatTool, ...]) -> tuple[ChatTool, ...]:
    """Stable per deployment, independent of task text, actor or lookup results."""
    return tuple(tool for tool in tools if tool.name in DIRECT_TOOL_NAMES)


def lookup_tools(api: ScriptApi, raw: str, *, declared_names: frozenset[str] | None = None) -> str:
    """Read one frozen API; workers never fall back to the main catalog."""
    try:
        args = json.loads(raw)
        if not isinstance(args, dict) or set(args) - {"query", "name", "offset", "limit"}:
            raise ValueError
        query, name = args.get("query", ""), args.get("name")
        offset, limit = args.get("offset", 0), args.get("limit", 5)
        if (
            not isinstance(query, str)
            or len(query) > 128
            or ("name" in args and (not isinstance(name, str) or not 1 <= len(name) <= 256))
            or type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= 10
            or (name is not None and any(key in args for key in ("query", "offset", "limit")))
        ):
            raise ValueError
    except (ValueError, TypeError):
        return json.dumps({"ok": False, "executed": False, "error": "invalid_lookup_arguments"})
    direct_names = DIRECT_TOOL_NAMES if declared_names is None else declared_names
    if name is not None:
        if name not in api.schemas:
            return json.dumps({"ok": False, "executed": False, "error": "unknown_capability"})
        from qq_ai_bot.codemode.api_projection import encode_wrapper_name

        data: object = {
            "name": name,
            "description": api.descriptions[name],
            "parameters": api.schemas[name],
            "script_name": encode_wrapper_name(name),
            "direct": name in direct_names,
        }
    else:
        words = query.casefold().split()
        names = [
            key
            for key in sorted(api.schemas)
            if all(word in (key + " " + api.descriptions[key]).casefold() for word in words)
        ]
        selected = names[offset : offset + limit]
        data = {
            "tools": [
                {
                    "name": key,
                    "description": api.descriptions[key][:240],
                    "direct": key in direct_names,
                }
                for key in selected
            ],
            "total": len(names),
            "next_offset": offset + len(selected) if offset + len(selected) < len(names) else None,
        }
    return json.dumps(
        {
            "ok": True,
            "data": data,
            "manifest_revision": api.manifest_revision,
            "authorization": "Execution still checks current source, permissions and budget.",
        },
        ensure_ascii=False,
    )
