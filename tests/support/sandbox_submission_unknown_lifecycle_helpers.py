"""Unconfirmed sandbox dispatch stays unknown through the real Host tool chain."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tests.conftest import build_harness, make_settings
from tests.unit.test_tool_effect_audit import active_work

from qq_ai_bot.capabilities.coordinator import ToolInvocationCoordinator
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.runtime.work_activation import current_work_control
from qq_ai_bot.sandbox.client import SandboxClient
from qq_ai_bot.sandbox.task_repository import SandboxTaskRepository
from qq_ai_bot.services.main_agent_backend import MainAgentBackend


def socket_receipts(monkeypatch, responses):
    """Only transport is fake; no socket, model or terminal process is opened."""
    wire = []
    replies = iter(responses)

    class Writer:
        def write(self, data):
            wire.append(json.loads(data))

        async def drain(self):
            pass

        def close(self):
            pass

        async def wait_closed(self):
            pass

    async def connect(*args, **kwargs):
        reply = next(replies)
        read = (
            AsyncMock(side_effect=reply)
            if isinstance(reply, Exception)
            else AsyncMock(return_value=json.dumps(reply).encode() + b"\n")
        )
        return SimpleNamespace(readline=read), Writer()

    monkeypatch.setattr(asyncio, "open_unix_connection", connect, raising=False)
    return wire


async def host_case(database, tmp_path):
    env, work, tool_runtime = await active_work(database, tmp_path)
    chat = build_harness(database, make_settings(database.url)).processor._chat
    tasks = SandboxTaskRepository(database)
    chat._tools.sandbox_client = SandboxClient(tmp_path / "never-opened.sock", tasks=tasks)
    inbound = InboundMessage(
        message_id="offline-rpc-fixture",
        event_type="message:test",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity("10001"),
        text="offline",
        bot_user_id="80001",
        group_id="20001",
        person_id=env.person,
        space_id=env.space,
        conversation_id=env.context.conversation_id,
    )
    tool_runtime = replace(
        tool_runtime,
        inbound=inbound,
        actor_context=None,
        origin=TurnOrigin.USER_MESSAGE,
        space_id=env.space,
        runtime_config=await chat._runtime_config.snapshot(),
    )
    backend = MainAgentBackend(chat, tool_runtime)
    runtime = SimpleNamespace(
        work_control=work.control, origin=TurnOrigin.USER_MESSAGE, delegated_authority=None
    )
    await backend.prepare()
    backend.definitions(runtime, web_was_used=False)
    return work, tasks, backend, runtime


async def invoke(work, backend, runtime, call):
    # The experimental backend binds the original Host Invocation directly;
    # it has no mutable name/argument batch cache. All wire/receipt assertions stay.
    token = current_work_control.set(work.control)
    try:
        return await ToolInvocationCoordinator().execute_batch(
            (call,), backend, runtime, remaining_calls=1, max_parallel_calls=1
        )
    finally:
        current_work_control.reset(token)
