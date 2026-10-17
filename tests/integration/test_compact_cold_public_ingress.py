import pytest
from tests.conftest import MemorySender, build_harness, make_settings
from tests.support.commands_and_chat_helpers import inbound

from yuki.domain.conversations import ConversationScope
from yuki.llm.fake import FakeLLMProvider
from yuki.runtime.work_repository import WorkConflict
from yuki.services.main_agent_contract import MainAgentContract
from yuki.workspace.short_state import ShortState
from yuki.workspace.store import WorkspaceStore


@pytest.mark.asyncio
async def test_actual_main_recovers_paid_public_compact_with_new_history(
    database, tmp_path, monkeypatch
):
    import yuki.services.context_compaction as compaction

    provider = FakeLLMProvider(
        lambda request: (
            "Public paid checkpoint."
            if "Host 会话上下文压缩要求" in (request.messages[-1].content or "")
            else "Continue the actual Main task."
        )
    )
    harness = build_harness(
        database, make_settings(database.url, runtime_work_enabled=False), provider
    )
    chat = harness.processor._chat
    state = ShortState(WorkspaceStore(tmp_path / "actual-workspace"))
    chat.runtime.runner.main_contract = MainAgentContract(chat, state)
    scope = ConversationScope.private("9999", "1001")
    await harness.scoped_events.append(
        scope=scope,
        platform_message_id="old-public",
        sender_user_id="1001",
        direction="inbound",
        content="Old public decision.",
    )
    command = inbound("/ai compact", message_id="paid-command", user_id="1001", group_id=None)
    publication = compaction.public_compact_publication
    compact = chat.compact
    admitted = []

    async def capture(*args, **kwargs):
        admitted.append((args, kwargs))
        return await compact(*args, **kwargs)

    async def fail(*args, **kwargs):
        raise WorkConflict("publication_failed")

    monkeypatch.setattr(compaction, "public_compact_publication", fail)
    monkeypatch.setattr(chat, "compact", capture)
    first = await harness.processor.handle(command, MemorySender())
    assert first.reason == "turn_interrupted"
    assert len(provider.requests) == 1
    await harness.scoped_events.append(
        scope=scope,
        platform_message_id="new-public",
        sender_user_id="1001",
        direction="inbound",
        content="Fresh public correction.",
    )
    state.update({"slot": 1, "text": "fresh activation sentinel"})
    monkeypatch.setattr(compaction, "public_compact_publication", publication)
    monkeypatch.setattr(chat, "compact", compact)
    # Reuse the original internally admitted source and turn. Re-ingressing
    # the same platform delivery only exercises transport deduplication.
    args, kwargs = admitted[0]
    result = await compact(*args, **kwargs)
    assert result == "已压缩当前会话的公共上下文。"
    assert len(provider.requests) == 1
    await harness.processor.handle(
        inbound("Continue now.", message_id="next-business", user_id="1001", group_id=None),
        MemorySender(),
    )
    assert len(provider.requests) == 2
    body = "\n".join(message.content or "" for message in provider.requests[-1].messages)
    assert "Public paid checkpoint." in body
    assert "Fresh public correction." in body
    assert "fresh activation sentinel" in body
