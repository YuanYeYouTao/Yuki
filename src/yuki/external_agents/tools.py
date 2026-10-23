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
                "prompt/queue/watch 前必须先 task_control(action=accept) 接纳 Work。"
                "cursor 续读服务器重要事件；native_cursor 续读原生目录，"
                "include_history 明确读取原生历史。"
                "watch 将已有会话纳入当前 Work 持续监督，跨轮接收进度；"
                "指定 execution_id 时只监督该轮。"
                "prompt 提交新输入，queue 明确排入原生队列，均沿当前 Work 保存原操作。"
                "steer 给指定活跃执行追加要求；respond 回答原问题；cancel 精确取消原操作；"
                "detach 结束监督，不关闭用户会话或 daemon。参数只使用目录返回的内部 ID。"
                "respond/detach 必须带原 operation_id 或 run_id；execution_id 不能代替原操作。"
                "respond 的 permission_id 使用服务器事件顶层值，option_id/response 沿原问题选项。"
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
                            "prompt",
                            "cwd",
                            "option_id",
                        )
                    },
                    "operation_id": {
                        **text,
                        "description": "原 operation_id；respond/detach 需此项或 run_id。",
                    },
                    "run_id": {
                        **text,
                        "description": "原监督 run_id，可代替 operation_id；不是原生执行 ID。",
                    },
                    "execution_id": {
                        **text,
                        "description": "内部执行 ID，用于 get/steer/cancel；"
                        "不代替 respond/detach 的原 operation_id。",
                    },
                    "request_id": {
                        **text,
                        "description": "仅 get 查证的兼容别名；新写调用身份由 Host 提供。",
                    },
                    "permission_id": {
                        **text,
                        "description": "原 operation 的 permission/question 顶层 permission_id；"
                        "不是 payload 内的原生 request ID。",
                    },
                    "resume": {"type": "boolean"},
                    "native_cursor": text,
                    "include_history": {"type": "boolean"},
                    "response": {"type": "object", "additionalProperties": True},
                    "outcome": {"type": "string", "enum": ["cancelled"]},
                    "cursor": {"type": "integer", "minimum": 0},
                },
                "required": ["action"],
                "additionalProperties": False,
            },
        ),
    )
