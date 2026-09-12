"""One public contract shared by every Yuki entrypoint and delegated automation."""

from __future__ import annotations

from typing import Any

from qq_ai_bot.domain.messages import ChatTool

EXECUTION_TOOLS = frozenset({"run_python", "terminal_exec", "environment_packages"})
READ_TOOLS = frozenset({"get_code_run", "terminal_read", "environment_status"})
ENVIRONMENT_TOOLS = frozenset(
    {
        "terminal_exec",
        "terminal_read",
        "terminal_write",
        "terminal_control",
        "environment_status",
        "environment_packages",
        "environment_service",
    }
)
SANDBOX_TOOLS = ENVIRONMENT_TOOLS | {"run_python", "get_code_run", "cancel_code_run"}


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
            "terminal_exec",
            "在 Yuki 的持久 Linux 环境运行 Bash 命令。/workspace 与文件工具共享，"
            "HOME=/home/yuki；pip/npm 依赖长期保留。约"
            " 5 秒返回，pending 时查询 run_id，勿重复执行。"
            "tty=true 可运行交互程序；command=bash 可创建保"
            "持目录、变量和函数的终端，再用 terminal_write 输入。"
            "无桌面，公网 HTTP/HTTPS 经代理，内部服务用 environment_service 登记。",
            {
                "command": {"type": "string", "maxLength": 65536},
                "cwd": text,
                "tty": {"type": "boolean"},
                "timeout_seconds": {"type": "integer", "minimum": 0, "maximum": 86400},
            },
            ("command",),
        ),
        tool(
            "terminal_read",
            "按字节游标读取终端增量输出和任务状态。返回 next_cursor；旧输出已轮转会明确标记。"
            "空输出不代表完成；使用 status、pending 和 exit_code 判断。",
            {**run, **cursor},
            ("run_id",),
            read=True,
        ),
        tool(
            "terminal_write",
            "向正在运行的终端发送原样输入；回车需包含换行。不会创建第二个命令任务。",
            {**run, "text": {"type": "string", "maxLength": 8192}},
            ("run_id", "text"),
        ),
        tool(
            "terminal_control",
            "中断、取消或关闭指定终端。INT 相当于 Ctrl+C；取消停止此任务，保留已写入的工作区文件。",
            {**run, "action": {"type": "string", "enum": ["interrupt", "cancel", "close"]}},
            ("run_id", "action"),
        ),
        tool(
            "environment_status",
            "查看持久环境、容量、任务、终端与已登记服务。资源不足时先查询并整理，不盲目重试。",
            {},
            read=True,
        ),
        tool(
            "environment_packages",
            "在沙箱内安装或移除 apt 系统软件包；无需人工批准。串行执行并保存成功检查点，"
            "约 5 秒返回任务 ID。Python/Node 依赖直接通过 terminal_exec 使用 pip/npm 安装。",
            {
                "action": {"type": "string", "enum": ["install", "remove", "repair"]},
                "packages": {"type": "array", "items": text, "maxItems": 30},
            },
            ("action",),
        ),
        tool(
            "environment_service",
            "管理环境内部后台服务；登记后可跨服务器重启恢复。"
            "服务只在沙箱内部监听，HTTP 请求可使用 loca"
            "lhost。故障重启有次数上限，停止后不会自动重启。",
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
