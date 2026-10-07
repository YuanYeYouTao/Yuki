"""Ordinary context preparation avoids empty leases without caching admission."""

from dataclasses import replace

import pytest
from tests.conftest import MemorySender, build_harness, make_settings
from tests.unit.test_commands_and_chat import inbound

from qq_ai_bot.conversation.hydrate import ensure_canonical_conversation
from qq_ai_bot.domain.messages import ChatResponse
from qq_ai_bot.identity.canonical_repository import ensure_person, ensure_presence
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.runtime import work_activation
from qq_ai_bot.runtime.work_activation import current_work_control
from qq_ai_bot.runtime.work_repository import WorkRepository


@pytest.mark.parametrize("case", ["empty", "present", "foreign", "handoff", "arriving"])
async def test_chat_preparation_candidate_hint_is_read_only_and_reloaded(
    database, monkeypatch, case
):
    provider = FakeLLMProvider(lambda request: ChatResponse(content="", latency_seconds=0))
    harness = build_harness(
        database, make_settings(database.url, runtime_work_enabled=True), provider
    )
    chat = harness.processor._chat
    async with database.sessions() as session, session.begin():
        person = await ensure_person(session, "1001")
        presence = await ensure_presence(session, "9999")
        conversation = await ensure_canonical_conversation(
            session, kind="private", primary_scope_key="private:9999:1001", person_id=person
        )
    message = replace(
        inbound("检查普通上下文", message_id=f"candidate-{case}"),
        conversation_id=conversation.conversation_id,
        person_id=person,
        presence_id=presence,
    )
    acquire = WorkRepository.acquire
    hint = work_activation.work_candidate_available
    build = chat._build_messages
    run = chat._run_agent
    acquisitions, prepared, dispatched = [], [], []
    candidate_id = None
    hint_args = None

    async def add_candidate(repository, conversation_id, generation, source_key, source):
        nonlocal candidate_id
        lease = await acquire(repository, conversation_id, generation)
        assert lease is not None
        candidate_source = {**source, "actor_user_id": "other"} if case == "foreign" else source
        row = await repository.accept(
            lease, source_key=source_key, source=candidate_source, goal="original goal"
        )
        candidate_id = row["id"]
        if case == "handoff":
            await repository.checkpoint(lease, row["id"], {"handoff_work_id": "already-handed-off"})
        await repository.release(lease)

    async def counted_acquire(repository, conversation_id, generation):
        acquisitions.append((conversation_id, generation))
        return await acquire(repository, conversation_id, generation)

    async def preflight(*args):
        nonlocal hint_args
        hint_args = args
        if case in {"present", "foreign", "handoff"}:
            await add_candidate(*args)
        return await hint(*args)

    async def preparing(*args, **kwargs):
        control = current_work_control.get()
        prepared.append(control.current["id"] if control and control.current else None)
        if case == "arriving":
            # A real Work appears after the hint; the formal activation must find it.
            await add_candidate(*hint_args)
        return await build(*args, **kwargs)

    async def dispatching(*args, **kwargs):
        control = current_work_control.get()
        assert control is not None
        dispatched.append(control.current["id"] if control.current else None)
        return await run(*args, **kwargs)

    monkeypatch.setattr(WorkRepository, "acquire", counted_acquire)
    monkeypatch.setattr(work_activation, "work_candidate_available", preflight)
    monkeypatch.setattr(chat, "_build_messages", preparing)
    monkeypatch.setattr(chat, "_run_agent", dispatching)
    result = await harness.processor.handle(message, MemorySender())
    assert result.reason == "chat", result
    assert provider.requests
    assert len(acquisitions) == 1
    assert prepared == ([candidate_id] if case == "present" else [None])
    assert dispatched == ([candidate_id] if case in {"present", "arriving"} else [None])


async def test_candidate_hint_does_not_authorize_replaced_source(database, tmp_path):
    from tests.support.social_identity_cases import social_env

    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    source = {"actor_user_id": "original", "origin": "user_message"}
    lease = await repository.acquire(env.context.conversation_id, 1)
    row = await repository.accept(lease, source_key="source", source=source, goal="original")
    await repository.release(lease)
    assert await work_activation.work_candidate_available(
        repository, env.context.conversation_id, 1, "source", source
    )
    await repository.cancel(env.context.conversation_id)

    async def validate():
        pass

    async with work_activation.activate_work(
        repository, env.context.conversation_id, 1, "source", source, validate
    ) as control:
        assert control.current is None
    assert (await repository.get(row["id"]))["state"] == "cancelled"
