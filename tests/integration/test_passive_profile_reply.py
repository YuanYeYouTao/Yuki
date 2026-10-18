"""Real native mentions reply without remote display-metadata enrichment."""

import json

import pytest
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment
from nonebot.adapters.onebot.v11.event import Sender
from sqlalchemy import select
from tests.integration.test_current_material_main_contract import _deployment

from yuki.adapters.onebot.normalizer import normalize_event
from yuki.domain.messages import ChatResponse, ToolCall, ToolFunction
from yuki.identity.db_models import CanonicalPersonModel, IdentityBindingModel
from yuki.llm.fake import FakeLLMProvider
from yuki.persistence.models import ChatEventModel, MembershipModel, PersonAliasModel
from yuki.social.db_models import SocialOperationModel


def _mention(identifier, **sender_fields):
    body = Message([MessageSegment.at(80001), MessageSegment.text(" 请回复这条消息")])
    return normalize_event(
        GroupMessageEvent(
            time=1,
            self_id=80001,
            post_type="message",
            sub_type="normal",
            user_id=10001,
            message_type="group",
            message_id=identifier,
            message=body,
            original_message=body,
            raw_message=str(body),
            font=0,
            sender=Sender(user_id=10001, **sender_fields),
            group_id=20001,
        )
    )


async def _reply_environment(database, tmp_path):
    def respond(_request):
        if len(provider.requests) % 2:
            return ChatResponse(
                "",
                0,
                tool_calls=(
                    ToolCall("reply", ToolFunction("send_message", json.dumps({"text": "收到"}))),
                ),
            )
        return "NO_REPLY"

    provider = FakeLLMProvider(respond)
    app, env, _message, sender = await _deployment(database, tmp_path, provider)
    original_call = env.bot.call_api
    env.bot.calls.clear()
    env.native_api_attempts = []

    async def native_call(action, **parameters):
        env.native_api_attempts.append((action, parameters))
        if action == "send_group_msg":
            assert parameters["group_id"] == 20001
        else:
            # A route may prove the original Bot is a member; this is not a profile read.
            assert action == "get_group_member_info"
            assert parameters == {"group_id": 20001, "user_id": 80001, "no_cache": True}
        return await original_call(action, **parameters)

    env.bot.call_api = native_call
    return app, env, sender, provider


@pytest.mark.parametrize("sender_fields", [{}, {"nickname": "平台昵称", "card": "群内名片"}])
async def test_native_mention_reaches_first_reply_without_remote_profile_reads(
    database, tmp_path, sender_fields
):
    app, env, sender, provider = await _reply_environment(database, tmp_path)
    message = _mention(301, **sender_fields)
    try:
        result = await app.processor.handle(message, sender)

        assert result.reason == "chat" and result.sent_messages == 1
        assert len(provider.requests) == 2
        assert env.native_api_attempts == env.bot.calls
        sends = [parameters for action, parameters in env.bot.calls if action == "send_group_msg"]
        assert len(sends) == 1 and sends[0]["message"] == [
            {"type": "text", "data": {"text": "收到"}}
        ]
        async with database.sessions() as reader:
            original = await reader.scalar(
                select(ChatEventModel).where(
                    ChatEventModel.platform_message_id == message.message_id
                )
            )
            assert original is not None and original.author_person_id == env.person
            assert original.sender_nickname == sender_fields.get("nickname", "")
            assert original.sender_group_card == sender_fields.get("card", "")
            assert await reader.get(MembershipModel, (env.person, env.space)) is not None
            receipt = (await reader.scalars(select(SocialOperationModel))).one()
            assert receipt.status == "succeeded" and receipt.presence_id == env.presence
            assert receipt.event_id is not None and receipt.platform_reference
        first = provider.requests[0]
        assert any(message.text in item.content for item in first.messages if item.role == "user")
    finally:
        assert not await app.lifecycle._close_entries(app.lifecycle._entries)


async def test_missing_profile_fields_preserve_observation_and_do_not_change_fixed_main_contract(
    database, tmp_path
):
    app, env, sender, provider = await _reply_environment(database, tmp_path)
    try:
        async with database.sessions() as reader:
            original_person_name = (await reader.get(CanonicalPersonModel, env.person)).display_name
        for identifier, fields, expected_card in (
            (401, {"nickname": "旧昵称", "card": "原名片"}, "原名片"),
            (402, {}, "原名片"),
            (403, {"nickname": "新昵称", "card": ""}, ""),
        ):
            result = await app.processor.handle(_mention(identifier, **fields), sender)
            assert result.reason == "chat" and result.sent_messages == 1
            async with database.sessions() as reader:
                membership = await reader.get(MembershipModel, (env.person, env.space))
                assert membership is not None and membership.group_card == expected_card
                person = await reader.get(CanonicalPersonModel, env.person)
                assert person is not None and person.display_name == original_person_name

        async with database.sessions() as reader:
            binding = await reader.scalar(
                select(IdentityBindingModel).where(IdentityBindingModel.person_id == env.person)
            )
            assert binding.display_name == "新昵称"
            aliases = set(
                await reader.scalars(
                    select(PersonAliasModel.alias).where(
                        PersonAliasModel.canonical_person_id == env.person
                    )
                )
            )
            assert {"旧昵称", "新昵称", "原名片"} <= aliases
            original_event = await reader.scalar(
                select(ChatEventModel).where(ChatEventModel.platform_message_id == "401")
            )
            assert original_event.sender_nickname == "旧昵称"
            assert original_event.sender_group_card == "原名片"
        initial_requests = provider.requests[::2]
        assert len(initial_requests) == 3
        first = initial_requests[0]
        system = tuple(item for item in first.messages if item.role == "system")
        assert system and first.tools
        for request in initial_requests[1:]:
            assert tuple(item for item in request.messages if item.role == "system") == system
            assert request.tools == first.tools
        assert sum(action == "send_group_msg" for action, _parameters in env.bot.calls) == 3
        assert env.native_api_attempts == env.bot.calls
    finally:
        assert not await app.lifecycle._close_entries(app.lifecycle._entries)
