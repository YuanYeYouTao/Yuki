"""The new send name keeps its original authority and old receipt facts."""

import json

import pytest
from tests.support.social_identity_cases import social_env
from tests.support.work_effect_results_helpers import owned_session
from tests.support.work_session import invoke_tool

from yuki.capabilities.media import MediaResultText
from yuki.domain.messages import ToolCall, ToolFunction
from yuki.social.models import SocialError


@pytest.mark.asyncio
async def test_retired_send_name_cannot_create_a_new_social_delivery(database, tmp_path):
    env = await social_env(database, tmp_path)
    before = list(env.bot.calls)
    with pytest.raises(SocialError, match="unknown_tool"):
        await env.service.execute("send_message", {"text": "retired new call"}, env.context)
    assert env.bot.calls == before


@pytest.mark.asyncio
async def test_old_paid_send_tool_receipt_is_read_with_its_original_name(database, tmp_path):
    control, session, _store = await owned_session(database, tmp_path)
    call = ToolCall("paid-before-rename", ToolFunction("send_message", '{"text":"original"}'))
    executions = 0
    original = '{"ok":true,"data":{"status":"succeeded","operation_id":"original-send"}}'

    async def send_once():
        nonlocal executions
        executions += 1
        return MediaResultText(original)

    accepted = await invoke_tool(session, call, send_once, side_effecting=True)
    key = session.call_key(call.id)
    before = await session.journal.effect_result(key, original_tool="send_message")
    recovered = await session.journal.effect_result(key, original_tool="send_message")
    assert json.loads(before) == json.loads(recovered) == json.loads(accepted)
    assert executions == 1
    assert json.loads(recovered)["data"]["operation_id"] == "original-send"
    assert control.tools_started == 1
