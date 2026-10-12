"""A paired Social receipt replaces only its own same-turn ledger echo."""

import json
from dataclasses import replace

import httpx
import pytest
from sqlalchemy import select
from tests.conftest import MemorySender
from tests.unit.test_current_activation_material import _saved
from tests.unit.test_history_dispatch_ownership import _scene

from qq_ai_bot.capabilities.results import ToolExecutionResult
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.event_prompt import ChatEventPromptRenderer
from qq_ai_bot.llm.gemini import GeminiProvider
from qq_ai_bot.persistence.event_repository import EventLedgerRepository
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.runtime.effect_outcomes import execution_evidence


def _gemini_reply(send_text=None):
    part = {"text": "内部结束，不再发送。"}
    if send_text is not None:
        part = {
            "functionCall": {
                "name": "send_message",
                "args": {"text": send_text},
                "id": "actual-boundary-send",
            },
            "thoughtSignature": "original-send-signature",
        }
    return {
        "candidates": [{"finishReason": "STOP", "content": {"role": "model", "parts": [part]}}],
        "usageMetadata": {"promptTokenCount": 20, "candidatesTokenCount": 5, "totalTokenCount": 25},
    }


def _plain_text(body):
    return "\n".join(part.get("text", "") for item in body["contents"] for part in item["parts"])


@pytest.mark.parametrize("interleaved_events", ["none", "outbound_and_inbound", "outbound_only"])
async def test_main_gemini_continues_after_send_without_repeating_its_ledger_echo(
    database, tmp_path, monkeypatch, interleaved_events
):
    wire = []
    sent_text = "本轮已经发送的真实结果。"
    foreign_text = "另一个执行刚刚发送的真实消息。"
    incoming_text = "并发到达的普通群消息。"

    async def transport(request):
        body = json.loads(request.content)
        wire.append(body)
        if body["contents"][-1]["role"] == "model":
            return httpx.Response(400, json={"error": {"message": "Requests ending with model"}})
        return httpx.Response(200, json=_gemini_reply(sent_text if len(wire) == 1 else None))

    async with httpx.AsyncClient(
        base_url="https://wire.invalid/", transport=httpx.MockTransport(transport)
    ) as client:
        provider = GeminiProvider(
            base_url="https://wire.invalid",
            api_key="synthetic",
            timeout_seconds=1,
            max_retries=0,
            client=client,
        )
        env, harness, chat, _, message = await _scene(database, tmp_path, provider)
        chat._tools.social_service = env.service
        sender = MemorySender()
        execute = env.service.execute
        interleaved = []

        async def send_with_concurrent_events(name, args, context):
            receipt = await execute(name, args, context)
            if interleaved_events != "none" and name == "send_message" and not interleaved:
                interleaved.append(True)
                other = await env.service.writer.append(
                    scope=ConversationScope.group("80001", "20001"),
                    platform_message_id="other-execution-outbound",
                    sender_user_id="80001",
                    direction="outbound",
                    sender_is_bot=True,
                    origin="social_tool",
                    content=foreign_text,
                )
                interleaved.append(other.event.id)
                if interleaved_events == "outbound_and_inbound":
                    observed = await harness.processor.handle(
                        replace(
                            message,
                            message_id="concurrent-ordinary-inbound",
                            text=incoming_text,
                            mentions_bot=False,
                        ),
                        MemorySender(),
                    )
                    assert observed.reason == "group_observed"
            return receipt

        monkeypatch.setattr(env.service, "execute", send_with_concurrent_events)
        result = await harness.processor.handle(message, sender)
        assert result.reason == "chat" and len(wire) == 2
        assert sum(action == "send_group_msg" for action, _ in env.bot.calls) == 1
        assert not sender.messages
        continuation = wire[1]["contents"]
        calls = [part for item in continuation for part in item["parts"] if "functionCall" in part]
        assert calls == [_gemini_reply(sent_text)["candidates"][0]["content"]["parts"][0]]
        responses = [
            part["functionResponse"]
            for item in continuation
            for part in item["parts"]
            if "functionResponse" in part
        ]
        assert len(responses) == 1 and responses[0]["id"] == "actual-boundary-send"
        receipt = json.loads(responses[0]["response"]["output"])
        assert receipt["ok"] and receipt["data"]["status"] == "succeeded"
        assert sent_text in responses[0]["response"]["output"]
        assert sent_text not in _plain_text(wire[1])
        assert continuation[-1]["role"] == "user"
        saved = await _saved(database)
        assert sent_text not in saved.payload_json
        if interleaved_events != "none":
            record = await EventLedgerRepository(database).get_event(interleaved[1])
            original = ChatEventPromptRenderer([record]).reference_message(record)
            assert original.role == "assistant"
            assert f"#{record.id}>" in original.content and "QQ:80001" in original.content
            observations = [
                item
                for item in continuation
                if any(part.get("text") == original.content for part in item["parts"])
            ]
            assert len(observations) == 1 and observations[0]["role"] == "user"
            assert foreign_text in saved.payload_json
        if interleaved_events == "outbound_and_inbound":
            assert incoming_text in _plain_text(wire[1]) and incoming_text in saved.payload_json

        async with database.sessions() as reader:
            delivered = list(
                await reader.scalars(
                    select(ChatEventModel).where(
                        ChatEventModel.direction == "outbound", ChatEventModel.content == sent_text
                    )
                )
            )
        assert len(delivered) == 1 and delivered[0].platform_message_id
        # A later independent activation still reads the permanent outbound fact.
        next_result = await harness.processor.handle(
            replace(message, message_id="later-independent-inbound", text="随后继续这段聊天。"),
            sender,
        )
        assert next_result.reason == "chat" and len(wire) == 3
        assert sent_text in _plain_text(wire[-1])
        assert sum(action == "send_group_msg" for action, _ in env.bot.calls) == 1


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"status": "succeeded", "event_id": 101}, [101]),
        (
            {
                "status": "failed",
                "parts": [
                    {"status": "succeeded", "event_id": 102},
                    {"status": "failed", "event_id": 103},
                    {"status": "uncertain", "event_id": 104},
                ],
            },
            [102],
        ),
        (
            {
                "status": "failed",
                "file": {"status": "succeeded", "event_id": 105},
                "caption": {"status": "succeeded", "event_id": 106},
            },
            [105, 106],
        ),
    ],
)
def test_typed_send_receipts_preserve_only_confirmed_internal_events(data, expected):
    outcome = ToolExecutionResult(ok=data["status"] == "succeeded", data=data)
    fact = execution_evidence(outcome, tool="send_message", side_effecting=True)
    assert fact["observed_event_ids"] == expected
    read = execution_evidence(outcome, tool="get_recent_chat_history", side_effecting=False)
    assert "observed_event_ids" not in read
