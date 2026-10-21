"""Incremental raw metadata obeys the original grouping and admission facts."""

import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from tests.unit.test_history_time_projection import _event

from yuki.conversation.frozen_fragments import FrozenFragments
from yuki.conversation.projections import ProjectionSnapshot
from yuki.conversation.prompt_history import PromptHistorySource
from yuki.event_prompt import ChatEventPromptRenderer


def _projection(fragments):
    return ProjectionSnapshot("epoch", 1, 0, "context", fragments, "bootstrap", 0)


def _source(rows, fragments):
    source = PromptHistorySource(
        (),
        projection=_projection(fragments),
        timezone="Asia/Shanghai",
        after_event_id=0,
    )
    source.extend(rows, through_event_id=rows[-1].id)
    return source


def _original(rows, frozen, current_id):
    renderer = ChatEventPromptRenderer(rows)
    grouped, individual = renderer.main_agent_history_views(
        row for row in rows if row.id != current_id
    )
    return frozen.extend_history(
        tuple((ids, message) for _, ids, message in grouped),
        tuple((ids, message) for _, ids, message in individual),
    )


def test_incremental_groups_match_full_raw_with_current_holes_and_partial_overlap():
    rng = random.Random(318)
    for _ in range(180):
        at = datetime(2026, 10, 18, 15, 59, tzinfo=UTC)
        rows = []
        for number in range(1, 42):
            at += timedelta(seconds=rng.choice((-20, 0, 15, 80, 180, 301)))
            rows.append(
                replace(
                    _event(number, at, rng.choice(("正文", "", "多行\n原文"))),
                    author_person_id=rng.choice(("person-1", "person-1", "person-2")),
                    direction=rng.choice(("inbound", "inbound", "outbound")),
                    sender_group_card=rng.choice(("旧昵称", "新昵称")),
                )
            )
        rows = tuple(rows)
        current_id = rng.randrange(1, len(rows) + 1)
        renderer = ChatEventPromptRenderer(rows)
        frozen = FrozenFragments.load([]).extend_history(
            (
                ((row.id,), renderer.reference_message(row))
                for row in rows
                if row.id != current_id and rng.random() < 0.4 and row.content
            ),
            (),
        )
        source = _source(rows, frozen)
        fresh = tuple(row for row in rows if row.id in source.pending(rows[-1].id))
        groups, singles = source.render(
            fresh,
            references=source.capture(rows[-1].id),
            through_event_id=rows[-1].id,
            current_event_id=current_id,
        )
        assert (
            frozen.extend_history(groups, singles).items
            == _original(rows, frozen, current_id).items
        )


def test_failed_candidate_keeps_pending_until_original_committed_head():
    at = datetime(2026, 10, 18, tzinfo=UTC)
    rows = tuple(_event(i, at) for i in range(1, 5))
    frozen = FrozenFragments.load([])
    source = _source(rows, frozen)
    captured = source.capture(4)
    groups, singles = source.render(
        rows,
        references=captured,
        through_event_id=4,
        current_event_id=4,
    )
    candidate = frozen.extend_history(groups, singles)
    # Mere rendering and a later append never promote an unsubmitted candidate.
    source.extend((_event(5, at),), through_event_id=5)
    assert source.pending(5) == (1, 2, 3, 4, 5)
    assert tuple(captured.event_ids) == (1, 2, 3, 4)
    assert source.reconcile(_projection(candidate))
    assert source.pending(5) == (4, 5)
    assert tuple(captured.event_ids) == (1, 2, 3, 4)


def test_tail_current_dense_group_visits_only_fresh_body(monkeypatch):
    at = datetime(2026, 10, 18, tzinfo=UTC)
    rows = tuple(_event(i, at) for i in range(1, 1001))
    renderer = ChatEventPromptRenderer(rows)
    groups, singles = renderer.main_agent_history_views(rows)
    frozen = FrozenFragments.load([]).extend_history(
        tuple((ids, message) for _, ids, message in groups),
        tuple((ids, message) for _, ids, message in singles),
    )
    source = _source(rows, frozen)
    added = (_event(1001, at), _event(1002, at))
    source.extend(added, through_event_id=1002)
    visited = []
    original = ChatEventPromptRenderer.reference_message

    def observe(self, row, **kwargs):
        visited.append(row.id)
        return original(self, row, **kwargs)

    monkeypatch.setattr(ChatEventPromptRenderer, "reference_message", observe)
    groups, singles = source.render(
        added,
        references=source.capture(1002),
        through_event_id=1002,
        current_event_id=1002,
    )
    assert visited == [1001]
    assert groups == singles and groups[0][0] == (1001,)
    delta = frozen.extend_history(groups, singles).delta_from(frozen)
    assert len(delta) == len(delta[0]) == 1 and delta[0][0].event_ids == (1001,)


def test_unsubmitted_non_tail_current_does_not_replace_the_full_raw_group_index():
    at = datetime(2026, 10, 18, tzinfo=UTC)
    rows = (
        _event(1, at, "A"),
        replace(_event(2, at, "B"), author_person_id="other"),
        _event(3, at, "C"),
    )
    frozen = FrozenFragments.load([])
    source = _source(rows, frozen)
    earlier = source.capture(3)
    captured_pending = source.pending(3)
    groups, _ = source.render(
        rows,
        references=earlier,
        through_event_id=3,
        current_event_id=2,
    )
    assert [ids for ids, _ in groups] == [(1, 3)]
    # That candidate never dispatches. The next preparation excludes D, rather
    # than continuing the previously rendered A/C group with B still missing.
    added = _event(4, at, "D")
    source.extend((added,), through_event_id=4)
    later, singles = source.render(
        (*rows, added),
        references=source.capture(4),
        through_event_id=4,
        current_event_id=4,
    )
    assert [ids for ids, _ in later] == [(1,), (2,), (3,)]
    assert frozen.extend_history(later, singles).items == _original((*rows, added), frozen, 4).items
    assert captured_pending == (1, 2, 3) and tuple(earlier.event_ids) == (1, 2, 3)
    # Reusing the older capture after the owner appends is byte-for-byte stable.
    assert (
        source.render(
            rows,
            references=earlier,
            through_event_id=3,
            current_event_id=2,
        )[0]
        == groups
    )


def test_source_memory_accounting_tracks_metadata_without_retaining_body_payload():
    at = datetime(2026, 10, 18, tzinfo=UTC)
    frozen = FrozenFragments.load([])
    source = _source(
        (
            replace(
                _event(1, at, "x" * 100000),
                segments=(
                    {
                        "type": "mention",
                        "data": {"display_name": "名", "account_id": "original-account"},
                    },
                ),
            ),
        ),
        frozen,
    )
    first = source.estimated_bytes
    metadata = source.capture(1).get(1)
    assert metadata.content == "" and metadata.segments == () and metadata.external_payload is None
    source.extend((_event(2, at, "y" * 100000),), through_event_id=2)
    assert first < source.estimated_bytes < first + 20000
