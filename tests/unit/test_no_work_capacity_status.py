"""Real Processor capacity stops keep Social facts and never dispatch over budget."""

import json
from copy import copy
from dataclasses import replace

import httpx
import pytest
from sqlalchemy import select
from tests.conftest import MemorySender, build_harness, make_settings
from tests.support.fixed_contract_fixture import bind_main_contract
from tests.support.social_identity_cases import social_env
from tests.unit.test_commands_and_chat import inbound

from qq_ai_bot.conversation.hydrate import require_primary_alias_for_conversation
from qq_ai_bot.llm.openai_compatible import OpenAICompatibleProvider
from qq_ai_bot.model_runtime.capacity import ModelCapacity, estimate_request_tokens
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.prompting.compiler import PromptCapacityError, PromptCompiler
from qq_ai_bot.prompting.models import (
    PromptChannel,
    PromptContribution,
    PromptProgram,
    PromptTrust,
)
from qq_ai_bot.runtime.activation_outcome import classify_failure, failure_status_text
from qq_ai_bot.runtime.work_repository import WorkCapacityError
from qq_ai_bot.runtime.work_schema_v1 import work


@pytest.mark.asyncio
@pytest.mark.parametrize("after_send", [False, True])
async def test_processor_reports_real_capacity_stop_without_replay_or_new_work(
    database, tmp_path, monkeypatch, after_send
):
    env = await social_env(database, tmp_path)
    async with database.sessions() as reader:
        primary_alias = await require_primary_alias_for_conversation(
            reader, env.context.conversation_id
        )
    window = 96000
    input_budget = window if after_send else 8192
    estimates = []
    http_calls = []

    def measured(request):
        tokens = estimate_request_tokens(request)
        estimates.append(tokens)
        return tokens

    monkeypatch.setattr("qq_ai_bot.services.agent_runner.estimate_request_tokens", measured)

    def transport(request):
        # Actual HTTP transport sees only requests admitted by the real guard.
        body = json.loads(request.content)
        http_calls.append(body)
        if len(http_calls) == 2:
            # A tool-free repair may be admitted before the hard stop. An
            # invalid summary must retain the delivered receipt and stop,
            # rather than replaying the first Social call.
            assert after_send and not body.get("tools")
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "invalid summary"},
                            "finish_reason": "stop",
                        }
                    ]
                },
            )
        assert len(http_calls) == 1 and estimates[-1] <= input_budget
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            # A deliberately large private tool tail exercises real
                            # request growth without sending that content to QQ.
                            "content": "x" * 300000,
                            "tool_calls": [
                                {
                                    "id": "confirmed-original-call",
                                    "type": "function",
                                    "function": {
                                        "name": "send_message",
                                        "arguments": json.dumps({"text": "已发送的原结果"}),
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        )

    async with httpx.AsyncClient(
        base_url="https://capacity.invalid/v1/", transport=httpx.MockTransport(transport)
    ) as client:
        provider = OpenAICompatibleProvider(
            base_url="https://capacity.invalid/v1",
            api_key="unused-test-key",
            timeout_seconds=2,
            max_retries=0,
            client=client,
        )
        harness = build_harness(
            database,
            make_settings(
                database.url,
                runtime_work_enabled=True,
                enabled_groups_csv="20001",
                context_window_tokens=window,
            ),
            provider,
        )
        bind_main_contract(harness, tmp_path)
        if not after_send:
            # Isolate the first-dispatch guard from history planning: the
            # connection ceiling is smaller than the assembled neutral request.
            models = copy(harness.processor._chat.runtime.runner._models)
            monkeypatch.setattr(
                models, "capacity", lambda _task: ModelCapacity(input_tokens=input_budget)
            )
            harness.processor._chat.runtime.runner._models = models
        harness.processor._chat._tools.social_service = env.service
        message = replace(
            inbound(
                "检查并告诉我",
                message_id=f"capacity-after-send-{after_send}",
                user_id="10001",
                group_id="20001",
                mentions_bot=True,
            ),
            bot_user_id="80001",
            conversation_id=env.context.conversation_id,
            legacy_conversation_key=primary_alias,
            person_id=env.person,
            space_id=env.space,
            presence_id=env.presence,
        )
        sender = MemorySender()
        result = await harness.processor.handle(message, sender)

    assert result.reason == "capacity_failure"
    assert result.sent_messages == 1
    assert len(sender.messages) == 1
    status = sender.messages[0].text
    assert "容量限制" in status
    if not after_send:
        assert "上下文超过容量限制" in status and "本次请求未完整完成" in status
    assert "已有结果会保留" in status
    assert "内部错误" not in status and "unused-test-key" not in status
    assert len(http_calls) == (2 if after_send else 0)
    assert len(estimates) >= (2 if after_send else 1)
    assert any(estimate > input_budget for estimate in estimates)
    assert sum(action == "send_group_msg" for action, _ in env.bot.calls) == int(after_send)
    async with database.sessions() as reader:
        original = (
            await reader.scalars(
                select(ChatEventModel).where(
                    ChatEventModel.canonical_conversation_id == env.context.conversation_id,
                    ChatEventModel.direction == "outbound",
                    ChatEventModel.content == "已发送的原结果",
                )
            )
        ).all()
        works = (await reader.execute(select(work))).mappings().all()
    assert len(original) == int(after_send)
    assert not works


def test_capacity_status_reuses_classification_without_leaking_private_exception_text():
    failure = classify_failure(WorkCapacityError("private storage detail: secret"))
    assert failure.stage == "capacity" and not failure.retryable
    status = failure_status_text(failure)
    assert "容量限制" in status and "secret" not in status
    assert "内部错误" not in status


@pytest.mark.parametrize("case", ["required_capacity", "negative_budget", "duplicate"])
def test_only_required_dynamic_capacity_is_typed_and_reported_as_capacity(case):
    contribution = PromptContribution(
        id="required-dynamic",
        channel=PromptChannel.CONTEXT,
        trust=PromptTrust.UNTRUSTED,
        content="private material: secret",
        required=True,
    )
    program = PromptProgram(
        contributions=(contribution, contribution) if case == "duplicate" else (contribution,)
    )
    with pytest.raises(ValueError) as caught:
        PromptCompiler().compile(
            program, dynamic_character_budget=-1 if case == "negative_budget" else 0
        )
    failure = classify_failure(caught.value)
    status = failure_status_text(failure)
    if case == "required_capacity":
        assert type(caught.value) is PromptCapacityError
        assert failure.code == "prompt_dynamic_capacity" and failure.stage == "capacity"
        assert not failure.retryable
        assert "上下文超过容量限制" in status and "本次请求未完整完成" in status
    else:
        assert type(caught.value) is ValueError
        assert failure.code == "ValueError" and failure.stage == "activation"
        assert "内部错误" in status
    assert "secret" not in status
