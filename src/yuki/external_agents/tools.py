"""Deployment-stable external Agent contract, independent of host or provider."""

from __future__ import annotations

from yuki.domain.messages import ChatTool

EXTERNAL_AGENT_TOOLS = frozenset({"external_agent"})


def external_agent_tools() -> tuple[ChatTool, ...]:
    text = {"type": "string"}
    return (
        ChatTool(
            name="external_agent",
            description=(
                "调用本人已授权电脑上的原生 Agent。list 查看设备、实例和会话的实际能力；"
                "get 读取原资源、操作、状态和历史，无需接纳 Work。"
                "watch 将已有执行纳入当前 Work 监督；"
                "prompt 提交新输入，queue 明确排入原生队列，均沿当前 Work 保存原操作。"
                "steer 给指定活跃执行追加要求；respond 回答原问题；cancel 精确取消原操作；"
                "detach 结束监督，不关闭用户会话或 daemon。参数只使用目录返回的内部 ID。"
                "pending 或 unknown 后查询原 operation_id，不重提，不暗中另开会话或取消重启。"
            ),
            result_cacheable=False,
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "list",
                            "get",
                            "watch",
                            "prompt",
                            "steer",
                            "queue",
                            "respond",
                            "cancel",
                            "detach",
                        ],
                    },
                    **{
                        name: text
                        for name in (
                            "device_id",
                            "instance_id",
                            "session_id",
                            "execution_id",
                            "operation_id",
                            "run_id",
                            "request_id",
                            "prompt",
                            "cwd",
                            "permission_id",
                            "option_id",
                        )
                    },
                    "resume": {"type": "boolean"},
                    "response": {"type": "object", "additionalProperties": True},
                    "outcome": {"type": "string", "enum": ["cancelled"]},
                    "cursor": {"type": "integer", "minimum": 0},
                },
                "required": ["action"],
                "additionalProperties": False,
            },
        ),
    )
