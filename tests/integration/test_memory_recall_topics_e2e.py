"""E2E: 可查话题 under person profiles (记忆意图召回评估 §2, memory-v2.md 人物画像).

Every case runs a real SQLite database, real fact writes with evidence on real fixture
chat events, the real Dream worker loop body (selection step with member lists, program
cleanup and profile calls, then ``finalize_run``) and real ``MessageProcessor`` ->
``ContextAssembler`` -> ``MainAgentTurns.compose`` turns. Only model replies are
scripted: profile calls answer from their own structured input (``f_N`` aliases). Each
case writes normalized JSON to ``test-results/memory-recall/<case>.json``; identical input
gives byte-identical artifacts. All fixture text is invented.

Failure cases, written before the implementation:
  T1 topics from the profile call do not render, render in the current message instead of
     the system end, or change between Dream runs when a new fact is written (the system
     prefix must stay byte-identical until the next Dream run, which then updates them);
     the grounding rule does not mention them.
  T2 a group renders a private-profile topic (private-chat source), another person's
     private chat renders B's topics, or B's private chat misses either profile's topics.
  T3 a void topic output is stored: an alias not offered to this call, a ``ref_N``, a
     duplicate topic, a topic of nine characters, five topics, an empty or multi-line
     topic; a void output writes a version instead of only the attempt time.
  T4 a topic whose fact becomes unavailable still renders, takes the profile's entries
     with it, or survives the next cleanup (which must write a ``program`` version).
  T5 a version written before topics existed (``topics_json`` NULL) is never maintained
     again while its signature is unchanged, or is maintained more than once.
  T6 the render issues more statements with topics than without, or more for five
     members with four topics each than for one member without topics.
  T7 the topic lines' length is not measured (group with five members, private chat).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from tests.integration.test_memory_profiles_scale_e2e import _business, recorded
from tests.support.memory_profile_scene import (
    MEMBERS_TITLE,
    PRIVATE_TITLE,
    PUBLIC_TITLE,
    ProfileScene,
    profile_blocks,
)
from tests.support.memory_resident_scene import normalize, system_text

from yuki.memory.enums import MemoryInvalidationReason

TOPICS = "- 可查话题："
RESULTS = Path(__file__).resolve().parents[2] / "test-results" / "memory-recall"
# Four-character topics: the length case measures the cap (4 topics x 8 characters is the
# hard limit; four characters is a typical topic).
TOPIC_WORDS = (
    "围棋入门",
    "露营装备",
    "胶片相机",
    "城市骑行",
    "手冲咖啡",
    "科幻小说",
    "独立游戏",
    "古典音乐",
    "烘焙甜点",
    "夜跑路线",
    "日语学习",
    "桌游聚会",
    "园艺盆栽",
    "历史纪录",
    "钢琴练习",
    "摄影后期",
    "电影配乐",
    "滑板入门",
    "博物馆展",
    "书法临帖",
)


def write_artifact(case: str, payload: dict[str, Any], labels: dict[str, str]) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"{case}.json").write_text(normalize(payload, labels) + "\n", encoding="utf-8")


@pytest.fixture
async def scene(database, tmp_path):
    return await ProfileScene(database, tmp_path).build()


def reply(
    entries: list[tuple[str | None, str, list[str]]],
    topics: list[tuple[str, list[str]]],
    reason: str = "更新",
) -> str:
    return json.dumps(
        {
            "entries": [
                {"section": section, "text": body, "facts": facts}
                for section, body, facts in entries
            ],
            "topics": [{"text": body, "facts": facts} for body, facts in topics],
            "reason": reason,
        },
        ensure_ascii=False,
    )


def topic_lines(segment: str) -> dict[str, list[str]]:
    """Profile block title -> its topic lines (a group block keeps every member's)."""
    return {
        title: [line for line in lines if line.startswith(TOPICS)]
        for title, lines in profile_blocks(segment).items()
    }


async def versions(scene: ProfileScene) -> list[dict]:
    async with scene.database.read_session() as reader:
        rows = (
            await reader.execute(
                text(
                    "SELECT p.person_id, p.audience, v.version, v.source, v.entries_json,"
                    " v.topics_json FROM memory_profile_versions v"
                    " JOIN memory_profiles p ON p.id = v.profile_id"
                    " ORDER BY p.person_id, p.audience, v.version"
                )
            )
        ).mappings()
        labels = scene.labels()
        return sorted(
            (
                {
                    "person": labels.get(row["person_id"], row["person_id"]),
                    "audience": row["audience"],
                    "version": row["version"],
                    "source": row["source"],
                    "entries": json.loads(row["entries_json"]),
                    "topics": None
                    if row["topics_json"] is None
                    else json.loads(row["topics_json"]),
                }
                for row in rows
            ),
            key=lambda row: (row["person"], row["audience"], row["version"]),
        )


async def _base(scene: ProfileScene) -> dict[str, int]:
    """B: two public facts (group evidence) and one private-only fact."""
    g1 = await scene.event("space:G", "B", "我周末常去爬山，最近在玩塞尔达", key="g1")
    b1 = await scene.event("private:B", "B", "我在准备考研", key="b1")
    return {
        "hike": await scene.fact("hike", "B", "周末常去爬山", evidence=(g1,)),
        "zelda": await scene.fact("zelda", "B", "最近在玩塞尔达", evidence=(g1,)),
        "exam": await scene.fact("exam", "B", "在准备考研", evidence=(b1,)),
        "g1": g1,
    }


def _responder(scene: ProfileScene, facts: dict[str, int], public_topics: list[str]):
    def respond(payload, request):
        offered = scene.offered(payload)
        if payload["audience"] == "public":
            return reply(
                [("兴趣", "周末常去爬山", [offered[facts["hike"]]])],
                [(topic, [offered[facts[topic_fact]]]) for topic, topic_fact in public_topics],
            )
        return reply(
            [("身份", "在准备考研", [offered[facts["exam"]]])],
            [("考研", [offered[facts["exam"]]])],
        )

    return respond


# ---------------------------------------------------------------------------- T1 T2
async def test_t1_t2_topics_render_at_system_end_change_only_at_dream_and_stay_in_scope(scene):
    facts = await _base(scene)
    scene.profile_responder = _responder(scene, facts, [("塞尔达", "zelda")])
    await scene.dream_run()
    private_b = await scene.turn("B", "最近怎么样")
    group = await scene.turn("A", "群里谁玩游戏", group="G")
    private_c = await scene.turn("C", "在吗")
    # B speaks again and a new fact lands; without a Dream run nothing changes.
    g2 = await scene.event("space:G", "B", "我养了两只猫", key="g2")
    facts["cats"] = await scene.fact("cats", "B", "养了两只猫", evidence=(g2,))
    private_b_again = await scene.turn("B", "还在吗")
    group_again = await scene.turn("A", "群里还有谁", group="G")
    scene.profile_responder = _responder(scene, facts, [("塞尔达", "zelda"), ("养猫", "cats")])
    await scene.dream_run()
    group_after = await scene.turn("A", "群里最近", group="G")

    assert topic_lines(private_b.segment) == {
        PUBLIC_TITLE: [f"{TOPICS}塞尔达"],
        PRIVATE_TITLE: [f"{TOPICS}考研"],
    }
    assert topic_lines(group.segment) == {MEMBERS_TITLE: [f"{TOPICS}塞尔达"]}
    assert "考研" not in group.segment and "可查话题" not in private_c.segment
    for turn in (private_b, group, group_after):
        assert "可查话题" not in turn.current
        assert "可查话题" in system_text(turn.requests[0])
    assert system_text(private_b.requests[0]) == system_text(private_b_again.requests[0])
    assert system_text(group.requests[0]) == system_text(group_again.requests[0])
    assert topic_lines(group_after.segment) == {MEMBERS_TITLE: [f"{TOPICS}塞尔达、养猫"]}
    from yuki.memory.context import MEMORY_GROUNDING_RULE

    assert "可查话题" in MEMORY_GROUNDING_RULE
    assert MEMORY_GROUNDING_RULE in system_text(private_b.requests[0])
    write_artifact(
        "T1-T2-topics",
        {
            "private_B": private_b.segment,
            "group_G": group.segment,
            "private_C": private_c.segment,
            "group_G_after_second_dream": group_after.segment,
            "versions": await versions(scene),
        },
        scene.labels(),
    )


# -------------------------------------------------------------------------------- T3
@pytest.mark.parametrize(
    ("case", "topics", "code"),
    [
        ("unknown_alias", [("考研", ["f_99"])], "unknown_alias"),
        ("reference_alias", [("考研", ["ref_1"])], "reference_alias"),
        ("duplicate_topic", [("塞尔达", ["zelda"]), ("塞尔达", ["hike"])], "duplicate_topic"),
        ("nine_characters", [("塞尔达传说旷野之息", ["zelda"])], None),
        ("five_topics", [(f"话题{index}", ["zelda"]) for index in range(5)], None),
        ("empty_topic", [(" ", ["zelda"])], "empty_topic"),
        ("multiline_topic", [("塞尔\n达", ["zelda"])], "multiline_topic"),
    ],
)
async def test_t3_void_topic_outputs_store_nothing(scene, caplog, case, topics, code):
    facts = await _base(scene)

    def respond(payload, request):
        offered = scene.offered(payload)
        if payload["audience"] != "public":
            return reply([("身份", "在准备考研", [offered[facts["exam"]]])], [])
        return reply(
            [("兴趣", "周末常去爬山", [offered[facts["hike"]]])],
            [
                (topic, [offered[facts[ref]] if ref in facts else ref for ref in refs])
                for topic, refs in topics
            ],
        )

    scene.profile_responder = respond
    with caplog.at_level("WARNING"):
        await scene.dream_run()
    stored = await versions(scene)
    public = [row for row in stored if row["person"] == "person:B" and row["audience"] == "public"]
    rejected = [
        record.getMessage()
        for record in caplog.records
        if "memory_profile_rejected" in record.getMessage()
    ]

    assert public == [], public
    assert rejected, case
    if code is not None:
        assert any(f"code={code}" in message for message in rejected), rejected
    async with scene.database.read_session() as reader:
        attempted = await reader.scalar(
            text(
                "SELECT attempted_at FROM memory_profiles WHERE person_id=:p AND audience='public'"
            ),
            {"p": scene.persons["B"]},
        )
    assert attempted is not None


# -------------------------------------------------------------------------------- T4
async def test_t4_unavailable_topic_drops_at_render_and_cleanup(scene):
    facts = await _base(scene)
    scene.profile_responder = _responder(scene, facts, [("塞尔达", "zelda")])
    await scene.dream_run()
    before = await scene.turn("A", "群里谁玩游戏", group="G")
    assert await scene.chat._memories.invalidate_fact(
        facts["zelda"], reason=MemoryInvalidationReason.USER_RETRACTED, actor_user_id="10002"
    )
    after = await scene.turn("A", "群里还玩吗", group="G")
    scene.profile_responder = None  # cleanup needs no model; an unscripted call would void
    await scene.dream_run()
    stored = await versions(scene)
    public = [row for row in stored if row["person"] == "person:B" and row["audience"] == "public"]

    assert topic_lines(before.segment) == {MEMBERS_TITLE: [f"{TOPICS}塞尔达"]}
    assert topic_lines(after.segment) == {MEMBERS_TITLE: []}
    assert profile_blocks(after.segment)[MEMBERS_TITLE][1:] == [
        f"- [兴趣] 周末常去爬山（fact:{facts['hike']}）"
    ]
    assert public[-1]["source"] == "program"
    assert public[-1]["topics"] == []
    assert public[-1]["entries"] == public[-2]["entries"]
    write_artifact(
        "T4-unavailable-topic",
        {"before": before.segment, "after": after.segment, "versions": stored},
        scene.labels(),
    )


# -------------------------------------------------------------------------------- T5
async def test_t5_version_without_topics_is_due_exactly_once(scene):
    facts = await _base(scene)
    store = scene.profiles
    available = await store.available(scene.persons["B"], "public")
    # A version as written before topics existed: entries only, the current signature.
    await store.commit(
        scene.persons["B"],
        "public",
        (scene.entry("周末常去爬山", facts["hike"], section="兴趣"),),
        source="dream",
        reason="旧版本",
        expected_version=0,
        signature=available.signature,
    )
    calls: list[str] = []

    def respond(payload, request):
        calls.append(payload["audience"])
        assert payload["current_topics"] == []
        return _responder(scene, facts, [("塞尔达", "zelda")])(payload, request)

    scene.profile_responder = respond
    await scene.dream_run()
    first = list(calls)
    # B speaks again, so B is maintained in the next run with an unchanged fact set.
    await scene.event("space:G", "B", "又来聊天了", key="g2")
    await scene.dream_run()
    stored = await versions(scene)
    public = [row for row in stored if row["person"] == "person:B" and row["audience"] == "public"]

    assert public[0]["topics"] is None
    assert first.count("public") == 1
    assert calls.count("public") == 1, calls
    assert public[-1]["topics"] == [{"text": "塞尔达", "fact_ids": [facts["zelda"]]}]
    write_artifact("T5-due-once", {"calls": calls, "versions": stored}, scene.labels())


# -------------------------------------------------------------------------------- T6
async def _count(scene: ProfileScene, key: str) -> tuple[int, str]:
    with recorded(scene.database) as statements:
        rendered = await scene.resident.render(scene.audience(key))
    return len(_business(statements)), rendered.text


async def test_t6_t7_statement_count_constant_and_topic_length(scene):
    persons, spaces, store = scene.persons, scene.spaces, scene.profiles
    g1 = await scene.event("space:G", "B", "我们群里的人都爱聊爱好", key="g1")
    b1 = await scene.event("private:B", "B", "我私下在学大提琴", key="b1")
    keys = ("A", "B", "C", "D", "B2")
    words = ("围棋入门", "露营装备", "胶片相机", "城市骑行")
    facts = {
        key: [
            await scene.fact(f"{key}_{index}", key, f"{key} 喜欢{word}", evidence=(g1,))
            for index, word in enumerate(words)
        ]
        for key in keys
    }
    private = [
        await scene.fact(f"B_private_{index}", "B", f"B 私下{word}", evidence=(b1,))
        for index, word in enumerate(("学大提琴", "准备考研", "早起跑步", "练习书法"))
    ]

    async def write(key: str, audience: str, ids: list[int], topics: bool) -> None:
        current = await store.load(persons[key], audience)
        await store.commit(
            persons[key],
            audience,
            (scene.entry("条目一", ids[0]), scene.entry("条目二", ids[1])),
            topics=tuple(
                scene.entry(TOPIC_WORDS[(keys.index(key) * 4 + index) % 20], ids[index])
                for index in range(4)
            )
            if topics
            else (),
            source="owner",
            reason="规模",
            expected_version=current.current_version if current is not None else 0,
        )

    await write("B", "public", facts["B"], False)
    await store.replace_dream_members({spaces["G"]: (persons["B"],)}, dream_run_public_id="one")
    plain_group, _ = await _count(scene, "G")
    plain_private, plain_private_text = await _count(scene, "B")
    for key in keys:
        await write(key, "public", facts[key], True)
    await write("B", "private", private, True)
    await store.replace_dream_members(
        {spaces["G"]: tuple(persons[key] for key in keys)}, dream_run_public_id="five"
    )
    topic_group, topic_group_text = await _count(scene, "G")
    topic_private, topic_private_text = await _count(scene, "B")

    group_lines = [line for line in topic_group_text.split("\n") if line.startswith(TOPICS)]
    private_lines = [line for line in topic_private_text.split("\n") if line.startswith(TOPICS)]
    assert len(group_lines) == 5 and len(private_lines) == 2
    assert plain_group == topic_group, (plain_group, topic_group)
    assert plain_private == topic_private, (plain_private, topic_private)
    assert "可查话题" not in plain_private_text
    length = {
        "group_five_members_four_topics": sum(len(line) + 1 for line in group_lines),
        "private_public_and_private_four_topics": sum(len(line) + 1 for line in private_lines),
    }
    write_artifact(
        "T6-length",
        {
            "statements": {
                "group_one_member_no_topics": plain_group,
                "group_five_members_four_topics": topic_group,
                "private_no_topics": plain_private,
                "private_four_plus_four_topics": topic_private,
            },
            "added_characters": length,
            "group_topic_lines": group_lines,
            "private_topic_lines": private_lines,
        },
        scene.labels(),
    )
