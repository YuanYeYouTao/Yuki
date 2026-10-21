"""Original public bytes survive shared chunks, indexed references and dense groups."""

from __future__ import annotations

import random
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta

import pytest
from tests.unit.test_history_time_projection import _event

from yuki.conversation.frozen_fragments import FrozenFragments
from yuki.conversation.projections import ProjectionConflict
from yuki.domain.messages import ChatMessage
from yuki.event_prompt import ChatEventPromptRenderer, EventPromptIndex, prompt_origin_class
from yuki.time.formatting import local_datetime, stored_utc


def _original_grouping(renderer, rows):
    """The prior renderer's grouping semantics, independent of the new accumulator."""
    grouped, individual = [], []
    start = last = day = None
    for row in rows:
        if row.event_kind == "external_event":
            continue
        message = renderer.reference_message(row)
        text = (message.content or "").strip()
        if not text:
            continue
        individual.append((row.id, (row.id,), message))
        identity = (
            (renderer._principal_ref(row),)
            if row.author_person_id or row.author_presence_id
            else (row.author_kind or "unknown", row.platform, row.sender_user_id)
        )
        key = (
            message.role,
            identity,
            row.sender_display_name,
            prompt_origin_class(row),
            row.caused_by_event_id,
        )
        now = stored_utc(row.occurred_at)
        local_day = local_datetime(row.occurred_at, "Asia/Shanghai").date()
        if (
            grouped
            and grouped[-1][3] == key
            and start is not None
            and last <= now
            and start <= now <= start + timedelta(minutes=5)
            and day == local_day
        ):
            anchor, ids, previous, _ = grouped[-1]
            _, separator, line = text.partition("\n")
            if separator:
                grouped[-1] = (
                    anchor,
                    (*ids, row.id),
                    ChatMessage(previous.role, f"{previous.content}\n{line}"),
                    key,
                )
                last = now
                continue
        grouped.append((row.id, (row.id,), message, key))
        start = last = now
        day = local_day
    return tuple((anchor, ids, message) for anchor, ids, message, _ in grouped), tuple(individual)


def test_linear_groups_match_original_bytes_across_mixed_boundaries():
    rng = random.Random(617)
    for _ in range(100):
        at = datetime(2026, 10, 18, 15, 59, tzinfo=UTC)
        rows = []
        for identifier in range(1, 35):
            at += timedelta(seconds=rng.choice((-25, 0, 20, 80, 300)))
            row = _event(identifier, at, rng.choice(("正文", "  两侧空白 \n", "", "多行\n正文")))
            rows.append(
                replace(
                    row,
                    sender_group_card=rng.choice(("同名", "别名")),
                    author_person_id=rng.choice(("person-1", "person-2")),
                    direction=rng.choice(("inbound", "inbound", "outbound")),
                    caused_by_event_id=rng.choice((None, None, 1)),
                    event_kind=rng.choice(("message", "message", "external_event")),
                )
            )
        renderer = ChatEventPromptRenderer(rows)
        assert renderer.main_agent_history_views(rows) == _original_grouping(renderer, rows)


def test_dense_group_copy_volume_stays_proportional_to_its_output(monkeypatch):
    rows = tuple(_event(i, datetime(2026, 10, 18, tzinfo=UTC), "正文") for i in range(1, 1001))
    renderer = ChatEventPromptRenderer(rows)
    original = ChatMessage
    merged = []

    def message(*args, **kwargs):
        value = original(*args, **kwargs)
        if (value.content or "").count("\n#") > 1:
            merged.append(value)
        return value

    monkeypatch.setattr("yuki.event_prompt.ChatMessage", message)
    grouped, individual = renderer.main_agent_history_views(rows)
    assert len(grouped) == 1 and len(individual) == len(rows)
    assert grouped[0][1] == tuple(row.id for row in rows)
    # Reject quadratic rebuilding without requiring a particular constructor count.
    assert sum(len(message.content) for message in merged) <= 4 * len(grouped[0][2].content)


def test_reference_capture_keeps_original_rendering_and_excludes_later_events():
    at = datetime(2026, 10, 18, tzinfo=UTC)
    old = replace(
        _event(2, at), sender_user_id="1002", sender_group_card="原名", bot_user_id="old-bot"
    )
    later = replace(old, id=8, sender_group_card="之后的名")
    foreign = replace(old, id=4, canonical_conversation_id="foreign", sender_group_card="别处")
    quoted = replace(
        _event(5, at),
        reply_to_event_id=old.id,
        mentioned_user_ids=("1002",),
        content="@原名 @Yuki",
        segments=(
            {"type": "mention", "data": {"display_name": "原名", "account_id": "original-account"}},
        ),
    )
    owner = EventPromptIndex((foreign, old, quoted))
    captured = owner.capture(through_event_id=5)
    renderer = ChatEventPromptRenderer(
        references=captured
    )
    original = renderer.render_reference_event(quoted)
    assert "原名/person:person-1" in original and "@原名 @Yuki" in original
    owner.extend((later, replace(old, id=3, sender_group_card="晚补旧洞")))
    assert renderer.render_reference_event(quoted) == original
    assert captured.get(3) is None and captured.get(8) is None
    assert set(captured.event_ids) == {2, 4, 5}
    assert len(captured.event_ids) == 3
    overlay = owner.capture(
        through_event_id=5, extra_events=(replace(old, id=3, sender_group_card="晚补旧洞"),)
    )
    updated = ChatEventPromptRenderer(references=overlay).render_reference_event(quoted)
    assert "@原名 @Yuki" in updated and "@晚补旧洞" not in updated
    assert renderer.render_reference_event(quoted) == original


def test_duplicate_reference_ids_keep_last_metadata_and_original_mention_snapshots():
    first = replace(
        _event(1, datetime(2026, 10, 18, tzinfo=UTC)), bot_user_id="old-bot", sender_group_card="旧"
    )
    last = replace(first, bot_user_id="new-bot", sender_group_card="新")
    focus = replace(
        first,
        id=2,
        sender_user_id="other",
        mentioned_user_ids=(first.sender_user_id,),
        content="@原Yuki @新Yuki",
        segments=(
            {"type": "mention", "data": {"display_name": "原Yuki", "account_id": "old-account"}},
            {"type": "mention", "data": {"display_name": "新Yuki", "account_id": "new-account"}},
        ),
    )
    rendered = ChatEventPromptRenderer((first, last, focus)).render_reference_event(focus)
    assert "提及:原Yuki/@{account:old-account},新Yuki/@{account:new-account}" in rendered
    assert rendered.endswith(">@原Yuki @新Yuki")


def _item(identifier, text="原正文"):
    return {
        "kind": "model_input",
        "event_ids": [identifier],
        "message": {"role": "user", "content": text},
    }


def test_frozen_input_and_exported_dtos_cannot_mutate_shared_public_bytes():
    data = [_item(1)]
    frozen = FrozenFragments.load(data)
    data[0]["event_ids"].append(2)
    data[0]["message"]["content"] = "外部更改"
    exported = frozen.items
    exported[0]["event_ids"].append(3)
    exported[0]["message"]["content"] = "DTO更改"
    assert frozen.event_ids == {1}
    assert frozen.messages() == (ChatMessage("user", "原正文"),)
    with pytest.raises(FrozenInstanceError):
        next(frozen.iter_fragments()).message.content = "更改"
    assert FrozenFragments.load(frozen) is frozen


def test_append_and_delta_only_visit_new_chunks_and_keep_siblings_isolated(monkeypatch):
    base = FrozenFragments.load([_item(i) for i in range(1, 501)])
    original_block = next(base.iter_blocks())

    def no_prefix_traversal(_self):
        raise AssertionError("hot append must not materialize or walk old fragments")

    monkeypatch.setattr(FrozenFragments, "iter_fragments", no_prefix_traversal)
    left = base.append_current(501, ChatMessage("user", "左分支"))
    right = base.append_current(502, ChatMessage("user", "右分支"))
    grown = left.append_current(503, ChatMessage("user", "末尾"))
    assert len(grown) == 502 and len(grown.event_ids) == 502
    assert 501 in left.event_ids and 501 not in right.event_ids and 501 not in base.event_ids
    assert next(grown.iter_blocks()) is original_block
    assert [fragment.event_ids for block in grown.delta_from(base) for fragment in block] == [
        (501,),
        (503,),
    ]
    assert grown.delta_from(grown) == ()
    with pytest.raises(ProjectionConflict):
        grown.delta_from(right)
    with pytest.raises(ProjectionConflict):
        grown.delta_from(FrozenFragments.load([_item(1)]))


class _CountedIndividuals(tuple):
    visited = 0

    def __iter__(self):
        for item in super().__iter__():
            self.visited += 1
            yield item


def test_partial_overlap_uses_one_individual_index_and_preserves_original_order():
    frozen = FrozenFragments.load([_item(i) for i in range(2, 202, 2)])
    grouped = tuple(((i, i + 1), ChatMessage("user", "whole group")) for i in range(1, 201, 2))
    individual = _CountedIndividuals(((i,), ChatMessage("user", f"#{i}")) for i in range(1, 202))
    extended = frozen.extend_history(grouped, individual)
    assert individual.visited == len(individual)
    new = tuple(fragment for block in extended.delta_from(frozen) for fragment in block)
    assert [fragment.event_ids for fragment in new] == [(i,) for i in range(1, 201, 2)]
    assert [fragment.message.content for fragment in new] == [f"#{i}" for i in range(1, 201, 2)]
    assert len(extended.event_ids) == 200


def test_partial_overlap_rejects_incomplete_or_duplicate_individual_coverage():
    frozen = FrozenFragments.load([_item(1)])
    grouped = (((1, 2), ChatMessage("user", "group")),)
    for individual in ((), (((2,), ChatMessage("user", "a")), ((2,), ChatMessage("user", "b")))):
        with pytest.raises(ProjectionConflict, match="coverage"):
            frozen.extend_history(grouped, individual)


def test_many_structural_appends_keep_sorted_membership_and_restore_original_blocks():
    frozen = FrozenFragments.load([])
    identifiers = list(range(1, 1001))
    random.Random(714).shuffle(identifiers)
    for identifier in identifiers:
        frozen = frozen.append_current(identifier, ChatMessage("user", str(identifier)))
    assert list(frozen.event_ids) == sorted(identifiers)
    assert len(frozen) == len(identifiers)
    restored = FrozenFragments.from_blocks(frozen.iter_blocks())
    assert restored.items == frozen.items
    assert restored.event_ids == frozen.event_ids
