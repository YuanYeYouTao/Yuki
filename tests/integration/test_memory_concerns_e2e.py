"""E2E: "the things I care about" in the resident segment (#541 P2).

Owner decisions of 2026-11-23 (docs/architecture/Yuki-长期在意与跨会话感知调研-2026-11-23.md
§8): a concern is a SELF memory fact ``kind=fact``, ``category='concern'`` (no new kind);
Yuki's own judgement is enough, no owner confirmation; the resident segment carries at
most five, one sentence plus ``fact:ID`` each, and the section follows the resident
segment's cadence (it changes with the nightly Dream at most once a day).

Real SQLite, real ``MessageProcessor`` -> Main -> ``save_memory`` / ``edit_memory``, the
real Dream worker and the real resident renderer; only model replies are scripted. The
facts (rendered sections per conversation, fact IDs, statement counts and the concern
query plan; IDs replaced by fixture labels) go to ``test-results/concern-resident-e2e.json``.

Failure cases, written before the implementation. The concern section:
  K1  is missing after ``save_memory(kind=concern)`` and the next finished Dream run, or
      the fact is not a SELF ``fact`` with category ``concern`` visible to the owner only;
  K2  shows a concern saved in A's private chat in a group or in B's private chat;
  K3  shows more than five, drops the newest, or hides that more exist;
  K4  still shows an invalidated concern (no Dream run needed to drop it);
  K5  shows the old text after ``edit_memory(correct)``, or loses the concern;
  K6  shows a group concern outside its group, or not in its group;
  K7  changes the segment bytes before the next Dream run when a new concern is saved;
  K8  accepts ``kind=concern`` for someone other than Yuki herself;
  K9  costs more statements with more concerns, or scans memory_facts for them.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event, select
from tests.support.memory_resident_scene import ResidentScene, normalize

from yuki.persistence.models import MemoryFactModel

ARTIFACT = Path(__file__).resolve().parents[2] / "test-results" / "concern-resident-e2e.json"
RESULTS: dict[str, Any] = {}
CONCERNS = "[我在意的事]"
SELF_SECTION = "[我自己的原则]"


@pytest.fixture(scope="module", autouse=True)
def _write_artifact():
    yield
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(
        json.dumps(
            {case: json.loads(text) for case, text in sorted(RESULTS.items())},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


@pytest.fixture
async def scene(database, tmp_path):
    built = await ResidentScene(database, tmp_path).build(self_memory_enabled=True)
    # The container wires Main's memory writes (save_memory / edit_memory).
    built.chat._tools._memory_mutations = built.harness_mutations()
    return built


def record(scene: ResidentScene, case: str, payload: dict[str, Any]) -> None:
    RESULTS[case] = normalize(payload, scene.labels())


def section(text: str, title: str = CONCERNS) -> list[str]:
    """The lines of one resident section, in order (empty when absent)."""
    lines = text.splitlines()
    if title not in lines:
        return []
    found: list[str] = []
    for line in lines[lines.index(title) + 1 :]:
        if line.startswith("["):
            break
        found.append(line)
    return found


async def rendered(scene: ResidentScene, key: str) -> str:
    return (await scene.resident.render(scene.audience(key))).text


async def save_concern(
    scene: ResidentScene, who: str, content: str, *, group: str | None = None, **extra: Any
) -> dict[str, Any]:
    replies, _ = await scene.tool_turn(
        who,
        f"帮我盯着：{content}",
        [
            (
                "save_memory",
                {"target": {"self": True}, "kind": "concern", "content": content, **extra},
            )
        ],
        group=group,
    )
    return replies[0]


async def stored(scene: ResidentScene, fact_id: int) -> dict[str, Any]:
    async with scene.database.read_session() as session:
        row = await session.get(MemoryFactModel, fact_id)
    return {
        "scope_type": row.scope_type,
        "kind": row.kind,
        "category": row.category,
        "visibility_type": row.visibility_type,
        "visibility_person": row.canonical_visibility_person_id,
        "visibility_space": row.canonical_visibility_space_id,
    }


def fact_id_of(reply: dict[str, Any]) -> int:
    return int(reply["fact_id"])


async def test_k1_k2_k7_private_concern_after_dream_only_in_its_private_chat(scene):
    before = await rendered(scene, "A")
    reply = await save_concern(scene, "A", "A 下个月的考试")
    fact_id = fact_id_of(reply)
    not_yet = await rendered(scene, "A")
    await scene.dream_run()
    after = await rendered(scene, "A")
    later = await save_concern(scene, "A", "A 的搬家")
    same_day = await rendered(scene, "A")
    await scene.dream_run()
    next_day = await rendered(scene, "A")
    elsewhere = {key: await rendered(scene, key) for key in ("G", "H", "B")}
    record(
        scene,
        "K1_K2_K7",
        {
            "saved": reply,
            "stored": await stored(scene, fact_id),
            "before_dream": section(not_yet),
            "after_dream": section(after),
            "same_day_after_new_concern_unchanged": same_day == after,
            "next_dream": section(next_day),
            "elsewhere": {key: section(text) for key, text in elsewhere.items()},
        },
    )
    assert reply["outcome"] == "committed"
    assert await stored(scene, fact_id) == {
        "scope_type": "self",
        "kind": "fact",
        "category": "concern",
        "visibility_type": "private",
        "visibility_person": scene.persons["A"],
        "visibility_space": None,
    }
    assert section(before) == [] and section(not_yet) == []
    assert section(after) == [f"- A 下个月的考试（fact:{fact_id}）"]
    lines = after.splitlines()
    if SELF_SECTION in lines:
        assert lines.index(CONCERNS) < lines.index(SELF_SECTION)
    assert same_day == after
    assert section(next_day) == [
        f"- A 下个月的考试（fact:{fact_id}）",
        f"- A 的搬家（fact:{fact_id_of(later)}）",
    ]
    assert all(section(text) == [] for text in elsewhere.values())


async def test_k3_at_most_five_newest_with_overflow_note(scene):
    ids = [fact_id_of(await save_concern(scene, "A", f"第 {n} 件事")) for n in range(1, 8)]
    await scene.dream_run()
    lines = section(await rendered(scene, "A"))
    record(scene, "K3", {"saved": ids, "section": lines})
    assert lines == [
        *(f"- 第 {n} 件事（fact:{ids[n - 1]}）" for n in range(3, 8)),
        "（另有 2 件在意的事未常驻，可用 search_memory 查）",
    ]


async def test_k4_k5_invalidate_and_correct_follow_without_dream(scene):
    first = fact_id_of(await save_concern(scene, "A", "A 的考试"))
    second = fact_id_of(await save_concern(scene, "A", "A 的面试"))
    await scene.dream_run()
    replies, _ = await scene.tool_turn(
        "A",
        "考试结束了，面试改到周五",
        [
            ("edit_memory", {"fact_id": first, "action": "invalidate", "reason": "考试结束"}),
            ("edit_memory", {"fact_id": second, "action": "correct", "content": "A 周五的面试"}),
        ],
    )
    lines = section(await rendered(scene, "A"))
    successor = int(replies[1]["fact_id"])
    record(scene, "K4_K5", {"edits": replies, "section": lines})
    assert all(reply["outcome"] == "committed" for reply in replies)
    assert successor != second
    assert lines == [f"- A 周五的面试（fact:{successor}）"]


async def test_k6_group_concern_stays_in_its_group(scene):
    reply = await save_concern(scene, "B", "群里周末的聚会", group="G")
    fact_id = fact_id_of(reply)
    await scene.dream_run()
    texts = {key: await rendered(scene, key) for key in ("G", "H", "A", "B")}
    record(
        scene,
        "K6",
        {
            "stored": await stored(scene, fact_id),
            "sections": {key: section(text) for key, text in texts.items()},
        },
    )
    assert reply["outcome"] == "committed"
    assert section(texts["G"]) == [f"- 群里周末的聚会（fact:{fact_id}）"]
    assert all(section(texts[key]) == [] for key in ("H", "A", "B"))


async def test_k8_concern_is_only_for_yuki_herself(scene):
    replies, _ = await scene.tool_turn(
        "A",
        "帮我记住",
        [
            (
                "save_memory",
                {"target": {"person_id": scene.persons["A"]}, "kind": "concern", "content": "x"},
            )
        ],
    )
    async with scene.database.read_session() as session:
        concerns = list(
            await session.scalars(
                select(MemoryFactModel.id).where(MemoryFactModel.category == "concern")
            )
        )
    record(scene, "K8", {"reply": replies[0], "concerns": concerns})
    assert replies[0]["error"] == "invalid_arguments"
    assert concerns == []


@contextmanager
def recorded(database):
    statements: list[tuple[str, Any]] = []

    def before(conn, cursor, statement, parameters, context, executemany):
        statements.append((statement, parameters))

    engine = database.engine.sync_engine
    event.listen(engine, "before_cursor_execute", before)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", before)


def business(statements):
    return [
        (sql, params)
        for sql, params in statements
        if not sql.lstrip()
        .upper()
        .startswith(("BEGIN", "COMMIT", "ROLLBACK", "PRAGMA", "SAVEPOINT", "RELEASE"))
    ]


async def test_k9_statement_count_is_constant_and_indexed(scene):
    await save_concern(scene, "A", "第一件")
    await scene.dream_run()
    with recorded(scene.database) as few:
        await rendered(scene, "A")
    for n in range(12):
        await save_concern(scene, "A", f"更多的事 {n}")
    await scene.dream_run()
    with recorded(scene.database) as many:
        await rendered(scene, "A")
    concern_sql = [
        (sql, params)
        for sql, params in business(many)
        if "'concern'" in sql or "concern" in str(params)
    ]
    path = Path(scene.database.url.split("///", 1)[1])
    connection = sqlite3.connect(path)
    try:
        plans = [
            [row[3] for row in connection.execute(f"EXPLAIN QUERY PLAN {sql}", params or ())]
            for sql, params in concern_sql
        ]
    finally:
        connection.close()
    record(
        scene,
        "K9",
        {
            "statements_one_concern": len(business(few)),
            "statements_thirteen_concerns": len(business(many)),
            "concern_query_plans": plans,
        },
    )
    assert len(business(few)) == len(business(many))
    assert plans and all(
        any("ix_memory_facts_self_concern" in step for step in plan) for plan in plans
    )
    assert not any(step.startswith("SCAN memory_facts") for plan in plans for step in plan)
