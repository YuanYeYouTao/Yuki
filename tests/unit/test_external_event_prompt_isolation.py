"""C1 external-event prompt isolation: history, carrier, digest, and trusted policy."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from tests.conftest import make_settings

from qq_ai_bot.conversation.rollup.errors import ConversationCoverageError
from qq_ai_bot.conversation.scope import ConversationTurnSnapshot
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import ChatMessage, InboundMessage, SenderIdentity
from qq_ai_bot.event_prompt import (
    EXTERNAL_EVENT_CONTENT_TRUST,
    EXTERNAL_EVENT_DIGEST_SUMMARY_MAX_CHARACTERS,
    ChatEventPromptRenderer,
    external_event_digest_appended_growth,
    external_event_digest_encoded_characters,
    recent_external_event_digest,
)
from qq_ai_bot.llm.deepseek_responses import DeepSeekResponsesProvider
from qq_ai_bot.persistence.repository_records import EventRecord
from qq_ai_bot.prompting import ContextBudgeter
from qq_ai_bot.services.context_assembler import (
    AssembledContext,
    ContextAssembler,
    ContextMetrics,
    _HistoryPromptWindow,
)
from qq_ai_bot.services.prompt_composer import EXTERNAL_EVENT_HOST_POLICY, PromptComposer
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
    inbound = InboundMessage(
        message_id=current.platform_message_id,
        event_type="external_event",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(user_id="1001"),
        text=current.content,
        bot_user_id="8000",
    )
    bounded = ContextAssembler._bounded_history(
        history_rows,
        inbound=inbound,
        content=current.content,
        current_event=current,
    )
    assert bounded.current_message.role == "user"
    assert "current trigger" in (bounded.current_message.content or "")
    assert all(item.role != "system" for item in bounded.history_messages)
    assert all("current trigger" not in (item.content or "") for item in bounded.history_messages)
    assert all("earlier notice" not in (item.content or "") for item in bounded.history_messages)
    assert all("abc" not in (item.content or "") for item in bounded.history_messages)


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


def test_trusted_external_policy_is_host_text_only() -> None:
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
    composed = composer.compose_external(
        context=context,
        runtime=runtime,
        source_plugin_id="github-monitor",
        external_source="github",
        event_type="PushEvent",
        agent_intent="comment on the pull request",
    )
    system_text = "\n".join(
        item.content or "" for item in composed.messages if item.role == "system"
    )
    assert EXTERNAL_EVENT_HOST_POLICY in system_text
    assert "github-monitor" not in system_text
    assert "PushEvent" not in system_text
    user_messages = tuple(item for item in composed.messages if item.role == "user")
    assert len(user_messages) == 2
    assert current.content in (user_messages[-1].content or "")
    history_blob = "\n".join(
        item.content or "" for item in composed.messages if item.role != "system"
    )
    assert history_blob.count("current summary") == 1
    assert composed.metrics.conversation_prefix_hash
    assert "hello from a person" not in composed.metrics.conversation_prefix_hash
    repeat = composer.compose_external(
        context=context,
        runtime=runtime,
        source_plugin_id="github-monitor",
        external_source="github",
        event_type="PushEvent",
        agent_intent="comment on the pull request",
    )
    assert tuple((item.role, item.content) for item in repeat.messages) == tuple(
        (item.role, item.content) for item in composed.messages
    )
    assert repeat.metrics.conversation_prefix_hash == composed.metrics.conversation_prefix_hash
    instructions, inputs = DeepSeekResponsesProvider._convert_messages(composed.messages)
    assert EXTERNAL_EVENT_HOST_POLICY in instructions
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
    assembler._memory_context.retrieve_for_turn = AsyncMock()
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
async def test_assemble_external_fails_closed_when_current_source_is_covered(
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
        await assembler.assemble_external(
            event=event,
            turn=turn,
            authorization_user_id="1001",
            runtime=runtime,
            agent_intent="comment",
            conversation_id="conv-covered",
        )
    assert str(exc.value) == "external trigger is already covered"
    assert marker not in str(exc.value)
    assert str(event.id) not in str(exc.value)
    assembler._memory_context.retrieve_for_turn.assert_not_called()
    # The covered rollup text and the isolated current carrier would both show
    # the marker if compose_external ran. Assemble must not return a prompt.
    isolated_current = ChatEventPromptRenderer((event,)).render_reference_event(event)
    assert marker in isolated_current
