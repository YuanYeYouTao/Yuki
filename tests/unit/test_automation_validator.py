from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tests.conftest import make_settings

from qq_ai_bot.automation.authority import PermissionLevel
from qq_ai_bot.automation.models import AutomationScript
from qq_ai_bot.automation.registry import build_capability_registry
from qq_ai_bot.automation.templates import TemplateError, resolve_templates
from qq_ai_bot.automation.validator import (
    AutomationValidator,
    CreationProvenance,
    canonical_script_hash,
)


def _script(*, target: str = "$creator_user_id", text: str = "去跑步") -> AutomationScript:
    return AutomationScript.model_validate(
        {
            "version": 1,
            "name": "提醒",
            "timezone": "Asia/Shanghai",
            "schedule": {"type": "after", "seconds": 1200},
            "context": {"scene": "none"},
            "steps": [
                {
                    "id": "send",
                    "call": "onebot.send_private_message",
                    "arguments": {"user_id": target, "text": text},
                }
            ],
            "limits": {
                "max_steps": 1,
                "max_llm_calls": 0,
                "max_tool_calls": 1,
                "max_messages": 1,
                "timeout_seconds": 30,
            },
        }
    )


def _provenance(*, superuser: bool = False, text: str = "20分钟后提醒我") -> CreationProvenance:
    return CreationProvenance(
        creator_user_id="9000" if superuser else "10001",
        bot_user_id="7777",
        message_id="m1",
        original_text=text,
        current_group_id="1049765710",
        mentioned_user_ids=("1808058482",),
        permission=PermissionLevel.SUPERUSER if superuser else PermissionLevel.USER,
    )


def _validator() -> AutomationValidator:
    return AutomationValidator(
        settings=make_settings("sqlite+aiosqlite:///:memory:", automation_enabled=True),
        registry=build_capability_registry(),
    )


def test_ordinary_user_can_create_owner_scoped_automation() -> None:
    result = _validator().validate(
        _script(), _provenance(), now_utc=datetime(2026, 7, 27, tzinfo=UTC)
    )
    assert result.required_capabilities == ("onebot.send_private_message",)


def test_ordinary_user_cannot_target_another_qq() -> None:
    with pytest.raises(PermissionError, match="本人"):
        _validator().validate(
            _script(target="1808058482"),
            _provenance(text="20分钟后提醒 1808058482"),
            now_utc=datetime(2026, 7, 27, tzinfo=UTC),
        )


def test_speech_automation_is_owner_scoped_and_profile_is_fixed_at_creation() -> None:
    payload = _script().model_dump(mode="json")
    payload["steps"][0] = {
        "id": "speak",
        "call": "speech.send_private",
        "arguments": {
            "user_id": "$creator_user_id",
            "text": "该喝水了",
            "style_hint": "gentle",
            "profile_id": "",
        },
    }
    script = AutomationScript.model_validate(payload)
    validated = _validator().validate(
        script,
        _provenance(),
        now_utc=datetime(2026, 7, 27, tzinfo=UTC),
    )
    assert validated.required_capabilities == ("speech.send_private",)

    payload["steps"][0]["arguments"]["user_id"] = "1808058482"
    with pytest.raises(PermissionError, match="本人"):
        _validator().validate(
            AutomationScript.model_validate(payload),
            _provenance(text="提醒 1808058482"),
            now_utc=datetime(2026, 7, 27, tzinfo=UTC),
        )

    payload["steps"][0]["arguments"]["user_id"] = "$creator_user_id"
    payload["steps"][0]["arguments"]["profile_id"] = "yuki"
    with pytest.raises(PermissionError, match="默认声线"):
        _validator().validate(
            AutomationScript.model_validate(payload),
            _provenance(text="用 yuki 声线提醒我"),
            now_utc=datetime(2026, 7, 27, tzinfo=UTC),
        )


def test_superuser_can_use_explicit_target_from_real_text() -> None:
    result = _validator().validate(
        _script(target="1808058482"),
        _provenance(superuser=True, text="20分钟后提醒 1808058482"),
        now_utc=datetime(2026, 7, 27, tzinfo=UTC),
    )
    assert result.next_run_at == datetime(2026, 7, 27, 0, 20, tzinfo=UTC)


def test_untrusted_step_output_cannot_become_target_but_can_be_text() -> None:
    payload = _script(text="${generate.text}").model_dump(mode="json")
    payload["steps"].insert(
        0,
        {
            "id": "generate",
            "call": "yuki.generate",
            "arguments": {
                "instruction": "生成提醒",
                "context_profile": "none",
                "max_characters": 20,
            },
        },
    )
    payload["limits"].update(max_steps=2, max_llm_calls=1, max_tool_calls=2)
    script = AutomationScript.model_validate(payload)
    assert _validator().validate(script, _provenance(), now_utc=datetime(2026, 7, 27, tzinfo=UTC))

    payload["steps"][1]["arguments"]["user_id"] = "${generate.text}"
    with pytest.raises(ValueError, match="不可信"):
        _validator().validate(
            AutomationScript.model_validate(payload),
            _provenance(),
            now_utc=datetime(2026, 7, 27, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    "text", ["今晚十一点写个日记，保存成文件发群里", "今天晚上十一点，重建一下", "就按刚才说的时间"]
)
def test_agent_resolved_schedule_is_not_reparsed_from_user_wording(text: str) -> None:
    payload = _script().model_dump(mode="json")
    payload["schedule"] = {
        "type": "once",
        "local_datetime": "2026-09-14T23:00:00",
        "timezone": "Asia/Shanghai",
    }
    result = _validator().validate(
        AutomationScript.model_validate(payload),
        _provenance(text=text),
        now_utc=datetime(2026, 9, 14, 14, 12, tzinfo=UTC),
    )
    assert result.next_run_at == datetime(2026, 9, 14, 15, 0, tzinfo=UTC)
    payload["timezone"] = "Invalid/Timezone"
    with pytest.raises(ValueError):
        _validator().validate(
            AutomationScript.model_validate(payload),
            _provenance(text=text),
            now_utc=datetime(2026, 9, 14, 14, 12, tzinfo=UTC),
        )


def test_unknown_capability_and_short_interval_are_rejected() -> None:
    payload = _script().model_dump(mode="json")
    payload["steps"][0]["call"] = "shell.exec"
    with pytest.raises(ValueError, match="未登记"):
        _validator().validate(
            AutomationScript.model_validate(payload),
            _provenance(),
            now_utc=datetime(2026, 7, 27, tzinfo=UTC),
        )
    payload = _script().model_dump(mode="json")
    payload["schedule"] = {"type": "interval", "seconds": 59}
    with pytest.raises(ValueError, match="最短"):
        _validator().validate(
            AutomationScript.model_validate(payload),
            _provenance(),
            now_utc=datetime(2026, 7, 27, tzinfo=UTC),
        )


def test_script_hash_is_stable_for_argument_order() -> None:
    first = _script()
    payload = first.model_dump(mode="json")
    arguments = payload["steps"][0]["arguments"]
    payload["steps"][0]["arguments"] = {
        "text": arguments["text"],
        "user_id": arguments["user_id"],
    }
    assert canonical_script_hash(first) == canonical_script_hash(
        AutomationScript.model_validate(payload)
    )


def test_capability_schema_and_backend_limits_are_enforced() -> None:
    payload = _script().model_dump(mode="json")
    payload["steps"][0]["arguments"]["unexpected"] = True
    with pytest.raises(ValueError, match="Schema"):
        _validator().validate(
            AutomationScript.model_validate(payload),
            _provenance(),
            now_utc=datetime(2026, 7, 27, tzinfo=UTC),
        )

    payload = _script().model_dump(mode="json")
    payload["limits"]["max_steps"] = 17
    with pytest.raises(ValueError, match="less than or equal to 16"):
        AutomationScript.model_validate(payload)


def test_llm_and_message_counts_must_fit_script_limits() -> None:
    payload = _script().model_dump(mode="json")
    payload["steps"].insert(
        0,
        {
            "id": "generate",
            "call": "yuki.generate",
            "arguments": {
                "instruction": "生成提醒",
                "context_profile": "none",
                "max_characters": 20,
            },
        },
    )
    payload["limits"].update(max_steps=2, max_tool_calls=2, max_llm_calls=0)
    with pytest.raises(ValueError, match="LLM"):
        _validator().validate(
            AutomationScript.model_validate(payload),
            _provenance(),
            now_utc=datetime(2026, 7, 27, tzinfo=UTC),
        )

    payload = _script().model_dump(mode="json")
    payload["limits"]["max_messages"] = 0
    with pytest.raises(ValueError, match="消息"):
        _validator().validate(
            AutomationScript.model_validate(payload),
            _provenance(),
            now_utc=datetime(2026, 7, 27, tzinfo=UTC),
        )


def test_current_group_target_is_allowed_only_from_a_real_group_event() -> None:
    payload = _script().model_dump(mode="json")
    payload["steps"][0] = {
        "id": "send",
        "call": "onebot.send_group_message",
        "arguments": {"group_id": "$current_group_id", "text": "测试"},
    }
    script = AutomationScript.model_validate(payload)
    assert _validator().validate(script, _provenance(), now_utc=datetime(2026, 7, 27, tzinfo=UTC))
    private = CreationProvenance(
        creator_user_id="10001",
        bot_user_id="7777",
        message_id="m2",
        original_text="20分钟后发到本群",
        current_group_id=None,
        mentioned_user_ids=(),
        permission=PermissionLevel.USER,
    )
    with pytest.raises(ValueError, match="不是群聊"):
        _validator().validate(script, private, now_utc=datetime(2026, 7, 27, tzinfo=UTC))


def test_untrusted_output_cannot_become_group_or_onebot_action() -> None:
    group_payload = _script().model_dump(mode="json")
    generate = {
        "id": "generate",
        "call": "yuki.generate",
        "arguments": {
            "instruction": "生成目标",
            "context_profile": "none",
            "max_characters": 20,
        },
    }
    group_payload["steps"] = [
        generate,
        {
            "id": "send",
            "call": "onebot.send_group_message",
            "arguments": {"group_id": "${generate.group_id}", "text": "测试"},
        },
    ]
    group_payload["limits"].update(max_steps=2, max_tool_calls=2, max_llm_calls=1)
    with pytest.raises(ValueError, match="不可信"):
        _validator().validate(
            AutomationScript.model_validate(group_payload),
            _provenance(),
            now_utc=datetime(2026, 7, 27, tzinfo=UTC),
        )

    action_payload = _script().model_dump(mode="json")
    action_payload["steps"] = [
        generate,
        {
            "id": "call",
            "call": "onebot.call_api",
            "arguments": {"action": "${generate.action}", "params": {}},
        },
    ]
    action_payload["limits"].update(max_steps=2, max_tool_calls=2, max_llm_calls=1)
    with pytest.raises(ValueError, match="不可信"):
        _validator().validate(
            AutomationScript.model_validate(action_payload),
            _provenance(superuser=True),
            now_utc=datetime(2026, 7, 27, tzinfo=UTC),
        )


def test_template_resolution_supports_builtins_and_prior_scalar_fields() -> None:
    resolved = resolve_templates(
        {
            "user_id": "$creator_user_id",
            "text": "提醒：${generate.text}",
            "run": "$automation_run_id",
        },
        builtins={"creator_user_id": "10001", "automation_run_id": 7},
        step_outputs={"generate": {"text": "去跑步"}},
    )
    assert resolved == {"user_id": "10001", "text": "提醒：去跑步", "run": 7}
    with pytest.raises(TemplateError, match="尚无输出"):
        resolve_templates(
            "${missing.text}",
            builtins={},
            step_outputs={},
        )


def test_script_cannot_reference_a_later_step() -> None:
    payload = _script(text="${generate.text}").model_dump(mode="json")
    payload["steps"].append(
        {
            "id": "generate",
            "call": "yuki.generate",
            "arguments": {
                "instruction": "生成提醒",
                "context_profile": "none",
                "max_characters": 20,
            },
        }
    )
    payload["limits"].update(max_steps=2, max_tool_calls=2, max_llm_calls=1)
    with pytest.raises(ValueError, match="尚未执行"):
        _validator().validate(
            AutomationScript.model_validate(payload),
            _provenance(),
            now_utc=datetime(2026, 7, 27, tzinfo=UTC),
        )


def test_dangerous_execution_capabilities_are_not_registered() -> None:
    names = {item.name for item in build_capability_registry().list()}
    assert {
        "python.exec",
        "shell.exec",
        "file.read_any",
        "file.write",
        "sql.execute",
        "docker.call",
        "http.request_any",
        "secret.read",
        "automation.create",
    }.isdisjoint(names)


@pytest.mark.parametrize("text", ["$model_generated_target", "${bad}", "${step}"])
def test_unknown_or_malformed_templates_are_rejected_at_creation(text: str) -> None:
    with pytest.raises(ValueError, match=r"模板|未知内置"):
        _validator().validate(
            _script(text=text),
            _provenance(),
            now_utc=datetime(2026, 7, 27, tzinfo=UTC),
        )


def test_explicit_target_must_be_a_complete_numeric_token() -> None:
    provenance = CreationProvenance(
        creator_user_id="9000",
        bot_user_id="7777",
        message_id="m-token",
        original_text="目标是 918080584821",
        current_group_id="1049765710",
        mentioned_user_ids=(),
        permission=PermissionLevel.SUPERUSER,
    )
    with pytest.raises(ValueError, match="明确出现在"):
        _validator().validate(
            _script(target="1808058482"),
            provenance,
            now_utc=datetime(2026, 7, 27, tzinfo=UTC),
        )


def test_yuki_agent_rejects_retired_tool_selection() -> None:
    payload = _script().model_dump(mode="json")
    payload["steps"] = [
        {
            "id": "agent",
            "call": "yuki.agent",
            "arguments": {
                "instruction": "总结最近信息",
                "context_profile": "none",
                "max_tool_calls": 2,
                "max_model_requests": 2,
                "allowed_capabilities": ["history.search", "web.search"],
            },
        }
    ]
    payload["limits"].update(
        max_steps=1,
        max_llm_calls=2,
        max_tool_calls=3,
        max_messages=0,
    )
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        _validator().validate(
            AutomationScript.model_validate(payload),
            _provenance(),
            now_utc=datetime(2026, 7, 27, tzinfo=UTC),
        )

    payload["steps"][0]["arguments"]["allowed_capabilities"] = [
        "onebot.call_api",
        "config.set",
    ]
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        _validator().validate(
            AutomationScript.model_validate(payload),
            _provenance(superuser=True),
            now_utc=datetime(2026, 7, 27, tzinfo=UTC),
        )


def test_yuki_agent_context_and_nested_limits_must_match_script() -> None:
    payload = _script().model_dump(mode="json")
    payload["steps"] = [
        {
            "id": "agent",
            "call": "yuki.agent",
            "arguments": {
                "instruction": "提醒我",
                "context_profile": "creator_private",
                "max_tool_calls": 3,
                "max_model_requests": 3,
            },
        }
    ]
    payload["limits"].update(
        max_steps=1,
        max_llm_calls=2,
        max_tool_calls=3,
        max_messages=0,
    )
    with pytest.raises(ValueError, match="context_profile"):
        _validator().validate(
            AutomationScript.model_validate(payload),
            _provenance(),
            now_utc=datetime(2026, 7, 27, tzinfo=UTC),
        )
    payload["context"]["scene"] = "creator_private"
    with pytest.raises(ValueError, match=r"LLM|工具次数"):
        _validator().validate(
            AutomationScript.model_validate(payload),
            _provenance(),
            now_utc=datetime(2026, 7, 27, tzinfo=UTC),
        )
