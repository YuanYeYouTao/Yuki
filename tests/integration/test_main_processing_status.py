"""The shared Main loop owns status across model and tool calls, including failures."""

from contextlib import asynccontextmanager
from dataclasses import replace

import pytest
from tests.conftest import MemorySender, build_harness, make_settings

from yuki.domain.conversations import ScopeType
from yuki.domain.messages import (
    ChatResponse,
    InboundMessage,
    SenderIdentity,
    ToolCall,
    ToolFunction,
)
from yuki.llm.base import LLMError
from yuki.llm.fake import FakeLLMProvider


class RecordingStatus:
    def __init__(self):
        self.active = False
        self.entries = []

    @asynccontextmanager
    async def during(self, **source):
        self.entries.append(source)
        self.active = True
        try:
            yield
        finally:
            self.active = False


def incoming(text="查询一下聊天记录"):
    return InboundMessage(
        ingress_key="main-processing-status",
        native_message_id="main-processing-status",
        event_type="message:private",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity("1001"),
        bot_user_id="8000",
        text=text,
    )


@pytest.mark.parametrize("failure", [False, True])
async def test_status_spans_the_actual_main_loop_and_closes_on_failure(database, failure):
    status = RecordingStatus()

    def respond(request):
        assert status.active
        if failure:
            raise LLMError("offline test failure")
        if len(provider.requests) == 1:
            return ChatResponse(
                "",
                0,
                tool_calls=(
                    ToolCall("read", ToolFunction("query_chat_history", '{"view":"recent"}')),
                ),
            )
        assert any(message.role == "tool" for message in request.messages)
        return "NO_REPLY"

    provider = FakeLLMProvider(respond)
    harness = build_harness(
        database, make_settings(database.url, runtime_work_enabled=False), provider
    )
    harness.processor._chat.runtime.main_turns._processing_status = status
    sender = MemorySender()

    result = await harness.processor.handle(incoming(), sender)

    assert result.reason == ("llm_failure" if failure else "chat")
    assert len(provider.requests) == (1 if failure else 2)
    assert not status.active and len(status.entries) == 1
    source = status.entries[0]
    event = await harness.ledger.get_event(source["source_event_id"])
    assert source["conversation_id"] == event.canonical_conversation_id
    assert source["presence_id"] == event.ingress_presence_id
    assert source["presence_id"] != incoming().bot_user_id
    assert source["group"] is False
    assert not sender.messages


async def test_no_status_for_empty_input_or_help_command(database):
    status = RecordingStatus()
    provider = FakeLLMProvider(lambda _: "NO_REPLY")
    harness = build_harness(database, make_settings(database.url), provider)
    harness.processor._chat.runtime.main_turns._processing_status = status
    sender = MemorySender()

    assert (await harness.processor.handle(incoming(""), sender)).reason == "empty"
    await harness.processor.handle(
        replace(incoming("/ai"), ingress_key="help", native_message_id="help"), sender
    )

    assert not provider.requests and not status.entries and not status.active
