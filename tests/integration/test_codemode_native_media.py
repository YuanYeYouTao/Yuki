"""Original private pixels cross composition settlement and segment recovery."""

import json

import pytest
from sqlalchemy import select
from tests.integration.test_codemode_composition import _wrap
from tests.integration.test_codemode_runner import ACCEPT, Backend, call, runner_env
from tests.support.codemode_cases import effect_rows, environment, outer_call, requires_worker

from qq_ai_bot.capabilities.media import MediaResultText, result_images
from qq_ai_bot.codemode.driver import CodeCompositionYield, CodeModeDriver
from qq_ai_bot.domain.messages import ChatImage, ChatMessage, ChatResponse
from qq_ai_bot.execution_trace.db_models import ExecutionTraceStateModel
from qq_ai_bot.model_runtime.models import ModelCapability
from qq_ai_bot.runtime.work_repository import WorkConflict
from qq_ai_bot.runtime.work_schema_v1 import effects

pytestmark = requires_worker
IMAGE = ChatImage("data:image/png;base64,b3JpZ2luYWw=", source="tool", tool_handle="original")


@pytest.mark.parametrize("segmented", [False, True])
async def test_children_and_parent_restore_original_pixels_without_redispatch(
    database, tmp_path, segmented
):
    env = await environment(database, tmp_path, tool_limit=1 if segmented else 4)

    async def domain(name, arguments):
        text = await env.domain(name, arguments)
        return MediaResultText(text, (IMAGE,))

    env.host.execute_business = _wrap(env, domain)
    outer = outer_call(
        env, "a = await yuki_lookup({'q': 1})\nb = await yuki_lookup({'q': 2})\n[a['ok'], b['ok']]"
    )
    if segmented:
        with pytest.raises(CodeCompositionYield):
            await CodeModeDriver(env.host, outer).run()
        env.control.tools_started = 0
    receipt = await CodeModeDriver(env.host, outer).run()
    assert result_images(receipt) == (IMAGE,)
    assert json.loads(receipt)["result"] == [True, True]
    replay = await CodeModeDriver(env.host, outer).resume()
    assert replay == receipt and result_images(replay) == (IMAGE,)
    assert env.domain.log == [("lookup", {"q": 1}), ("lookup", {"q": 2})]
    rows, tools, root = await effect_rows(database, env.control.current["id"])
    assert tools == root == 2
    assert all("data:image/" not in row["receipt_json"] for row in rows.values())
    assert "media_result_ref" in json.loads(rows[outer.identity.operation_id]["receipt_json"])


async def test_erasure_after_child_acceptance_cannot_republish_pixels_on_parent(
    database, tmp_path, monkeypatch
):
    env = await environment(database, tmp_path)

    async def domain(name, arguments):
        return MediaResultText(await env.domain(name, arguments), (IMAGE,))

    env.host.execute_business = _wrap(env, domain)
    outer = outer_call(env, "await yuki_lookup({'q': 1})")
    original = env.owner.journal.record_effect

    async def erase_before_parent(key, *args, **kwargs):
        if key == outer.identity.operation_id:
            async with database.sessions() as writer, writer.begin():
                writer.add(ExecutionTraceStateModel(id=1, privacy_generation=1))
        await original(key, *args, **kwargs)

    monkeypatch.setattr(env.owner.journal, "record_effect", erase_before_parent)
    with pytest.raises(WorkConflict, match="work_effect_media_source_changed"):
        await CodeModeDriver(env.host, outer).run()
    assert env.domain.log == [("lookup", {"q": 1})]
    async with database.sessions() as reader:
        parent = await reader.scalar(
            select(effects.c.receipt_json).where(
                effects.c.effect_key == outer.identity.operation_id
            )
        )
    assert "media_result_ref" not in json.loads(parent)


async def test_runner_observes_code_child_pixels_after_outer_receipt(database, tmp_path):
    responses = iter(
        [
            call("task_control", ACCEPT, "accept"),
            call("execute_code", {"code": "await yuki_lookup({'q': 1})"}, "code"),
            ChatResponse("done", 0),
        ]
    )
    chat, provider, control, runtime, _ = await runner_env(database, tmp_path, responses)
    chat.runtime.runner._models.capabilities = lambda _: frozenset({ModelCapability.IMAGE_INPUT})

    class MediaBackend(Backend):
        async def execute_call(self, invocation):
            return MediaResultText(await super().execute_call(invocation), (IMAGE,))

        async def validate_images(self, images, runtime):
            assert images == (IMAGE,)

    backend = MediaBackend()
    await chat.runtime.runner.run((ChatMessage("user", "select pixels"),), runtime, backend)
    messages = provider.requests[-1].messages
    assert [image for message in messages for image in message.images] == [IMAGE]
    paired = next(i for i, message in enumerate(messages) if message.tool_call_id == "code")
    observation = next(i for i, message in enumerate(messages) if message.images)
    assert paired < observation
    assert len(backend.log) == control.tools_started == 1
