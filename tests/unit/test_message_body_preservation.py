"""Canonical bodies keep ordinary text; the actual state tail remains a protocol."""

from datetime import UTC, datetime

import pytest

from yuki.domain.conversations import ScopeType
from yuki.event_prompt import ChatEventPromptRenderer
from yuki.llm.base import LLMEmptyResponseError
from yuki.persistence.repository_records import EventRecord
from yuki.services.renderer import sanitize_model_output


@pytest.mark.parametrize(
    "body",
    ["#473 修复说明", "明天 [12:34] 见", "```python\n#123 is a comment\nx = 1\n```"],
)
def test_output_preserves_issue_references_times_and_code(body):
    assert sanitize_model_output(body) == body


@pytest.mark.parametrize("body", ["#473 修复说明", "明天 [12:34] 见"])
def test_history_preserves_body_inside_its_structured_event_envelope(body):
    row = EventRecord(
        id=91,
        bot_user_id="8000",
        platform_message_id="transport-91",
        scope_type=ScopeType.GROUP,
        sender_user_id="1001",
        direction="inbound",
        content=body,
        visual_summary="",
        segments=(),
        occurred_at=datetime(2026, 10, 20, tzinfo=UTC),
        group_id="2001",
        canonical_conversation_id="original-conversation",
        author_kind="person",
        author_person_id="original-person",
    )
    rendered = ChatEventPromptRenderer((row,)).render_reference_event(row)
    assert rendered.endswith(f"#91>{body}")


def test_state_tail_and_control_character_cleanup_remain_separate_from_body():
    assert sanitize_model_output('好\x00呀\r\n<yuki-state>{"engage":"stay"}</yuki-state>') == "好呀"


@pytest.mark.parametrize("body", ["", " \n\x00", '<yuki-state>{"engage":"stay"}</yuki-state>'])
def test_actual_empty_output_is_still_empty(body):
    with pytest.raises(LLMEmptyResponseError):
        sanitize_model_output(body)
