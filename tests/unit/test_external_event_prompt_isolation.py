"""External-event isolation with stable Main-Agent prompt composition."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from jsonschema import Draft202012Validator
from tests.conftest import build_harness, make_settings

from yuki.automation.models import TurnOrigin
from yuki.conversation.prompt_history import PromptHistorySource
from yuki.conversation.scope import ConversationTurnSnapshot
from yuki.conversation.scope_models import ConversationCoverageError
from yuki.domain.conversations import ConversationScope, ScopeType
from yuki.domain.messages import ChatMessage, InboundMessage, SenderIdentity
from yuki.domain.profiles import UserProfileSnapshot
from yuki.event_prompt import ChatEventPromptRenderer
from yuki.llm.deepseek_responses import DeepSeekResponsesProvider
from yuki.llm.fake import FakeLLMProvider
from yuki.memory.context import MEMORY_GROUNDING_RULE
from yuki.persistence.database import Database
from yuki.persistence.event_repository import ConversationReadVersion
from yuki.persistence.repository_records import EventRecord
from yuki.prompting.contracts import core_contract
from yuki.runtime.trigger import ExternalEventTurnTrigger
from yuki.services.agent_tools import ToolRuntime
from yuki.services.context_assembler import (
    AssembledContext,
    ContextAssembler,
    ContextMetrics,
    _HistoryPromptWindow,
)
from yuki.services.prompt_composer import PromptComposer

_OCCURRED = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
# The user explicitly retained this search policy while revising the other guidance.
_PUBLIC_INFORMATION_RULE = (
    "公开信息只要可能变化、更新或过时，即使熟悉话题或对方说得肯定，也不等用户要求，"
    "主动联网搜索最新可靠来源，核实后再作事实判断；不熟悉、拿不准或用户要求核实的公开"
    "事实同样先查证，指定网页用受控读取。"
)


def _message(
    event_id: int,
    content: str,
    *,
    sender: str = "1001",
    direction: str = "inbound",
) -> EventRecord:
    return EventRecord(
        id=event_id,
        bot_user_id="8000",
        platform_message_id=f"msg-{event_id}",
        scope_type=ScopeType.PRIVATE,
        sender_user_id=sender,
        direction=direction,
        content=content,
        visual_summary="",
        segments=(),
        occurred_at=_OCCURRED + timedelta(seconds=event_id),
        event_kind="message",
        private_peer_user_id="1001",
    )


def _external(
    event_id: int,
    summary: str,
    *,
    payload: dict[str, object] | None = None,
    event_type: str = "PushEvent",
    plugin_id: str = "github-monitor",
) -> EventRecord:
    return EventRecord(
        id=event_id,
        bot_user_id="8000",
        platform_message_id=f"ext-{event_id}",
        platform="qq",
        scope_type=ScopeType.PRIVATE,
        sender_user_id="8000",
        direction="external",
        content=summary,
        visual_summary="",
        segments=(),
        occurred_at=_OCCURRED + timedelta(seconds=event_id),
        event_kind="external_event",
        origin="plugin_background",
        author_kind="system",
        source_plugin_id=plugin_id,
        external_source="github",
        external_event_key=f"key-{event_id}",
        external_event_type=event_type,
        external_payload=payload if payload is not None else {"secret": "body", "id": event_id},
        private_peer_user_id="1001",
    )


def _assembler(**overrides: object) -> ContextAssembler:
    settings = make_settings("sqlite+aiosqlite:///:memory:", **overrides)
    return ContextAssembler(
        settings=settings,
        ledger=MagicMock(read_version_matches=AsyncMock(return_value=True)),
        conversation_scopes=MagicMock(),
    )


def _snapshot(current, recent=(), **checkpoint):
    """Capture the same metadata/body split supplied by the real scope reader."""
    coverage = checkpoint.get("coverage_end", 0)
    rows = tuple(row for row in recent if row.event_kind == "message")
    through = max((coverage, current.id, *(row.id for row in rows)))
    source = PromptHistorySource(
        (),
        projection=None,
        timezone="Asia/Shanghai",
        after_event_id=coverage,
    )
    source.extend(rows, through_event_id=through)
    references = source.capture(through)
    return _HistoryPromptWindow(
        recent=rows,
        source=source,
        references=references,
        current_event=current,
        read_version=ConversationReadVersion(
            current.scope,
            current.canonical_conversation_id,
            1,
            checkpoint.get("starts_after_event_id", 0),
            0,
            references.event_ids,
        ),
        raw_tail_end_event_id=source.raw_tail(through),
        **checkpoint,
    )


def _assembled(
    *,
    history: tuple[ChatMessage, ...],
    current: ChatMessage,
    metadata: dict[str, object] | None = None,
    checkpoint_text: str = "",
) -> AssembledContext:
    return AssembledContext(
        metadata_payload=metadata or {},
        history_messages=history,
        current_message=current,
        metrics=ContextMetrics(
            metadata_characters=0,
            history_characters=0,
            history_messages=len(history),
            current_message_characters=len(current.content or ""),
            raw_history_window_shifted=False,
        ),
        checkpoint_text=checkpoint_text,
        prompt_effective_coverage=0,
        prompt_raw_tail_end_event_id=3,
    )


def test_main_history_omits_external_source_rows_and_does_not_use_system_role() -> None:
    renderer = ChatEventPromptRenderer()
    rows = (_message(1, "hello"), _external(2, "opened a pull request"), _message(3, "thanks"))
    rendered = renderer.main_agent_history(rows)
    contents = [item.content or "" for _, _, item in rendered]
    roles = [item.role for _, _, item in rendered]
    ids = [event_id for _, event_ids, _ in rendered for event_id in event_ids]
    assert 2 not in ids
    assert all("opened a pull request" not in text for text in contents)
    assert "system" not in roles
    assert all(role in {"user", "assistant"} for role in roles)


def test_proactive_history_groups_by_origin_and_cause_without_changing_body() -> None:
    ordinary = replace(
        _message(1, "ordinary yuki reply", sender="8000", direction="outbound"),
        author_kind="yuki",
    )
    first_cause = _external(2, "first source", event_type="PullRequestEvent")
    first_part = replace(
        _message(3, "first proactive part", sender="8000", direction="outbound"),
        author_kind="yuki",
        origin="plugin_background",
        caused_by_event_id=2,
    )
    second_part = replace(
        _message(4, "second proactive part", sender="8000", direction="outbound"),
        author_kind="yuki",
        origin="plugin_background",
        caused_by_event_id=2,
    )
    second_cause = _external(5, "second source", event_type="IssueEvent")
    other_episode = replace(
        _message(6, "other proactive episode", sender="8000", direction="outbound"),
        author_kind="yuki",
        origin="plugin_background",
        caused_by_event_id=5,
    )
    legacy = replace(
        _message(7, "legacy proactive body", sender="8000", direction="outbound"),
        author_kind="yuki",
        origin="plugin_background",
    )
    renderer = ChatEventPromptRenderer(
        (ordinary, first_cause, first_part, second_part, second_cause, other_episode, legacy)
    )

    rendered = renderer.main_agent_history(
        (ordinary, first_cause, first_part, second_part, second_cause, other_episode, legacy)
    )

    assert [event_ids for _anchor, event_ids, _message in rendered] == [
        (1,),
        (3, 4),
        (6,),
        (7,),
    ]
    first_episode = rendered[1][2].content or ""
    assert "[20:00:03｜主动消息｜由外部事件 #2 触发｜source=github｜type=PullRequestEvent]" in (
        first_episode
    )
    assert "first proactive part" in first_episode
    assert "second proactive part" in first_episode
    assert (rendered[2][2].content or "").count("由外部事件 #5 触发") == 1
    assert "历史来源未知" in (rendered[3][2].content or "")
    assert first_part.content == "first proactive part"
    assert second_part.content == "second proactive part"


def test_public_source_projection_preserves_proactive_cause_label() -> None:
    event = replace(
        _message(9, "release note", sender="8000", direction="outbound"),
        author_kind="yuki",
        origin="plugin_background",
        caused_by_event_id=8,
        caused_by_external_source="github",
        caused_by_external_event_type="ReleaseEvent",
    )

    projected = ChatEventPromptRenderer((event,)).render_reference_event(event)

    assert "由外部事件 #8 触发" in projected
    assert "source=github" in projected
    assert "type=ReleaseEvent" in projected
    assert "release note" in projected


def test_current_external_carrier_keeps_its_source_and_user_role_once() -> None:
    current = _external(9, "external summary", payload={"body": "payload body"})
    renderer = ChatEventPromptRenderer((current,))
    message = renderer.reference_message(current)
    assert message.role == "user"
    assert "external summary" in (message.content or "")
    assert "payload body" not in (message.content or "")
    assert "不是任何聊天成员的发言" in (message.content or "")
    history = renderer.main_agent_history((current,))
    assert history == ()


def test_incremental_history_keeps_current_external_out_of_main_history() -> None:
    history_rows = (_message(1, "hello"), _external(2, "earlier notice"))
    current = _external(3, "current trigger", payload={"token": "abc"})
    trigger = ExternalEventTurnTrigger(
        plugin_id="github-monitor",
        source_event_id=current.id,
        target_type="private",
        target_id="1001",
        agent_intent="comment briefly",
    )
    bounded = _assembler()._prepared_context(
        _snapshot(
            current,
            history_rows,
            checkpoint_text="",
            coverage_end=0,
            revision=1,
            checkpoint_kind=None,
        ),
        identity=current.scope,
        metadata={},
        content=current.content,
        current_override=ContextAssembler._external_wakeup_message(current, trigger),
    )
    assert bounded.current_message.role == "user"
    assert "current trigger" in (bounded.current_message.content or "")
    assert all(item.role != "system" for item in bounded.history_messages)
    assert all("current trigger" not in (item.content or "") for item in bounded.history_messages)
    assert all("earlier notice" not in (item.content or "") for item in bounded.history_messages)
    assert all("abc" not in (item.content or "") for item in bounded.history_messages)


async def _assemble_wakeup(
    current: EventRecord,
    recent: tuple[EventRecord, ...] = (),
    *,
    target_type: str = "private",
    target_id: str = "1001",
) -> tuple[ContextAssembler, AssembledContext]:
    """Run the real actorless ``assemble`` entry for one plugin wakeup."""

    assembler = _assembler()
    assembler._load_history_snapshot = AsyncMock(  # type: ignore[method-assign]
        return_value=_snapshot(
            current,
            recent,
            checkpoint_text="",
            coverage_end=0,
            revision=1,
            checkpoint_kind="llm",
        )
    )
    identity = current.scope
    turn = ConversationTurnSnapshot(
        conversation_id=current.canonical_conversation_id,
        scope_key=identity.key,
        generation=1,
        trigger_event_id=current.id,
        coordinator_version=1,
        transport_scope_key=identity.key,
    )
    runtime = MagicMock()
    runtime.context.local_event_limit = 2_048
    context = await assembler.assemble(
        inbound=None,
        profile=None,
        identity=identity,
        turn=turn,
        content=current.content,
        runtime=runtime,
        external_event=current,
        external_trigger=ExternalEventTurnTrigger(
            plugin_id="github-monitor",
            source_event_id=current.id,
            target_type=target_type,
            target_id=target_id,
            agent_intent="comment briefly",
        ),
    )
    return assembler, context


def _metadata_items(context: AssembledContext) -> dict[str, object]:
    return context.metadata_payload


@pytest.mark.asyncio
async def test_private_wakeup_targets_the_person_without_an_actor() -> None:
    current = replace(_external(3, "current trigger"), canonical_conversation_id="conv-private")

    _assembler, context = await _assemble_wakeup(current)

    items = _metadata_items(context)
    assert items["scene"] == "私聊"
    # The target is the conversation person, never a fabricated current speaker.
    assert "current_person" not in items
    assert "current_group" not in items
    assert items == {"scene": "私聊", "platform": "qq"}
    # Automatic memory recall stays off; the agent must use memory tools explicitly.


@pytest.mark.asyncio
async def test_group_wakeup_targets_the_group_without_a_person() -> None:
    current = replace(
        _external(3, "current trigger"),
        scope_type=ScopeType.GROUP,
        group_id="group-100",
        private_peer_user_id=None,
        canonical_conversation_id="conv-group",
    )

    _assembler_value, context = await _assemble_wakeup(
        current, target_type="group", target_id="group-100"
    )

    items = _metadata_items(context)
    assert items["scene"] == "群聊"
    assert "conversation_target_person" not in items and "current_person" not in items


@pytest.mark.asyncio
async def test_wakeup_never_attaches_external_digest_or_payload() -> None:
    """Older external rows and payloads stay out of every assembled prompt part."""

    huge = "x" * 2_000
    recent = (
        _external(1, "old notice", payload={"body": "secret-1"}),
        _message(2, "hello"),
        _external(3, huge, payload={"body": "secret-3"}),
    )
    current = replace(
        _external(4, "current trigger", payload={"body": "secret-4"}),
        canonical_conversation_id="conv-private",
    )

    _assembler_used, context = await _assemble_wakeup(current, recent)

    metadata = json.dumps(context.metadata_payload, ensure_ascii=False, default=str)
    history = "\n".join(item.content or "" for item in context.history_messages)
    current_text = context.current_message.content or ""
    assert "recent_external_events" not in metadata
    for blob in (metadata, history, current_text):
        assert "secret-" not in blob
        assert '"body"' not in blob
    assert "old notice" not in metadata + history
    assert huge[:100] not in metadata + history
    assert "hello" in history
    assert context.current_message.role == "user"
    assert current_text.count("current trigger") == 1
    assert "current trigger" not in history + metadata
    assert all(item.role != "system" for item in context.history_messages)


@pytest.mark.asyncio
async def test_external_wakeup_assembles_the_same_stable_conversation_window() -> None:
    """The wakeup path may replace only the current turn, never the stable history."""

    early_marker = "EARLY-STABLE-CONTEXT-MARKER"
    recent = (
        _message(6, early_marker),
        replace(
            _message(7, "ordinary Yuki history", sender="8000", direction="outbound"),
            author_kind="yuki",
        ),
        _external(8, "old external event must not be replayed"),
    )
    checkpoint = dict(
        checkpoint_text="stable checkpoint before both current turns",
        coverage_end=5,
        revision=9,
        checkpoint_kind="llm",
        starts_after_event_id=0,
    )
    runtime = MagicMock()
    runtime.context.local_event_limit = 2_048

    ordinary_event = replace(
        _message(10, "ordinary current turn"),
        canonical_conversation_id="conv-stable",
    )
    ordinary = _assembler()
    ordinary._load_history_snapshot = AsyncMock(  # type: ignore[method-assign]
        return_value=_snapshot(ordinary_event, recent, **checkpoint)
    )
    ordinary._ledger.get_event = AsyncMock(return_value=ordinary_event)
    ordinary_identity = ordinary_event.scope
    turn = ConversationTurnSnapshot(
        conversation_id=ordinary_event.canonical_conversation_id,
        scope_key=ordinary_identity.key,
        generation=1,
        trigger_event_id=ordinary_event.id,
        coordinator_version=1,
        transport_scope_key=ordinary_identity.key,
    )
    ordinary_context = await ordinary.assemble(
        inbound=InboundMessage(
            message_id=ordinary_event.platform_message_id,
            event_type="message",
            scope_type=ScopeType.PRIVATE,
            sender=SenderIdentity(user_id="1001"),
            text=ordinary_event.content,
            bot_user_id="8000",
        ),
        profile=UserProfileSnapshot(
            user_id="1001",
            scope_type=ScopeType.PRIVATE,
            nickname="Ada",
        ),
        identity=ordinary_identity,
        turn=turn,
        content=ordinary_event.content,
        runtime=runtime,
    )

    external_event = replace(
        _external(10, "external current turn"),
        canonical_conversation_id="conv-stable",
    )
    wakeup = _assembler()
    wakeup._load_history_snapshot = AsyncMock(  # type: ignore[method-assign]
        return_value=_snapshot(external_event, recent, **checkpoint)
    )
    wakeup_context = await wakeup.assemble(
        inbound=None,
        profile=None,
        identity=external_event.scope,
        turn=turn,
        content=external_event.content,
        runtime=runtime,
        external_event=external_event,
        external_trigger=ExternalEventTurnTrigger(
            plugin_id="github-monitor",
            source_event_id=external_event.id,
            target_type="private",
            target_id="1001",
            agent_intent="comment if useful",
        ),
    )

    assert wakeup_context.history_messages == ordinary_context.history_messages
    assert "recent_external_events" not in json.dumps(ordinary_context.metadata_payload)
    assert "recent_external_events" not in json.dumps(wakeup_context.metadata_payload)
    assert early_marker in "\n".join(
        message.content or "" for message in wakeup_context.history_messages
    )
    assert (
        wakeup_context.checkpoint_text
        == ordinary_context.checkpoint_text
        == checkpoint["checkpoint_text"]
    )
    assert wakeup_context.read_version.conversation_id == turn.conversation_id
    assert ordinary_context.read_version.conversation_id == turn.conversation_id
    assert wakeup_context.read_version.generation == turn.generation
    assert ordinary_context.read_version.generation == turn.generation
    assert (
        wakeup_context.prompt_effective_coverage == ordinary_context.prompt_effective_coverage == 5
    )
    assert wakeup_context.prompt_raw_tail_end_event_id == (
        ordinary_context.prompt_raw_tail_end_event_id
    )
    assert wakeup_context.metadata_payload == {"scene": "私聊", "platform": "qq"}
    assert wakeup_context.current_message != ordinary_context.current_message
    assert "external_event_wakeup" in (wakeup_context.current_message.content or "")


@pytest.mark.parametrize("code_enabled", (False, True))
def test_external_wakeup_uses_the_same_main_agent_prompt_program(code_enabled) -> None:
    settings = make_settings("sqlite+aiosqlite:///:memory:", code_mode_enabled=code_enabled)
    composer = PromptComposer(settings)
    current = ChatMessage(role="user", content="[external] current summary")
    history = (ChatMessage(role="user", content="hello from a person"),)
    context = _assembled(
        history=history,
        current=current,
        metadata={"scene": "私聊"},
    )
    runtime = MagicMock()
    composed = composer.compose(
        inbound=None,
        context=context,
        runtime=runtime,
    )
    ordinary = composer.compose(
        inbound=InboundMessage(
            message_id="ordinary",
            event_type="message",
            scope_type=ScopeType.PRIVATE,
            sender=SenderIdentity(user_id="1001"),
            text="ordinary",
            bot_user_id="8000",
        ),
        context=context,
        runtime=runtime,
    )
    system_text = "\n".join(
        item.content or "" for item in composed.messages if item.role == "system"
    )
    assert "github-monitor" not in system_text
    assert "PushEvent" not in system_text
    for component in (
        settings.character_prompt,
        core_contract(code_enabled=settings.code_mode_enabled),
        MEMORY_GROUNDING_RULE,
    ):
        assert system_text.count(component) == 1
    assert system_text.count(_PUBLIC_INFORMATION_RULE) == 1
    assert tuple(item.content for item in composed.messages if item.role == "system") == tuple(
        item.content for item in ordinary.messages if item.role == "system"
    )
    user_messages = tuple(item for item in composed.messages if item.role == "user")
    assert len(user_messages) == 2
    assert current.content in (user_messages[-1].content or "")
    history_blob = "\n".join(
        item.content or "" for item in composed.messages if item.role != "system"
    )
    assert history_blob.count("current summary") == 1
    assert _PUBLIC_INFORMATION_RULE not in history_blob
    repeat = composer.compose(
        inbound=None,
        context=context,
        runtime=runtime,
    )
    assert tuple((item.role, item.content) for item in repeat.messages) == tuple(
        (item.role, item.content) for item in composed.messages
    )
    instructions, inputs = DeepSeekResponsesProvider(
        base_url="https://provider.invalid",
        api_key="",
        timeout_seconds=1,
        max_retries=0,
        client=MagicMock(),
    )._convert_messages(composed.messages)
    assert "github-monitor" not in instructions
    assert all(item["role"] in {"user", "assistant"} for item in inputs)
    assert inputs[-1]["role"] == "user"
    assert current.content in str(inputs[-1]["content"])
    assert sum(1 for item in inputs if "current summary" in str(item.get("content", ""))) == 1
    chat_payload = [{"role": item.role, "content": item.content} for item in composed.messages]
    assert chat_payload[-1]["role"] == "user"
    assert current.content in str(chat_payload[-1]["content"])
    assert all(
        item["role"] != "system" or "current summary" not in str(item["content"])
        for item in chat_payload
    )


@pytest.mark.asyncio
async def test_external_wakeup_and_ordinary_turn_send_the_same_provider_shape(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from yuki.services.main_agent_contract import MainAgentContract

    provider = FakeLLMProvider(lambda _request: "ok")
    harness = build_harness(database, make_settings(database.url), provider)
    chat = harness.processor._chat
    chat.runtime.runner.main_contract = MainAgentContract(
        chat, SimpleNamespace(snapshot=lambda: [])
    )
    runtime_config = await chat._runtime_config.snapshot()
    stable_prefix = (
        ChatMessage(role="system", content="stable instructions"),
        ChatMessage(role="user", content="old user marker"),
        ChatMessage(role="assistant", content="old assistant marker"),
    )
    ordinary_inbound = InboundMessage(
        message_id="ordinary-current",
        event_type="message",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(user_id="1001"),
        text="ordinary current",
        bot_user_id="8000",
        yuki_account_ids=frozenset({"8000"}),
        conversation_id="conv-stable",
        presence_id="presence-stable",
    )
    shared = {
        "conversation_key": "canonical:conv-stable:generation:1",
        "actor_is_superuser": False,
        "runtime_config": runtime_config,
        "tools_closed": False,
        "visible_event_ids": frozenset({1, 2}),
        "selection_query": "same capability-neutral query",
        "scope_type": ScopeType.PRIVATE,
        "read_scope": ConversationScope.private("8000", "1001"),
        "conversation_id": "conv-stable",
        "target_presence_id": "presence-stable",
        "person_id": "person-stable",
        "external_target_id": "1001",
    }
    ordinary_runtime = ToolRuntime(
        inbound=ordinary_inbound,
        trigger_message_id="ordinary-current",
        trigger_event_id=1,
        origin=TurnOrigin.USER_MESSAGE,
        **shared,
    )
    wakeup_runtime = ToolRuntime(
        inbound=None,
        trigger_message_id="external-current",
        trigger_event_id=2,
        origin=TurnOrigin.PLUGIN_BACKGROUND,
        **shared,
    )

    await chat._run_agent(
        str(shared["conversation_key"]),
        (*stable_prefix, ChatMessage(role="user", content="ordinary current")),
        ordinary_runtime,
    )
    await chat._run_agent(
        str(shared["conversation_key"]),
        (*stable_prefix, ChatMessage(role="user", content="external current")),
        wakeup_runtime,
    )

    assert len(provider.requests) == 2
    ordinary_request, wakeup_request = provider.requests
    assert ordinary_request.messages[:-1] == wakeup_request.messages[:-1] == stable_prefix
    assert ordinary_request.messages[-1].role == wakeup_request.messages[-1].role == "user"
    assert ordinary_request.messages[-1].content != wakeup_request.messages[-1].content
    assert ordinary_request.tools == wakeup_request.tools
    assert ordinary_request.native_tools == wakeup_request.native_tools
    assert ordinary_request.tool_choice == wakeup_request.tool_choice
    assert ordinary_request.model == wakeup_request.model
    assert ordinary_request.temperature == wakeup_request.temperature
    assert ordinary_request.max_output_tokens == wakeup_request.max_output_tokens
    assert ordinary_request.thinking_enabled == wakeup_request.thinking_enabled
    assert ordinary_request.reasoning_effort == wakeup_request.reasoning_effort
    assert ordinary_request.response_format == wakeup_request.response_format
    assert ordinary_request.structured_output == wakeup_request.structured_output


@pytest.mark.asyncio
async def test_plugin_wakeup_can_end_without_creating_a_fake_reply(
    database: Database,
) -> None:
    harness = build_harness(database, make_settings(database.url))
    chat = harness.processor._chat
    runtime_config = await chat._runtime_config.snapshot()
    runtime = ToolRuntime(
        inbound=None,
        conversation_key="canonical:conv-stable:generation:1",
        trigger_message_id="external-current",
        trigger_event_id=2,
        runtime_config=runtime_config,
        origin=TurnOrigin.PLUGIN_BACKGROUND,
        scope_type=ScopeType.PRIVATE,
        conversation_id="conv-stable",
        person_id="person-stable",
        external_target_id="1001",
    )

    definitions = chat._tools.definitions(runtime)
    names = {tool.name for tool in definitions}
    assert "send" in names
    assert "decline_reply" not in names


@pytest.mark.asyncio
async def test_plugin_wakeup_read_tools_use_canonical_target_without_a_fake_actor(
    database: Database,
    tmp_path,
) -> None:
    from tests.support.event_seed import seed_scoped_event
    from tests.support.social_identity_cases import social_env

    from yuki.services.main_agent_contract import MainAgentContract
    from yuki.workspace.short_state import ShortState
    from yuki.workspace.store import WorkspaceStore

    env = await social_env(database, tmp_path)
    harness = build_harness(
        database, make_settings(database.url, agent_tool_result_max_characters=24000)
    )
    contract = MainAgentContract(
        harness.processor._chat, ShortState(WorkspaceStore(tmp_path / "state"))
    )
    frozen = await contract.definitions()
    declared = next(item for item in frozen if item.name == "query_chat_history")
    Draft202012Validator(declared.parameters).validate({})
    assert "around" in declared.parameters["properties"]["view"]["enum"]
    assert "platform_message_id" not in declared.parameters["properties"]
    event = (
        await seed_scoped_event(
            env.service.writer,
            scope=ConversationScope.group("80001", "20001"),
            platform_message_id="history-attachment",
            sender_user_id="10001",
            direction="inbound",
            content="release clip",
            segments=(
                {"type": "unknown", "data": {}},
                {"type": "video", "data": {"name": "clip.mp4"}},
            ),
        )
    ).event
    tools = harness.processor._chat._tools
    env.bot.calls.clear()
    runtime = ToolRuntime(
        inbound=None,
        trigger_event_id=event.id,
        origin=TurnOrigin.PLUGIN_BACKGROUND,
        scope_type=ScopeType.GROUP,
        conversation_id=event.canonical_conversation_id,
        external_target_id="20001",
        space_id=env.space,
        read_scope=ConversationScope.group(
            "80001", "20001", conversation_id=event.canonical_conversation_id
        ),
    )
    recent = (await tools.execute("query_chat_history", '{"limit":1}', runtime)).model_payload()
    assert recent["ok"] and len(recent["data"]["events"]) == 1
    assert recent["data"]["events"][0]["id"] == event.id
    invalid = (
        await tools.execute(
            "query_chat_history",
            '{"view":"around", "platform_message_id":"history-attachment"}',
            runtime,
        )
    ).model_payload()
    assert not invalid["ok"]
    around = (
        await tools.execute(
            "query_chat_history", json.dumps({"view": "around", "event_id": event.id}), runtime
        )
    ).model_payload()
    center = next(row for row in around["data"]["events"] if row["id"] == event.id)
    assert around["ok"] and center["attachments"] == [
        {"attachment_index": 1, "kind": "video", "name": "clip.mp4"}
    ]
    searched = (
        await tools.execute("query_chat_history", '{"view":"search", "keyword":"release"}', runtime)
    ).model_payload()
    assert searched["ok"] and [row["id"] for row in searched["data"]["events"]] == [event.id]
    metadata = (
        await tools.execute("query_chat_history", '{"view":"search", "has":["video"]}', runtime)
    ).model_payload()
    assert metadata["ok"] and metadata["data"]["events"][0]["id"] == event.id
    assert env.bot.calls == []


def _covered_external_turn(
    *,
    coverage_end: int,
    checkpoint_kind: str,
    marker: str,
    starts_after_event_id: int = 0,
) -> tuple[ContextAssembler, EventRecord, ConversationTurnSnapshot, MagicMock]:
    event = replace(
        _external(10, marker),
        canonical_conversation_id="conv-covered",
    )
    assembler = _assembler()
    assembler._load_history_snapshot = AsyncMock(  # type: ignore[method-assign]
        return_value=_snapshot(
            event,
            checkpoint_text=f"checkpoint already contains {marker}",
            coverage_end=coverage_end,
            revision=1,
            checkpoint_kind=checkpoint_kind,
            starts_after_event_id=starts_after_event_id,
        )
    )
    turn = ConversationTurnSnapshot(
        conversation_id=event.canonical_conversation_id,
        scope_key="bot:8000:private:1001",
        generation=1,
        trigger_event_id=event.id,
        coordinator_version=1,
        transport_scope_key="bot:8000:private:1001",
    )
    runtime = MagicMock()
    runtime.context.local_event_limit = 2_048
    return assembler, event, turn, runtime


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("coverage_end", "checkpoint_kind"),
    (
        (10, "extractive"),
        (12, "emergency"),
    ),
)
async def test_actorless_current_source_is_not_cancelled_by_history_checkpoint_coverage(
    coverage_end: int,
    checkpoint_kind: str,
) -> None:
    marker = "UNIQUE-CURRENT-EXTERNAL-MARKER"
    assembler, event, turn, runtime = _covered_external_turn(
        coverage_end=coverage_end,
        checkpoint_kind=checkpoint_kind,
        marker=marker,
    )
    context = await assembler.assemble(
        inbound=None,
        profile=None,
        identity=event.scope,
        turn=turn,
        content=event.content,
        runtime=runtime,
        external_event=event,
        external_trigger=ExternalEventTurnTrigger(
            plugin_id="github-monitor",
            source_event_id=event.id,
            target_type="private",
            target_id="1001",
            agent_intent="comment",
        ),
    )
    assert context.current_event_id is None and context.projection_scope == "main"
    assert marker in context.current_message.content
    assert context.prompt_effective_coverage == coverage_end
    assert context.checkpoint_text == f"checkpoint already contains {marker}"


@pytest.mark.asyncio
async def test_actorless_current_source_still_obeys_the_canonical_reset_boundary():
    assembler, event, turn, runtime = _covered_external_turn(
        coverage_end=12,
        checkpoint_kind="emergency",
        marker="original source",
        starts_after_event_id=10,
    )
    with pytest.raises(ConversationCoverageError, match="precedes the reset boundary"):
        await assembler.assemble(
            inbound=None,
            profile=None,
            identity=event.scope,
            turn=turn,
            content=event.content,
            runtime=runtime,
            external_event=event,
            external_trigger=ExternalEventTurnTrigger(
                plugin_id="github-monitor",
                source_event_id=event.id,
                target_type="private",
                target_id="1001",
            ),
        )
