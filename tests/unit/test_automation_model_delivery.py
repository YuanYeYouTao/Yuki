"""DSL output references cannot publish a main Agent's internal final text."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from tests.conftest import make_settings

from qq_ai_bot.automation.authority import PermissionLevel
from qq_ai_bot.automation.model_delivery import classify_model_delivery
from qq_ai_bot.automation.models import AutomationScript, AutomationStep
from qq_ai_bot.automation.registry import build_capability_registry
from qq_ai_bot.automation.validator import AutomationValidator, CreationProvenance


def script(*steps: AutomationStep) -> AutomationScript:
    return AutomationScript.model_validate(
        {
            "version": 1,
            "name": "delivery boundary",
            "schedule": {"type": "after", "seconds": 120},
            "steps": steps,
            "limits": {
                "agent_budget_managed": True,
                "max_steps": len(steps),
                "max_llm_calls": 2,
                "max_tool_calls": len(steps),
                "max_messages": 2,
            },
        }
    )


def model(call: str = "yuki.generate") -> AutomationStep:
    return AutomationStep(
        id="compose",
        call=call,
        arguments={"instruction": "完成任务", "context_profile": "none"},
        save_as="result",
    )


def send(text: str = "${result.text}") -> AutomationStep:
    return AutomationStep(
        id="notify",
        call="social.send_message",
        arguments={"target": {"kind": "person", "subject_ref": "current_speaker"}, "text": text},
    )


def provenance(group: str | None = "12345") -> CreationProvenance:
    return CreationProvenance(
        creator_user_id="10001",
        bot_user_id="80001",
        message_id="test",
        original_text="稍后执行并通知我",
        current_group_id=group,
        mentioned_user_ids=(),
        permission=PermissionLevel.USER,
    )


def validator() -> AutomationValidator:
    return AutomationValidator(
        settings=make_settings("sqlite+aiosqlite:///:memory:", automation_enabled=True),
        registry=build_capability_registry(),
    )


def test_direct_model_text_cannot_be_sent_by_a_second_dsl_step():
    web = AutomationStep(id="lookup", call="web.search", arguments={"query": "independent query"})
    for call in ("yuki.generate", "yuki.agent"):
        source = model(call)
        for reference in ("compose", "result"):
            for target in (None, {"kind": "person", "subject_ref": "current_speaker"}):
                delivery = AutomationStep(
                    id="arbitrary_tail_name",
                    call="social.send_message",
                    arguments={
                        **({"target": target} if target else {}),
                        "text": "${" + reference + ".text}",
                    },
                )
                result = classify_model_delivery(script(source, web, delivery), 2)
                assert result


def test_transformed_indirect_media_and_plugin_model_sends_require_update():
    source = model()
    derived = AutomationStep(
        id="lookup",
        call="web.search",
        arguments={"query": "${result.text}"},
        save_as="derived",
    )
    variants = [
        send("提醒：${result.text}"),
        send("${compose.text} ${result.text}"),
        send("${result.tool_calls_used}"),
        send("${derived.content}"),
        send().model_copy(update={"arguments": {"text": "${result.text}"}}),
        AutomationStep(
            id="notify",
            call="social.send_message",
            arguments={"text": "${result.text}"},
        ),
        AutomationStep(
            id="notify",
            call="plugin.notice",
            arguments={"payload": {"message": "${result.text}"}},
        ),
    ]
    for delivery in variants:
        result = classify_model_delivery(
            script(source, derived, delivery), 2, send_capabilities={"plugin.notice"}
        )
        assert result


def test_literal_web_output_and_overwritten_alias_remain_ordinary_dsl():
    web = AutomationStep(
        id="lookup", call="web.search", arguments={"query": "literal query"}, save_as="page"
    )
    cases = [
        script(send("字面提醒")),
        script(web, send("${page.results}")),
        script(model(), web, send("独立字面提醒")),
        script(model(), web, send("${page.results}")),
        # The executor overwrites outputs['result'] with this independent web step.
        script(model(), web.model_copy(update={"id": "result"}), send()),
    ]
    for declaration in cases:
        assert not classify_model_delivery(declaration, len(declaration.steps) - 1)
    for declaration in cases[:4]:
        validator().validate(declaration, provenance(), now_utc=datetime(2026, 9, 22, tzinfo=UTC))


def test_validator_rejects_direct_and_derived_model_delivery_including_plugins():
    check = validator()
    check._registry.register(
        replace(check._registry.require("social.send_message"), name="plugin.notice")
    )
    derived = AutomationStep(
        id="lookup", call="web.search", arguments={"query": "${result.text}"}, save_as="web"
    )
    for call in ("yuki.generate", "yuki.agent"):
        for declaration in (
            script(model(call), send()),
            script(model(call), send("前缀${result.text}")),
            script(model(call), derived, send("${web.results}")),
            script(model(call), send().model_copy(update={"call": "plugin.notice"})),
        ):
            with pytest.raises(ValueError, match="不能通过 DSL 投递模型输出"):
                check.validate(declaration, provenance(), now_utc=datetime(2026, 9, 22, tzinfo=UTC))


def test_agent_current_group_delivery_requires_group_provenance():
    agent = model("yuki.agent")
    declaration = script(
        agent.model_copy(
            update={"arguments": {**agent.arguments, "delivery_target": "current_group"}}
        )
    )
    check = validator()
    with pytest.raises(ValueError, match="不是群聊"):
        check.validate(declaration, provenance(None), now_utc=datetime(2026, 9, 22, tzinfo=UTC))
    check.validate(declaration, provenance(), now_utc=datetime(2026, 9, 22, tzinfo=UTC))
    for target in ("none", "self_private"):
        check.validate(
            script(
                agent.model_copy(
                    update={"arguments": {**agent.arguments, "delivery_target": target}}
                )
            ),
            provenance(None),
            now_utc=datetime(2026, 9, 22, tzinfo=UTC),
        )
