"""Generated and agentic tasks share one Agent-owned delivery contract."""

import json

import pytest
from pydantic import ValidationError
from tests.conftest import make_settings

from qq_ai_bot.automation.authority import PermissionLevel
from qq_ai_bot.automation.compiler import AutomationCompiler
from qq_ai_bot.automation.models import AfterSchedule
from qq_ai_bot.automation.registry import build_capability_registry
from qq_ai_bot.automation.task_spec import TaskDelivery, TaskSpec, TaskStrategy
from qq_ai_bot.automation.validator import CreationProvenance


def provenance(group_id=None):
    return CreationProvenance(
        creator_user_id="10001",
        bot_user_id="80001",
        message_id="1",
        original_text="提醒我",
        current_group_id=group_id,
        mentioned_user_ids=(),
        permission=PermissionLevel.USER,
    )


def test_generated_and_agentic_compile_the_same_delivery_contract():
    for case in (
        (None, "auto", "self_private"),
        ("20001", "auto", "current_group"),
        ("20001", "self_private", "self_private"),
        ("20001", "current_group", "current_group"),
        (None, "none", "none"),
        ("20001", "none", "none"),
    ):
        _assert_model_delivery(*case)


def _assert_model_delivery(group_id, target, resolved):
    settings = make_settings("sqlite+aiosqlite:///:memory:")
    compiler = AutomationCompiler(settings=settings)
    definition = build_capability_registry().require("yuki.agent")
    plans = [
        compiler.compile(
            TaskSpec(
                name="reminder",
                goal="给我一句提醒",
                trigger=AfterSchedule(type="after", seconds=60),
                strategy=strategy,
                delivery=TaskDelivery(target=target),
            ),
            provenance(group_id),
            default_timezone="Asia/Shanghai",
        )
        for strategy in (TaskStrategy.GENERATED, TaskStrategy.AGENTIC, TaskStrategy.AUTO)
    ]
    assert plans[0].script == plans[1].script == plans[2].script
    script = plans[0].script
    assert [(step.id, step.call) for step in script.steps] == [("execute", "yuki.agent")]
    arguments = definition.validate_arguments(script.steps[0].arguments)
    assert arguments["delivery_target"] == resolved
    instruction = json.loads(arguments["instruction"])
    assert instruction["delivery"] == resolved
    assert "send_message" in instruction["rules"]
    assert script.uses_runtime_budget
    assert script.limits.timeout_seconds == settings.automation_max_runtime_seconds


def test_static_delivery_remains_an_explicit_text_send():
    for case in (
        (None, "onebot.send_private_message", "user_id"),
        ("20001", "onebot.send_group_message", "group_id"),
    ):
        _assert_static_delivery(*case)


def _assert_static_delivery(group_id, call, target_argument):
    compiler = AutomationCompiler(settings=make_settings("sqlite+aiosqlite:///:memory:"))
    plan = compiler.compile(
        TaskSpec(
            name="reminder",
            goal="喝水",
            trigger=AfterSchedule(type="after", seconds=60),
            strategy=TaskStrategy.STATIC,
        ),
        provenance(group_id),
        default_timezone="Asia/Shanghai",
    )
    assert len(plan.script.steps) == 1
    assert plan.script.steps[0].call == call
    assert plan.script.steps[0].arguments["text"] == "喝水"
    assert target_argument in plan.script.steps[0].arguments
    assert not plan.script.uses_runtime_budget


def test_delivery_target_is_optional_only_on_the_internal_agent_dsl():
    definition = build_capability_registry().require("yuki.agent")
    assert definition.validate_arguments({"instruction": "legacy"})["delivery_target"] is None
    with pytest.raises(ValidationError):
        definition.validate_arguments({"instruction": "work", "delivery_target": "anywhere"})
    task_schema = TaskSpec.model_json_schema()
    assert task_schema["$defs"]["TaskStrategy"]["enum"] == [
        "auto",
        "static",
        "generated",
        "agentic",
    ]
    assert "delivery_target" not in json.dumps(task_schema)
