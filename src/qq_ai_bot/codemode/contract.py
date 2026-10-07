"""The fixed `execute_code` declaration and its versioned Code Mode contract."""

from __future__ import annotations

from qq_ai_bot.domain.messages import ChatTool

EXECUTE_CODE_NAME = "execute_code"
# Wrapper template, receipt view and control rules. A change is a new API
# revision: saved snapshots of the previous revision are never resumed.
CODE_API_REVISION = "yuki.codemode.api.v1"
DUMP_FORMAT = "monty-1.0.1"

CODE_MODE_POLICY = (
    "【持续工作工具编排】\n"
    "基础工具（含联网搜索与网页读取）直接声明；其余工具先用 lookup_tools 搜索目录，"
    "再按精确 name 读取原参数与 script_name，通过 execute_code 调用。查询不追加声明或"
    "授予权限；不要猜参数或直接调用未声明工具。基础工具同样可以在脚本中组合。"
    "隐藏工具需要先接纳 Work；Code Mode 不可用时明确说明该能力当前无法执行。"
    "已接纳的有效 Work 中，多个步骤已知且无需模型逐步理解新证据时，默认用 execute_code "
    "编排批量读取、过滤汇总、确定性循环、串行操作和原执行回执检查。不要把已知流程拆成"
    "每次一个工具的多轮模型请求，也不要将每次直接调用一对一机械包进脚本。"
    "已知规则中的数值判断、字段分支和下一路径选择仍由脚本完成；数据依赖不等于需要"
    "模型语义判断。只有判定规则尚不确定、必须理解新内容时才交回模型。"
    "已知的读取、计算、写入和读回校验尽量在同一程序完成，严格遵守目标要求的顺序；"
    "先按目标的依赖和验收要求组织完整流程。步骤有依赖或副作用顺序时逐步 await，"
    "不能并发预读后续步骤、先读全部再统一写入，或用取样绕开要求的执行顺序。"
    "不要只取样或列出中间值后，再让模型逐步重读、计算和写入。只读并发使用有界分批，"
    "不要一次 gather 大量调用；不知道队列容量时逐个 await，宿主仍决定并发与队列限额。"
    "脚本内部按真实回执核验 status/ok/pending/uncertain，过滤和聚合中间结果；最后只返回"
    "任务需要的摘要、计算结果、必要 ID、证据或 artifact 引用，不返回全部子工具原文。"
    "print/stdout 同样会交给模型，不打印全量原文或调试转储；核验用脚本断言和必要摘要。"
    "回执不完整时按 result_ref 读取必要证据，不把截断当成完整结果。"
    "异步操作 pending 时保留原 run_id，使用已有等待/恢复机制；禁止在脚本内死循环轮询，"
    "结果未知不重跑或重发。"
    "普通聊天、单次简短回复、发送已准备好的结果、尚未接纳 Work 的确认澄清、单个独立"
    "简单操作、下一步必须由模型对新证据做语义判断，以及等待/完成/终止等生命周期控制，"
    "可在原权限内直接调用工具。单个简单操作指目标本身是独立单步，不把已知多步目标"
    "拆小当作例外。只有 execute_code 明确报告未配置、不可用或程序失败时才适用降级，"
    "其他工具失败不代表 Code Mode 不可用。降级时如实报告，先核对"
    "原回执；确认未派发的剩余步骤可用当前授权且本轮已声明的直接工具接续。不得通过改走直接工具"
    "绕过权限或效果围栏的拒绝、重做已提交操作或扩大权限；pending/unknown 仍须原链恢复。\n\n"
)

EXECUTE_CODE_TOOL = ChatTool(
    name=EXECUTE_CODE_NAME,
    description=(
        "已接纳有效持续 Work 的默认工具编排入口，运行受限 Python 组合脚本。多个已知步骤"
        "无需模型逐步语义判断时，用脚本完成批量读取、过滤汇总、确定性循环、串行操作与"
        "原回执检查；不要拆成多轮单工具请求，也不要一对一机械包装每个直接调用。"
        "未直接声明的工具先用 lookup_tools 查询原参数与 script_name，不猜工具或参数。"
        "已知数值判断、字段分支和路径选择在脚本里执行；数据依赖不等于模型语义判断。"
        "已知读、算、写、读回校验尽量在同一程序按目标顺序完成；只读并发有界分批，"
        "不知道队列容量时逐个 await。依赖步骤不能并发预读或先读全部再统一写入。"
        "短聊、未接纳 Work、单个独立简单操作、新证据需模型判断及生命周期控制可直接调用"
        "原工具。脚本内部处理子工具结果，最后只返回必要摘要、结果和证据引用；"
        "print/stdout 也不能转储全部中间原文。"
        "pending 保留原 run_id，交回已有等待/恢复机制，禁止死循环轮询；未知效果不重跑。"
        "脚本里每个已声明工具都是"
        "`await yuki_<工具名>({参数})`，参数与直接调用同一 schema，返回回执字典："
        "status/ok/data/error/operation_id/result_ref/executed/reused/pending/uncertain/complete；"
        "用 r['ok']、r['data'] 读取，不能用 r.ok。"
        "业务失败是回执而不是异常；先 import asyncio，再用 asyncio.gather 并发只读调用，"
        "发送与修改按顺序执行。只能导入 Monty 内置支持的模块（例如 asyncio、math），"
        "不能导入宿主 Python 包；没有宿主文件系统、网络或环境变量访问。"
        "最后一个表达式是脚本结果。"
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
    }
)
NEW_INPUT_ERRORS = frozenset({"new_input_before_execution"})
