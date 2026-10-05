"""Preparation batching retains old acceptance/rejection and exact input order."""

import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from qq_ai_bot.conversation.frozen_fragments import FrozenFragments, _input
from qq_ai_bot.conversation.observations import ContextObservation
from qq_ai_bot.conversation.projections import ProjectionConflict, ProjectionSnapshot
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.domain.messages import ChatImage, ChatMessage
from qq_ai_bot.execution_trace.phases import current_metrics
from qq_ai_bot.persistence.event_repository import ConversationReadVersion
from qq_ai_bot.services import history_projection
from qq_ai_bot.services.context_assembler import AssembledContext, ContextMetrics
from qq_ai_bot.time.models import TimeContext


def context(parts=()):
    now = datetime.now(UTC)
    return AssembledContext(
        metadata_payload={},
        history_messages=tuple(m for _, m in parts),
        current_message=ChatMessage("user", "current envelope"),
        recent_delivery=(),
        current_time=TimeContext(now, now, "UTC"),
        current_relationship=None,
        metrics=ContextMetrics(0, 0, 0, 0, False),
        read_version=ConversationReadVersion(
            ConversationScope.private("bot", "user"), "owner", 1, 0
        ),
        history_fragments=parts,
        history_event_fragments=parts,
        visible_event_ids=frozenset(i for ids, _ in parts for i in ids),
        current_event_id=999,
    )


def snapshot(frozen, *, summary=None, coverage=0):
    return ProjectionSnapshot(
        "epoch",
        1,
        1,
        "b" * 64,
        "c" * 64,
        json.dumps(list(frozen.items), ensure_ascii=False, separators=(",", ":")),
        "bootstrap",
        0,
        summary,
        coverage,
    )


@pytest.mark.parametrize("count", [0, 1, 32, 256, 512])
@pytest.mark.parametrize("tagged", [False, True])
def test_batch_exact_identity_first_order_and_one_load(monkeypatch, count, tagged):
    existing = _input((1,) if tagged else (), ChatMessage("user", "frozen"))
    existing.update(observation_id="old", observation_version=1)
    initial = FrozenFragments.load([existing])
    offered = [
        ("old", 2, ChatMessage("user", "ignored old version")),
        *((f"new:{i}", 1, ChatMessage("user", f"note:{i}")) for i in range(count)),
        *((f"new:{i}", 2, ChatMessage("user", "ignored duplicate")) for i in range(count)),
    ]
    # Explicit old algorithm reference, independent of the new append method.
    baseline = initial
    for identity, version, message in offered:
        if identity in {key for key, _ in baseline.observation_sources}:
            continue
        item = _input((), message)
        item.update(observation_id=identity, observation_version=version)
        baseline = FrozenFragments.load([*deepcopy(baseline.items), item])
    calls = []
    load = FrozenFragments.load.__func__

    def counted(cls, items):
        calls.append(len(items))
        return load(cls, items)

    monkeypatch.setattr(FrozenFragments, "load", classmethod(counted))
    result = initial.append_observations(tuple(offered))
    assert result.items == baseline.items and result.messages() == baseline.messages()
    assert calls == ([count + 1] if count else [])
    assert result.observation_sources[0] == ("old", 1)
    assert initial.items == (existing,)
    if not count:
        assert result is initial


def test_batch_deepcopies_nested_inputs_and_preserves_duplicate_noop():
    original = _input((1,), ChatMessage("user", "prefix"))
    frozen = FrozenFragments.load([original])
    result = frozen.append_observations((("note", 1, ChatMessage("user", "new")),))
    original["message"]["content"] = "mutated caller"
    frozen.items[0]["message"]["content"] = "mutated previous copy"
    assert result.items[0]["message"]["content"] == "prefix"
    # Identity-first no-op must not validate a second offered representation.
    invalid = ChatMessage("user", "ignored", images=(ChatImage("data:image/png;base64,AA=="),))
    assert result.append_observations((("note", 0, invalid),)) is result


@pytest.mark.parametrize("count", [0, 1, 32, 256, 512])
async def test_prepare_batch_is_linear_and_keeps_unconditional_fresh(monkeypatch, count):
    parts = (((1,), ChatMessage("user", "old-history")),)
    observations = tuple(
        ContextObservation(f"n:{i}", 1, '{"text":"note"}', ()) for i in range(count)
    )
    old = FrozenFragments.load([]).extend_history(parts, parts)
    old = old.append_observations(tuple((r.id, r.version, r.message()) for r in observations))
    repo = SimpleNamespace(database=None, read=AsyncMock(return_value=snapshot(old)))
    monkeypatch.setattr(
        history_projection.ContextObservationRepository,
        "read",
        AsyncMock(return_value=observations),
    )
    load = FrozenFragments.load.__func__
    loads = []

    def counted(cls, items):
        loads.append(len(items))
        return load(cls, items)

    monkeypatch.setattr(FrozenFragments, "load", classmethod(counted))
    metrics = {}
    token = current_metrics.set(metrics)
    try:
        prepared = await history_projection.prepare_history(
            repo,
            context(parts),
            view_key="a" * 64,
            context_key="b" * 64,
            contract_revision="c" * 64,
            actor_id="actor",
            read_scope="main",
            history_fits=lambda _: True,
        )
    finally:
        current_metrics.reset(token)
    assert prepared.fragments.items == old.items
    assert len(loads) <= 6 and sum(loads) <= 3 * count + 5
    assert metrics == {"F": count + 1, "E": 1, "K": count, "candidate_count": 2}


@pytest.mark.parametrize("illegal", ["coverage", "images", "reasoning"])
@pytest.mark.parametrize("hard_fit", [False, True])
async def test_unused_fresh_still_rejects_invalid_representation(monkeypatch, illegal, hard_fit):
    valid = FrozenFragments.load([]).extend_history(
        (((1, 2, 3), ChatMessage("user", "old group")),), ()
    )
    repo = SimpleNamespace(database=None, read=AsyncMock(return_value=snapshot(valid)))
    if illegal == "coverage":
        parts = (((1, 2), ChatMessage("user", "first")), ((2, 3), ChatMessage("user", "overlap")))
    else:
        message = ChatMessage(
            "user",
            "old-selected",
            **(
                {"images": (ChatImage("data:image/png;base64,AA=="),)}
                if illegal == "images"
                else {"reasoning_content": "provider-private"}
            ),
        )
        parts = (((1, 2, 3), message),)
    selected = replace(context(parts), history_event_fragments=())
    with pytest.raises(ProjectionConflict):
        await history_projection.prepare_history(
            repo,
            selected,
            view_key="a" * 64,
            context_key="b" * 64,
            contract_revision="c" * 64,
            history_fits=lambda _: hard_fit,
            context_hard_fits=lambda _: hard_fit,
        )


def test_batch_preserves_group_overlap_and_individual_coverage():
    prefix = FrozenFragments.load([]).extend_history((((1,), ChatMessage("user", "original")),), ())
    grouped = (((1, 2, 3), ChatMessage("user", "new combined")),)
    individual = (
        ((1,), ChatMessage("user", "changed old")),
        ((2,), ChatMessage("user", "second")),
        ((3,), ChatMessage("user", "third")),
    )
    extended = prefix.extend_history(grouped, individual).append_observations(
        (("note", 1, ChatMessage("user", "eventless")),)
    )
    assert extended.messages() == (
        ChatMessage("user", "original"),
        ChatMessage("user", "second"),
        ChatMessage("user", "third"),
        ChatMessage("user", "eventless"),
    )
    assert [item["event_ids"] for item in extended.items] == [[1], [2], [3], []]


async def test_summary_page_filter_binds_frozen_ids_once_not_per_row(monkeypatch):
    from tests.unit.test_context_render_reuse import record

    now = datetime.now(UTC)
    parts = tuple(((i,), ChatMessage("user", f"original:{i}")) for i in range(1, 601))
    frozen = FrozenFragments.load([]).extend_history(parts, parts)
    repo = SimpleNamespace(database=None, read=AsyncMock(return_value=snapshot(frozen, summary="")))
    rows = tuple(record(i, now) for i in range(1, 602))
    pages = []

    bound_ids = []

    async def read(
        expected,
        *,
        after_event_id,
        through_event_id,
        frozen_event_ids,
        current_event_id,
    ):
        # The reader now owns ID paging in one snapshot. The preparation must
        # bind its already-built immutable exclusion set once for that read.
        bound_ids.append(frozen_event_ids)
        assert expected == ctx.read_version
        assert (after_event_id, through_event_id, current_event_id) == (0, 601, 601)
        assert frozen_event_ids == frozenset(range(1, 601))
        discovered = tuple(row for row in rows if after_event_id < row.id <= through_event_id)
        pages.extend(
            len(discovered[start : start + 256]) for start in range(0, len(discovered), 256)
        )
        return expected, tuple(
            row
            for row in discovered
            if row.id not in frozen_event_ids and row.id != current_event_id
        )

    monkeypatch.setattr(
        history_projection,
        "EventLedgerRepository",
        lambda _: SimpleNamespace(read_scope_missing_history=read),
    )
    getter = FrozenFragments.event_ids.fget
    calls = []

    def ids(self):
        calls.append(len(self.items))
        return getter(self)

    monkeypatch.setattr(FrozenFragments, "event_ids", property(ids))
    ctx = replace(
        context(parts),
        current_event_id=601,
        prompt_raw_tail_end_event_id=601,
        visible_event_ids=frozenset(range(1, 602)),
    )
    prepared = await history_projection.prepare_history(
        repo,
        ctx,
        view_key="a" * 64,
        context_key="b" * 64,
        contract_revision="c" * 64,
        history_fits=lambda _: True,
    )
    assert prepared.fragments.items == frozen.items
    assert pages == [256, 256, 89]
    assert len(bound_ids) == 1 and isinstance(bound_ids[0], frozenset)
    assert len(calls) <= 5  # independent of R=601; no per-row set construction


async def test_same_fragments_new_summary_coverage_keep_existing_decision(monkeypatch):
    parts = (((1,), ChatMessage("user", "old")),)
    frozen = FrozenFragments.load([]).extend_history(parts, parts)
    repo = SimpleNamespace(
        database=None, read=AsyncMock(return_value=snapshot(frozen, summary="old summary"))
    )
    ctx = replace(
        context(parts),
        rollup_text="new summary",
        prompt_effective_coverage=1,
        prompt_raw_tail_end_event_id=1,
    )
    ledger = SimpleNamespace(
        read_scope_missing_history=AsyncMock(return_value=(ctx.read_version, ()))
    )
    monkeypatch.setattr(history_projection, "EventLedgerRepository", lambda _: ledger)
    prepared = await history_projection.prepare_history(
        repo,
        ctx,
        view_key="a" * 64,
        context_key="b" * 64,
        contract_revision="c" * 64,
        history_fits=lambda _: False,
        context_hard_fits=lambda _: True,
    )
    assert prepared.reason is None
    assert prepared.context.rollup_text == "old summary"
    assert prepared.context.prompt_effective_coverage == 0
    assert prepared.fragments.items == frozen.items
    ledger.read_scope_missing_history.assert_awaited_once_with(
        ctx.read_version,
        after_event_id=0,
        through_event_id=1,
        frozen_event_ids=frozenset({1}),
        current_event_id=ctx.current_event_id,
    )
