"""Default guidance is shared and does not change tools or execution authority."""

import pytest
from scripts.benchmark_long_tasks import orchestration_guidance
from scripts.export_pi_codemode_inventory import export_inventory

from qq_ai_bot.codemode.api_projection import project
from qq_ai_bot.codemode.contract import CODE_MODE_POLICY, EXECUTE_CODE_TOOL
from qq_ai_bot.domain.messages import ChatTool
from qq_ai_bot.prompting.contracts import CORE_CONTRACT
from qq_ai_bot.runtime.subagent_tools import WORKER_PROMPT


def test_main_and_worker_receive_one_shared_default_policy():
    assert CORE_CONTRACT.count(CODE_MODE_POLICY) == 1
    assert WORKER_PROMPT.count(CODE_MODE_POLICY) == 1
    assert "已接纳的有效 Work" in CODE_MODE_POLICY
    assert "无需模型逐步理解新证据" in CODE_MODE_POLICY
    assert "一对一机械包进脚本" in CODE_MODE_POLICY
    assert "必要 ID、证据或 artifact 引用" in CODE_MODE_POLICY
    assert "禁止在脚本内死循环轮询" in CODE_MODE_POLICY
    assert "有界分批" in CODE_MODE_POLICY
    assert "读取、计算、写入和读回校验尽量在同一程序" in CODE_MODE_POLICY
    assert "步骤有依赖或副作用顺序时逐步 await" in CODE_MODE_POLICY
    assert "print/stdout 同样会交给模型" in CODE_MODE_POLICY
    assert "数据依赖不等于需要" in CODE_MODE_POLICY
    assert "单个简单操作指目标本身是独立单步" in CODE_MODE_POLICY
    assert "其他工具失败不代表 Code Mode 不可用" in CODE_MODE_POLICY


@pytest.mark.parametrize(
    "exception",
    ["普通聊天", "单次简短回复", "尚未接纳 Work", "单个独立", "语义判断", "生命周期控制"],
)
def test_direct_call_exceptions_remain_explicit(exception):
    assert exception in CODE_MODE_POLICY
    assert "可在原权限内直接调用工具" in CODE_MODE_POLICY
    # Engine availability is not execution authority: explicitly allow only
    # undispatched work, retaining authorization and unresolved-effect refusals.
    assert "确认未派发的剩余步骤可用当前授权下的直接工具接续" in CODE_MODE_POLICY
    assert "绕过权限或效果围栏的拒绝" in CODE_MODE_POLICY
    assert "pending/unknown 仍须原链恢复" in CODE_MODE_POLICY


async def test_default_policy_reaches_frozen_manifest_without_replacing_direct_tools():
    inventory = await export_inventory()
    tools = tuple(ChatTool(**row) for row in inventory["frozen_definitions"])
    declared = {tool.name: tool for tool in tools}
    assert declared["execute_code"].description == EXECUTE_CODE_TOOL.description
    assert "默认工具编排入口" in declared["execute_code"].description
    assert {"workspace_read", "workspace_write", "send_message", "task_control"} <= declared.keys()
    api = project(tools, inventory["manifest_revision"])
    for name, tool in declared.items():
        if name != "execute_code":
            assert api.schemas[name] == tool.parameters
    assert set(api.wrappers.values()) == declared.keys() - {"execute_code"}


def test_real_acceptance_supplies_policy_without_a_program_or_forced_single_call():
    instruction = orchestration_guidance("code", default_policy=True)
    assert instruction.count(CORE_CONTRACT) == 1
    assert "Choose your own approach and program" in instruction
    assert "Use exactly one execute_code" not in instruction
    assert "with this program" not in instruction
    assert "Experimental direct-call control" in orchestration_guidance(
        "direct", default_policy=True
    )
    assert orchestration_guidance("direct") == (
        "Use direct workspace_read/workspace_write/workspace_list calls only; never execute_code. "
        "You may batch independent direct calls in one response."
    )
