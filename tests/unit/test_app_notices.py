"""Pre-Main App notices keep the original input and a real local delivery receipt."""

import json
from uuid import uuid4

import pytest
from sqlalchemy import select
from tests.unit.test_admin_api import admin as admin_fixture
from tests.unit.test_app_messages import app_env as app_fixture
from tests.unit.test_app_messages import drain
from tests.unit.test_client_api import bearer, pair, person_and_code

from yuki.conversation.canonical_db_models import ControlCommandReceiptModel
from yuki.persistence.models import ChatEventModel
from yuki.services.rate_limit import SlidingWindowRateLimiter
from yuki.social.db_models import SocialOperationModel

admin = admin_fixture
app_env = app_fixture


@pytest.mark.parametrize("notice", ["overlong", "rate_limited"])
async def test_pre_main_app_notice_records_original_event_and_replays_without_resending(
    app_env, database, monkeypatch, notice
):
    env = app_env
    person, code = await person_and_code(env.client)
    device = await pair(env.client, code)
    principal = await env.identity.authenticate(device["token"])
    headers = bearer(device["token"])
    too_long = "長" * (env.harness.settings.max_input_characters + 1)
    text, expected, outcome = (
        too_long,
        f"消息过长，请控制在 {env.harness.settings.max_input_characters} 个字符以内。",
        "input_too_long",
    )
    if notice == "rate_limited":
        env.harness.processor._rate_limiter = SlidingWindowRateLimiter(per_user=1, per_group=1)
        consumed = await env.client.post(
            "/client/v1/messages",
            headers=headers,
            json={"request_id": str(uuid4()), "text": too_long, "attachments": []},
        )
        assert consumed.status_code == 200, consumed.text
        await drain(env)
        text, expected, outcome = (
            "仍在原 App 入口",
            "请求过于频繁，请稍后再试。",
            "user_rate_limited",
        )
    appended_sources = []
    append = env.social.writer.append

    async def append_original_notice(**arguments):
        appended_sources.append(arguments)
        return await append(**arguments)

    monkeypatch.setattr(env.social.writer, "append", append_original_notice)
    request = {"request_id": str(uuid4()), "text": text, "attachments": []}
    response = await env.client.post("/client/v1/messages", headers=headers, json=request)
    assert response.status_code == 200, response.text
    original = response.json()["data"]
    await drain(env)
    assert not env.calls
    async with database.read_session() as reader:
        inbound = await reader.get(ChatEventModel, original["event_id"])
        assert inbound is not None and inbound.content == text
        assert inbound.author_person_id == person and inbound.direction == "inbound"
        operation = await reader.scalar(
            select(SocialOperationModel).where(
                SocialOperationModel.source_turn_id
                == f"app:{principal.binding_id}:{request['request_id']}",
                SocialOperationModel.tool_call_id == "reply:1",
            )
        )
        assert operation is not None and operation.status == "succeeded"
        assert operation.event_id is not None
        outbound = await reader.get(ChatEventModel, operation.event_id)
        assert outbound is not None and outbound.content == expected
        assert outbound.direction == "outbound" and outbound.author_kind == "yuki"
        assert outbound.caused_by_event_id is None
        assert outbound.canonical_conversation_id == inbound.canonical_conversation_id
        assert outbound.author_presence_id == inbound.ingress_presence_id
        assert operation.original_event_id == outbound.id
        assert operation.original_conversation_id == inbound.canonical_conversation_id
        command = await reader.scalar(
            select(ControlCommandReceiptModel).where(
                ControlCommandReceiptModel.request_id == request["request_id"],
                ControlCommandReceiptModel.request_person_id == principal.request_person_id,
                ControlCommandReceiptModel.request_conversation_id
                == principal.request_conversation_id,
            )
        )
        assert command is not None and command.operation_kind == "client.message"
        state = json.loads(command.effective_state_json)
        assert state == {
            "event_id": inbound.id,
            "conversation_id": inbound.canonical_conversation_id,
            "outcome": outcome,
            "handled": True,
            "sent_messages": 1,
            "error_category": None,
        }
        outbound_id = outbound.id
    assert appended_sources
    assert all(item["source_event_id"] == original["event_id"] for item in appended_sources)
    assert all(item["caused_by_event_id"] is None for item in appended_sources)
    receipt = await env.client.get(f"/client/v1/requests/{request['request_id']}", headers=headers)
    assert receipt.status_code == 200, receipt.text
    assert receipt.json()["data"]["result_event_ids"] == [outbound_id]
    assert receipt.json()["data"]["event_id"] == original["event_id"]
    replay = await env.client.post("/client/v1/messages", headers=headers, json=request)
    assert replay.status_code == 200 and replay.json()["data"] == {**original, "replay": True}
    await drain(env)
    assert not env.calls
    after = await env.client.get(f"/client/v1/requests/{request['request_id']}", headers=headers)
    assert after.json() == receipt.json()
    async with database.read_session() as reader:
        operations = list(
            await reader.scalars(
                select(SocialOperationModel).where(
                    SocialOperationModel.source_turn_id
                    == f"app:{principal.binding_id}:{request['request_id']}"
                )
            )
        )
        assert [(item.tool_call_id, item.event_id) for item in operations] == [
            ("reply:1", outbound_id)
        ]
