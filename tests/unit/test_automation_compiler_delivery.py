"""Generated scripts retain explicit DSL delivery; Agentic scripts do not."""

import json

import pytest
from tests.conftest import make_settings

from qq_ai_bot.automation.authority import PermissionLevel
from qq_ai_bot.automation.compiler import AutomationCompiler
from qq_ai_bot.automation.models import AfterSchedule
from qq_ai_bot.automation.task_spec import TaskSpec, TaskStrategy
from qq_ai_bot.automation.validator import CreationProvenance


@pytest.mark.parametrize(
    ("strategy", "calls"),
    [
        (TaskStrategy.AGENTIC, ["yuki.agent"]),
        (TaskStrategy.GENERATED, ["yuki.generate", "onebot.send_private_message"]),
    ],
)
def test_delivery_is_explicit_for_each_automation_strategy(database, strategy, calls) -> None:
    compiler = AutomationCompiler(settings=make_settings(database.url))
    provenance = CreationProvenance(
        creator_user_id="10001",
        bot_user_id="80001",
        message_id="1",
        original_text="提醒我",
        current_group_id=None,
        mentioned_user_ids=(),
        permission=PermissionLevel.USER,
    )
    plan = compiler.compile(
        TaskSpec(
            name="reminder",
            goal="给我一句提醒",
            trigger=AfterSchedule(type="after", seconds=60),
            strategy=strategy,
        ),
        provenance,
        default_timezone="Asia/Shanghai",
    )
    assert [step.call for step in plan.script.steps] == calls
    if strategy is TaskStrategy.AGENTIC:
        instruction = json.loads(plan.script.steps[0].arguments["instruction"])
        assert instruction["delivery"] == "self_private"
        assert "send_message" in instruction["rules"]
    else:
        assert plan.script.steps[1].arguments["text"] == "${result.text}"
