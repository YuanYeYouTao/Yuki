"""The fixed `execute_code` declaration and its versioned Code Mode contract."""

from __future__ import annotations

from qq_ai_bot.domain.messages import ChatTool

EXECUTE_CODE_NAME = "execute_code"
# Wrapper template, receipt view and control rules. A change is a new API
# revision: saved snapshots of the previous revision are never resumed.
CODE_API_REVISION = "yuki.codemode.api.v1"
DUMP_FORMAT = "monty-1.0.1"

EXECUTE_CODE_TOOL = ChatTool(
    name=EXECUTE_CODE_NAME,
    description=(
        "在已接纳的持续工作内运行一段受限 Python 组合脚本。短聊和单次发送直接用原工具；"
        "只有需要批量读取、循环、汇总或多步组合时才用本工具。脚本里每个已声明工具都是"
        "`await yuki_<工具名>({参数})`，参数与直接调用同一 schema，返回回执字典："
        "status/ok/data/error/operation_id/result_ref/executed/reused/pending/uncertain/complete。"
        "业务失败是回执而不是异常；asyncio.gather 可并发只读调用，发送与修改按顺序执行。"
        "没有文件系统、网络、环境变量或模块导入；最后一个表达式是脚本结果。"
        "memory_change、task_control 结束/等待、新输入、未知副作用或权限变化会由宿主"
        "停止脚本并交回模型，脚本无法捕获后继续。脚本结果不会自动发送给任何人。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "minLength": 1,
                "maxLength": 65536,
                "description": "受限 Python 源码；用 await yuki_<工具名>(参数字典) 调用工具。",
            },
            "inputs": {
                "type": "object",
                "description": "可选的纯 JSON 输入，在脚本中以同名变量出现。",
            },
        },
        "required": ["code"],
        "additionalProperties": False,
    },
    tags=("code", "composition"),
    result_cacheable=False,
)

# Script stops that return control to the model. The Host closes admission
# before any of these is reported; a script cannot catch and continue.
STOP_ADMISSION_CLOSED = "admission_closed"
STOP_UNKNOWN_EFFECT = "unknown_effect"
STOP_HOST_CONTROL = "host_control"
STOP_MEMORY = "memory_observation_required"
STOP_NEW_INPUT = "new_input"
STOP_BUDGET = "work_total_budget_exhausted"
STOP_SNAPSHOT = "snapshot_unavailable"

# Direct-call refusals that also close a script's admission (C05). Ordinary
# argument/validation errors stay ordinary receipts the script may handle.
ADMISSION_CLOSING_ERRORS = frozenset(
    {
        "accept_work_before_execution",
        "capability_no_longer_authorized",
        "capability_not_allowed",
        "main_agent_contract_unavailable",
        "mutation_already_committed",
        "tools_closed",
        "unresolved_prior_effect",
        "work_activation_obsolete",
        "work_start_delivery_unconfirmed",
        "work_start_required",
    }
)
NEW_INPUT_ERRORS = frozenset({"new_input_before_execution"})
