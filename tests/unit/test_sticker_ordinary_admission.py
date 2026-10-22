"""Ordinary Main captures are execution evidence without inventing an owned Work."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, update
from tests.support.agent_backend import StubAgentBackend
from tests.support.social_identity_cases import social_env
from tests.support.sticker_delivery import make_delivery, settle

from yuki.capabilities.coordinator import ToolInvocationCoordinator
from yuki.domain.messages import ChatMessage, ToolCall, ToolFunction
from yuki.runtime.effect_outcomes import current_result_capture
from yuki.runtime.work_activation import current_work_control
from yuki.runtime.work_control import WorkControl
from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_schema_v1 import effects, scope, work
from yuki.runtime.work_session import WorkSession
from yuki.services.turn_transcript import TurnTranscript
from yuki.social.models import OperationStatus


async def activation(env):
    repository = WorkRepository(env.db)
    first = await repository.acquire(env.context.conversation_id, 1)
    await repository.release(first)
    async with env.db.immediate_session() as writer:
        await writer.execute(
            update(scope)
            .where(scope.c.conversation_id == env.context.conversation_id)
            .values(cancel_epoch=11)
        )
    lease = await repository.acquire(env.context.conversation_id, 1)
    assert lease.cancel_epoch == 11
    source = {"origin": "user_message", "actor_person_id": env.person, "principal_kind": "person"}

    async def validate():
        assert await repository.valid(lease)

    control = WorkControl(repository, lease, "original-message", source, validate)
    control.session = WorkSession(control, "sticker-admission")
    await control.session.restore(TurnTranscript((ChatMessage("user", "配个表情"),)))
    return control


async def call_through_main(env, control, arguments):
    captures, contexts = [], []

    class Backend(StubAgentBackend):
        async def execute_call(self, invocation):
            capture = current_result_capture.get()
            assert capture is not None
            captures.append(capture.work_id)
            context = replace(env.context, call_id=invocation.identity.operation_id)
            contexts.append(context)
            receipt = await env.service.execute(
                "send", json.loads(invocation.call.function.arguments), context
            )
            return json.dumps({"ok": True, "data": receipt})

    token = current_work_control.set(control)
    try:
        result = await ToolInvocationCoordinator().execute_batch(
            (ToolCall("original-message", ToolFunction("send", json.dumps(arguments))),),
            Backend(),
            SimpleNamespace(work_control=control, execution_id=control.lease.owner),
            remaining_calls=None,
            max_parallel_calls=1,
        )
    finally:
        current_work_control.reset(token)
    assert len(result.calls) == 1 and result.calls[0][2]
    return json.loads(result.calls[0][1])["data"], contexts[0], captures


def sends(env):
    return [(method, args) for method, args in env.bot.calls if method.startswith("send_")]


@pytest.mark.parametrize("text", ["正文先发", ""])
async def test_ordinary_main_admits_and_delivers_without_registering_a_work(
    database, tmp_path, text
):
    env = await social_env(database, tmp_path)
    delivery, library = make_delivery(env)
    control = await activation(env)
    arguments = {"text": text, "emoji": {"goal": "开心"}}
    result, context, captures = await call_through_main(env, control, arguments)
    await control.repository.release(control.lease)
    assert captures == [""]  # The actual coordinator/InvocationService ordinary route.
    assert result["emoji"]["status"] == "accepted", result
    assert result["status"] == ("succeeded" if text else "accepted")
    assert len(sends(env)) == bool(text)
    intent = await delivery.intents.find(context.turn_id, context.call_id)
    assert intent is not None and intent.source["work_id"] is None
    assert intent.source["cancel_epoch"] == 11
    assert intent.source["work_cancel_epoch"] is None
    async with database.read_session() as reader:
        assert not await reader.scalar(select(func.count()).select_from(work))
        assert not await reader.scalar(select(func.count()).select_from(effects))
    await delivery.start()
    try:
        delivered = await settle(delivery, context)
        assert delivered["status"] == "succeeded"
        assert len(sends(env)) == 1 + bool(text)
        replay = await env.service.execute("send", arguments, context)
        assert replay["emoji"]["status"] == "succeeded"
        assert len(sends(env)) == 1 + bool(text) and library.select_calls == 1
        if text:
            original = await env.service.receipts.get(result["operation_id"])
            assert original.status is OperationStatus.SUCCEEDED
    finally:
        await delivery.close()
