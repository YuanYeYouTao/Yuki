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
PRIVATE_CORE = "[关于对方]"


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
    rule = await scene.fact("rule", "A", "汇报要区分本地修改和提交", evidence=(a1,), **EXPLICIT)
    core = await scene.fact("core", "A", "用 Python 写工具", evidence=(a1,))
    await scene.write_set("A", [core], source="dream")
    before_turn = await scene.turn("A", "提取之前")
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

    assert processed >= 1 and written, "the real extraction path must write the new fact"
    assert after_turn.segment == before_turn.segment
    assert sections(after_turn.segment) == {PRIVATE_EXPLICIT: [rule], PRIVATE_CORE: [core]}
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
    a1 = await scene.event("private:A", "A", "记住：我每天早上喝美式", key="a1")
    a2 = await scene.event("private:A", "A", "我早上一般喝不加糖的美式咖啡", key="a2")
    e = await scene.fact("e", "A", "每天早上喝美式", evidence=(a1,), **EXPLICIT)
    await scene.fact("e_auto", "A", "早上一般喝不加糖的美式咖啡", evidence=(a2,))
    other = await scene.fact("other", "A", "周末喜欢徒步", evidence=(a2,))
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
    turn = await scene.turn("A", "早上喝什么")

    # The anchor rule may refuse the cluster (partial_failed); the run still finishes.
    assert run.status.value in {"completed", "partial_failed"}
    assert (after.id, after.content, after.status) == (e, before.content, MemoryStatus.ACTIVE)
    assert list(pinned.values()) == [e]
    # The pick referenced pinned_N: the whole result is void, nothing was written.
    assert _row(await scene.table(), "person:A") is None
    assert e in sections(turn.segment)[PRIVATE_EXPLICIT]
    assert other not in turn.fact_ids
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
    await scene.event("private:A", "A", "这次先把报告导出成 PDF；以后回复我都用中文", key="a1")
    before = await scene.turn("A", "提取前")
    newest = max(scene.contents) if scene.contents else 0
    claims = [
        {
            "subject_ref": "speaker",
            "scope_type": "person",
            "kind": "preference",
            "memory_key": f"resident:{key}",
            "category": "instruction",
            "content": content,
            "evidence_quote": quote,
            "importance": 4,
            "confidence": 0.9,
            "source_type": "automatic",
        }
        for key, content, quote in (
            ("once", "这次把报告导出成 PDF", "这次先把报告导出成 PDF"),
            ("rule", "以后回复都用中文", "以后回复我都用中文"),
        )
    ]
    assert await scene.extract("a1", claims) >= 1
    written = await scene.fact_ids_after(newest)
    scene.contents.update(await scene.contents_of(written))
    by_content = {content: fid for fid, content in scene.contents.items()}
    once, rule = by_content["这次把报告导出成 PDF"], by_content["以后回复都用中文"]
    unselected = await scene.turn("A", "挑选前")

    # A pick outside the offered aliases voids the whole result.
    scene.selection_responder = lambda request: selection_reply(["new_99"])
    await scene.dream_run()
    void_table = await scene.table()
    # Dry run through the per-owner entry the CLI uses: prints, never writes.
    scene.selection_responder = lambda request: selection_reply(
        [alias for alias, fid in _offered(scene, request).items() if fid == rule]
    )
    await scene.dream().selection.select(scene.owner("A"), dry_run=True)
    dry_table = await scene.table()
    await scene.dream_run()
    selected = await scene.turn("A", "挑选后")

    assert unselected.segment == before.segment
    assert _row(void_table, "person:A") is None
    assert dry_table == void_table
    assert sections(selected.segment)[PRIVATE_CORE] == [rule]
    assert once not in selected.fact_ids
    row = _row(await scene.table(), "person:A")
    assert row["source"] == "dream" and row["fact_ids"] == [rule]
    assert row["considered_through_fact_id"] >= max(once, rule)
    contract = await scene.contract()
    scene.write(
        "F14",
        {
            "facts": scene.facts,
            "extracted": {"once": once, "rule": rule},
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
    b1 = await scene.event("private:B", "B", "我喜欢拍照", key="b1")
    await scene.fact("b_photo", "B", "喜欢拍照", evidence=(b1,))
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
    g1 = await scene.event("space:G", "A", "B 好像在学吉他", key="g1")
    b1 = await scene.event("private:B", "B", "我在学钢琴", key="b1")
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
    b_ok = await scene.fact("b_ok", "B", "在学钢琴", evidence=(b1,))
    offered_for_b: dict[str, int] = {}

    def select(request):
        offered = _offered(scene, request)
        if b_ok in offered.values():
            offered_for_b.update(offered)
            # The double tries to pick t: it has no alias, so it names one not offered.
            return selection_reply(["new_1", f"new_{len(offered) + 1}"])
        return selection_reply([])

    scene.selection_responder = select
    await scene.dream_run()

    assert b_ok in offered_for_b.values()
    assert t not in offered_for_b.values() and t_pg not in offered_for_b.values()
    assert _requests_for(scene, t) == [] and _requests_for(scene, t_pg) == []
    assert _row(await scene.table(), "person:B") is None
    scene.write(
        "F19-selection",
        {
            "facts": scene.facts,
            "offered_to_B": sorted(offered_for_b.values()),
            "table": await scene.table(),
        },
    )


# --------------------------------------------------------------------------- F20
async def test_f20_next_selection_replaces_superseded_item_with_successor(scene):
    """Case 20 (selection part): after Dream supersedes m, selection swaps m for m'."""
    a1 = await scene.event("private:A", "A", "我喜欢美式咖啡，不加糖", key="a1")
    await scene.fact("anchor", "A", "平时喜欢喝美式咖啡", evidence=(a1,))
    m = await scene.fact("m", "A", "喝咖啡不加糖", evidence=(a1,))
    keep = await scene.fact("keep", "A", "早起跑步", evidence=(a1,))
    await scene.write_set("A", [m, keep], source="dream")
    scene.dream_responder = lambda request: json.dumps(
        {
            "actions": [
                {
                    "operation": "synthesize",
                    "source_refs": ["memory_1", "memory_2"],
                    "anchor_ref": "memory_1",
                    "content": "喜欢不加糖的美式咖啡",
                    "importance": 4,
                }
            ]
        },
        ensure_ascii=False,
    )
    successor_text = "喜欢不加糖的美式咖啡"
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
    turn = await scene.turn("A", "我喝什么咖啡")

    assert m not in picks.values() and 0 in picks.values()
    row = _row(await scene.table(), "person:A")
    assert row["fact_ids"] == [successor, keep]
    assert sections(turn.segment)[PRIVATE_CORE] == [successor, keep]
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
    b1 = await scene.event("private:B", "B", "我在学日语", key="b1")
    first = await scene.fact("b_first", "B", "在学日语", evidence=(b1,))
    await scene.fact("b_second", "B", "准备去大阪", evidence=(b1,))
    scene.selection_responder = lambda request: "not structured output"
    runs = [await scene.dream_run() for _ in range(3)]
    health = await scene.dream().worker.health()
    async with scene.database.read_session() as reader:
        oldest = (await reader.get(MemoryFactModel, first)).created_at
    status = await run_cli(scene.database.url, "memory", "resident", "status")

    assert [run.status.value for run in runs] == ["completed"] * 3
    assert health.resident_pending_owners >= 1
    assert _utc(health.resident_oldest_pending_at) == _utc(oldest)
    assert _row(await scene.table(), "person:B") is None
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
    """Case 21 (part 2): cap 1 and three due owners -> one owner per run, in order."""
    scene = await ResidentScene(database, tmp_path).build(memory_resident_max_selections_per_run=1)
    candidates = {}
    for key in ("A", "B", "C"):
        event = await scene.event(f"private:{key}", key, f"{key} 的一件长期的事")
        candidates[key] = await scene.fact(
            f"{key}_fact", key, f"{key} 的长期爱好", evidence=(event,)
        )
    scene.selection_responder = lambda request: selection_reply(
        [alias for alias in _offered(scene, request) if alias.startswith("new_")]
    )
    # No rows yet: every watermark is 0, so the queue is ordered by owner ID.
    expected = sorted(("A", "B", "C"), key=lambda key: scene.persons[key])
    processed = []
    for _ in range(3):
        before = {row["owner"] for row in await scene.table()}
        await scene.dream_run()
        after = {row["owner"] for row in await scene.table()}
        processed.append(sorted(after - before))

    assert processed == [[f"person:{key}"] for key in expected]
    for key in ("A", "B", "C"):
        assert _row(await scene.table(), f"person:{key}")["fact_ids"] == [candidates[key]]
    health = await scene.dream().worker.health()
    assert health.resident_pending_owners == 0
    # Person IDs are random per run: record the order check, not the labels.
    scene.write(
        "F21-cap",
        {
            "processed_one_per_run_in_owner_id_order": processed
            == [[f"person:{key}"] for key in expected],
            "table": await scene.table(),
        },
    )


# --------------------------------------------------------------------------- F28
async def test_f28_selection_with_eleven_picks_is_void(scene):
    """Case 28 (selection part): 11 picks exceed the cap of 10 and are rejected whole."""
    b1 = await scene.event("private:B", "B", "关于我的很多事", key="b1")
    ids = [
        await scene.fact(f"b{index:02d}", "B", f"B 的长期事实 {index:02d}", evidence=(b1,))
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
    assert _row(await scene.table(), "person:B") is None
    scene.write("F28-selection", {"facts": scene.facts, "table": await scene.table()})
