"""One preparation renderer retains span/day/cause/reference and page boundaries."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.domain.messages import ChatMessage
from qq_ai_bot.event_prompt import ChatEventPromptRenderer
from qq_ai_bot.persistence.repository_records import EventRecord
from qq_ai_bot.services.context_assembler import ContextAssembler, _HistoryPromptWindow


def record(identity, when):
    return EventRecord(
        identity,
        "bot",
        f"platform:{identity}",
        ScopeType.PRIVATE,
        "user",
        "inbound",
        f"message:{identity}",
        "",
        (),
        when,
        private_peer_user_id="user",
        sender_nickname="name",
        canonical_conversation_id="owner",
    )


def assembler():
    instance = object.__new__(ContextAssembler)
    instance._settings = SimpleNamespace(
        bot_display_name="Yuki",
        default_timezone="UTC",
        conversation_rollup_enabled=True,
        conversation_rollup_model_timeout_seconds=1,
        conversation_rollup_foreground_max_batches=1,
    )
    return instance


def test_render_views_exact_group_day_span_sender_cause_and_reply():
    begin = datetime(2026, 10, 4, 23, 50, tzinfo=UTC)
    rows = (
        record(1, begin),
        record(2, begin + timedelta(minutes=2)),
        record(3, begin + timedelta(minutes=6)),
        record(4, begin + timedelta(minutes=10)),
        replace(record(5, begin + timedelta(minutes=11)), reply_to_event_id=1),
        replace(record(6, begin + timedelta(minutes=12)), sender_user_id="another"),
        replace(
            record(7, begin + timedelta(minutes=13)), sender_user_id="another", caused_by_event_id=9
        ),
        replace(record(8, begin + timedelta(minutes=14)), event_kind="external_event"),
    )
    renderer = ChatEventPromptRenderer(rows, timezone="UTC")
    grouped, individual = renderer.main_agent_history_views(rows)
    assert [ids for _, ids, _ in grouped] == [(1, 2), (3,), (4, 5), (6,), (7,)]
    assert individual == tuple(
        (row.id, (row.id,), renderer.reference_message(row)) for row in rows[:-1]
    )
    expected_messages = []
    by_id = {i: message for i, _, message in individual}
    for _, ids, _ in grouped:
        first = by_id[ids[0]]
        expected_messages.append(
            ChatMessage(
                first.role,
                "\n".join(
                    [
                        first.content or "",
                        *((by_id[i].content or "").partition("\n")[2] for i in ids[1:]),
                    ]
                ),
            )
        )
    assert [message for _, _, message in grouped] == expected_messages
    assert "回复:#1" in (by_id[5].content or "")


@pytest.mark.parametrize("count", [2, 258])
async def test_same_sender_across_page_boundary_one_render_for_ensure_and_bounded(
    monkeypatch, count
):
    start = datetime(2026, 10, 4, 12, tzinfo=UTC)
    recent = tuple(record(i, start + timedelta(seconds=i)) for i in range(1, count + 1))
    service = assembler()
    options = dict(
        current_event_id=count,
        content="complete-current",
        yuki_account_ids=frozenset({"bot"}),
        current_message_override=None,
        current_event=recent[-1],
    )
    baseline = service._bounded_history(recent, **options, timezone="UTC")
    calls = []
    reference = ChatEventPromptRenderer.reference_message

    def counted(self, row, **kwargs):
        calls.append(row.id)
        return reference(self, row, **kwargs)

    monkeypatch.setattr(ChatEventPromptRenderer, "reference_message", counted)
    initial = _HistoryPromptWindow(recent, "", 0, 0, None, None)
    saved, rows, _, shifted = await service._ensure_uncovered_fits_budget(
        snapshot=initial,
        recent=recent,
        remainder=1_000_000,
        identity=ConversationScope.private("bot", "user"),
        turn=SimpleNamespace(),
        **options,
    )
    before = len(calls)
    reused = service._bounded_history(
        rows,
        **options,
        timezone="UTC",
        prepared_view=saved.uncovered_view,
        raw_history_window_shifted=shifted,
    )
    assert reused == baseline
    assert before == count and len(calls) == before
    assert len(reused.history_messages) == 1
    assert reused.history_fragments[0][0] == tuple(range(1, count))


@pytest.mark.parametrize(
    "change", ["timezone", "content", "accounts", "override", "absent_current"]
)
def test_render_reuse_rejects_changed_dependencies(monkeypatch, change):
    start = datetime(2026, 10, 4, 12, tzinfo=UTC)
    recent = (record(1, start), record(2, start + timedelta(seconds=1)))
    service = assembler()
    options = dict(
        current_event_id=2,
        content="complete",
        yuki_account_ids=frozenset({"bot"}),
        current_message_override=None,
        current_event=recent[-1],
    )
    view = service._uncovered_prompt_view(recent, **options)
    timezone = "UTC"
    if change == "timezone":
        timezone = "Asia/Taipei"
    elif change == "content":
        options["content"] = "different current envelope"
    elif change == "accounts":
        options["yuki_account_ids"] = frozenset({"different"})
    elif change == "override":
        options["current_message_override"] = ChatMessage("user", "override")
    else:
        recent = recent[:1]
    baseline = service._bounded_history(recent, **options, timezone=timezone)
    calls = []
    reference = ChatEventPromptRenderer.reference_message

    def counted(self, row, **kwargs):
        calls.append(row.id)
        return reference(self, row, **kwargs)

    monkeypatch.setattr(ChatEventPromptRenderer, "reference_message", counted)
    reused = service._bounded_history(recent, **options, timezone=timezone, prepared_view=view)
    assert reused == baseline and calls


@pytest.mark.parametrize("kind", ["self", "actorless"])
async def test_real_self_and_actorless_callers_pass_the_prepared_view(database, monkeypatch, kind):
    from tests.unit.test_history_soft_coverage import _history

    from qq_ai_bot.conversation.rollup.models import RollupPolicyConfig
    from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork
    from qq_ai_bot.runtime.trigger import ExternalEventTurnTrigger

    service, _, model, arguments = await _history(database, hold=False)
    calls = []
    bounded = ContextAssembler._bounded_history

    def real_bounded(recent, **kwargs):
        view = kwargs.get("prepared_view")
        assert view is not None and view.recent is recent
        calls.append(view)
        result = bounded(recent, **kwargs)
        assert result == bounded(
            recent, **{k: v for k, v in kwargs.items() if k != "prepared_view"}
        )
        return result

    monkeypatch.setattr(ContextAssembler, "_bounded_history", staticmethod(real_bounded))
    if kind == "self":
        await service.assemble_self_initiative(**arguments)
    else:
        identity = ConversationScope.group("8000", "2001")
        appended = await ScopedEventLedgerUnitOfWork(
            database, config=RollupPolicyConfig()
        ).append_external(
            scope=identity,
            platform_message_id="external-later",
            source_plugin_id="soft-history",
            external_source="test",
            external_event_key="later",
            external_event_type="test",
            external_payload={},
            external_target_id="2001",
            content="later event",
            occurred_at=datetime.now(UTC),
        )
        trigger = ExternalEventTurnTrigger(
            "soft-history", appended.event.id, "group", "2001", "react"
        )
        turn = replace(
            arguments["turn"],
            trigger_event_id=appended.event.id,
            transport_scope_key=identity.key,
            initiative_run_id=None,
        )
        await service.assemble(
            inbound=None,
            profile=None,
            identity=identity,
            content="later event",
            runtime=arguments["runtime"],
            turn=turn,
            external_event=appended.event,
            external_trigger=trigger,
        )
    assert len(calls) == 1 and model.requests == []
