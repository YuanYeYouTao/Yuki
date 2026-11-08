"""Deployment-stable coding-Agent contract, independent of host or provider."""

from __future__ import annotations

from typing import Any

from yuki.domain.messages import ChatTool
from yuki.external_agents.errors import ExternalAgentArgumentError

EXTERNAL_AGENT_TOOLS = frozenset({"get_coding_agents", "use_coding_agent"})
_TARGET_KEYS = ("instance_id", "session_id", "execution_id", "operation_id")


def _target(description: str) -> dict[str, object]:
    return {
        "type": "object",
        "description": description,
        "properties": {key: {"type": "string"} for key in _TARGET_KEYS},
        "oneOf": [{"required": [key]} for key in _TARGET_KEYS],
        "additionalProperties": False,
    }


def external_agent_tools() -> tuple[ChatTool, ...]:
    text = {"type": "string"}
    return (
        ChatTool(
            name="get_coding_agents",
            namespace="external_agent.read",
            effect="read_state",
            result_cacheable=False,
            description=(
                "查看本人电脑上的编码 Agent（如 Codex、Claude Code）。省略 target 列出设备与实例；"
                "target 给实例列出会话，给会话或执行读取最近进展，给 operation_id 读取原操作的"
                "状态、事件与待回答的问题。history=true 带上最近历史；用 next_cursor 继续翻页。"
                "在线或空闲不代表任务已完成。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "device": text,
                    "target": _target("要查看的实例、会话、执行或原操作，只给一个 ID"),
                    "history": {"type": "boolean"},
                    "cursor": text,
                },
                "additionalProperties": False,
            },
        ),
        ChatTool(
            name="use_coding_agent",
            namespace="external_agent.control",
            effect="write_state",
            result_cacheable=False,
            description=(
                "让本人电脑上的编码 Agent 工作。prompt 向实例或会话提交新任务，queue 排入原生队列，"
                "watch 持续跟进已有会话，steer 给运行中的执行追加要求，answer 回答它的权限请求或"
                "提问，cancel 取消，detach 停止跟进（不关闭会话）。prompt、queue、watch 须在"
                "工作中调用。结果未知时用 get_coding_agents 按 operation_id 查询，不要重提。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "prompt",
                            "queue",
                            "steer",
                            "watch",
                            "answer",
                            "cancel",
                            "detach",
                        ],
                    },
                    "target": _target(
                        "目录返回的实例、会话、执行或原操作 ID，只给一个；"
                        "answer 与 detach 用原 operation_id"
                    ),
                    "prompt": {**text, "description": "prompt、queue、steer 的文字"},
                    "question": {
                        "type": ["string", "integer"],
                        "description": "answer 要回答的权限请求或提问事件 ID（见原操作的事件）",
                    },
                    "answer": {
                        "type": ["string", "object"],
                        "description": "所选选项 ID，或该 Agent 原生回答对象",
                    },
                    "cwd": text,
                    "resume": {"type": "boolean"},
                },
                "required": ["action", "target"],
                "additionalProperties": False,
            },
        ),
    )


def coding_agent_arguments(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Translate a model call into the service arguments of the original contract."""
    target = args.get("target") or {}
    if not isinstance(target, dict) or len(target) > 1:
        raise ExternalAgentArgumentError("invalid_target", "target 只给一个 ID")
    key, identity = next(iter(target.items()), (None, None))
    if name == "get_coding_agents":
        wire: dict[str, Any] = {
            "action": "get" if key in {"session_id", "execution_id", "operation_id"} else "list"
        }
        if "device" in args:
            wire["device_id"] = args["device"]
        if key is not None:
            wire[key] = identity
        if args.get("history"):
            wire["include_history"] = True
        if "cursor" in args:
            if key == "operation_id":
                # Server event pages are numbered; native catalogs keep their own cursor.
                from yuki.sandbox.environment_tools import _cursor

                wire["cursor"] = _cursor(args["cursor"])
            else:
                wire["native_cursor"] = args["cursor"]
        return wire
    action = args.get("action")
    wire = {"action": "respond" if action == "answer" else action}
    if key is not None:
        wire[key] = identity
    for field in ("prompt", "cwd", "resume"):
        if field in args:
            wire[field] = args[field]
    if action == "answer":
        if "question" not in args:
            raise ExternalAgentArgumentError(
                "question_required", "answer 需要 question：原操作事件中的请求 ID"
            )
        wire["permission_id"] = str(args["question"])
        answer = args.get("answer")
        if isinstance(answer, dict):
            wire["response"] = answer
        elif isinstance(answer, str):
            wire["option_id"] = answer
    return wire


# Receipt bookkeeping kept in typed data for recovery, never shown to the model.
_AGENT_INTERNAL = frozenset(
    {
        "kind",
        "request_id",
        "admitted",
        "native_reference",
        "ownership",
        "cursor_parameter",
        "query_scope",
        "status_scope",
        "retryable",
        "uncertain",
        "error",
        "last_confirmed",
    }
)


def coding_agent_view(data: dict[str, Any]) -> dict[str, Any]:
    view = {
        key: value
        for key, value in data.items()
        if key not in _AGENT_INTERNAL and value is not None
    }
    cursor = data.get("next_cursor")
    if isinstance(cursor, int) and not isinstance(cursor, bool):
        view["next_cursor"] = str(cursor)
    return view
