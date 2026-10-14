"""The fixed `execute_code` declaration and its versioned Code Mode contract."""

from __future__ import annotations

from qq_ai_bot.domain.messages import ChatTool

EXECUTE_CODE_NAME = "execute_code"
# Wrapper template, receipt view and control rules. A change is a new API
# revision: saved snapshots of the previous revision are never resumed.
CODE_API_REVISION = "yuki.codemode.api.v2"
DUMP_FORMAT = "monty-1.0.1"

CODE_MODE_POLICY = (
    "【持续工作工具编排】\n"
    "基础工具（含联网搜索与网页读取）直接声明；其余工具先用 lookup_tools 搜索目录，"
    "再按精确 name 读取原参数与 script_name，通过 execute_code 调用。查询不追加声明或"
    "授予权限；不要猜参数或直接调用未声明工具。隐藏工具需要先接纳 Work。"
    "已接纳的有效 Work 中，多个步骤已知且无需模型逐步理解新证据时，默认用 execute_code "
    "编排批量读取、过滤汇总、确定性循环、串行操作和原执行回执检查，基础工具也可在脚本中"
    "组合。不要把已知流程拆成每次一个工具的多轮模型请求，也不要把单个直接调用机械包进脚本。"
    "已知规则中的数值判断、字段分支和下一路径选择由脚本完成；数据依赖不等于需要模型语义"
    "判断，只有判定规则尚不确定、必须理解新内容时才交回模型。"
    "已知的读取、计算、写入和读回校验尽量在同一程序按目标要求的顺序完成：有依赖或副作用"
    "顺序的步骤逐步 await，不并发预读后续步骤、先读全部再统一写入，或用取样绕开执行顺序。"
    "只读并发有界分批，不一次 gather 大量调用；不知道队列容量时逐个 await，宿主仍决定"
    "并发与队列限额。"
    "脚本内部按回执核验 status/ok/pending/uncertain，过滤和聚合中间结果；最后只返回必要的"
    "摘要、计算结果、ID、证据或 artifact 引用。print/stdout 同样会交给模型，不打印全量"
    "原文或调试转储。回执不完整时按 result_ref 读取必要证据，不把截断当成完整结果。"
    "pending 时保留原 run_id，交给已有等待/恢复机制，禁止在脚本内死循环轮询；结果未知不"
    "重跑或重发。"
    "普通聊天、单次简短回复、发送已准备好的结果、尚未接纳 Work 的确认澄清、目标本身就是"
    "独立单步的简单操作、下一步须由模型对新证据做语义判断，以及等待/完成/终止等生命周期"
    "控制，可在原权限内直接调用工具；不把已知多步目标拆小当作例外。"
    "只有 execute_code 明确报告未配置、不可用或程序失败时才降级，其他工具失败不代表 "
    "Code Mode 不可用。降级时如实报告，先核对原回执，只用当前授权且本轮已声明的直接工具"
    "接续确认未派发的剩余步骤；不得借此绕过权限或效果围栏的拒绝、重做已提交操作或扩大"
    "权限，pending/unknown 仍须原链恢复。\n\n"
)

EXECUTE_CODE_TOOL = ChatTool(
    name=EXECUTE_CODE_NAME,
    description=(
        "已接纳有效持续 Work 的默认工具编排入口，运行受限 Python 组合脚本，按系统说明的"
        "编排规则完成多个已知步骤。未直接声明的工具先用 lookup_tools 查询原参数与 "
        "script_name。脚本里每个已声明工具都是"
        "`await yuki_<工具名>({参数})`，参数与直接调用同一 schema，返回回执字典："
        "status/ok/data/error/operation_id/result_ref/executed/reused/pending/uncertain/complete；"
        "用 r['ok']、r['data'] 读取，不能用 r.ok。"
        "业务失败是回执而不是异常；先 import asyncio，再用 asyncio.gather 有界并发只读调用，"
        "有依赖、发送与修改按顺序 await。只能导入 Monty 内置支持的模块（例如 asyncio、math），"
        "不能导入宿主 Python 包；没有宿主文件系统、网络或环境变量访问。"
        "最后一个表达式是脚本结果，print/stdout 也会交给模型。"
        "task_control 完成、等待或请求补充信息后，程序继续收拢原获准步骤，再结算工作。"
        "单项权限拒绝返回该项回执；真实当前工作取消、失败、来源失效、新要求或资源耗尽时"
        "宿主停止脚本，脚本不能绕过这些事实继续。脚本结果不会自动发送给任何人。"
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
# Kept only to read an original stop recorded by an older Code API.
STOP_HOST_CONTROL = "host_control"
STOP_NEW_INPUT = "new_input"
STOP_BUDGET = "work_total_budget_exhausted"
STOP_SNAPSHOT = "snapshot_unavailable"

# Refusals of the execution source itself close script admission. Refusal of
# one tool stays an ordinary receipt; every later call checks its own authority.
ADMISSION_CLOSING_ERRORS = frozenset(
    {
        "accept_work_before_execution",
        "main_agent_contract_unavailable",
        "tools_closed",
        "work_activation_obsolete",
    }
)
NEW_INPUT_ERRORS = frozenset({"new_input_before_execution"})
