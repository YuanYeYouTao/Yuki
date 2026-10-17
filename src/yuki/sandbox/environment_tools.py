"""One public contract shared by every Yuki entrypoint and delegated automation."""

from __future__ import annotations

from typing import Any

from yuki.domain.messages import ChatTool

EXECUTION_TOOLS = frozenset({"terminal_exec"})
READ_TOOLS = frozenset({"terminal_read", "environment_status"})
ENVIRONMENT_TOOLS = frozenset(
    {
        "terminal_exec",
        "terminal_read",
        "terminal_control",
        "environment_status",
        "environment_service",
        "external_agent",
    }
)
SANDBOX_TOOLS = ENVIRONMENT_TOOLS


def is_execution(name: str, args: dict[str, Any]) -> bool:
    return name == "terminal_exec" or (name == "external_agent" and args.get("action") == "prompt")


def tool(
    name: str,
    description: str,
    fields: dict[str, Any],
    required: tuple[str, ...] = (),
    *,
    read: bool = False,
) -> ChatTool:
    return ChatTool(
        name=name,
        description=description,
        result_cacheable=False,
        parameters={
            "type": "object",
            "properties": fields,
            "required": list(required),
            "additionalProperties": False,
        },
    )


def environment_tools() -> tuple[ChatTool, ...]:
    text = {"type": "string"}
    run = {"run_id": text}
    cursor = {"cursor": {"type": "integer", "minimum": 0}}
    return (
        tool(
            "external_agent",
            "使用已配置的外部 Agent：list 查看目录；prompt 在原 Work 中委托工作，"
            "peer_id 续用本 Work 原会话；get 按 run_id 查原进展与结果。"
            "respond 以原 permission_id/option_id 回答权限，必要时才询问用户；"
            "cancel 停止原 prompt，close 释放原 peer。pending 后等待或查询原执行，不重复提交。",
            {
                "action": {
                    "type": "string",
                    "enum": ["list", "prompt", "get", "respond", "cancel", "close"],
                },
                "agent_id": text,
                "prompt": text,
                "peer_id": text,
                **run,
                "permission_id": text,
                "option_id": text,
                "outcome": {"type": "string", "enum": ["cancelled"]},
                **cursor,
            },
            ("action",),
        ),
        tool(
            "terminal_exec",
            "以管理员身份操作 Yuki 自己的持久 Linux 环境，HOME=/home/yuki，"
            "文件在 /workspace；可直接使用 Bash、Python、Node、apt、pip/npm 和公网。"
            "默认持续运行，约5秒返回原 run_id；pending 后用 terminal_read 查询，勿重提命令。"
            "tty=true 支持交互，terminal_control(action=input) 发送输入。",
            {
                "command": text,
                "cwd": text,
                "tty": {"type": "boolean"},
                "timeout_seconds": {"type": "integer", "minimum": 0},
            },
            ("command",),
        ),
        tool(
            "terminal_read",
            "按原 run_id 读取增量输出、状态、退出码及终态 completion/artifact；"
            "也可查旧安装或代码任务。"
            "wait_seconds 只等待观察，不停止进程；next_cursor 续读，"
            "轮转丢失会标记，空输出不代表完成。",
            {**run, **cursor, "wait_seconds": {"type": "number", "minimum": 0}},
            ("run_id",),
            read=True,
        ),
        tool(
            "terminal_control",
            "控制原终端：input 发送原样 text（回车显式写换行），interrupt 相当于 Ctrl+C，"
            "cancel 停止原任务，close 关闭会话。已写文件保留，不创建第二个命令。",
            {
                **run,
                "action": {"type": "string", "enum": ["input", "interrupt", "cancel", "close"]},
                "text": text,
            },
            ("run_id", "action"),
        ),
        tool(
            "environment_status",
            "查看持久环境、实际容量、任务、终端与已登记服务。",
            {},
            read=True,
        ),
        tool(
            "environment_service",
            "管理环境内部后台服务；登记后可跨服务器重启恢复。"
            "环境内服务可通过 localhost 访问；按 restart 策略恢复，显式停止后不自动重启。",
            {
                "action": {
                    "type": "string",
                    "enum": ["register", "start", "stop", "delete", "status", "logs"],
                },
                "name": text,
                "command": text,
                "cwd": text,
                "restart": {"type": "string", "enum": ["on-failure", "always", "never"]},
                **cursor,
            },
            ("action", "name"),
        ),
    )
