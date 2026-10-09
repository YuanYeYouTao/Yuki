"""External-event isolation with stable Main-Agent prompt composition."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from tests.conftest import build_harness, make_settings

from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.conversation.rollup.errors import ConversationCoverageError
from qq_ai_bot.conversation.rollup.renderer import rollup_source_projection
from qq_ai_bot.conversation.scope import ConversationTurnSnapshot
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.domain.messages import ChatMessage, ChatRequest, InboundMessage, SenderIdentity
from qq_ai_bot.domain.profiles import UserProfileSnapshot
from qq_ai_bot.event_prompt import (
    EXTERNAL_EVENT_CONTENT_TRUST,
    ChatEventPromptRenderer,
    external_event_digest_appended_growth,
    external_event_digest_encoded_characters,
)
from qq_ai_bot.llm.deepseek_responses import DeepSeekResponsesProvider
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.model_runtime.executor import provider_cache_shape_diagnostics
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.people_repository import PersonPromptMetadata
from qq_ai_bot.persistence.repository_records import EventRecord
from qq_ai_bot.prompting import ContextBudgeter
from qq_ai_bot.runtime.trigger import ExternalEventTurnTrigger
from qq_ai_bot.services.agent_tools import ToolRuntime
from qq_ai_bot.services.context_assembler import (
    AssembledContext,
    ContextAssembler,
    ContextMetrics,
    _HistoryPromptWindow,
)
from qq_ai_bot.services.prompt_composer import PromptComposer
from qq_ai_bot.time.models import TimeContext

_OCCURRED = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


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
        ledger=MagicMock(),
        people=MagicMock(),
        time_service=MagicMock(),
        rollup_repository=MagicMock(),
        rollup_service=MagicMock(),
    )


def _time() -> TimeContext:
    return TimeContext(utc=_OCCURRED, local=_OCCURRED, timezone="Asia/Shanghai")


def _assembled(
    *,
    history: tuple[ChatMessage, ...],
    current: ChatMessage,
    metadata: dict[str, object] | None = None,
    rollup_text: str = "",
) -> AssembledContext:
    return AssembledContext(
        metadata_payload=metadata or {},
        history_messages=history,
        current_message=current,
        recent_delivery=(),
        current_time=_time(),
        metrics=ContextMetrics(
            metadata_characters=0,
            history_characters=0,
            history_messages=len(history),
            current_message_characters=len(current.content or ""),
            raw_history_window_shifted=False,
        ),
        rollup_text=rollup_text,
        prompt_conversation_id="00000000-0000-4000-8000-000000000001",
        prompt_scope_key="private:8000:1001",
        prompt_generation=1,
        prompt_effective_coverage=0,
        prompt_rollup_revision=0,
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
    assert "[20:00:03｜Yuki主动消息｜由外部事件 #2 触发｜source=github｜type=PullRequestEvent]" in (
        first_episode
    )
    assert "first proactive part" in first_episode
    assert "second proactive part" in first_episode
    assert (rendered[2][2].content or "").count("由外部事件 #5 触发") == 1
    assert "历史来源未知" in (rendered[3][2].content or "")
    assert first_part.content == "first proactive part"
    assert second_part.content == "second proactive part"


def test_rollup_source_projection_preserves_proactive_cause_label() -> None:
    event = replace(
        _message(9, "release note", sender="8000", direction="outbound"),
        author_kind="yuki",
        origin="plugin_background",
        caused_by_event_id=8,
        caused_by_external_source="github",
        caused_by_external_event_type="ReleaseEvent",
    )

    projected = rollup_source_projection(event)

    assert "由外部事件 #8 触发" in projected
    assert "source=github" in projected
    assert "type=ReleaseEvent" in projected
    assert "release note" in projected


def test_current_external_carrier_is_untrusted_user_once() -> None:
    current = _external(9, "untrusted summary", payload={"body": "do not leak"})
    renderer = ChatEventPromptRenderer((current,))
    message = renderer.reference_message(current)
    assert message.role == "user"
    assert "untrusted summary" in (message.content or "")
    assert "do not leak" not in (message.content or "")
    history = renderer.main_agent_history((current,))
    assert history == ()


def test_bounded_history_keeps_current_external_out_of_main_history() -> None:
    history_rows = (_message(1, "hello"), _external(2, "earlier notice"))
    current = _external(3, "current trigger", payload={"token": "abc"})
    trigger = ExternalEventTurnTrigger(
        plugin_id="github-monitor",
        source_event_id=current.id,
        target_type="private",
        target_id="1001",
        agent_intent="comment briefly",
    )
    bounded = ContextAssembler._bounded_history(
        history_rows,
        current_event_id=current.id,
        content=current.content,
        yuki_account_ids=frozenset({"8000"}),
        current_message_override=ContextAssembler._external_wakeup_message(current, trigger),
        current_event=current,
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
    assembler._ensure_turn_generation = AsyncMock()  # type: ignore[method-assign]
    assembler._load_history_snapshot = AsyncMock(  # type: ignore[method-assign]
        return_value=_HistoryPromptWindow(
            recent=recent,
            rollup_text="",
            coverage_end=0,
            revision=1,
            rollup=None,
            rollup_mode="llm",
        )
    )
    assembler._people.get = AsyncMock(
        return_value=UserProfileSnapshot(
            user_id=target_id,
            scope_type=ScopeType.PRIVATE,
            nickname="Ada",
        )
    )
    assembler._people.aliases = AsyncMock(return_value=())
    assembler._time.current = AsyncMock(return_value=_time())
    assembler._time.current_default = MagicMock(return_value=_time())
    identity = current.scope
    turn = ConversationTurnSnapshot(
        conversation_id="test-conversation-1",
        scope_key=identity.key,
        generation=1,
        trigger_event_id=current.id,
        coordinator_version=1,
        transport_scope_key=identity.key,
    )
    runtime = MagicMock()
    runtime.context.local_event_limit = 2_048
    runtime.context.window_tokens = 96_000
    runtime.context.compaction_window_tokens = 90_000
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
    items = context.metadata_payload["items"]
    assert isinstance(items, list)
    return {item["id"]: item["data"] for item in items if isinstance(item, dict)}


@pytest.mark.asyncio
async def test_private_wakeup_targets_the_person_without_an_actor() -> None:
    current = replace(_external(3, "current trigger"), canonical_conversation_id="conv-private")

    assembler, context = await _assemble_wakeup(current)

    items = _metadata_items(context)
    assert items["scene"] == {
        "type": "private",
        "group_id": None,
        "trigger": "external_event",
        "current_actor": None,
    }
    # The target is the conversation person, never a fabricated current speaker.
    assert "current_person" not in items
    assert "current_group" not in items
    target = items["conversation_target_person"]
    assert isinstance(target, dict)
    assert target["user_id"] == "1001" and target["not_current_speaker"] is True
    assembler._people.get.assert_awaited_once_with(user_id="1001")
    assembler._time.current.assert_awaited_once_with("1001")
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

    assembler, context = await _assemble_wakeup(current, target_type="group", target_id="group-100")

    items = _metadata_items(context)
    assert items["scene"] == {
        "type": "group",
        "group_id": "group-100",
        "trigger": "external_event",
        "current_actor": None,
    }
    assert "conversation_target_person" not in items and "current_person" not in items
    assembler._people.get.assert_not_called()


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
    assert context.external_events == ()
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


def test_digest_items_are_required_contributions_within_budget() -> None:
    assembler = _assembler()
    events = (
        {
            "source": "github",
            "source_plugin_id": "github-monitor",
            "event_type": "PushEvent",
            "occurred_at": "2026-08-26T20:00:00+08:00",
            "summary": "bounded",
            "content_trust": EXTERNAL_EVENT_CONTENT_TRUST,
        },
    )
    contributions = assembler._context_contributions(
        {"scene": {"type": "private"}, "recent_external_events": list(events)}
    )
    digest_items = tuple(item for item in contributions if item.id == "recent_external_events")
    assert len(digest_items) == 1
    assert digest_items[0].required
    assert digest_items[0].cost == external_event_digest_appended_growth(events)
    selected = ContextBudgeter().select(contributions, character_budget=4_000)
    assert any(item.id == "recent_external_events" for item in selected.selected)


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
    snapshot = _HistoryPromptWindow(
        recent=recent,
        rollup_text="stable rollup before both current turns",
        coverage_end=5,
        revision=9,
        rollup=None,
        rollup_mode="llm",
        starts_after_event_id=0,
    )
    runtime = MagicMock()
    runtime.context.local_event_limit = 2_048
    runtime.context.window_tokens = 96_000
    runtime.context.compaction_window_tokens = 90_000

    ordinary_event = replace(
        _message(10, "ordinary current turn"),
        canonical_conversation_id="conv-stable",
    )
    ordinary = _assembler()
    ordinary._ensure_turn_generation = AsyncMock()  # type: ignore[method-assign]
    ordinary._load_history_snapshot = AsyncMock(  # type: ignore[method-assign]
        return_value=snapshot
    )
    ordinary._ledger.get_event = AsyncMock(return_value=ordinary_event)
    ordinary._people.prompt_metadata = AsyncMock(
        return_value=PersonPromptMetadata("synthetic-person", (), _time().timezone)
    )
    ordinary._time.default_timezone = _time().timezone
    ordinary._time.current_in_timezone = MagicMock(return_value=_time())
    ordinary_identity = ordinary_event.scope
    turn = ConversationTurnSnapshot(
        conversation_id="test-conversation-1",
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
    wakeup._ensure_turn_generation = AsyncMock()  # type: ignore[method-assign]
    wakeup._load_history_snapshot = AsyncMock(  # type: ignore[method-assign]
        return_value=snapshot
    )
    wakeup._people.get = AsyncMock(
        return_value=UserProfileSnapshot(
            user_id="1001",
            scope_type=ScopeType.PRIVATE,
            nickname="Ada",
        )
    )
    wakeup._people.aliases = AsyncMock(return_value=())
    wakeup._time.current = AsyncMock(return_value=_time())
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
    assert ordinary_context.external_events == wakeup_context.external_events == ()
    assert "recent_external_events" not in json.dumps(ordinary_context.metadata_payload)
    assert "recent_external_events" not in json.dumps(wakeup_context.metadata_payload)
    assert early_marker in "\n".join(
        message.content or "" for message in wakeup_context.history_messages
    )
    assert wakeup_context.rollup_text == ordinary_context.rollup_text == snapshot.rollup_text
    assert (
        wakeup_context.prompt_conversation_id
        == ordinary_context.prompt_conversation_id
        == turn.conversation_id
    )
    assert wakeup_context.prompt_scope_key == ordinary_context.prompt_scope_key == turn.scope_key
    assert wakeup_context.prompt_generation == ordinary_context.prompt_generation == turn.generation
    assert (
        wakeup_context.prompt_effective_coverage == ordinary_context.prompt_effective_coverage == 5
    )
    assert wakeup_context.prompt_rollup_revision == ordinary_context.prompt_rollup_revision == 9
    assert wakeup_context.prompt_raw_tail_end_event_id == (
        ordinary_context.prompt_raw_tail_end_event_id
    )
    target_person = next(
        item
        for item in wakeup_context.metadata_payload["items"]  # type: ignore[index]
        if isinstance(item, dict) and item.get("id") == "conversation_target_person"
    )
    assert target_person["data"] == {
        "user_id": "1001",
        "nickname": "Ada",
        "display_name": "Ada",
        "not_current_speaker": True,
    }
    assert wakeup_context.current_message != ordinary_context.current_message
    assert "external_event_wakeup" in (wakeup_context.current_message.content or "")


def test_external_wakeup_uses_the_same_main_agent_prompt_program() -> None:
    settings = make_settings("sqlite+aiosqlite:///:memory:")
    composer = PromptComposer(settings)
    current = ChatMessage(role="user", content="[external] current summary")
    history = (ChatMessage(role="user", content="hello from a person"),)
    context = _assembled(
        history=history,
        current=current,
        metadata={
            "items": [
                {
                    "id": "recent_external_events",
                    "data": {
                        "events": [
                            {
                                "source": "github",
                                "source_plugin_id": "github-monitor",
                                "event_type": "PushEvent",
                                "summary": "untrusted digest",
                                "content_trust": EXTERNAL_EVENT_CONTENT_TRUST,
                            }
                        ]
                    },
                }
            ]
        },
    )
    runtime = MagicMock()
    runtime.context.window_tokens = 96_000
    runtime.context.compaction_window_tokens = 90_000
    runtime.plugins.max_total_prompt_characters = 8_000
    composed = composer.compose(
        inbound=None,
        context=context,
        runtime=runtime,
        visual_observation=None,
        visual_failure=False,
        scope_type=ScopeType.PRIVATE,
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
        visual_observation=None,
        visual_failure=False,
    )
    system_text = "\n".join(
        item.content or "" for item in composed.messages if item.role == "system"
    )
    assert "github-monitor" not in system_text
    assert "PushEvent" not in system_text
    assert "【资料与查证】" in system_text
    assert "已有材料充分时不重复查询" in system_text
    assert "不代表长期记忆不存在" in system_text
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
    assert composed.metrics.conversation_prefix_hash
    assert "hello from a person" not in composed.metrics.conversation_prefix_hash
    repeat = composer.compose(
        inbound=None,
        context=context,
        runtime=runtime,
        visual_observation=None,
        visual_failure=False,
        scope_type=ScopeType.PRIVATE,
    )
    assert tuple((item.role, item.content) for item in repeat.messages) == tuple(
        (item.role, item.content) for item in composed.messages
    )
    assert repeat.metrics.conversation_prefix_hash == composed.metrics.conversation_prefix_hash
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

    from qq_ai_bot.services.main_agent_contract import MainAgentContract

    provider = FakeLLMProvider(lambda _request: "ok")
    harness = build_harness(database, make_settings(database.url), provider)
    chat = harness.processor._chat
    chat.runtime.runner.main_contract = MainAgentContract(
        chat, SimpleNamespace(snapshot=lambda: [])
    )
    runtime_config = await chat._runtime_config.snapshot(user_id="1001", group_id=None)
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
        conversation_id="conv-stable",
        presence_id="presence-stable",
        yuki_account_ids=frozenset({"8000"}),
    )
    shared = {
        "gateway": None,
        "allow_generic_onebot": False,
        "allow_admin_actions": False,
        "allow_automation": True,
        "conversation_key": "canonical:conv-stable:generation:1",
        "actor_is_superuser": False,
        "runtime_config": runtime_config,
        "tools_closed": False,
        "read_only": False,
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
    assert ordinary_request.request_shape_hash == wakeup_request.request_shape_hash
    ordinary_shape = provider_cache_shape_diagnostics(
        ordinary_request,
        provider="fake",
        model=ordinary_request.model,
        profile_id="legacy",
        protocol="chat_completions",
    )
    wakeup_shape = provider_cache_shape_diagnostics(
        wakeup_request,
        provider="fake",
        model=wakeup_request.model,
        profile_id="legacy",
        protocol="chat_completions",
    )
    assert ordinary_shape == wakeup_shape
    assert all(
        len(value) == 64
        for value in (
            ordinary_shape.provider_shape_hash,
            ordinary_shape.instructions_hash,
            ordinary_shape.tools_hash,
            ordinary_shape.input_prefix_hash,
        )
    )


def test_provider_cache_shape_excludes_only_the_current_user_tail() -> None:
    base = ChatRequest(
        messages=(
            ChatMessage(role="system", content="stable instructions"),
            ChatMessage(role="user", content="stable history"),
            ChatMessage(role="assistant", content="stable reply"),
            ChatMessage(role="user", content="ordinary current tail"),
        ),
        model="deepseek-v4-flash",
        temperature=None,
        max_output_tokens=2_000,
        thinking_enabled=True,
    )

    def diagnostics(request: ChatRequest):
        return provider_cache_shape_diagnostics(
            request,
            provider="deepseek",
            model=request.model,
            profile_id="main",
            protocol="responses",
        )

    baseline = diagnostics(base)
    other_tail = diagnostics(
        replace(
            base,
            messages=(*base.messages[:-1], ChatMessage(role="user", content="external wakeup")),
        )
    )
    changed_history = diagnostics(
        replace(
            base,
            messages=(
                base.messages[0],
                ChatMessage(role="user", content="different stable history"),
                *base.messages[2:],
            ),
        )
    )
    changed_limits = diagnostics(replace(base, max_output_tokens=4_000))

    assert baseline == other_tail
    assert baseline.input_prefix_hash != changed_history.input_prefix_hash
    assert baseline.provider_shape_hash != changed_history.provider_shape_hash
    assert baseline.provider_shape_hash != changed_limits.provider_shape_hash


@pytest.mark.asyncio
async def test_plugin_wakeup_can_end_without_creating_a_fake_reply(
    database: Database,
) -> None:
    harness = build_harness(database, make_settings(database.url))
    chat = harness.processor._chat
    runtime_config = await chat._runtime_config.snapshot(user_id="1001", group_id=None)
    runtime = ToolRuntime(
        inbound=None,
        gateway=None,
        allow_generic_onebot=False,
        allow_admin_actions=False,
        allow_automation=True,
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
    assert "send_message" in names
    assert "decline_reply" not in names


@pytest.mark.asyncio
async def test_plugin_wakeup_read_tools_use_canonical_target_without_a_fake_actor(
    database: Database,
    tmp_path,
) -> None:
    from qq_ai_bot.services.main_agent_contract import MainAgentContract
    from qq_ai_bot.workspace.short_state import ShortState
    from qq_ai_bot.workspace.store import WorkspaceStore

    harness = build_harness(
        database, make_settings(database.url, agent_tool_result_max_characters=24000)
    )
    contract = MainAgentContract(
        harness.processor._chat, ShortState(WorkspaceStore(tmp_path / "state"))
    )
    frozen = await contract.definitions()
    frozen_around = next(item for item in frozen if item.name == "get_chat_history_around")
    assert frozen_around.parameters["required"] == ["event_id"]
    assert "platform_message_id" not in frozen_around.parameters["properties"]
    assert contract.health()["frozen"] is True
    assert contract.revision
    tools = harness.processor._chat._tools
    runtime_config = await harness.processor._chat._runtime_config.snapshot(
        user_id="1001",
        group_id=None,
    )
    gateway = MagicMock()
    gateway.provider_id = "snowluma"
    gateway.call_api = AsyncMock(return_value={"messages": []})
    group_runtime = ToolRuntime(
        inbound=None,
        gateway=gateway,
        allow_generic_onebot=False,
        conversation_key="canonical:space-100:generation:1",
        trigger_message_id="external-current",
        trigger_event_id=2,
        runtime_config=runtime_config,
        origin=TurnOrigin.PLUGIN_BACKGROUND,
        scope_type=ScopeType.GROUP,
        conversation_id="space-100",
        space_id="space-100",
        external_target_id="group-100",
        read_scope=ConversationScope.group("8000", "group-100"),
    )
    tools._memories.list_group = AsyncMock(return_value=())  # type: ignore[method-assign]
    tools._ledger.search = AsyncMock(return_value=())  # type: ignore[method-assign]
    recent = (await tools.execute("get_recent_chat_history", "{}", group_runtime)).model_payload()
    actual_record = EventRecord(
        id=123,
        visual_summary="",
        occurred_at=datetime.now(UTC),
        bot_user_id="8000",
        platform_message_id="history-attachment",
        scope_type=ScopeType.GROUP,
        sender_user_id="1001",
        group_id="group-100",
        direction="inbound",
        content="render this",
        segments=({"type": "video", "data": {"name": "clip.mp4"}},),
    )
    tools._ledger.list_scope_around = AsyncMock(return_value=(actual_record, (), ()))
    around_tool = next(
        tool for tool in tools.definitions(group_runtime) if tool.name == "get_chat_history_around"
    )
    assert around_tool.parameters["required"] == ["event_id"]
    assert "platform_message_id" not in around_tool.parameters["properties"]
    invalid_history = (
        await tools.execute(
            "get_chat_history_around", '{"platform_message_id":"history-attachment"}', group_runtime
        )
    ).model_payload()
    assert not invalid_history["ok"]
    assert tools._ledger.list_scope_around.await_count == 0
    actual_history = (
        await tools.execute(
            "get_chat_history_around", json.dumps({"event_id": actual_record.id}), group_runtime
        )
    ).model_payload()
    assert actual_history["ok"]
    assert actual_history["data"]["events"][0]["attachments"] == [
        {"attachment_index": 0, "kind": "video", "name": "clip.mp4"}
    ]
    tools._ledger.list_scope_around = AsyncMock(return_value=(None, (), ()))
    around = (
        await tools.execute("get_chat_history_around", '{"event_id":999}', group_runtime)
    ).model_payload()
    scoped_search = (
        await tools.execute(
            "search_chat_history",
            '{"keyword":"release","user_id":"9999","group_id":"other-group"}',
            group_runtime,
        )
    ).model_payload()


    assert recent["ok"] is True
    assert recent["data"]["source"] == "ledger"
    assert recent["data"]["events"] == []
    assert around["error_code"] == "not_found"
    assert scoped_search["error_code"] == "history_scope_denied"
    valid_search = (
        await tools.execute("search_chat_history", '{"keyword":"release"}', group_runtime)
    ).model_payload()
    assert valid_search["ok"] is True
    assert valid_search["data"] == {"events": [], "returned_count": 0, "truncated": False}
    tools._ledger.search = AsyncMock(return_value=[replace(actual_record, content="x" * 1000)] * 12)
    longer_search = (
        await tools.execute("search_chat_history", '{"keyword":"release"}', group_runtime)
    ).model_payload()
    assert longer_search["ok"]
    assert longer_search["data"]["returned_count"] == 12
    assert not longer_search["data"]["truncated"]
    tools._ledger.search = AsyncMock(return_value=[replace(actual_record, content="x" * 2000)] * 20)
    bounded_search = (
        await tools.execute("search_chat_history", '{"keyword":"release"}', group_runtime)
    ).model_payload()
    assert bounded_search["ok"]
    assert bounded_search["data"]["truncated"]
    assert 0 < bounded_search["data"]["returned_count"] < 20
    assert bounded_search["data"]["returned_count"] == len(bounded_search["data"]["events"])
    gateway.call_api.assert_not_awaited()
    assert tools._ledger.search.await_args.kwargs["group_id"] == "group-100"
    assert tools._ledger.search.await_args.kwargs["user_id"] is None
    assert "memory_change" not in {tool.name for tool in tools.definitions(group_runtime)}


def _digest_item_with_encoded_size(target: int) -> dict[str, object]:
    item: dict[str, object] = {
        "source": "github",
        "source_plugin_id": "github-monitor",
        "event_type": "PushEvent",
        "occurred_at": "2026-08-26T20:00:00+08:00",
        "summary": "",
        "content_trust": EXTERNAL_EVENT_CONTENT_TRUST,
    }
    padding = target - external_event_digest_encoded_characters((item,))
    assert padding > 0
    item["summary"] = "x" * padding
    assert external_event_digest_encoded_characters((item,)) == target
    return item


def test_digest_parent_comma_stays_within_contribution_cost() -> None:
    cap = 400
    assembler = _assembler()
    context: dict[str, object] = {
        "scene": {"type": "private", "group_id": None},
        "current_person": {"user_id": "1001", "nickname": "Ada", "display_name": "Ada"},
    }
    parent = assembler._fit_metadata(context, 4_000)
    items = parent["items"]
    assert isinstance(items, list)
    assert items
    exact = (_digest_item_with_encoded_size(cap - 1),)
    assert external_event_digest_appended_growth(exact) == cap
    with_digest = {**context, "recent_external_events": list(exact)}
    contributions = assembler._context_contributions(with_digest)
    digest_item = next(item for item in contributions if item.id == "recent_external_events")
    assert digest_item.required
    assert digest_item.cost == external_event_digest_appended_growth(exact)
    # The real metadata fit appends exactly the costed growth, comma included.
    payload = assembler._fit_metadata(with_digest, 8_000)
    before = json.dumps(parent, ensure_ascii=False, separators=(",", ":"), default=str)
    after = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    assert len(after) - len(before) == digest_item.cost == cap
    assert payload["items"][-1] == {  # type: ignore[index]
        "id": "recent_external_events",
        "data": {"events": list(exact), "content_trust": EXTERNAL_EVENT_CONTENT_TRUST},
    }


def _covered_external_turn(
    *,
    coverage_end: int,
    rollup_mode: str,
    marker: str,
    starts_after_event_id: int = 0,
) -> tuple[ContextAssembler, EventRecord, ConversationTurnSnapshot, MagicMock]:
    event = replace(
        _external(10, marker),
        canonical_conversation_id="conv-covered",
    )
    assembler = _assembler()
    assembler._ensure_turn_generation = AsyncMock()  # type: ignore[method-assign]
    assembler._load_history_snapshot = AsyncMock(  # type: ignore[method-assign]
        return_value=_HistoryPromptWindow(
            recent=(),
            rollup_text=f"rollup already contains {marker}",
            coverage_end=coverage_end,
            revision=1,
            rollup=None,
            rollup_mode=rollup_mode,
            starts_after_event_id=starts_after_event_id,
        )
    )
    turn = ConversationTurnSnapshot(
        conversation_id="test-conversation-1",
        scope_key="bot:8000:private:1001",
        generation=1,
        trigger_event_id=event.id,
        coordinator_version=1,
        transport_scope_key="bot:8000:private:1001",
    )
    runtime = MagicMock()
    runtime.context.local_event_limit = 2_048
    runtime.context.window_tokens = 96_000
    runtime.context.compaction_window_tokens = 90_000
    return assembler, event, turn, runtime


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("coverage_end", "rollup_mode"),
    (
        (10, "extractive"),
        (12, "emergency"),
    ),
)
async def test_actorless_main_context_fails_closed_when_current_source_is_covered(
    coverage_end: int,
    rollup_mode: str,
) -> None:
    marker = "UNIQUE-CURRENT-EXTERNAL-MARKER"
    assembler, event, turn, runtime = _covered_external_turn(
        coverage_end=coverage_end,
        rollup_mode=rollup_mode,
        marker=marker,
    )
    with pytest.raises(ConversationCoverageError) as exc:
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
                agent_intent="comment",
            ),
        )
    assert str(exc.value) == "external trigger is already covered"
    assert marker not in str(exc.value)
    assert str(event.id) not in str(exc.value)
    # The covered rollup text and the isolated current carrier would both show
    # the marker if prompt composition ran. Assemble must not return a prompt.
    isolated_current = ChatEventPromptRenderer((event,)).render_reference_event(event)
    assert marker in isolated_current
