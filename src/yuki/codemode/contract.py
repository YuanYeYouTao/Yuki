"""The fixed `execute_code` declaration and its versioned Code Mode contract."""

from __future__ import annotations

from yuki.domain.messages import ChatTool

EXECUTE_CODE_NAME = "execute_code"
# Wrapper template, receipt view and control rules. A change is a new API
# revision: saved snapshots of the previous revision are never resumed.
CODE_API_REVISION = "yuki.codemode.api.v2"
DUMP_FORMAT = "monty-1.0.1"

CODE_MODE_POLICY = (
    "【持续工作工具编排】\n"
    "基础工具（含联网搜索与网页读取）直接声明；其余工具先用 lookup_tools 搜索目录，"
    "再按精确 name 读取原参数与 script_name，通过 execute_code 调用。目录查询不追加声明或"
    "授予权限，脚本调用也核验同一授权。execute_code 需要当前已接纳的有效 Work。"
    "多个步骤已知且无需逐步理解新证据时，默认用脚本编排读取、计算、过滤汇总和串行操作；"
    "基础工具也可组合使用。按已知规则处理字段分支，需要理解新内容时再交回模型。"
    "按任务的真实依赖和副作用顺序逐步 await，不用并发预读、统一后写或取样绕过顺序要求。"
    "独立只读调用可有界并发，宿主继续核验并发与队列限额。"
    "脚本内部按回执核验 status/ok/pending/uncertain，过滤和聚合中间结果；最后只返回必要的"
    "摘要、计算结果、ID、证据或 artifact 引用。print/stdout 同样会交给模型，不打印全量"
    "原文或调试转储。回执不完整时按 result_ref 读取必要证据，不把截断当成完整结果。"
    "pending 时保留原 run_id，交给已有等待/恢复机制，禁止在脚本内死循环轮询；结果未知不"
    "重跑或重发。"
    "普通聊天、交付、确认澄清、语义判断和生命周期控制使用本轮已直接声明的工具；"
    "独立单步操作也按该声明选择，未直接声明的工具仍经目录和 execute_code 调用。"
    "不机械包装每个直接调用，也不把已知多步流程拆成逐步请求。"
    "execute_code 未配置、不可用或程序失败时，先核对原回执，只用当前授权且本轮已声明的"
    "直接工具接续确认未派发的剩余步骤；单项工具失败不代表 Code Mode 不可用。"
    "权限拒绝、pending 或未知效果不能靠切换工具绕过或重做。是否向人反馈按任务需要和"
    "原反馈约定判断，不因切换调用方式额外发言。\n\n"
)

EXECUTE_CODE_TOOL = ChatTool(
    name=EXECUTE_CODE_NAME,
    description=(
        "已接纳有效持续 Work 的默认工具编排入口，运行受限 Python 组合脚本，按系统说明的"
        "编排规则完成多个已知步骤。未直接声明的工具先用 lookup_tools 查询原参数与 "
        "script_name。脚本用 `await script_name({参数})` 调用该精确函数名，"
        "普通名称如 `await yuki_task_control({参数})`，参数与直接调用同一 schema，返回回执字典："
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
                "description": "受限 Python 源码；用 await 和工具目录的精确 script_name 调用工具。",
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
