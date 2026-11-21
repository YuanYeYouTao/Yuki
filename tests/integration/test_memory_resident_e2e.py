"""E2E: the resident memory segment of every Main turn (memory layering P1, #681).

Authoritative spec: docs/architecture/Yuki-记忆分层P1实施任务书-2026-11-21.md §10
(cases 1-29 = F1-F29), §15 #18/#19/#20, with the owner decisions of
docs/architecture/Yuki-记忆分层架构书-2026-11-21.md §8.0. Q16 = b: the group
"speaker" and "@-mentioned" sections do not exist in P1, so cases 5, 11 and 29 and
the @-part of case 28 are deleted (task book §13, Q16 row); cases 1, 2 and 4 assert
against the group core section "[本群]". Q13 a/a: subagent and plugin turns carry no
resident segment. Q14 a: facts without evidence rows render only in the subject's own
private chat. Q19 b does not change any case here.

Every case runs a real SQLite database, writes facts and evidence through the real
``MemoryFactService`` with evidence on real fixture chat events, and drives real
``MessageProcessor`` -> ``ContextAssembler`` -> ``MainAgentTurns.compose`` -> request
turns; only the model reply is scripted. Each case writes normalized JSON (resident
segment text as it appeared in the request, rendered fact IDs, ``recovery_contract``
hash; person/space/conversation IDs replaced by fixture labels, times stripped) to
``test-results/memory-resident/<F-case>.json``; identical input gives byte-identical
artifacts. Selection-step parts of cases 6, 12, 14, 15, 19, 20, 21, 28 live in
``test_memory_resident_selection_e2e.py``.

Failure cases, written before the implementation. A resident segment that:
  F1  shows a fact whose only evidence is a private chat in a group;
  F2  shows a group fact in the group when any one of its evidence rows is elsewhere;
  F3  shows another person's fact in the owner's private chat (CLI or direct row);
  F3b shows the counterpart's own fact whose evidence is someone else's private chat;
  F4  shows another group's fact, or a fact whose evidence is all in another group;
  F7  changes between turns although nothing resident changed;
  F8  still shows an invalidated, contested or expired fact, or reorders the rest;
  F9  still shows a privacy-deleted fact or a forgotten person's facts;
  F10 renders a merged person twice, by the old ID, or from the non-root row;
  F13 shows a fact:ID that ``search_memory(fact_id=ID)`` cannot read in that chat;
  F15 is written for SELF by anyone but the owner CLI, or with a non-SELF fact;
  F16 makes the turn fail when the renderer fails;
  F17 changes inside one Work (requests or continuation) or re-pays its first request;
  F18 moves the recovery contract hash (core set, explicit fact, flag);
  F18b renders while the flag is off, or does not come back when it is on again;
  F19 shows a third-party fact (CLI or direct row);
  F20 shows a superseded fact instead of its active successor (or an ineligible one);
  F22 hides or reorders contradicting explicit facts, or shows a contested one;
  F23 leaks into a compact checkpoint, or is duplicated / lost after compaction;
  F24 reaches a subagent or plugin turn, or carries the wrong sections per entry;
  F25 shows a disabled person's facts, renders a merged person off-root, or a forgotten one;
  F26 differs between QQ, App and ACP private chats of the same person;
  F27 depends on row insertion order or the rendering process;
  F28 exceeds the caps (30 explicit with one overflow line, 10 core) or accepts 11.

Failure baselines for the leakage cases (§15 #20). These pass on the old
implementation that has no resident segment at all, so their red baseline is the
renderer with one filter removed; each removal below turns the named case red:
  F1  remove the "本群证据条件" (group facts render with private-chat evidence): the
      G core fact ``x_g`` whose only evidence is B's private chat appears in "[本群]".
  F2  weaken "every evidence row is in this space" to "at least one": the G core fact
      ``y_g`` with one G event and one B-private event appears in "[本群]".
  F3  remove the owner-match condition on core rows: B's fact ``z_shared`` (evidence
      in G, which A and B share) written directly into A's row appears in A's private
      chat (``z`` itself is also caught by the private-origin filter); remove the CLI
      owner check: the ``set --person A --facts z`` call succeeds.
  F3b remove "no evidence from someone else's private chat" on the private sections:
      A's own facts ``a_core``/``a_explicit`` evidenced only in B's private chat appear
      in A's private chat.
  F4  remove the owner-match condition: H's group fact ``h_in_g`` (evidence in G)
      written into G's row appears in G; compare evidence conversations by kind
      instead of by space ID: the G fact ``h_g`` evidenced only in H appears in G.
  F19 remove the authority condition (explicit / self_report only): the third-party
      facts ``t`` / ``t_pg`` written directly into B's row appear in B's private chat;
      remove the CLI common-condition check: ``set --person B --facts t`` succeeds.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from tests.support.memory_resident_scene import (
    HEADER,
    ResidentScene,
    capture,
    fact_marks,
    request_text,
    resident_segment,
    run_cli,
    sections,
    tool_call,
    tool_result,
    turn_record,
    write_artifact,
)

from yuki.domain.messages import ChatResponse
from yuki.memory.enums import (
    MemoryAuthority,
    MemoryInvalidationReason,
    MemorySourceType,
)
from yuki.memory.resident import ResidentRender, ResidentValidationError
from yuki.persistence.models import ChatEventModel

EXPLICIT = {"source_type": MemorySourceType.EXPLICIT, "authority": MemoryAuthority.EXPLICIT}
PRIVATE_EXPLICIT = "[对方明确要求记住的]"
PRIVATE_CORE = "[关于对方]"
SELF_SECTION = "[我自己的原则]"
GROUP_CORE = "[本群]"


@pytest.fixture
async def scene(database, tmp_path):
    return await ResidentScene(database, tmp_path).build()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


async def _render(scene: ResidentScene, key: str) -> ResidentRender:
    return await scene.resident.render(scene.audience(key))


async def _assert_matches_render(scene: ResidentScene, key: str, turn) -> None:
    """The request carries exactly the renderer's text, before the scene line."""
    rendered = await _render(scene, key)
    assert turn.segment == rendered.text
    assert turn.fact_ids == list(rendered.fact_ids)
    if rendered.text:
        assert turn.current.count(rendered.text) == 1
        assert turn.current.index(rendered.text) < turn.current.index("当前是")
    else:
        assert HEADER not in turn.current


# --------------------------------------------------------------------------- F1
async def test_f01_private_origin_never_reaches_group_core(scene):
    """Case 1: a private-chat-only fact never renders in G; B's own private chat shows it."""
    b_private = await scene.event("private:B", "B", "我其实很怕狗，别在群里说", key="b1")
    g_event = await scene.event("space:G", "A", "我们群每周五一起看电影", key="g1")
    x_b = await scene.fact("x_b", "B", "很怕狗", evidence=(b_private,))
    x_g = await scene.fact(
        "x_g", "G", "群里有人怕狗，组织活动避开宠物", foreign_evidence=(b_private,)
    )
    g_ok = await scene.fact("g_ok", "G", "每周五一起看电影", evidence=(g_event,))
    await scene.write_set("B", [x_b])
    await scene.raw_set("G", [x_g, g_ok])

    group = await scene.turn("B", "大家好", group="G")
    private = await scene.turn("B", "在吗")

    assert sections(group.segment) == {GROUP_CORE: [g_ok]}
    assert x_b not in group.fact_ids and x_g not in group.fact_ids
    assert x_b in sections(private.segment)[PRIVATE_CORE]
    await _assert_matches_render(scene, "G", group)
    await _assert_matches_render(scene, "B", private)
    contract = await scene.contract()
    scene.write(
        "F01",
        {
            "facts": scene.facts,
            "group_G_speaker_B": turn_record(group, contract),
            "private_B": turn_record(private, contract),
        },
    )


# --------------------------------------------------------------------------- F2
async def test_f02_group_core_needs_every_evidence_in_this_group(scene):
    """Case 2: one G event plus one B-private event is not enough for "[本群]"."""
    g_event = await scene.event("space:G", "B", "周末一起爬山吧", key="g1")
    b_private = await scene.event("private:B", "B", "群里爬山其实是我张罗的", key="b1")
    y_g = await scene.fact(
        "y_g", "G", "周末爬山由群友张罗", evidence=(g_event,), foreign_evidence=(b_private,)
    )
    y_b = await scene.fact("y_b", "B", "张罗周末爬山", evidence=(g_event, b_private))
    g_ok = await scene.fact("g_ok", "G", "周末常组织爬山", evidence=(g_event,))
    await scene.raw_set("G", [y_g, g_ok])
    await scene.write_set("B", [y_b])

    group = await scene.turn("B", "这周爬山吗", group="G")

    assert sections(group.segment) == {GROUP_CORE: [g_ok]}
    assert y_g not in group.fact_ids and y_b not in group.fact_ids
    await _assert_matches_render(scene, "G", group)
    scene.write(
        "F02",
        {"facts": scene.facts, "group_G_speaker_B": turn_record(group, await scene.contract())},
    )


# --------------------------------------------------------------------------- F3
async def test_f03_another_persons_fact_never_enters_owner_core(scene):
    """Case 3: CLI refuses B's fact in A's set; a direct row still does not render it."""
    b_private = await scene.event("private:B", "B", "我在准备考研", key="b1")
    a_private = await scene.event("private:A", "A", "我喜欢喝乌龙茶", key="a1")
    g_event = await scene.event("space:G", "B", "我在 G 群说过我会弹琴", key="g1")
    z = await scene.fact("z", "B", "在准备考研", evidence=(b_private,))
    # Evidence in a group A and B share: only the owner-match condition excludes it.
    z_shared = await scene.fact("z_shared", "B", "会弹琴", evidence=(g_event,))
    a_ok = await scene.fact("a_ok", "A", "喜欢喝乌龙茶", evidence=(a_private,))
    await scene.write_set("A", [a_ok])
    before = await scene.table()

    with pytest.raises(ResidentValidationError):
        await scene.resident.write_set(scene.owner("A"), [a_ok, z], source="owner")
    cli = await run_cli(
        scene.database.url,
        "memory",
        "resident",
        "set",
        "--person",
        scene.persons["A"],
        "--facts",
        f"{a_ok},{z}",
    )
    assert cli.returncode != 0, cli
    assert await scene.table() == before

    await scene.raw_set("A", [z, z_shared, a_ok], source="owner")
    turn = await scene.turn("A", "早")

    assert z not in turn.fact_ids and z_shared not in turn.fact_ids
    assert sections(turn.segment)[PRIVATE_CORE] == [a_ok]
    await _assert_matches_render(scene, "A", turn)
    scene.write(
        "F03",
        {
            "facts": scene.facts,
            "cli_returncode_nonzero": cli.returncode != 0,
            "table_before_direct_write": before,
            "private_A": turn_record(turn, await scene.contract()),
        },
    )


async def test_f03b_counterparts_fact_from_someone_elses_private_chat(scene):
    """§15 #18: A's own facts evidenced only in B's private chat stay out of A's chat."""
    b_private = await scene.event("private:B", "B", "A 说过他最近在换工作", key="b1")
    a_private = await scene.event("private:A", "A", "请记住我周末不处理工作", key="a1")
    a_core = await scene.fact("a_core", "A", "最近在换工作", foreign_evidence=(b_private,))
    a_explicit = await scene.fact(
        "a_explicit", "A", "周末不想被提醒工作", foreign_evidence=(b_private,), **EXPLICIT
    )
    a_ok_explicit = await scene.fact(
        "a_ok_explicit", "A", "周末不处理工作", evidence=(a_private,), **EXPLICIT
    )
    a_ok_core = await scene.fact("a_ok_core", "A", "习惯早起", evidence=(a_private,))
    await scene.raw_set("A", [a_core, a_ok_core], source="owner")

    turn = await scene.turn("A", "周六安排一下")

    assert sections(turn.segment) == {
        PRIVATE_EXPLICIT: [a_ok_explicit],
        PRIVATE_CORE: [a_ok_core],
    }
    assert a_core not in turn.fact_ids and a_explicit not in turn.fact_ids
    await _assert_matches_render(scene, "A", turn)
    scene.write(
        "F03b",
        {"facts": scene.facts, "private_A": turn_record(turn, await scene.contract())},
    )


# --------------------------------------------------------------------------- F4
async def test_f04_other_group_facts_never_reach_this_group(scene):
    """Case 4: H's facts and facts evidenced only in H stay out of G."""
    h_event = await scene.event("space:H", "C", "我们 H 群下周线下聚餐", key="h1")
    g_event = await scene.event("space:G", "A", "G 群的规矩是先发预告", key="g1")
    h = await scene.fact("h", "H", "下周线下聚餐", evidence=(h_event,))
    # H's fact evidenced in G (refused by the write path): only owner match excludes it.
    h_in_g = await scene.fact("h_in_g", "H", "H 群的人来 G 群打过招呼", foreign_evidence=(g_event,))
    h_g = await scene.fact("h_g", "G", "据说下周线下聚餐", foreign_evidence=(h_event,))
    w = await scene.fact("w", "B", "负责聚餐订位", evidence=(h_event,))
    g_ok = await scene.fact("g_ok", "G", "发活动前先发预告", evidence=(g_event,))
    await scene.raw_set("G", [h, h_in_g, h_g, g_ok])
    await scene.raw_set("B", [w])

    group = await scene.turn("B", "最近有活动吗", group="G")

    assert sections(group.segment) == {GROUP_CORE: [g_ok]}
    assert not {h, h_in_g, h_g, w} & set(group.fact_ids)
    await _assert_matches_render(scene, "G", group)
    scene.write(
        "F04",
        {"facts": scene.facts, "group_G_speaker_B": turn_record(group, await scene.contract())},
    )


async def _owner_private_scene(scene: ResidentScene) -> dict[str, int]:
    """A's private chat with explicit facts, a core set and SELF principles."""
    a1 = await scene.event("private:A", "A", "记住：委派的任务写审最多三轮", key="a1")
    a2 = await scene.event("private:A", "A", "我平时用 Python 和 Rust", key="a2")
    g1 = await scene.event("space:G", "A", "群里先发预告再开活动", key="g1")
    ids = {
        "rule": await scene.fact("rule", "A", "委派任务的写审最多三轮", evidence=(a1,), **EXPLICIT),
        "report": await scene.fact(
            "report", "A", "汇报时区分本地修改、验证与提交", evidence=(a1,), **EXPLICIT
        ),
        "lang": await scene.fact("lang", "A", "平时用 Python 和 Rust", evidence=(a2,)),
        "legacy": await scene.fact("legacy", "A", "  称呼他   老板  "),
        "principle": await scene.fact("principle", "self", "不确定时先查证再回答"),
        "principle_g": await scene.fact(
            "principle_g", "self", "在 G 群发言前先看预告", visibility="space:G", evidence=(g1,)
        ),
        "principle_b": await scene.fact("principle_b", "self", "替 B 保密", visibility="private:B"),
    }
    await scene.write_set("A", [ids["lang"], ids["legacy"]])
    await scene.write_set("self", [ids["principle"], ids["principle_g"], ids["principle_b"]])
    return ids


# --------------------------------------------------------------------------- F7
async def test_f07_five_turns_are_byte_identical(scene):
    """Case 7 (+Q14 a): no resident change means the same bytes in every turn."""
    ids = await _owner_private_scene(scene)
    turns = [await scene.turn("A", f"第 {index} 轮") for index in range(5)]

    assert turns[0].segment
    assert {turn.segment for turn in turns} == {turns[0].segment}
    assert sections(turns[0].segment) == {
        PRIVATE_EXPLICIT: [ids["rule"], ids["report"]],
        PRIVATE_CORE: [ids["lang"], ids["legacy"]],
        SELF_SECTION: [ids["principle"]],
    }
    # Whitespace is folded, never truncated or rewritten.
    assert f"- 称呼他 老板（fact:{ids['legacy']}）" in turns[0].segment
    await _assert_matches_render(scene, "A", turns[0])
    contract = await scene.contract()
    scene.write(
        "F07",
        {"facts": scene.facts, "turns": [turn_record(turn, contract) for turn in turns]},
    )


# --------------------------------------------------------------------------- F8
async def test_f08_lifecycle_changes_drop_items_and_keep_order(scene):
    """Case 8: invalidated, contested and expired core items vanish next turn."""
    a1 = await scene.event("private:A", "A", "一些关于我的事", key="a1")
    names = ("s1", "p", "s2", "q", "r", "s3")
    ids = [await scene.fact(name, "A", f"关于 A 的事实 {name}", evidence=(a1,)) for name in names]
    await scene.write_set("A", ids)
    before = await scene.turn("A", "早")
    memories = scene.chat._memories
    assert await memories.invalidate_fact(
        scene.facts["p"],
        reason=MemoryInvalidationReason.USER_RETRACTED,
        actor_user_id="10001",
    )
    assert await memories.contest_fact(scene.facts["q"], reason_code="resident_e2e_contest")
    await scene.set_fact(scene.facts["r"], valid_until=datetime.now(UTC) - timedelta(minutes=1))

    after = await scene.turn("A", "午")

    assert sections(before.segment)[PRIVATE_CORE] == ids
    expected = [scene.facts[name] for name in ("s1", "s2", "s3")]
    assert sections(after.segment)[PRIVATE_CORE] == expected
    # The row itself is not synchronised; filtering happens at render time.
    assert [row["fact_ids"] for row in await scene.table() if row["owner"] == "person:A"] == [ids]
    await _assert_matches_render(scene, "A", after)
    contract = await scene.contract()
    scene.write(
        "F08",
        {
            "facts": scene.facts,
            "before": turn_record(before, contract),
            "after": turn_record(after, contract),
        },
    )


# --------------------------------------------------------------------------- F13
async def test_f13_every_rendered_fact_is_readable_in_the_same_chat(scene):
    """Case 13: search_memory(fact_id=ID) reads each rendered ID with the same text."""
    await _owner_private_scene(scene)
    g1 = await scene.event("space:G", "B", "本群周五观影", key="g2")
    g_core = await scene.fact("g_core", "G", "每周五群内观影", evidence=(g1,))
    await scene.write_set("G", [g_core])

    results = {}
    for key, group in (("A", None), ("G", "G")):
        rendered = await _render(scene, key)
        assert rendered.fact_ids
        speaker = "A" if group is None else "B"
        replies, turn = await scene.tool_turn(
            speaker,
            f"读一下记忆 {key}",
            [("search_memory", {"fact_id": fact_id}) for fact_id in rendered.fact_ids],
            group=group,
        )
        assert turn.segment == rendered.text
        lines = {int(mark): line for line in rendered.text.split("\n") for mark in fact_marks(line)}
        for fact_id, reply in zip(rendered.fact_ids, replies, strict=True):
            rows = [row for row in reply.get("memories", ()) if row["fact_id"] == fact_id]
            assert rows, (fact_id, reply)
            content = " ".join(rows[0]["content"].split())
            assert lines[fact_id] == f"- {content}（fact:{fact_id}）"
        results[key] = {
            "turn": turn_record(turn, await scene.contract()),
            "read_fact_ids": [
                row["fact_id"] for reply in replies for row in reply.get("memories", ())
            ],
        }
    scene.write("F13", {"facts": scene.facts, **results})


# --------------------------------------------------------------------------- F15
async def test_f15_self_principles_only_from_owner_cli(scene):
    """Case 15 (render/CLI part): SELF rows hold only SELF facts; SOUL wins on conflict."""
    a1 = await scene.event("private:A", "A", "我喜欢猫", key="a1")
    principle = await scene.fact("principle", "self", "先查证再回答")
    contrary = await scene.fact("contrary", "self", "我其实很讨厌说话，尽量不回复")
    person = await scene.fact("person", "A", "喜欢猫", evidence=(a1,))
    soul_before = scene.settings.character_prompt

    with pytest.raises(ResidentValidationError):
        await scene.resident.write_set(scene.owner("self"), [person], source="owner")
    with pytest.raises(ResidentValidationError):
        await scene.resident.write_set(scene.owner("self"), [principle], source="dream")
    cli = await run_cli(
        scene.database.url, "memory", "resident", "set", "--self", "--facts", str(person)
    )
    assert cli.returncode != 0, cli
    assert await scene.table() == []
    cli_ok = await run_cli(
        scene.database.url, "memory", "resident", "set", "--self", "--facts", str(principle)
    )
    assert cli_ok.returncode == 0, cli_ok
    assert [(row["owner_kind"], row["source"], row["fact_ids"]) for row in await scene.table()] == [
        ("self", "owner", [principle])
    ]

    turn = await scene.turn("A", "你是谁")
    system = "\n".join(
        str(message.content or "")
        for message in turn.requests[0].messages
        if message.role == "system"
    )

    assert sections(turn.segment) == {SELF_SECTION: [principle]}
    assert contrary not in turn.fact_ids
    assert "与人格设定冲突时以人格设定为准" in system
    assert scene.settings.character_prompt == soul_before
    scene.write(
        "F15",
        {
            "facts": scene.facts,
            "table": await scene.table(),
            "private_A": turn_record(turn, await scene.contract()),
            "soul_sha256": _sha(soul_before),
        },
    )


# --------------------------------------------------------------------------- F16
async def test_f16_render_failure_keeps_the_turn(scene, monkeypatch, caplog):
    """Case 16: a failing read session in the renderer leaves only this segment empty."""
    await _owner_private_scene(scene)
    from yuki.memory.resident import ResidentMemory

    def broken_read_session(*args, **kwargs):
        raise RuntimeError("resident read session failed")

    def faulted(method):
        # Only the renderer's own read session fails; the rest of the turn is untouched.
        async def wrapper(self, *args, **kwargs):
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(scene.database, "read_session", broken_read_session)
                return await method(self, *args, **kwargs)

        return wrapper

    for name in ("render", "render_conversation"):
        monkeypatch.setattr(ResidentMemory, name, faulted(getattr(ResidentMemory, name)))
    scene.responder = lambda request: ChatResponse("收到", 0)
    caplog.set_level(logging.DEBUG)

    turn = await scene.turn("A", "渲染出错也要回复")

    assert turn.segment == "" and HEADER not in turn.current
    assert "渲染出错也要回复" in turn.current
    assert turn.result.reason == "chat"
    failures = [
        record
        for record in caplog.records
        if "memory_resident_render_failed" in record.getMessage()
    ]
    assert len(failures) == 1
    scene.write(
        "F16",
        {
            "private_A": turn_record(turn, await scene.contract()),
            "render_failed_logs": len(failures),
        },
    )


# --------------------------------------------------------------------------- F18
async def test_f18_recovery_contract_ignores_resident_changes(scene):
    """Case 18: the hash covers invariant messages only."""
    ids = await _owner_private_scene(scene)
    a3 = await scene.event("private:A", "A", "记住：周报周五交", key="a3")
    hashes = [await scene.contract()]
    await scene.write_set("A", [ids["legacy"]])
    hashes.append(await scene.contract())
    await scene.fact("weekly", "A", "周报周五交", evidence=(a3,), **EXPLICIT)
    hashes.append(await scene.contract())
    await scene.set_resident_enabled(False)
    hashes.append(await scene.contract())

    assert len(set(hashes)) == 1
    scene.write("F18", {"recovery_contract_sha256": [_sha(value) for value in hashes]})


async def test_f18b_flag_off_stops_render_and_selection_and_comes_back(scene):
    """§15 #19: flag off -> no segment and no automatic selection; on -> both return."""
    ids = await _owner_private_scene(scene)
    on_before = await scene.turn("A", "开着")
    await scene.set_resident_enabled(False)
    off = await scene.turn("A", "关了")
    off_selection = await scene.dream_selection_ran()
    # CLI render ignores the flag (rollout steps 5 and 6 depend on it).
    cli = await run_cli(
        scene.database.url,
        "memory",
        "resident",
        "render",
        "--conversation",
        scene.conversations["private:A"],
    )
    await scene.set_resident_enabled(True)
    on_after = await scene.turn("A", "又开了")
    on_selection = await scene.dream_selection_ran()

    assert on_before.segment and on_after.segment == on_before.segment
    assert off.segment == "" and HEADER not in off.current
    assert off_selection is False and on_selection is True
    assert cli.returncode == 0, cli
    assert on_before.segment in cli.stdout
    assert str(ids["rule"]) in cli.stdout
    contract = await scene.contract()
    scene.write(
        "F18b",
        {
            "on_before": turn_record(on_before, contract),
            "off": turn_record(off, contract),
            "on_after": turn_record(on_after, contract),
            "selection_ran": {"off": off_selection, "on": on_selection},
        },
    )


# --------------------------------------------------------------------------- F19
async def test_f19_third_party_facts_never_render(scene):
    """Case 19 (CLI/render part): third-party person and person_group facts stay out."""
    g_event = await scene.event("space:G", "A", "B 好像在学吉他", key="g1")
    b_private = await scene.event("private:B", "B", "我在学钢琴", key="b1")
    t = await scene.fact(
        "t", "B", "在学吉他", evidence=(g_event,), authority=MemoryAuthority.THIRD_PARTY
    )
    t_pg = await scene.fact(
        "t_pg",
        "B@G",
        "在 G 群被说成吉他手",
        evidence=(g_event,),
        authority=MemoryAuthority.THIRD_PARTY,
    )
    b_ok = await scene.fact("b_ok", "B", "在学钢琴", evidence=(b_private,))
    await scene.write_set("B", [b_ok])
    before = await scene.table()

    for bad in (t, t_pg):
        with pytest.raises(ResidentValidationError):
            await scene.resident.write_set(scene.owner("B"), [b_ok, bad], source="owner")
    cli = await run_cli(
        scene.database.url,
        "memory",
        "resident",
        "set",
        "--person",
        scene.persons["B"],
        "--facts",
        f"{b_ok},{t}",
    )
    assert cli.returncode != 0, cli
    assert await scene.table() == before
    await scene.raw_set("B", [t, b_ok, t_pg])

    turn = await scene.turn("B", "我在学什么")

    assert sections(turn.segment) == {PRIVATE_CORE: [b_ok]}
    await _assert_matches_render(scene, "B", turn)
    scene.write(
        "F19",
        {"facts": scene.facts, "private_B": turn_record(turn, await scene.contract())},
    )


# --------------------------------------------------------------------------- F22
async def test_f22_contradicting_explicit_facts_both_render_by_id(scene):
    """Case 22: both explicit facts in ID order; the header says the larger ID wins."""
    a1 = await scene.event("private:A", "A", "记住：我早上喝咖啡", key="a1")
    a2 = await scene.event("private:A", "A", "记住：我现在早上改喝茶了", key="a2")
    old = await scene.fact("old", "A", "早上喝咖啡", evidence=(a1,), **EXPLICIT)
    new = await scene.fact("new", "A", "早上改喝茶了", evidence=(a2,), **EXPLICIT)
    disputed = await scene.fact("disputed", "A", "不吃早饭", evidence=(a2,), **EXPLICIT)
    assert await scene.chat._memories.contest_fact(disputed, reason_code="resident_e2e")

    turn = await scene.turn("A", "早上喝什么")
    header = turn.segment.split("\n")[0]

    assert sections(turn.segment) == {PRIVATE_EXPLICIT: [old, new]}
    assert "以编号大的为准" in header
    # §15 #25: the header wording does not depend on who the counterpart is.
    assert "所有者" not in header
    await _assert_matches_render(scene, "A", turn)
    scene.write(
        "F22",
        {"facts": scene.facts, "private_A": turn_record(turn, await scene.contract())},
    )


# --------------------------------------------------------------------------- F27
async def test_f27_insertion_order_and_process_do_not_matter(scene, tmp_path):
    """Case 27: the same rows inserted in reverse order render identically elsewhere."""
    await _owner_private_scene(scene)
    conversation = scene.conversations["private:A"]
    first = await scene.reordered_copy(tmp_path / "forward.db", reverse=False)
    second = await scene.reordered_copy(tmp_path / "reverse.db", reverse=True)
    outputs = []
    for url in (first, second):
        result = await run_cli(
            url,
            "memory",
            "resident",
            "render",
            "--conversation",
            conversation,
        )
        assert result.returncode == 0, result
        outputs.append(result.stdout)

    rendered = await scene.resident.render_conversation(conversation)
    assert outputs[0] == outputs[1]
    assert rendered.text and rendered.text in outputs[0]
    scene.write(
        "F27",
        {
            "cli_stdout_sha256": [_sha(value) for value in outputs],
            "segment": rendered.text,
            "fact_ids": list(rendered.fact_ids),
        },
    )


# --------------------------------------------------------------------------- F28
async def test_f28_caps_and_overflow_line(scene):
    """Case 28 (no @-part, Q16 b): 30 explicit + overflow line; 11 refused; 15 -> 10."""
    a1 = await scene.event("private:A", "A", "记住这些规则", key="a1")
    b1 = await scene.event("private:B", "B", "关于我的一些事", key="b1")
    explicit = [
        await scene.fact(f"e{index:02d}", "A", f"规则 {index:02d}", evidence=(a1,), **EXPLICIT)
        for index in range(40)
    ]
    core = [
        await scene.fact(f"b{index:02d}", "B", f"B 的事实 {index:02d}", evidence=(b1,))
        for index in range(15)
    ]
    with pytest.raises(ResidentValidationError):
        await scene.resident.write_set(scene.owner("B"), core[:11], source="owner")
    cli = await run_cli(
        scene.database.url,
        "memory",
        "resident",
        "set",
        "--person",
        scene.persons["B"],
        "--facts",
        ",".join(str(value) for value in core[:11]),
    )
    assert cli.returncode != 0, cli
    assert await scene.table() == []
    await scene.raw_set("B", core)

    owner = await scene.turn("A", "列一下规则")
    member = await scene.turn("B", "关于我")
    rendered = await _render(scene, "A")

    assert sections(owner.segment)[PRIVATE_EXPLICIT] == explicit[:30]
    assert "（另有 10 条明确记忆未常驻，可用 search_memory 查）" in owner.segment.split("\n")
    assert rendered.omitted_explicit == 10
    assert sections(member.segment)[PRIVATE_CORE] == core[:10]
    contract = await scene.contract()
    scene.write(
        "F28",
        {
            "owner_private": turn_record(owner, contract),
            "omitted_explicit": rendered.omitted_explicit,
            "member_private": turn_record(member, contract),
        },
    )


# --------------------------------------------------------------------------- F9
async def test_f09_forgetting_a_fact_and_a_person(scene):
    """Case 9 (no @-part, Q16 b): privacy delete one fact, then forget B entirely."""
    b1 = await scene.event("private:B", "B", "记住我对花生过敏，我也喜欢下棋", key="b1")
    g1 = await scene.event("space:G", "A", "本群周末下棋", key="g1")
    allergy = await scene.fact("allergy", "B", "对花生过敏", evidence=(b1,), **EXPLICIT)
    chess = await scene.fact("chess", "B", "喜欢下棋", evidence=(b1,))
    go = await scene.fact("go", "B", "也会下围棋", evidence=(b1,))
    g_core = await scene.fact("g_core", "G", "周末组织下棋", evidence=(g1,))
    await scene.write_set("B", [chess, go])
    await scene.write_set("G", [g_core])
    before = await scene.turn("B", "第一轮")

    deleted = await scene.command("B", f"/ai memory delete {chess}")
    after_fact = await scene.turn("B", "第二轮")
    forgot = await scene.command("B", "/ai forgetme")
    group = await scene.turn("A", "群里还在吗", group="G")

    assert sections(before.segment) == {PRIVATE_EXPLICIT: [allergy], PRIVATE_CORE: [chess, go]}
    assert deleted == ["记忆已删除。"]
    assert sections(after_fact.segment) == {PRIVATE_EXPLICIT: [allergy], PRIVATE_CORE: [go]}
    assert forgot and "删除" in forgot[0]
    assert [row for row in await scene.table() if row["owner_kind"] == "person"] == []
    assert sections(group.segment) == {GROUP_CORE: [g_core]}
    assert not {allergy, chess, go} & set(group.fact_ids)
    contract = await scene.contract()
    scene.write(
        "F09",
        {
            "facts": scene.facts,
            "before": turn_record(before, contract),
            "after_fact_delete": turn_record(after_fact, contract),
            "group_after_forget": turn_record(group, contract),
            "table": await scene.table(),
        },
    )


# --------------------------------------------------------------------------- F10
async def test_f10_merge_renders_once_by_root(scene):
    """Case 10: B' merged into B renders B's root view from both accounts' chats."""
    from tests.support.person_merge import merge_persons

    b1 = await scene.event("private:B", "B", "记住我叫小北", key="b1")
    b2 = await scene.event("private:B2", "B2", "记住我住在杭州", key="b2")
    b_explicit = await scene.fact("b_explicit", "B", "叫小北", evidence=(b1,), **EXPLICIT)
    b2_explicit = await scene.fact("b2_explicit", "B2", "住在杭州", evidence=(b2,), **EXPLICIT)
    b_core = await scene.fact("b_core", "B", "喜欢骑车", evidence=(b1,))
    b2_core = await scene.fact("b2_core", "B2", "养了一只狗", evidence=(b2,))
    await scene.write_set("B", [b_core])
    await scene.write_set("B2", [b2_core])
    before = await scene.table()

    async with scene.database.immediate_session() as writer:
        root = await merge_persons(writer, scene.persons["B2"], scene.persons["B"])
    assert root == scene.persons["B"]
    root_turn = await scene.turn("B", "合并后 B")
    old_account_turn = await scene.turn("B2", "合并后 B′ 的账号", person="B")
    by_root = await scene.resident.render_conversation(scene.conversations["private:B"])
    by_old = await scene.resident.render_conversation(scene.conversations["private:B2"])
    loaded_root = await scene.resident.load_set(scene.owner("B"))
    loaded_old = await scene.resident.load_set(scene.owner("B2"))
    await scene.resident.write_set(scene.owner("B2"), [b_core, b2_core], source="owner")
    after_write = await scene.table()

    assert by_root.text == by_old.text == root_turn.segment == old_account_turn.segment
    assert len(set(root_turn.fact_ids)) == len(root_turn.fact_ids)
    assert sections(root_turn.segment) == {
        PRIVATE_EXPLICIT: [b_explicit, b2_explicit],
        PRIVATE_CORE: [b_core],
    }
    assert loaded_old.fact_ids == loaded_root.fact_ids
    old_row = [row for row in before if row["owner"] == "person:B2"]
    assert [row for row in after_write if row["owner"] == "person:B2"] == old_row
    assert [row["fact_ids"] for row in after_write if row["owner"] == "person:B"] == [
        [b_core, b2_core]
    ]
    contract = await scene.contract()
    scene.write(
        "F10",
        {
            "facts": scene.facts,
            "root_chat": turn_record(root_turn, contract),
            "old_account_chat": turn_record(old_account_turn, contract),
            "table_after_root_write": after_write,
        },
    )


# --------------------------------------------------------------------------- F17
async def test_f17_work_keeps_its_first_current_message(database, tmp_path):
    """Case 17: inside one Work activation the first current message never changes.

    The first activation is interrupted by a retryable provider outage before its
    third dispatch (no request bought); WorkScheduler continues the same Work.

    Observed and asserted (task book §6.3 left this as an assumption for case 17):
    a continuation is a new activation whose ``work_resume`` wakeup is composed like
    every other wakeup (§6.5 "Work 续接唤醒: 按该轮所在会话"), so it carries the resident
    segment rendered at that moment, once, and keeps it verbatim for the rest of that
    activation. The original first request is never re-sent or re-paid.
    """
    import asyncio

    from tests.unit.test_history_dispatch_ownership import retry_due_now
    from tests.unit.test_work_owner_recovery import original_scheduler

    from yuki.llm.base import LLMUnavailableError
    from yuki.runtime.work_repository import WorkRepository
    from yuki.runtime.work_schema_v1 import work

    scene = await ResidentScene(database, tmp_path).build(work_enabled=True)
    ids = await _owner_private_scene(scene)
    phase: dict = {"name": "work", "work": []}

    def respond(request):
        if phase["name"] == "work":
            phase["work"].append(request)
            number = len(phase["work"])
            if number <= 3:
                return tool_call(
                    "set_short_note", {"slot": 1, "text": f"步骤 {number}"}, f"s{number}"
                )
            return "完成原工作"
        return "NO_REPLY"

    start = len(scene.provider.requests)
    complete = scene.provider.complete
    armed = {"on": True}

    async def interrupt_before_third(request):
        if armed["on"] and len(scene.provider.requests) - start >= 2:
            armed["on"] = False
            raise LLMUnavailableError("synthetic outage before the third dispatch")
        return await complete(request)

    scene.provider.complete = interrupt_before_third
    scene.responder = respond
    pause = start + 2
    scene.provider.arrived[pause] = asyncio.Event()
    scene.provider.release[pause] = asyncio.Event()
    running = asyncio.create_task(scene.turn("A", "把周报整理好"))
    await scene.provider.wait(pause)
    a9 = await scene.event("private:A", "A", "记住：周报用表格", key="a9")
    table_rule = await scene.fact("table_rule", "A", "周报用表格", evidence=(a9,), **EXPLICIT)
    await scene.write_set("A", [ids["legacy"]])
    scene.provider.release[pause].set()
    first = await asyncio.wait_for(running, 30)
    original = first.current
    async with database.read_session() as reader:
        interrupted = dict((await reader.execute(select(work))).mappings().one())
    assert interrupted["state"] == "queued", interrupted["state"]
    paid_before_resume = len(scene.provider.requests)

    await retry_due_now(database)
    repository = WorkRepository(database)
    await original_scheduler(repository, scene.env, scene.harness, scene.chat).drain_once()
    final = await repository.get(interrupted["id"])
    phase["name"] = "after"
    later = await scene.turn("A", "新的一轮")

    assert final["state"] == "completed"
    assert first.segment, first.current
    assert f"fact:{table_rule}" not in first.segment
    assert len(phase["work"]) > 2 and len(scene.provider.requests) > paid_before_resume

    def resident_messages(request):
        return [m.content for m in request.messages if (m.content or "").startswith(HEADER)]

    for request in phase["work"][:2]:
        assert resident_messages(request) == [original]
    resumed = phase["work"][2:]
    wakeup = resident_messages(resumed[0])
    assert len(wakeup) == 1 and "work_resume" in wakeup[0]
    assert wakeup[0].startswith((await _render(scene, "A")).text + "\n")
    assert f"fact:{table_rule}" in wakeup[0]
    for request in resumed:
        assert resident_messages(request) == wakeup
    # No request was composed fresh around the original message after the first one.
    assert (
        sum(1 for request in scene.provider.requests if request.messages[-1].content == original)
        == 1
    )
    assert table_rule in sections(later.segment)[PRIVATE_EXPLICIT]
    assert sections(later.segment)[PRIVATE_CORE] == [ids["legacy"]]
    contract = await scene.contract()
    scene.write(
        "F17",
        {
            "facts": scene.facts,
            "work_first_current_segment": first.segment,
            "resumed_wakeup_segment": resident_segment(wakeup[0]),
            "work_requests": len(phase["work"]),
            "work_state": final["state"],
            "later": turn_record(later, contract),
        },
    )


# --------------------------------------------------------------------------- F20
async def test_f20_superseded_core_item_renders_its_active_successor(scene):
    """Case 20 (render part): Dream synthesis; m' renders, m never; ineligible m' hides both."""
    a1 = await scene.event("private:A", "A", "我喜欢美式咖啡，不加糖", key="a1")
    anchor = await scene.fact("anchor", "A", "平时喜欢喝美式咖啡", evidence=(a1,))
    m = await scene.fact("m", "A", "喝咖啡不加糖", evidence=(a1,))
    keep = await scene.fact("keep", "A", "早起跑步", evidence=(a1,))
    await scene.write_set("A", [m, keep])
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
    run = await scene.dream_run((("anchor", "m"),))
    assert run.status.value == "completed"
    page = await scene.dream().repository.run_page(run.public_id)
    successor = page.operations[0].output_fact_id
    assert successor not in (anchor, m)

    replies, turn = await scene.tool_turn(
        "A", "我喝什么咖啡", [("search_memory", {"fact_id": successor})]
    )
    await scene.chat._memories.contest_fact(successor, reason_code="resident_e2e")
    hidden = await scene.turn("A", "再问一次")

    assert sections(turn.segment)[PRIVATE_CORE] == [successor, keep]
    assert f"- 喜欢不加糖的美式咖啡（fact:{successor}）" in turn.segment
    assert m not in turn.fact_ids
    assert [row["fact_id"] for row in replies[0]["memories"]] == [successor]
    assert sections(hidden.segment)[PRIVATE_CORE] == [keep]
    # The row still holds m: successor resolution happens at render time only.
    assert [row["fact_ids"] for row in await scene.table() if row["owner"] == "person:A"] == [
        [m, keep]
    ]
    contract = await scene.contract()
    scene.write(
        "F20",
        {
            "facts": {**scene.facts, "successor": successor},
            "successor_turn": turn_record(turn, contract),
            "ineligible_successor_turn": turn_record(hidden, contract),
        },
    )


# --------------------------------------------------------------------------- F23
async def test_f23_public_compact_checkpoint_never_holds_the_segment(database, tmp_path):
    """Case 23 (public part): the checkpoint input/text has no resident text."""
    from yuki.conversation.projection_models import PromptProjectionModel

    scene = await ResidentScene(database, tmp_path).build()
    await _owner_private_scene(scene)
    for index in range(6):
        await scene.event("private:A", "A", f"较早的聊天 {index}：" + "讨论周报格式。" * 40)
    compact_requests = []

    def respond(request):
        if "Host 会话上下文压缩要求" in str(request.messages[-1].content or ""):
            compact_requests.append(request)
            return "PUBLIC_CHECKPOINT：讨论过周报格式。"
        return "NO_REPLY"

    scene.responder = respond
    before = await scene.turn("A", "压缩前")
    replies = await scene.command("A", "/ai compact")
    after = await scene.turn("A", "压缩后")
    async with database.read_session() as reader:
        checkpoints = [
            row.selected_summary_text or ""
            for row in (await reader.scalars(select(PromptProjectionModel))).all()
        ]

    assert compact_requests, replies
    assert before.segment
    for request in compact_requests:
        text = request_text(request)
        assert HEADER not in text and "（fact:" not in text
    assert any("PUBLIC_CHECKPOINT" in text for text in checkpoints)
    assert all(HEADER not in text and "（fact:" not in text for text in checkpoints)
    assert request_text(after.requests[0]).count(HEADER) == 1
    assert after.segment == before.segment
    await _assert_matches_render(scene, "A", after)
    contract = await scene.contract()
    scene.write(
        "F23",
        {
            "before": turn_record(before, contract),
            "after": turn_record(after, contract),
            "compact_requests": len(compact_requests),
        },
    )


async def test_f23_execution_compact_keeps_the_segment_in_the_work(database, tmp_path, monkeypatch):
    """Case 23 (Work part): after an execution compact the first current message stays."""
    import yuki.services.context_compaction as compaction
    from yuki.runtime.work_repository import WorkCapacityError
    from yuki.services.context_compaction import (
        EXECUTION_COMPACT_INSTRUCTION,
        PUBLIC_COMPACT_INSTRUCTION,
    )

    scene = await ResidentScene(database, tmp_path).build(
        work_enabled=True, context_compact_trigger_tokens=96000
    )
    await _owner_private_scene(scene)
    await scene.event("private:A", "A", ("PUBLIC_OLD " + "historical decision. " * 9500).rstrip())
    answer = "LONG_ANSWER " + "independent branch; " * 2400
    kinds: list[str] = []
    state = {"sent": False}

    def respond(request):
        content = str(request.messages[-1].content or "")
        if content.startswith(PUBLIC_COMPACT_INSTRUCTION):
            kinds.append("public_compact")
            return "PUBLIC_CHECKPOINT"
        if content.startswith(EXECUTION_COMPACT_INSTRUCTION):
            kinds.append("execution_compact")
            assert HEADER not in content
            return "PRIVATE_CHECKPOINT：已发送长回答。"
        kinds.append("normal")
        if not state["sent"]:
            state["sent"] = True
            return tool_call("send", {"text": answer}, "long-send")
        return "NO_REPLY"

    def refuse_public(*args, **kwargs):
        raise WorkCapacityError("model_request_capacity")

    monkeypatch.setattr(compaction, "public_compact_plan", refuse_public)
    scene.responder = respond
    first = await scene.turn("A", "写一段很长的回答")
    normals = [
        request
        for request, kind in zip(scene.provider.requests[-len(kinds) :], kinds, strict=True)
        if kind == "normal"
    ]

    assert "execution_compact" in kinds, kinds
    assert first.segment
    following = normals[-1]
    assert any(message.content == first.current for message in following.messages)
    assert request_text(following).count(HEADER) == 1
    scene.write(
        "F23-work",
        {"first": turn_record(first, await scene.contract()), "request_kinds": kinds},
    )


# --------------------------------------------------------------------------- F24
async def _entry_scene(database, tmp_path) -> tuple[ResidentScene, dict[str, int]]:
    scene = await ResidentScene(database, tmp_path).build(work_enabled=True)
    ids = await _owner_private_scene(scene)
    g1 = await scene.event("space:G", "B", "本群周五观影", key="g-core")
    ids["g_core"] = await scene.fact("g_core", "G", "每周五群内观影", evidence=(g1,))
    await scene.write_set("G", [ids["g_core"]])
    return scene, ids


async def test_f24_scheduled_turns_follow_their_conversation(database, tmp_path):
    """Case 24 (automation part): A's private schedule has A's sections; G's has G's."""
    from tests.integration.test_execution_continuity_e2e import _automation

    from yuki.runtime.work_activation import current_work_control

    scene, ids = await _entry_scene(database, tmp_path)
    _, clock, dispatch = await _automation(scene.env, scene.chat, scene.settings, database)
    scheduled: dict[str, object] = {}
    phase = {"create": None}

    def respond(request):
        control = current_work_control.get()
        if control is not None and control.source.get("origin") == "scheduled_automation":
            text = request_text(request)
            key = "private" if "PRIVATE_SCHEDULE_GOAL" in text else "group"
            scheduled.setdefault(key, request)
            return "NO_REPLY"
        if phase["create"] is not None and not any(m.role == "tool" for m in request.messages):
            name, goal = phase["create"]
            return tool_call(
                "manage_schedule",
                {
                    "action": "create",
                    "name": name,
                    "goal": goal,
                    "trigger": {"type": "after", "seconds": 1},
                },
                f"create-{name}",
            )
        return "NO_REPLY"

    scene.responder = respond
    phase["create"] = ("private", "PRIVATE_SCHEDULE_GOAL")
    await scene.turn("A", "一分钟后提醒我看周报")
    phase["create"] = ("group", "GROUP_SCHEDULE_GOAL")
    await scene.turn("A", "一分钟后在群里提醒大家观影", group="G")
    phase["create"] = None
    clock.advance(2)
    await dispatch()

    private = capture([scheduled["private"]])
    group = capture([scheduled["group"]])
    assert sections(private.segment) == {
        PRIVATE_EXPLICIT: [ids["rule"], ids["report"]],
        PRIVATE_CORE: [ids["lang"], ids["legacy"]],
        SELF_SECTION: [ids["principle"]],
    }
    assert sections(group.segment) == {
        GROUP_CORE: [ids["g_core"]],
        # The global principle has no evidence row: groups need all evidence in G.
        SELF_SECTION: [ids["principle_g"]],
    }
    contract = await scene.contract()
    scene.write(
        "F24-schedule",
        {
            "facts": scene.facts,
            "private_schedule": turn_record(private, contract),
            "group_schedule": turn_record(group, contract),
        },
    )


async def test_f24_self_initiative_turn_has_group_core_and_self_only(database, tmp_path):
    """Case 24 (SELF part): the autonomous turn in G renders "[本群]" and SELF only."""
    from tests.integration.test_self_common_capabilities import runtime, self_actor

    from yuki.conversation.scope import ConversationTurnSnapshot
    from yuki.domain.conversations import ConversationScope
    from yuki.runtime.origin import TurnOrigin
    from yuki.runtime.trigger import SelfInitiativeTrigger

    scene, ids = await _entry_scene(database, tmp_path)
    chat = scene.chat
    identity = ConversationScope.group("80001", "20001")
    scope = await chat._conversation_scopes.get(identity)
    actor = await self_actor(database, scene.env)
    config = await chat._runtime_config.snapshot(group_id=scene.env.space)
    token = await chat._turn_coordinator.notify_message(
        scope.runtime_scope_key, TurnOrigin.SELF_INITIATIVE
    )
    snapshot = ConversationTurnSnapshot(
        conversation_id=scope.id,
        scope_key=scope.runtime_scope_key,
        generation=scope.generation,
        trigger_event_id=None,
        coordinator_version=token.version,
        initiative_run_id=actor.initiative_run_id,
    )
    start = len(scene.provider.requests)

    async def validate():
        assert chat._turn_coordinator.is_current(token)

    await chat.generate_self_initiative(
        trigger=SelfInitiativeTrigger(
            run_id=actor.initiative_run_id,
            conversation_id=scope.id,
            generation=scope.generation,
            space_id=scene.env.space,
            presence_id=scene.env.presence,
            group_id="20001",
            bot_user_id="80001",
            instruction=actor.instruction,
        ),
        runtime=config,
        turn_token=token,
        turn_snapshot=snapshot,
        before_model_request=validate,
        source_runtime=runtime(actor, scene.env),
    )
    turn = capture(scene.provider.requests[start : start + 1])

    assert sections(turn.segment) == {
        GROUP_CORE: [ids["g_core"]],
        # The global principle has no evidence row: groups need all evidence in G.
        SELF_SECTION: [ids["principle_g"]],
    }
    await _assert_matches_render(scene, "G", turn)
    scene.write(
        "F24-self",
        {"facts": scene.facts, "self_initiative": turn_record(turn, await scene.contract())},
    )


async def test_f24_subagent_requests_carry_no_segment(database, tmp_path):
    """Case 24 (subagent part, Q13 a): the parent has the segment, the child never."""
    from tests.support.runtime_execution import make_child_executor

    from yuki.runtime.work_repository import WorkRepository

    scene, ids = await _entry_scene(database, tmp_path)
    database.subagents_enabled = True
    script = {"phase": "accept", "child": None}
    children: list = []

    def respond(request):
        if any(
            message.role == "user" and '"cwd": "/workspace/tasks/' in (message.content or "")
            for message in request.messages
        ):
            children.append(request)
            return tool_call(
                "manage_task", {"action": "finish", "result": "核对完毕"}, "child-done"
            )
        if script["phase"] == "accept":
            script["phase"] = "spawn"
            return tool_call("manage_task", {"action": "start", "goal": "核对周报"}, "accept")
        if script["phase"] == "spawn":
            script["phase"] = "wait"
            return tool_call(
                "start_subtask", {"goal": "核对周报数字", "acceptance": "返回核对结果"}, "spawn"
            )
        if script["phase"] == "wait":
            script["child"] = tool_result(request, "spawn")["id"]
            script["phase"] = "waiting"
            return tool_call("wait", {"for": [{"run": script["child"]}]}, "wait-child")
        return "NO_REPLY"

    scene.responder = respond
    parent = await scene.turn("A", "派个子任务核对周报")
    executor = make_child_executor(
        WorkRepository(database),
        chat=scene.chat,
        config=scene.chat._runtime_config,
        runner=scene.chat.runtime.runner,
        load_tools=scene.chat.runtime.runner.main_contract.definitions,
        router=scene.env.router,
    )
    await executor.run(script["child"])

    assert parent.segment and ids["rule"] in parent.fact_ids
    assert children
    for request in children:
        text = request_text(request)
        assert HEADER not in text and "（fact:" not in text
    scene.write(
        "F24-subagent",
        {
            "parent": turn_record(parent, await scene.contract()),
            "child_requests": len(children),
            "child_has_segment": any(HEADER in request_text(request) for request in children),
        },
    )


async def test_f24_plugin_turn_carries_no_segment(database, tmp_path):
    """Case 24 (plugin part, Q13 a): assemble_plugin never renders resident memory."""
    from tests.integration.test_plugin_agent_domain_grants import source_host
    from tests.support.memory_resident_scene import install_resident

    from yuki.llm.fake import FakeLLMProvider
    from yuki.memory.resident import ResidentAudience, ResidentMemory, ResidentOwner

    provider = FakeLLMProvider()
    env, _harness, chat, source, host = await source_host(database, tmp_path, provider, ())
    install_resident(chat, database)
    change = await chat._runtime_config.set_override(
        "memory.resident_enabled",
        True,
        scope_type="global",
        scope_id="",
        actor_user_id="10001",
        trigger_message_id="resident-plugin",
    )
    assert change.success, change
    async with database.read_session() as reader:
        group_event = await reader.scalar(select(ChatEventModel.id).order_by(ChatEventModel.id))
    principle = await scene_free_self_fact(chat, "插件轮不该看到的原则", group_event)
    await ResidentMemory(database).write_set(
        ResidentOwner(kind="self"), [principle], source="owner"
    )
    control = await ResidentMemory(database).render(
        ResidentAudience(kind="space", person_id=None, space_id=env.space)
    )
    assert control.fact_ids == (principle,)
    provider._responder = lambda request: tool_call(
        "manage_task", {"action": "finish", "result": "done"}, "finish"
    )
    try:
        with host.invocation_scope(host.plugin_id, source):
            result = await host.agent.run("插件工作")
        assert result.ok, result
    finally:
        await host.close_host_resources()

    assert provider.requests
    for request in provider.requests:
        text = request_text(request)
        assert HEADER not in text and "（fact:" not in text
    write_artifact(
        "F24-plugin",
        {"plugin_requests": len(provider.requests), "control_render": control.text},
        {env.person: "person:A", env.space: "space:G"},
    )


async def scene_free_self_fact(chat, content: str, event_id: int) -> int:
    """A global SELF fact evidenced in the plugin source's group (G)."""
    from yuki.memory.enums import MemoryEvidenceRelation, MemoryScopeType, SelfMemoryVisibility
    from yuki.memory.models import MemoryEvidenceCreate, MemoryFactCreate

    fact = await chat._memories.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.SELF,
            visibility_type=SelfMemoryVisibility.GLOBAL,
            memory_key="resident:plugin-principle",
            category="resident-e2e",
            content=content,
            source_type=MemorySourceType.AUTOMATIC,
            authority=MemoryAuthority.AGENT_REFLECTION,
        ),
        evidence=MemoryEvidenceCreate(
            event_id=event_id,
            source_speaker_user_id="10001",
            relation=MemoryEvidenceRelation.AGENT_REFLECTION,
            authority=MemoryAuthority.AGENT_REFLECTION,
            excerpt=content,
        ),
    )
    return fact.id


# --------------------------------------------------------------------------- F25
async def test_f25_disable_merge_forget_lifecycle(scene):
    """Case 25: disabled B renders nothing; merged B renders by root; forgotten renders none."""
    from tests.support.person_merge import merge_persons

    b1 = await scene.event("private:B", "B", "记住我是左撇子，我喜欢游泳", key="b1")
    c1 = await scene.event("private:C", "C", "我喜欢爬山", key="c1")
    g1 = await scene.event("space:G", "A", "本群周日游泳", key="g1")
    b_explicit = await scene.fact("b_explicit", "B", "是左撇子", evidence=(b1,), **EXPLICIT)
    b_core = await scene.fact("b_core", "B", "喜欢游泳", evidence=(b1,))
    c_core = await scene.fact("c_core", "C", "喜欢爬山", evidence=(c1,))
    g_core = await scene.fact("g_core", "G", "周日组织游泳", evidence=(g1,))
    await scene.write_set("B", [b_core])
    await scene.write_set("C", [c_core])
    await scene.write_set("G", [g_core])
    enabled = await scene.resident.render_conversation(scene.conversations["private:B"])

    await scene.harness.private_users.set_enabled(scene.persons["B"], False)
    disabled_private = await scene.resident.render_conversation(scene.conversations["private:B"])
    disabled_group = await scene.turn("A", "群里谁会游泳", group="G")
    await scene.harness.private_users.set_enabled(scene.persons["B"], True)
    async with scene.database.immediate_session() as writer:
        root = await merge_persons(writer, scene.persons["B"], scene.persons["C"])
    assert root == scene.persons["C"]
    merged_b = await scene.resident.render_conversation(scene.conversations["private:B"])
    merged_c = await scene.turn("C", "合并后")
    forgot = await scene.command("C", "/ai forgetme")
    forgotten_group = await scene.turn("A", "群里还有谁", group="G")

    assert sections(enabled.text) == {PRIVATE_EXPLICIT: [b_explicit], PRIVATE_CORE: [b_core]}
    assert disabled_private.text == "" and disabled_private.fact_ids == ()
    assert sections(disabled_group.segment) == {GROUP_CORE: [g_core]}
    assert merged_b.text == merged_c.segment
    assert sections(merged_c.segment) == {PRIVATE_EXPLICIT: [b_explicit], PRIVATE_CORE: [c_core]}
    assert forgot and "删除" in forgot[0]
    assert [row for row in await scene.table() if row["owner_kind"] == "person"] == []
    assert sections(forgotten_group.segment) == {GROUP_CORE: [g_core]}
    contract = await scene.contract()
    scene.write(
        "F25",
        {
            "facts": scene.facts,
            "enabled_private_B": enabled.text,
            "disabled_private_B": disabled_private.text,
            "disabled_group": turn_record(disabled_group, contract),
            "merged_private_C": turn_record(merged_c, contract),
            "forgotten_group": turn_record(forgotten_group, contract),
        },
    )


# --------------------------------------------------------------------------- F26
async def test_f26_qq_app_and_acp_private_chats_render_the_same(database, tmp_path):
    """Case 26: one person's QQ, App and ACP private chats get byte-identical segments."""
    import asyncio
    from uuid import uuid4

    import httpx
    from fastapi import FastAPI
    from tests.integration.test_current_material_main_contract import _deployment
    from tests.unit.test_client_api import bearer

    from yuki.client_api.http import attach_client_api
    from yuki.domain.conversations import ScopeType
    from yuki.domain.messages import InboundMessage, SenderIdentity
    from yuki.llm.fake import FakeLLMProvider
    from yuki.memory.enums import MemoryScopeType, SelfMemoryVisibility
    from yuki.memory.models import MemoryFactCreate
    from yuki.memory.repository import MemoryFactRepository
    from yuki.memory.resident import ResidentMemory, ResidentOwner
    from yuki.memory.service import MemoryFactService

    provider = FakeLLMProvider(lambda request: "NO_REPLY")
    app, qq, _message, qq_sender = await _deployment(database, tmp_path, provider)
    await app.app_gateway.start()
    await app.acp_gateway.start()
    await app._freeze_main_manifest()
    change = await app.runtime_config.set_override(
        "memory.resident_enabled",
        True,
        scope_type="global",
        scope_id="",
        actor_user_id="10001",
        trigger_message_id="resident-entries",
    )
    assert change.success, change
    memories = MemoryFactService(MemoryFactRepository(database))

    async def remember(name, content, **fields):
        fact = await memories.remember(
            MemoryFactCreate(
                memory_key=f"resident:{name}",
                category="resident-e2e",
                content=content,
                **fields,
            )
        )
        return fact.id

    person = {
        "scope_type": MemoryScopeType.PERSON,
        "subject_user_id": "10001",
        "canonical_subject_person_id": qq.person,
    }
    explicit = await remember("explicit", "周报周五交", **person, **EXPLICIT)
    core = await remember(
        "core", "喜欢简洁的回答", **person, source_type=MemorySourceType.AUTOMATIC
    )
    principle = await remember(
        "principle",
        "先查证再回答",
        scope_type=MemoryScopeType.SELF,
        visibility_type=SelfMemoryVisibility.GLOBAL,
        source_type=MemorySourceType.AUTOMATIC,
        authority=MemoryAuthority.AGENT_REFLECTION,
    )
    resident = ResidentMemory(database)
    await resident.write_set(
        ResidentOwner(kind="person", person_id=qq.person), [core], source="owner"
    )
    await resident.write_set(ResidentOwner(kind="self"), [principle], source="owner")

    issued = await app.client_api.identity.issue_for_operator(qq.person, str(uuid4()), str(uuid4()))
    device = await app.client_api.identity.pair(issued["code"], "resident entries device")
    http_app = FastAPI()
    attach_client_api(http_app, lambda: app.client_api)
    segments: dict[str, object] = {}

    def turn_for(text):
        requests = [r for r in provider.requests if text in str(r.messages[-1].content or "")]
        assert requests, text
        return capture(requests[:1])

    async def settle(http, headers, request_id):
        async with asyncio.timeout(15):
            while True:
                response = await http.get(f"/client/v1/requests/{request_id}", headers=headers)
                assert response.status_code == 200, response.text
                if response.json()["data"].get("turn", {}).get("state") == "ended":
                    return
                await asyncio.sleep(0.01)

    try:
        result = await app.processor.handle(
            InboundMessage(
                ingress_key="resident-qq",
                native_message_id="resident-qq",
                event_type="message:test",
                scope_type=ScopeType.PRIVATE,
                sender=SenderIdentity("10001", nickname="本人"),
                text="QQ_ENTRY_MESSAGE",
                bot_user_id="80001",
                person_id=qq.person,
                presence_id=qq.presence,
            ),
            qq_sender,
        )
        assert result.reason == "chat", result
        segments["qq"] = turn_for("QQ_ENTRY_MESSAGE")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=http_app), base_url="http://test"
        ) as http:
            app_headers = bearer(device["token"])
            request_id = str(uuid4())
            sent = await http.post(
                "/client/v1/messages",
                headers=app_headers,
                json={"request_id": request_id, "text": "APP_ENTRY_MESSAGE"},
            )
            assert sent.status_code == 200, sent.text
            await settle(http, app_headers, request_id)
            segments["app"] = turn_for("APP_ENTRY_MESSAGE")
            created = await http.post(
                "/client/v1/acp/sessions", headers=app_headers, json={"request_id": str(uuid4())}
            )
            assert created.status_code == 200, created.text
            session = created.json()["data"]
            acp_headers = {**app_headers, "X-Yuki-Conversation": session["session_id"]}
            request_id = str(uuid4())
            sent = await http.post(
                "/client/v1/messages",
                headers=acp_headers,
                json={"request_id": request_id, "text": "ACP_ENTRY_MESSAGE"},
            )
            assert sent.status_code == 200, sent.text
            await settle(http, acp_headers, request_id)
            segments["acp"] = turn_for("ACP_ENTRY_MESSAGE")
    finally:
        assert not await app.lifecycle._close_entries(app.lifecycle._entries)

    texts = {key: value.segment for key, value in segments.items()}
    assert texts["qq"] and len(set(texts.values())) == 1
    assert sections(texts["qq"]) == {
        PRIVATE_EXPLICIT: [explicit],
        PRIVATE_CORE: [core],
        SELF_SECTION: [principle],
    }
    write_artifact(
        "F26",
        {
            key: {"segment": value.segment, "fact_ids": value.fact_ids}
            for key, value in segments.items()
        },
        {qq.person: "person:A"},
    )
