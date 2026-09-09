"""External-event isolation with stable Main-Agent prompt composition."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from tests.conftest import build_harness, make_settings

from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.conversation.delivery import ReplyControlState, default_reply_spec
from qq_ai_bot.conversation.rollup.errors import ConversationCoverageError
from qq_ai_bot.conversation.rollup.renderer import rollup_source_projection
from qq_ai_bot.conversation.scope import ConversationTurnSnapshot
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import ChatMessage, ChatRequest, InboundMessage, SenderIdentity
from qq_ai_bot.domain.profiles import UserProfileSnapshot
from qq_ai_bot.event_prompt import (
    EXTERNAL_EVENT_CONTENT_TRUST,
    EXTERNAL_EVENT_DIGEST_SUMMARY_MAX_CHARACTERS,
    ChatEventPromptRenderer,
    external_event_digest_appended_growth,
    external_event_digest_encoded_characters,
    recent_external_event_digest,
)
from qq_ai_bot.llm.deepseek_responses import DeepSeekResponsesProvider
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.memory.enums import MemoryTargetRole
from qq_ai_bot.model_runtime.executor import provider_cache_shape_diagnostics
from qq_ai_bot.persistence.database import Database
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
from qq_ai_bot.services.reply_target import ReplyTargetControl
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
        memory_context=MagicMock(),
        relationships=MagicMock(),
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
        current_relationship=None,
        metrics=ContextMetrics(
            metadata_characters=0,
            history_characters=0,
            history_messages=len(history),
            current_message_characters=len(current.content or ""),
            raw_history_window_shifted=False,
        ),
        rollup_text=rollup_text,
        prompt_scope_id=1,
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
    assert "[Yuki主动消息｜由外部事件 #2 触发｜source=github｜type=PullRequestEvent]" in (
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
        current_message_id=current.platform_message_id,
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


def test_private_wakeup_memory_targets_include_person_without_an_actor() -> None:
    current = _external(3, "current trigger")
    trigger = ExternalEventTurnTrigger(
        plugin_id="github-monitor",
        source_event_id=current.id,
        target_type="private",
        target_id="1001",
        agent_intent="comment briefly",
    )

    targets = ContextAssembler._actorless_memory_targets(current, trigger)

    assert {target.role for target in targets} == {
        MemoryTargetRole.CURRENT_SELF,
        MemoryTargetRole.CURRENT_PERSON,
    }
    person = next(target for target in targets if target.role is MemoryTargetRole.CURRENT_PERSON)
    assert person.subject_user_id == "1001"


def test_digest_has_host_fields_only_excludes_current_and_caps_summary() -> None:
    huge = "x" * 2_000
    rows = (
        _external(1, "old", payload={"body": "secret-1"}),
        _message(2, "hello"),
        _external(3, huge, payload={"body": "secret-3"}),
        _external(4, "current", payload={"body": "secret-4"}),
    )
    digest = recent_external_event_digest(
        rows,
        timezone="Asia/Shanghai",
        exclude_event_id=4,
        limit=10,
        character_limit=6_000,
    )
    assert tuple(item["source"] for item in digest) == ("github", "github")
    for item in digest:
        assert set(item) == {
            "source",
            "source_plugin_id",
            "event_type",
            "occurred_at",
            "summary",
            "content_trust",
        }
        assert "payload" not in item
        assert item["content_trust"] == EXTERNAL_EVENT_CONTENT_TRUST
        assert len(str(item["summary"])) <= EXTERNAL_EVENT_DIGEST_SUMMARY_MAX_CHARACTERS
        encoded = json.dumps(item)
        assert "secret-" not in encoded
        assert "body" not in encoded
    assert all(item["summary"] != "current" for item in digest)
    assert any(
        len(str(item["summary"])) == EXTERNAL_EVENT_DIGEST_SUMMARY_MAX_CHARACTERS for item in digest
    )


def test_digest_respects_total_character_budget() -> None:
    rows = tuple(_external(index, f"summary-{index}" + ("y" * 200)) for index in range(1, 8))
    newest = recent_external_event_digest(
        rows,
        timezone="Asia/Shanghai",
        limit=1,
        character_limit=6_000,
    )
    newest_growth = external_event_digest_appended_growth(newest)
    digest = recent_external_event_digest(
        rows,
        timezone="Asia/Shanghai",
        limit=10,
        character_limit=newest_growth,
    )
    assert digest == newest
    assert external_event_digest_appended_growth(digest) <= newest_growth
    assert all("payload" not in item for item in digest)
    skipped = recent_external_event_digest(
        rows,
        timezone="Asia/Shanghai",
        limit=10,
        character_limit=newest_growth - 1,
    )
    assert skipped == ()
    two = recent_external_event_digest(
        rows,
        timezone="Asia/Shanghai",
        limit=2,
        character_limit=6_000,
    )
    two_growth = external_event_digest_appended_growth(two)
    assert len(two) == 2
    almost_two = recent_external_event_digest(
        rows,
        timezone="Asia/Shanghai",
        limit=10,
        character_limit=two_growth - 1,
    )
    assert len(almost_two) == 1
    assert almost_two[0]["summary"].startswith("summary-7")


def test_digest_summary_cap_default_and_settings_override() -> None:
    default_settings = make_settings("sqlite+aiosqlite:///:memory:")
    assert (
        default_settings.plugin_external_event_summary_characters
        == EXTERNAL_EVENT_DIGEST_SUMMARY_MAX_CHARACTERS
    )
    default_digest = _assembler()._external_event_context((_external(1, "x" * 2_000),))
    assert len(str(default_digest[0]["summary"])) == EXTERNAL_EVENT_DIGEST_SUMMARY_MAX_CHARACTERS
    override = 24
    overridden = _assembler(
        plugin_external_event_summary_characters=override
    )._external_event_context((_external(1, "x" * 2_000),))
    assert len(str(overridden[0]["summary"])) == override


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


def test_assembler_attaches_digest_from_final_uncovered_tail() -> None:
    assembler = _assembler()
    covered = _external(1, "should vanish after coverage")
    remaining = _external(3, "still uncovered")
    current = _external(4, "current trigger")
    payload = assembler._with_external_digest(
        {"items": [{"id": "scene", "data": {"type": "private"}}]},
        assembler._external_event_context((covered, remaining, current), exclude_event_id=4),
    )
    digest = next(
        item["data"]
        for item in payload["items"]  # type: ignore[index]
        if isinstance(item, dict) and item.get("id") == "recent_external_events"
    )
    assert isinstance(digest, dict)
    events = digest["events"]
    assert isinstance(events, list)
    summaries = [item["summary"] for item in events if isinstance(item, dict)]
    assert "still uncovered" in summaries
    assert "should vanish after coverage" in summaries
    final_only = assembler._external_event_context((remaining,), exclude_event_id=4)
    replaced = assembler._with_external_digest(payload, final_only)
    final_digest = next(
        item["data"]
        for item in replaced["items"]  # type: ignore[index]
        if isinstance(item, dict) and item.get("id") == "recent_external_events"
    )
    assert isinstance(final_digest, dict)
    final_events = final_digest["events"]
    assert isinstance(final_events, list)
    assert [item["summary"] for item in final_events if isinstance(item, dict)] == [
        "still uncovered"
    ]


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
    empty_retrieval = MagicMock(blocks=(), hits=())

    ordinary_event = replace(
        _message(10, "ordinary current turn"),
        canonical_conversation_id="conv-stable",
    )
    ordinary = _assembler(relationship_enabled=False)
    ordinary._ensure_lightweight_backlog = AsyncMock()  # type: ignore[method-assign]
    ordinary._load_history_snapshot = AsyncMock(  # type: ignore[method-assign]
        return_value=snapshot
    )
    ordinary._ledger.get_event = AsyncMock(return_value=ordinary_event)
    ordinary._memory_context.retrieve_for_turn = AsyncMock(return_value=empty_retrieval)
    ordinary._people.aliases = AsyncMock(return_value=())
    ordinary._time.current = AsyncMock(return_value=_time())
    ordinary_identity = ordinary_event.scope
    turn = ConversationTurnSnapshot(
        scope_id=1,
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
        persist_memory_exposure=False,
    )

    external_event = replace(
        _external(10, "external current turn"),
        canonical_conversation_id="conv-stable",
    )
    wakeup = _assembler(relationship_enabled=False)
    wakeup._ensure_lightweight_backlog = AsyncMock()  # type: ignore[method-assign]
    wakeup._load_history_snapshot = AsyncMock(  # type: ignore[method-assign]
        return_value=snapshot
    )
    wakeup._memory_context.retrieve_for_targets = AsyncMock(return_value=empty_retrieval)
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
    assert early_marker in "\n".join(
        message.content or "" for message in wakeup_context.history_messages
    )
    assert wakeup_context.rollup_text == ordinary_context.rollup_text == snapshot.rollup_text
    assert wakeup_context.prompt_scope_id == ordinary_context.prompt_scope_id == turn.scope_id
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
    assert "查证合同" in system_text
    assert "普通闲聊、创作和表达感受不强制查询" in system_text
    assert "不表示长期记忆不存在" in system_text
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
    instructions, inputs = DeepSeekResponsesProvider._convert_messages(composed.messages)
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
    database: Database,
) -> None:
    provider = FakeLLMProvider(lambda _request: "ok")
    harness = build_harness(database, make_settings(database.url), provider)
    chat = harness.processor._chat
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
        "current_group_id": None,
        "runtime_config": runtime_config,
        "tools_closed": False,
        "read_only": False,
        "reply_target_control": ReplyTargetControl(visible_event_ids=frozenset({1, 2})),
        "selection_query": "same capability-neutral query",
        "scope_type": ScopeType.PRIVATE,
        "bot_user_id": "8000",
        "conversation_id": "conv-stable",
        "presence_id": "presence-stable",
        "person_id": "person-stable",
        "external_target_id": "1001",
    }
    ordinary_runtime = ToolRuntime(
        inbound=ordinary_inbound,
        trigger_message_id="ordinary-current",
        actor_user_id="1001",
        origin=TurnOrigin.USER_MESSAGE,
        **shared,
    )
    wakeup_runtime = ToolRuntime(
        inbound=None,
        trigger_message_id="external-current",
        actor_user_id="",
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
async def test_plugin_wakeup_can_decline_without_creating_a_fake_reply(
    database: Database,
) -> None:
    harness = build_harness(database, make_settings(database.url))
    chat = harness.processor._chat
    runtime_config = await chat._runtime_config.snapshot(user_id="1001", group_id=None)
    control = ReplyControlState(
        spec=default_reply_spec(hard_max_messages=runtime_config.reply.hard_max_messages)
    )
    runtime = ToolRuntime(
        inbound=None,
        gateway=None,
        allow_generic_onebot=False,
        allow_admin_actions=False,
        allow_automation=True,
        conversation_key="canonical:conv-stable:generation:1",
        trigger_message_id="external-current",
        runtime_config=runtime_config,
        origin=TurnOrigin.PLUGIN_BACKGROUND,
        reply_control=control,
        scope_type=ScopeType.PRIVATE,
        bot_user_id="8000",
        conversation_id="conv-stable",
        presence_id="presence-stable",
        person_id="person-stable",
        external_target_id="1001",
    )

    definitions = chat._tools.definitions(runtime)
    assert "decline_reply" in {tool.name for tool in definitions}
    result = json.loads(
        await chat._tools.execute(
            "decline_reply",
            '{"reason_code":"not_relevant"}',
            runtime,
        )
    )
    assert result["ok"] is True
    assert control.declined is True


@pytest.mark.asyncio
async def test_plugin_wakeup_read_tools_use_canonical_target_without_a_fake_actor(
    database: Database,
) -> None:
    harness = build_harness(database, make_settings(database.url))
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
        actor_user_id="",
        runtime_config=runtime_config,
        origin=TurnOrigin.PLUGIN_BACKGROUND,
        scope_type=ScopeType.GROUP,
        bot_user_id="8000",
        conversation_id="space-100",
        presence_id="presence-stable",
        space_id="space-100",
        current_group_id="group-100",
        external_target_id="group-100",
    )
    tools._memories.list_group = AsyncMock(return_value=())  # type: ignore[method-assign]
    tools._ledger.search = AsyncMock(return_value=())  # type: ignore[method-assign]
    recent = json.loads(await tools.execute("get_recent_chat_history", "{}", group_runtime))
    group_memory = json.loads(
        await tools.execute(
            "get_group_memories",
            '{"group_id":"group-100"}',
            group_runtime,
        )
    )
    around = json.loads(
        await tools.execute("get_chat_history_around", '{"event_id":999}', group_runtime)
    )
    scoped_search = json.loads(
        await tools.execute(
            "search_chat_history",
            '{"keyword":"release","user_id":"9999","group_id":"other-group"}',
            group_runtime,
        )
    )
    relationship = json.loads(
        await tools.execute(
            "get_relationship",
            '{"user_id":"1001"}',
            group_runtime,
        )
    )

    assert recent["ok"] is True
    assert recent["data"]["source"] == "snowluma"
    assert recent["data"]["newly_recorded"] == 0
    assert group_memory == {
        "ok": True,
        "evidence_state": {
            "source": "memory_tool",
            "query_status": "empty",
            "returned_count": 0,
            "truncated": False,
            "partial_failure": False,
            "source_refs": [],
            "delivery": "staged",
        },
        "data": {
            "group_id": "group-100",
            "memories": [],
            "effective_query": {
                "mode": "overview",
                "purpose": "recall",
                "start_at": None,
                "end_at": None,
                "temporal_constraint": None,
                "interval": "start_inclusive_end_exclusive",
            },
        },
    }
    assert around["error"] == "not_found"
    assert scoped_search == {"ok": True, "data": {"events": []}}
    assert relationship["error"] == "permission_denied"
    gateway.call_api.assert_awaited_once_with(
        "get_group_msg_history",
        {"group_id": "group-100", "count": tools._settings.recent_history_tool_limit},
    )
    assert tools._ledger.search.await_args.kwargs["group_id"] == "group-100"
    assert tools._ledger.search.await_args.kwargs["user_id"] is None
    assert "memory_change" not in {tool.name for tool in tools.definitions(group_runtime)}


def _pad_external_to_encoded_size(event_id: int, target: int) -> EventRecord:
    content = "x"
    for _ in range(target + 8):
        row = _external(event_id, content)
        digest = recent_external_event_digest(
            (row,),
            timezone="Asia/Shanghai",
            limit=1,
            character_limit=10_000,
        )
        encoded = external_event_digest_encoded_characters(digest)
        if encoded == target:
            return row
        if encoded > target:
            raise AssertionError(f"digest item overshot encoded size {encoded} > {target}")
        content += "x" * max(1, target - encoded)
    raise AssertionError("could not pad digest item to target encoded size")


def test_digest_parent_comma_stays_within_context_cap() -> None:
    cap = 400
    assembler = _assembler(plugin_external_event_context_characters=cap)
    parent, _selected = assembler._fit_metadata(
        {
            "scene": {"type": "private", "group_id": None},
            "current_person": {
                "user_id": "1001",
                "nickname": "Ada",
                "display_name": "Ada",
            },
        },
        4_000,
    )
    items = parent["items"]
    assert isinstance(items, list)
    assert items
    exact = assembler._external_event_context((_pad_external_to_encoded_size(11, cap - 1),))
    assert external_event_digest_encoded_characters(exact) == cap - 1
    assert external_event_digest_appended_growth(exact) == cap
    payload = assembler._with_external_digest(parent, exact)
    before = json.dumps(parent, ensure_ascii=False, separators=(",", ":"), default=str)
    after = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    assert len(after) - len(before) == cap
    assert len(after) - len(before) <= assembler._settings.plugin_external_event_context_characters
    contributions = assembler._context_contributions(
        {
            "scene": {"type": "private", "group_id": None},
            "current_person": {"user_id": "1001", "nickname": "Ada", "display_name": "Ada"},
            "recent_external_events": list(exact),
        }
    )
    digest_item = next(item for item in contributions if item.id == "recent_external_events")
    assert digest_item.required
    assert digest_item.cost == external_event_digest_appended_growth(exact)
    selected = ContextBudgeter().select(contributions, character_budget=8_000)
    assert any(item.id == "recent_external_events" for item in selected.selected)
    oversized = assembler._external_event_context((_pad_external_to_encoded_size(12, cap),))
    assert oversized == ()


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
    assembler._ensure_lightweight_backlog = AsyncMock()  # type: ignore[method-assign]
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
    assembler._memory_context.retrieve_for_targets = AsyncMock()
    turn = ConversationTurnSnapshot(
        scope_id=1,
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
    assembler._memory_context.retrieve_for_targets.assert_not_called()
    # The covered rollup text and the isolated current carrier would both show
    # the marker if prompt composition ran. Assemble must not return a prompt.
    isolated_current = ChatEventPromptRenderer((event,)).render_reference_event(event)
    assert marker in isolated_current
