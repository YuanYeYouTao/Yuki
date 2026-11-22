"""E2E: the resident core-set selection step after the Dream batch (memory layering P1).

Spec: docs/architecture/Yuki-记忆分层P1实施任务书-2026-11-21.md §7.2 and §10, the
selection-step parts of cases 6, 12, 14, 15, 19, 20, 21 and 28 (F-numbering kept in
artifact names); owner decisions in docs/architecture/Yuki-记忆分层架构书-2026-11-21.md
§8.0 (Q3 a: explicit facts + selection; Q15 a: no health/sex/finance picks).

Real SQLite, real ``MemoryFactService`` writes with evidence on real chat events, the
real batch extraction worker, real ``DreamService``/``DreamRepository`` and the Dream
worker loop body (``_drain_active``: clusters, then the selection step, then
``finalize_run``), and real Main turns for the resident segment. The scripted model is
the only stand-in: Dream cluster replies address ``memory_N`` refs and selection
replies are alias-only structured output (``kept_N`` / ``new_N``; ``pinned_N`` is
reference only). Artifacts: ``test-results/memory-resident/<F-case>.json``.

Person core sets were replaced by person profiles (layered memory P4 task book §4.4);
every case below now runs on the group owner G, whose core set keeps the P1 rules.

Failure cases, written before the implementation. The selection step:
  F6   runs (or the segment changes) without a Dream run after extraction writes a fact;
  F12  lets Dream rewrite or absorb an explicit fact, or accepts a pick of ``pinned_N``;
  F14  changes the segment before it runs, keeps a one-off instruction, or accepts an
       alias that was not in its input;
  F15  writes the SELF row;
  F19  offers a third-party fact as a candidate or accepts a pick outside its input;
  F20  keeps a superseded ID in the row after its next run instead of the successor;
  F21  hides a backlog (pending owners / oldest pending time), changes the Dream run
       status when it fails, or ignores the per-run cap and queue order;
  F28  accepts more than 10 picks.
Every void result leaves the owner's row (list, revision, watermark) untouched.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from tests.support.memory_resident_scene import (
    ResidentScene,
    run_cli,
    sections,
    selection_aliases,
    selection_reply,
    turn_record,
)

from yuki.memory.enums import MemoryAuthority, MemorySourceType, MemoryStatus
from yuki.persistence.models import MemoryFactModel

EXPLICIT = {"source_type": MemorySourceType.EXPLICIT, "authority": MemoryAuthority.EXPLICIT}
PRIVATE_EXPLICIT = "[对方明确要求记住的]"
GROUP_CORE = "[本群]"


@pytest.fixture
async def scene(database, tmp_path):
    return await ResidentScene(database, tmp_path).build(
        memory_batch_trigger_count=1, memory_batch_max_wait_seconds=0
    )


def _offered(scene: ResidentScene, request) -> dict[str, int]:
    return selection_aliases(request, scene.contents)


def _keep_current(scene: ResidentScene):
    """Selection double that keeps every currently kept item (the default instruction)."""

    def respond(request):
        offered = _offered(scene, request)
        return selection_reply(sorted(alias for alias in offered if alias.startswith("kept_")))

    return respond


def _requests_for(scene: ResidentScene, fact_id: int) -> list:
    content = scene.contents[fact_id]
    return [
        request
        for request in scene.selection_requests
        if any(content in str(message.content or "") for message in request.messages)
    ]


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _row(table: list[dict], owner: str) -> dict | None:
    rows = [row for row in table if row["owner"] == owner]
    return rows[0] if rows else None


# --------------------------------------------------------------------------- F6
async def test_f06_extraction_alone_changes_nothing_resident(scene):
    """Case 6: an automatic importance-5 fact written by extraction waits for Dream."""
    a1 = await scene.event("private:A", "A", "记住：汇报要区分本地修改和提交", key="a1")
    g1 = await scene.event("space:G", "B", "本群用 Python 写工具", key="g1")
    rule = await scene.fact("rule", "A", "汇报要区分本地修改和提交", evidence=(a1,), **EXPLICIT)
    core = await scene.fact("core", "G", "本群用 Python 写工具", evidence=(g1,))
    await scene.write_set("G", [core], source="dream")
    before_turn = await scene.turn("A", "提取之前")
    before_group = await scene.turn("B", "群里提取之前", group="G")
    before_table = await scene.table()
    newest = max(scene.contents)

    await scene.event("private:A", "A", "我下个月要搬去上海住", key="a2")
    processed = await scene.extract(
        "a2",
        [
            {
                "subject_ref": "speaker",
                "scope_type": "person",
                "kind": "fact",
                "memory_key": "residence:plan",
                "category": "residence",
                "content": "下个月搬去上海住",
                "evidence_quote": "我下个月要搬去上海住",
                "importance": 5,
                "confidence": 0.9,
                "source_type": "automatic",
            }
        ],
    )
    written = await scene.fact_ids_after(newest)
    after_turn = await scene.turn("A", "提取之后")
    after_group = await scene.turn("B", "群里提取之后", group="G")

    assert processed >= 1 and written, "the real extraction path must write the new fact"
    assert after_turn.segment == before_turn.segment
    assert sections(after_turn.segment) == {PRIVATE_EXPLICIT: [rule]}
    assert after_group.segment == before_group.segment
    assert sections(after_group.segment) == {GROUP_CORE: [core]}
    assert await scene.table() == before_table
    assert scene.selection_requests == []
    contract = await scene.contract()
    scene.write(
        "F06",
        {
            "facts": scene.facts,
            "extracted_fact_ids": written,
            "before": turn_record(before_turn, contract),
            "after": turn_record(after_turn, contract),
            "table": await scene.table(),
        },
    )


# --------------------------------------------------------------------------- F12
@pytest.mark.parametrize("dream_output", ["merge_into_automatic", "rewrite_explicit"])
async def test_f12_explicit_fact_survives_dream_and_is_never_selectable(scene, dream_output):
    """Case 12: the anchor rule keeps e; e is ``pinned_N`` and picking it voids the result."""
    g1 = await scene.event("space:G", "A", "记住：本群聚会都喝美式", key="g1")
    g2 = await scene.event("space:G", "B", "我们聚会一般喝不加糖的美式咖啡", key="g2")
    e = await scene.fact("e", "G", "聚会都喝美式", evidence=(g1,), **EXPLICIT)
    await scene.fact("e_auto", "G", "聚会一般喝不加糖的美式咖啡", evidence=(g2,))
    other = await scene.fact("other", "G", "周末常组织徒步", evidence=(g2,))
    before = await scene.chat._memories.get_fact(e)
    action = (
        {
            "operation": "merge",
            "source_refs": ["memory_1", "memory_2"],
            "anchor_ref": "memory_2",
        }
        if dream_output == "merge_into_automatic"
        else {
            "operation": "synthesize",
            "source_refs": ["memory_1"],
            "anchor_ref": "memory_1",
            "content": "改写后的早餐习惯",
            "importance": 4,
        }
    )
    scene.dream_responder = lambda request: json.dumps({"actions": [action]}, ensure_ascii=False)
    pinned: dict[str, int] = {}

    def select(request):
        offered = _offered(scene, request)
        pinned.update({alias: fid for alias, fid in offered.items() if alias.startswith("pinned_")})
        picks = [alias for alias, fid in offered.items() if fid in (e, other)]
        return selection_reply(sorted(picks))

    scene.selection_responder = select
    run = await scene.dream_run((("e", "e_auto"),))
    after = await scene.chat._memories.get_fact(e)
    turn = await scene.turn("B", "聚会喝什么", group="G")

    # The anchor rule may refuse the cluster (partial_failed); the run still finishes.
    assert run.status.value in {"completed", "partial_failed"}
    assert (after.id, after.content, after.status) == (e, before.content, MemoryStatus.ACTIVE)
    assert list(pinned.values()) == [e]
    # The pick referenced pinned_N: the whole result is void, nothing was written.
    assert _row(await scene.table(), "space:G") is None
    assert e not in turn.fact_ids and other not in turn.fact_ids
    scene.write(
        f"F12-{dream_output}",
        {
            "facts": scene.facts,
            "explicit_after": {"id": after.id, "content": after.content, "status": after.status},
            "pinned_aliases": sorted(pinned),
            "table": await scene.table(),
            "turn": turn_record(turn, await scene.contract()),
        },
    )


# --------------------------------------------------------------------------- F14
async def test_f14_selection_keeps_the_long_term_rule_only(scene):
    """Case 14: segment unchanged until selection; then only the chosen rule appears."""
    g1 = await scene.event("space:G", "A", "这次先把海报导出成 PDF；以后群公告都用中文", key="g1")
    before = await scene.turn("B", "写入前", group="G")
    once = await scene.fact("once", "G", "这次把海报导出成 PDF", evidence=(g1,))
    rule = await scene.fact("rule", "G", "以后群公告都用中文", evidence=(g1,))
    unselected = await scene.turn("B", "挑选前", group="G")

    # A pick outside the offered aliases voids the whole result.
    scene.selection_responder = lambda request: selection_reply(["new_99"])
    await scene.dream_run()
    void_table = await scene.table()
    # Dry run through the per-owner entry the CLI uses: prints, never writes.
    scene.selection_responder = lambda request: selection_reply(
        [alias for alias, fid in _offered(scene, request).items() if fid == rule]
    )
    await scene.dream().selection.select(scene.owner("G"), dry_run=True)
    dry_table = await scene.table()
    await scene.dream_run()
    selected = await scene.turn("B", "挑选后", group="G")

    assert unselected.segment == before.segment
    assert _row(void_table, "space:G") is None
    assert dry_table == void_table
    assert sections(selected.segment)[GROUP_CORE] == [rule]
    assert once not in selected.fact_ids
    row = _row(await scene.table(), "space:G")
    assert row["source"] == "dream" and row["fact_ids"] == [rule]
    assert row["considered_through_fact_id"] >= max(once, rule)
    contract = await scene.contract()
    scene.write(
        "F14",
        {
            "facts": scene.facts,
            "before": turn_record(before, contract),
            "unselected": turn_record(unselected, contract),
            "selected": turn_record(selected, contract),
            "table": await scene.table(),
        },
    )


# --------------------------------------------------------------------------- F15
async def test_f15_selection_never_writes_the_self_row(scene):
    """Case 15 (selection part): SELF is not a selection owner."""
    await scene.fact("self_a", "self", "先查证再回答")
    await scene.fact("self_soul_contrary", "self", "我其实不喜欢和人说话")
    g1 = await scene.event("space:G", "B", "本群常一起拍照", key="g1")
    await scene.fact("g_photo", "G", "常一起拍照", evidence=(g1,))
    scene.selection_responder = lambda request: selection_reply(
        sorted(_offered(scene, request))[:10]
    )

    run = await scene.dream_run()

    assert run.status.value == "completed"
    assert [row for row in await scene.table() if row["owner_kind"] == "self"] == []
    for name in ("self_a", "self_soul_contrary"):
        assert _requests_for(scene, scene.facts[name]) == []
    scene.write("F15-selection", {"facts": scene.facts, "table": await scene.table()})


# --------------------------------------------------------------------------- F19
async def test_f19_third_party_facts_are_never_candidates(scene):
    """Case 19 (selection part): t and t_pg are not offered; a pick outside input is void."""
    g1 = await scene.event("space:G", "A", "B 好像在学吉他，本群周末练琴", key="g1")
    t = await scene.fact(
        "t", "B", "在学吉他", evidence=(g1,), authority=MemoryAuthority.THIRD_PARTY
    )
    t_pg = await scene.fact(
        "t_pg",
        "B@G",
        "在 G 群被说成吉他手",
        evidence=(g1,),
        authority=MemoryAuthority.THIRD_PARTY,
    )
    g_ok = await scene.fact("g_ok", "G", "周末一起练琴", evidence=(g1,))
    offered_for_g: dict[str, int] = {}

    def select(request):
        offered = _offered(scene, request)
        if g_ok in offered.values():
            offered_for_g.update(offered)
            # The double tries to pick t: it has no alias, so it names one not offered.
            return selection_reply(["new_1", f"new_{len(offered) + 1}"])
        return selection_reply([])

    scene.selection_responder = select
    await scene.dream_run()

    assert g_ok in offered_for_g.values()
    assert t not in offered_for_g.values() and t_pg not in offered_for_g.values()
    assert _requests_for(scene, t) == [] and _requests_for(scene, t_pg) == []
    assert _row(await scene.table(), "space:G") is None
    scene.write(
        "F19-selection",
        {
            "facts": scene.facts,
            "offered_to_G": sorted(offered_for_g.values()),
            "table": await scene.table(),
        },
    )


# --------------------------------------------------------------------------- F20
async def test_f20_next_selection_replaces_superseded_item_with_successor(scene):
    """Case 20 (selection part): after Dream supersedes m, selection swaps m for m'."""
    g1 = await scene.event("space:G", "A", "本群聚会喝美式咖啡，不加糖", key="g1")
    await scene.fact("anchor", "G", "聚会常喝美式咖啡", evidence=(g1,))
    m = await scene.fact("m", "G", "聚会的咖啡不加糖", evidence=(g1,))
    keep = await scene.fact("keep", "G", "周末早起跑步", evidence=(g1,))
    await scene.write_set("G", [m, keep], source="dream")
    scene.dream_responder = lambda request: json.dumps(
        {
            "actions": [
                {
                    "operation": "synthesize",
                    "source_refs": ["memory_1", "memory_2"],
                    "anchor_ref": "memory_1",
                    "content": "聚会喝不加糖的美式咖啡",
                    "importance": 4,
                }
            ]
        },
        ensure_ascii=False,
    )
    successor_text = "聚会喝不加糖的美式咖啡"
    picks: dict[str, int] = {}

    def select(request):
        # The successor is created by this run's cluster; it is addressed by its text.
        offered = selection_aliases(request, {**scene.contents, 0: successor_text})
        picks.update(offered)
        by_fact = {fid: alias for alias, fid in offered.items()}
        return selection_reply([by_fact[0], by_fact[keep]])

    scene.selection_responder = select
    run = await scene.dream_run((("anchor", "m"),))
    page = await scene.dream().repository.run_page(run.public_id)
    successor = page.operations[0].output_fact_id
    turn = await scene.turn("B", "聚会喝什么咖啡", group="G")

    assert m not in picks.values() and 0 in picks.values()
    row = _row(await scene.table(), "space:G")
    assert row["fact_ids"] == [successor, keep]
    assert sections(turn.segment)[GROUP_CORE] == [successor, keep]
    scene.write(
        "F20-selection",
        {
            "facts": {**scene.facts, "successor": successor},
            "table": await scene.table(),
            "turn": turn_record(turn, await scene.contract()),
        },
    )


# --------------------------------------------------------------------------- F21
async def test_f21_backlog_is_visible_and_never_fails_the_dream_run(scene):
    """Case 21 (part 1): three failing selection runs show up in DreamHealth and CLI."""
    g1 = await scene.event("space:G", "B", "本群在一起学日语", key="g1")
    first = await scene.fact("g_first", "G", "一起学日语", evidence=(g1,))
    await scene.fact("g_second", "G", "准备一起去大阪", evidence=(g1,))
    scene.selection_responder = lambda request: "not structured output"
    runs = [await scene.dream_run() for _ in range(3)]
    health = await scene.dream().worker.health()
    async with scene.database.read_session() as reader:
        oldest = (await reader.get(MemoryFactModel, first)).created_at
    status = await run_cli(scene.database.url, "memory", "resident", "status")

    assert [run.status.value for run in runs] == ["completed"] * 3
    assert health.resident_pending_owners >= 1
    assert _utc(health.resident_oldest_pending_at) == _utc(oldest)
    assert _row(await scene.table(), "space:G") is None
    assert status.returncode == 0, status
    assert str(health.resident_pending_owners) in status.stdout
    scene.write(
        "F21-backlog",
        {
            "run_status": [run.status.value for run in runs],
            "resident_pending_owners": health.resident_pending_owners,
            "oldest_pending_is_first_unseen_candidate": _utc(health.resident_oldest_pending_at)
            == _utc(oldest),
        },
    )


async def test_f21_per_run_cap_processes_owners_in_queue_order(database, tmp_path):
    """Case 21 (part 2): cap 1 and two due owners -> one owner per run, in order."""
    scene = await ResidentScene(database, tmp_path).build(memory_resident_max_selections_per_run=1)
    candidates = {}
    for key, speaker in (("G", "B"), ("H", "C")):
        event = await scene.event(f"space:{key}", speaker, f"{key} 群的一件长期的事")
        candidates[key] = await scene.fact(
            f"{key}_fact", key, f"{key} 群的长期活动", evidence=(event,)
        )
    scene.selection_responder = lambda request: selection_reply(
        [alias for alias in _offered(scene, request) if alias.startswith("new_")]
    )
    # No rows yet: every watermark is 0, so the queue is ordered by owner ID.
    expected = sorted(("G", "H"), key=lambda key: scene.spaces[key])
    processed = []
    for _ in range(2):
        before = {row["owner"] for row in await scene.table()}
        await scene.dream_run()
        after = {row["owner"] for row in await scene.table()}
        processed.append(sorted(after - before))

    assert processed == [[f"space:{key}"] for key in expected]
    for key in ("G", "H"):
        assert _row(await scene.table(), f"space:{key}")["fact_ids"] == [candidates[key]]
    health = await scene.dream().worker.health()
    assert health.resident_pending_owners == 0
    # Space IDs are random per run: record the order check, not the labels.
    scene.write(
        "F21-cap",
        {
            "processed_one_per_run_in_owner_id_order": processed
            == [[f"space:{key}"] for key in expected],
            "table": await scene.table(),
        },
    )


# --------------------------------------------------------------------------- F28
async def test_f28_selection_with_eleven_picks_is_void(scene):
    """Case 28 (selection part): 11 picks exceed the cap of 10 and are rejected whole."""
    g1 = await scene.event("space:G", "B", "关于本群的很多事", key="g1")
    ids = [
        await scene.fact(f"g{index:02d}", "G", f"G 的长期事实 {index:02d}", evidence=(g1,))
        for index in range(12)
    ]
    scene.selection_responder = lambda request: selection_reply(
        sorted(
            (alias for alias in _offered(scene, request) if alias.startswith("new_")),
            key=lambda alias: int(alias.split("_")[1]),
        )[:11]
    )

    await scene.dream_run()

    assert _requests_for(scene, ids[0])
    assert _row(await scene.table(), "space:G") is None
    scene.write("F28-selection", {"facts": scene.facts, "table": await scene.table()})
