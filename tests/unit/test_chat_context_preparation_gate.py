"""Context preparation cannot monopolize reset/privacy's effect gate."""

import asyncio
from dataclasses import replace

import pytest
from sqlalchemy import select
from tests.conftest import MemorySender, build_harness, make_settings
from tests.support.social_identity_cases import social_env
from tests.unit.test_commands_and_chat import inbound

from qq_ai_bot.conversation.hydrate import bump_canonical_generation
from qq_ai_bot.identity.canonical_uow import CanonicalIngressUnitOfWork
from qq_ai_bot.identity.ingress import CanonicalIngressResolver
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import scope


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "work_enabled,boundary", [(False, "reset"), (True, "reset"), (True, "owner")]
)
async def test_reset_can_win_gate_while_context_preparation_is_waiting(
    database, tmp_path, work_enabled, boundary, monkeypatch
):
    env = await social_env(database, tmp_path)
    provider = FakeLLMProvider("must not dispatch")
    harness = build_harness(
        database,
        make_settings(database.url, runtime_work_enabled=work_enabled, enabled_groups_csv="20001"),
        provider,
    )
    harness.processor._canonical_ingress = CanonicalIngressResolver(
        database, env.registry, env.router
    )
    harness.processor._canonical_uow = CanonicalIngressUnitOfWork(database, env.router)
    chat = harness.processor._chat
    prepared, release = asyncio.Event(), asyncio.Event()
    original = chat._build_messages
    source = {}

    async def wait_for_preparation(*args, **kwargs):
        # Complete a real SQLite/context read, then emulate external preparation
        # waiting before its result can be admitted for a model request.
        result = await original(*args, **kwargs)
        source["inbound"] = args[0]
        source["turn"] = kwargs["turn_snapshot"]
        if work_enabled:
            async with database.sessions() as reader:
                assert (
                    await reader.scalar(
                        select(scope.c.owner).where(
                            scope.c.conversation_id == args[0].conversation_id
                        )
                    )
                    is None
                )
        prepared.set()
        await release.wait()
        return result

    monkeypatch.setattr(chat, "_build_messages", wait_for_preparation)
    sender = MemorySender()
    sender.bot = env.bot
    message = replace(
        inbound(
            "inspect the context",
            message_id="preparation",
            user_id="10001",
            group_id="20001",
            mentions_bot=True,
        ),
        bot_user_id=env.bot.self_id,
    )
    task = asyncio.create_task(harness.processor.handle(message, sender))
    replacement = None
    repository = WorkRepository(database)
    try:
        await asyncio.wait_for(prepared.wait(), timeout=2)
        turn = source["turn"]
        async with chat._effect_gate.hold(turn.scope_key, timeout_seconds=0.2):
            if boundary == "reset":
                async with database.immediate_session() as writer:
                    await bump_canonical_generation(
                        writer,
                        source["inbound"].conversation_id,
                        event_id=turn.trigger_event_id,
                    )
            else:
                replacement = await repository.acquire(source["inbound"].conversation_id, 1)
                assert replacement
                current = await repository.accept(
                    replacement, source_key="other-original-work", source={}, goal="original owner"
                )
                await repository.checkpoint(
                    replacement, current["id"], {"retain": "other"}, models=2
                )
        release.set()
        result = await asyncio.wait_for(task, timeout=2)
        if replacement:
            actual = await repository.get(current["id"])
            assert actual["state"] == "running" and actual["model_requests"] == 2
            assert await repository.valid(replacement)
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if replacement:
            await repository.release(replacement)
    assert result.handled
    assert provider.requests == []
    assert sender.calls == 0
