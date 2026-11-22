"""E2E: person profiles in Main turns and in the Dream selection step (memory layering P4).

Authoritative spec: docs/architecture/Yuki-记忆分层P4人物画像任务书-2026-11-22.md §5
(member lists), §7 (rendering), §8 (maintenance) and the §13 cases; the data-layer
parts of the same cases live in ``test_memory_profiles_e2e.py``.

Every case runs a real SQLite database, real fact writes with evidence on real fixture
chat events, the real Dream worker loop body (clusters, then the selection step with
member lists, program cleanup and profile calls, then ``finalize_run``) and real
``MessageProcessor`` -> ``ContextAssembler`` -> ``MainAgentTurns.compose`` turns. Only
model replies are scripted: profile calls are recognised by their instruction and
answered from their own structured input (``cur_N`` / ``f_N`` / ``ref_N``). Each case
writes the profile part of the system message, the current message, the three P4
tables, the scripted model's profile inputs and the ``recovery_contract`` hash as
normalized JSON to ``test-results/memory-profiles/<case>.json``. All text is invented.

Fixture (P4 §13): A (owner), B, C, D, B2 (merged into B where a case needs it), E/F
created by the cases that need them; groups G (A, B, C) and H (B, C, D). The first
Dream run's window is the 24 hours before it, so it sees every opening message.

Failure cases are the §13 list, written before the implementation:
  H1/H31 a non-public fact (own private, no evidence, non-public explicit) reaches the
         public call input, or a public output citing it is accepted;
  H2/H5  a fact with someone else's private evidence, or a person_group fact, reaches any
         profile input;
  H3     a private profile renders in a group or another person's private chat;
  H4     a public profile from another group's evidence is hidden, or the group's own core
         set / rules loosen;
  H7     an entry with one failing ID still renders, or the other entries change;
  H8/H24 cleanup leaves failing entries, calls a model, skips locked profiles, or a locked
         profile gets a model call;
  H9     a superseded ID breaks the entry instead of mapping to the successor;
  H10/H11 eleven entries, 41 characters, an unknown alias, empty facts, a duplicate alias,
         a ``ref_N`` citation, an unknown section, a newline or an empty reason is stored
         (or not recorded as an attempt with its code);
  H12    an unchanged output writes a version, or a reorder does not;
  H13/H15/H16/H21/H26 member lists count Yuki, disabled or merged people wrongly, break
         ties by anything but root ID, clear a quiet group, overwrite a pinned list, or
         keep someone who stopped speaking after the next run;
  H14    the system prefix changes within a day without a fact becoming unavailable;
  H17/H18 an inactive person is maintained or loses the profile; a private-only person is
         not maintained; the private call runs before the public one or misses the new
         public entries as reference;
  H19    a merged person renders twice, misses the old ID, or the root is not due;
  H20    a forgotten person still renders in a group;
  H27    the per-run cap is ignored, or a voiding profile keeps the queue head;
  H29    a profile change moves the recovery contract hash or reaches a compact checkpoint
         or the current message;
  H32/H34 a fact losing eligibility still renders, cleanup leaves it, or a fact gaining
         public eligibility is missing from the next public input;
  H33/H35 Dream overwrites an owner change made during its call, or calls a locked profile;
         unlocking does not hand it back.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from tests.support.event_seed import seed_scoped_event
from tests.support.memory_profile_scene import (
    MEMBERS_TITLE,
    PRIVATE_TITLE,
    PUBLIC_TITLE,
    ProfileScene,
    current_reply,
    profile_blocks,
    profile_payload,
    profile_reply,
)
from tests.support.memory_resident_scene import GROUPS, request_text, system_text
from tests.support.person_merge import merge_persons

from yuki.domain.conversations import ConversationScope
from yuki.memory.enums import (
    MemoryAuthority,
    MemoryEvidenceRelation,
    MemoryInvalidationReason,
    MemorySourceType,
)
from yuki.memory.models import MemoryEvidenceCreate
from yuki.persistence.models import MemoryEvidenceModel

EXPLICIT = {"source_type": MemorySourceType.EXPLICIT, "authority": MemoryAuthority.EXPLICIT}
BOT = "80001"


@pytest.fixture
async def scene(database, tmp_path):
    return await ProfileScene(database, tmp_path).build()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


async def _seed(scene: ProfileScene, conversation: str, sender: str, count: int) -> None:
    for index in range(count):
        await scene.event(conversation, sender, f"{sender} 在 {conversation} 的第 {index} 句")


async def _outbound(scene: ProfileScene, group: str, count: int) -> None:
    """Yuki's own replies in a group (``author_kind='yuki'``)."""
    for index in range(count):
        await seed_scoped_event(
            scene.harness.scoped_events,
            scope=ConversationScope.group(BOT, GROUPS[group]),
            origin_key=f"profile-outbound-{group}-{index}-{scene._next()}",
            sender_user_id=BOT,
            direction="outbound",
            content=f"Yuki 在 {group} 的第 {index} 句回复",
            sender_is_bot=True,
        )


async def _evidence(scene: ProfileScene, fact_id: int, event_id: int, speaker: str) -> None:
    """A later evidence row through the real append path."""
    memories = scene.chat._memories
    async with memories.write_session((fact_id,)) as writer:
        await memories.append_evidence_bundle(
            fact_id,
            (
                MemoryEvidenceCreate(
                    event_id=event_id,
                    source_speaker_user_id=speaker,
                    relation=MemoryEvidenceRelation.SELF_STATEMENT,
                    authority=MemoryAuthority.SELF_REPORT,
                    excerpt="后来又说了一次",
                ),
            ),
            confirmed_at=datetime.now(UTC),
            session=writer,
        )


async def _foreign(scene: ProfileScene, fact_id: int, event_id: int) -> None:
    """A later evidence row from someone else's private chat (legacy/imported row)."""
    async with scene.database.immediate_session() as writer:
        writer.add(
            MemoryEvidenceModel(
                fact_id=fact_id,
                event_id=event_id,
                source_speaker_user_id="10001",
                relation="self_statement",
                authority="self_report",
                excerpt="私聊里提过",
                created_at=datetime.now(UTC),
            )
        )


async def _members(scene: ProfileScene) -> dict[str, list[str]]:
    labels = scene.labels()
    return {
        labels[item.space_id]: [labels[person] for person in item.person_ids]
        for item in await scene.profiles.all_members()
    }


async def _assert_render(scene: ProfileScene, key: str, turn) -> None:
    """The system message ends with exactly the renderer's text; the current message has none."""
    rendered = await scene.resident.render(scene.audience(key))
    assert turn.segment == rendered.text
    for title in (PUBLIC_TITLE, PRIVATE_TITLE, MEMBERS_TITLE):
        assert title not in turn.current


def _record(turn, contract: str) -> dict:
    return {
        "segment": turn.segment,
        "profiles": profile_blocks(turn.segment),
        "current_has_profiles": any(
            title in turn.current for title in (PUBLIC_TITLE, PRIVATE_TITLE, MEMBERS_TITLE)
        ),
        "recovery_contract": contract,
    }


def _inputs(scene: ProfileScene) -> list[dict]:
    return [profile_payload(request) for request in scene.profile_requests]


# ------------------------------------------------------------ H1 H2 H3 H4 H5 H6 H31
async def test_h01_h06_h31_inputs_rendering_and_where_profiles_show(scene):
    """Public input holds public facts only; private input adds explicit/public references;
    profiles render in the right chats only; group core rules stay strict (H4)."""
    b1 = await scene.event("private:B", "B", "我在学大提琴，也不太吃辣", key="b1")
    g1 = await scene.event("space:G", "B", "我周末常去爬山", key="g1")
    h1 = await scene.event("space:H", "B", "我是做前端开发的", key="h1")
    hc = await scene.event("space:H", "C", "我们 H 群下周线下聚餐", key="hc")
    a1 = await scene.event("private:A", "A", "B 跟我说他想换个城市", key="a1")
    group = await scene.fact("group", "B", "周末常去爬山", evidence=(g1,))
    other_group = await scene.fact("other_group", "B", "做前端开发", evidence=(h1,))
    explicit_group = await scene.fact(
        "explicit_group", "B", "请叫他北哥", evidence=(g1,), **EXPLICIT
    )
    own_private = await scene.fact("own_private", "B", "在学大提琴", evidence=(b1,))
    no_evidence = await scene.fact("no_evidence", "B", "喜欢别人叫他小北")
    foreign = await scene.fact("foreign", "B", "想换个城市", evidence=(g1,), foreign_evidence=(a1,))
    person_group = await scene.fact("person_group", "B@G", "在 G 群张罗活动", evidence=(g1,))
    explicit_private = await scene.fact(
        "explicit_private", "B", "不太吃辣", evidence=(b1,), **EXPLICIT
    )
    h_g = await scene.fact("h_g", "H", "下周线下聚餐", evidence=(hc,))
    await scene.raw_set("G", [h_g])
    seen: list[str] = []

    def respond(payload, request):
        offered = scene.offered(payload)
        seen.append(payload["audience"])
        if payload["audience"] == "public" and seen.count("public") == 1:
            # H1: the double cites the private-only fact by an alias it was never given.
            return profile_reply([("兴趣", "周末爬山，在学大提琴", [offered[group], "f_99"])])
        if payload["audience"] == "public":
            return profile_reply(
                [
                    ("兴趣", "周末常去爬山", [offered[group]]),
                    ("身份", "做前端开发", [offered[other_group]]),
                    ("相处", "喜欢被叫北哥", [offered[explicit_group]]),
                ]
            )
        return profile_reply(
            [
                ("兴趣", "私下在学大提琴", [offered[own_private]]),
                ("相处", "喜欢被叫小北", [offered[no_evidence]]),
            ]
        )

    scene.profile_responder = respond
    first = await scene.dream_run()
    after_first = await scene.profile_tables(tie_free=True)
    second = await scene.dream_run()
    group_turn = await scene.turn("A", "群里谁爱爬山", group="G")
    other_turn = await scene.turn("D", "H 群最近怎样", group="H")
    b_turn = await scene.turn("B", "在吗")
    c_turn = await scene.turn("C", "在吗")
    inputs = _inputs(scene)
    public_requests = [
        request_text(request)
        for request in scene.profile_requests
        if profile_payload(request)["audience"] == "public"
    ]
    contents = scene.contents

    assert first.status.value == second.status.value == "completed"
    public_inputs = [item for item in inputs if item["audience"] == "public"]
    private_inputs = [item for item in inputs if item["audience"] == "private"]
    assert len(public_inputs) == 2 and len(private_inputs) == 1
    assert {row["content"] for row in public_inputs[0]["facts"]} == {
        contents[group],
        contents[other_group],
        contents[explicit_group],
    }
    assert "reference" not in public_inputs[0]
    for text in public_requests:
        for fact_id in (own_private, no_evidence, explicit_private, foreign, person_group):
            assert contents[fact_id] not in text
    assert {row["content"] for row in private_inputs[0]["facts"]} == {
        contents[own_private],
        contents[no_evidence],
    }
    assert {row["content"] for row in private_inputs[0]["reference"]} == {
        contents[explicit_group],
        contents[explicit_private],
    }
    for request in scene.profile_requests:
        assert contents[foreign] not in request_text(request)
        assert contents[person_group] not in request_text(request)
    first_public = [row for row in after_first["memory_profiles"] if row["audience"] == "public"]
    assert first_public[0]["current_version"] == 0
    assert first_public[0]["considered_signature"] is None
    groups = profile_blocks(group_turn.segment)
    assert list(groups) == [MEMBERS_TITLE]
    assert groups[MEMBERS_TITLE][0].endswith(f"（person:{scene.persons['B']}）")
    assert groups[MEMBERS_TITLE][1:] == [
        f"- [兴趣] 周末常去爬山（fact:{group}）",
        f"- [身份] 做前端开发（fact:{other_group}）",
        f"- [相处] 喜欢被叫北哥（fact:{explicit_group}）",
    ]
    assert f"fact:{h_g}" not in group_turn.segment
    assert PRIVATE_TITLE not in group_turn.segment + other_turn.segment + c_turn.segment
    assert "person-B" not in other_turn.segment and "person-B" not in c_turn.segment
    blocks = profile_blocks(b_turn.segment)
    assert list(blocks) == [PUBLIC_TITLE, PRIVATE_TITLE]
    assert blocks[PRIVATE_TITLE] == [
        f"- [兴趣] 私下在学大提琴（fact:{own_private}）",
        f"- [相处] 喜欢被叫小北（fact:{no_evidence}）",
    ]
    await _assert_render(scene, "G", group_turn)
    await _assert_render(scene, "B", b_turn)
    contract = await scene.contract()
    scene.write(
        "H01-H06-H31-inputs",
        {
            "facts": scene.facts,
            "inputs": inputs,
            "group_G": _record(group_turn, contract),
            "group_H": _record(other_turn, contract),
            "private_B": _record(b_turn, contract),
            "private_C": _record(c_turn, contract),
            "tables": await scene.profile_tables(tie_free=True),
        },
    )


# ------------------------------------------------------------------ H7 H8 H9 H24
async def test_h07_h08_h09_h24_failing_ids_render_cleanup_and_lock(scene):
    """One failing ID hides its entry from the next turn; cleanup drops it and maps the
    successor as a program version, also when locked, with no model call."""
    g1 = await scene.event("space:G", "B", "我爱吃辣，周末打羽毛球，作息规律", key="g1")
    x = await scene.fact("x", "B", "爱吃辣", evidence=(g1,))
    y = await scene.fact("y", "B", "口味偏重", evidence=(g1,))
    z = await scene.fact("z", "B", "周末打羽毛球", evidence=(g1,))
    w = await scene.fact("w", "B", "作息规律", evidence=(g1,))
    store, person = scene.profiles, scene.persons["B"]
    await store.commit(
        person,
        "public",
        (
            scene.entry("口味偏辣偏重", x, y, section="兴趣"),
            scene.entry("周末打羽毛球", z),
            scene.entry("作息规律", w),
        ),
        source="owner",
        reason="首版",
        expected_version=0,
    )
    await store.replace_dream_members({scene.spaces["G"]: (person,)}, dream_run_public_id="seed")
    before = await scene.turn("A", "第一轮", group="G")
    assert await scene.chat._memories.invalidate_fact(
        x, reason=MemoryInvalidationReason.USER_RETRACTED, actor_user_id="10002"
    )
    after = await scene.turn("A", "第二轮", group="G")
    successor = await scene.chat._memories.correct_fact(
        z, content="周末常打羽毛球", actor_user_id="10002"
    )
    assert successor is not None
    mapped = await scene.turn("A", "第三轮", group="G")
    await store.set_locked(person, "public", True)
    await scene.fact("n", "B", "常带零食分给大家", evidence=(g1,))
    run = await scene.dream_run()
    history = await store.history(person, "public")
    final = await scene.turn("A", "第四轮", group="G")

    lines = profile_blocks(before.segment)[MEMBERS_TITLE]
    assert lines[1:] == [
        f"- [兴趣] 口味偏辣偏重（fact:{x},{y}）",
        f"- 周末打羽毛球（fact:{z}）",
        f"- 作息规律（fact:{w}）",
    ]
    assert profile_blocks(after.segment)[MEMBERS_TITLE] == [lines[0], lines[2], lines[3]]
    assert (
        profile_blocks(mapped.segment)[MEMBERS_TITLE][1] == f"- 周末打羽毛球（fact:{successor.id}）"
    )
    assert run.status.value == "completed"
    assert scene.profile_requests == []
    assert [item.source for item in history] == ["owner", "program"]
    assert [entry.fact_ids for entry in history[1].entries] == [(successor.id,), (w,)]
    assert f"fact {x}" in history[1].reason and f"{z}->{successor.id}" in history[1].reason
    assert (await store.load(person, "public")).locked
    assert profile_blocks(final.segment) == profile_blocks(mapped.segment)
    contract = await scene.contract()
    scene.write(
        "H07-H08-H09-H24-cleanup",
        {
            "facts": {**scene.facts, "successor": successor.id},
            "before": _record(before, contract),
            "after_invalidate": _record(after, contract),
            "after_successor": _record(mapped, contract),
            "after_cleanup": _record(final, contract),
            "tables": await scene.profile_tables(tie_free=True),
        },
    )


# --------------------------------------------------------------------- H10 H11 H12
async def test_h10_h11_h12_void_outputs_unchanged_and_reordered(scene, caplog):
    """Each invalid output voids the whole profile with its code and only the attempt
    time moves; an unchanged echo writes no version; a reorder writes one."""
    g1 = await scene.event("space:G", "B", "我周末爬山，平时写代码，喜欢猫", key="g1")
    p1 = await scene.fact("p1", "B", "周末常去爬山", evidence=(g1,))
    p2 = await scene.fact("p2", "B", "平时写代码", evidence=(g1,))
    store, person = scene.profiles, scene.persons["B"]
    cases = {
        "schema_validation:too_many": lambda o: profile_reply(
            [(None, f"条目 {index}", [o[p1]]) for index in range(11)]
        ),
        "schema_validation:too_long": lambda o: profile_reply([(None, "长" * 41, [o[p1]])]),
        "unknown_alias": lambda o: profile_reply([(None, "周末爬山", ["f_99"])]),
        "schema_validation:empty_facts": lambda o: profile_reply([(None, "周末爬山", [])]),
        "duplicate_alias": lambda o: profile_reply([(None, "周末爬山", [o[p1], o[p1]])]),
        "schema_validation:section": lambda o: profile_reply([("爱好", "周末爬山", [o[p1]])]),
        "multiline_text": lambda o: profile_reply([(None, "周末\n爬山", [o[p1]])]),
        "empty_reason": lambda o: profile_reply([(None, "周末爬山", [o[p1]])], reason=" "),
    }
    attempts = []
    caplog.set_level(logging.WARNING, logger="yuki.memory.profile_selection")
    for expected, build in cases.items():
        scene.profile_responder = lambda payload, request, build=build: build(
            scene.offered(payload)
        )
        caplog.clear()
        await scene.dream_run()
        profile = await store.load(person, "public")
        codes = [
            record.getMessage().rsplit("code=", 1)[1]
            for record in caplog.records
            if "memory_profile_rejected" in record.getMessage()
        ]
        assert codes == [expected.split(":")[0]], (expected, codes)
        assert profile.current_version == 0 and profile.considered_signature is None
        attempts.append(profile.attempted_at)
    scene.profile_responder = lambda payload, request: profile_reply(
        [
            ("兴趣", "周末常去爬山", [scene.offered(payload)[p1]]),
            ("做事方式", "平时写代码", [scene.offered(payload)[p2]]),
        ]
    )
    await scene.dream_run()
    written = await store.load(person, "public")
    await scene.fact("p3", "B", "喜欢猫", evidence=(g1,))
    scene.profile_responder = lambda payload, request: current_reply(payload)
    await scene.dream_run()
    unchanged = await store.load(person, "public")
    await scene.fact("p4", "B", "爱喝美式", evidence=(g1,))

    def reorder(payload, request):
        rows = list(reversed(payload["current"]))
        return profile_reply([(row["section"], row["text"], row["facts"]) for row in rows], "调序")

    scene.profile_responder = reorder
    await scene.dream_run()
    reordered = await store.load(person, "public")
    available = await store.available(person, "public")

    assert attempts == sorted(attempts) and len(set(attempts)) == len(attempts)
    assert written.current_version == 1
    assert unchanged.current_version == 1 and unchanged.entries == written.entries
    assert unchanged.considered_signature != written.considered_signature
    assert reordered.current_version == 2
    assert reordered.entries == tuple(reversed(written.entries))
    assert reordered.considered_signature == available.signature
    scene.write(
        "H10-H11-H12-outputs",
        {
            "facts": scene.facts,
            "codes": list(cases),
            "tables": await scene.profile_tables(tie_free=True),
        },
    )


# ------------------------------------------------------------- H13 H15 H16 H21 H26
async def test_h13_h15_h16_h21_h26_member_lists_from_dream(scene):
    """Counts per root, ties by root ID, no Yuki/disabled; quiet groups keep their list;
    pins hold; who stops speaking leaves after the next run."""
    await scene.person("E", groups=("G",))
    await scene.person("F", groups=("G",))
    await scene.dream_run()
    after_openings = await _members(scene)
    async with scene.database.immediate_session() as writer:
        await merge_persons(writer, scene.persons["B2"], scene.persons["B"])
    await scene.harness.private_users.set_enabled(scene.persons["F"], False)
    for sender, count in (("B", 3), ("B2", 2), ("A", 4), ("C", 4), ("D", 3), ("E", 1), ("F", 6)):
        await _seed(scene, "space:G", sender, count)
    await _outbound(scene, "G", 7)
    await scene.dream_run()
    counted = await _members(scene)
    await scene.profiles.pin_members(scene.spaces["G"], (scene.persons["C"],))
    await _seed(scene, "space:G", "B", 9)
    await scene.dream_run()
    pinned = await _members(scene)
    await scene.profiles.unpin_members(scene.spaces["G"])
    await _seed(scene, "space:G", "C", 2)
    await _seed(scene, "space:G", "A", 1)
    await scene.dream_run()
    recomputed = await _members(scene)

    tied = sorted(("A", "C"), key=lambda key: scene.persons[key])
    assert "person:B" in after_openings["space:G"] and after_openings["space:H"] == ["person:C"]
    assert counted == {
        "space:G": ["person:B", *(f"person:{key}" for key in tied), "person:D", "person:E"],
        "space:H": ["person:C"],
    }
    assert pinned["space:G"] == ["person:C"]
    assert recomputed == {"space:G": ["person:C", "person:A"], "space:H": ["person:C"]}
    # Person IDs are random: record the tie order check, not the labels' order.
    scene.write(
        "H13-H26-lists",
        {
            "after_openings": {key: sorted(value) for key, value in after_openings.items()},
            "counted_has_tie_by_root_id": counted["space:G"][1:3]
            == [f"person:{key}" for key in tied],
            "pinned": pinned,
            "recomputed": recomputed,
        },
    )


# ---------------------------------------------------------------------- H17 H18 H11
async def test_h17_h18_inactive_kept_private_only_maintained_in_order(scene):
    """An inactive person is not maintained and renders the kept profile on return; a
    private-only person gets both profiles, public first, private seeing the new public."""
    await scene.person("E")
    hd = await scene.event("space:H", "D", "我在学吉他", key="hd")
    d1 = await scene.event("private:D", "D", "我在准备搬家", key="d1")
    d_public = await scene.fact("d_public", "D", "在学吉他", evidence=(hd,))
    d_private = await scene.fact("d_private", "D", "在准备搬家", evidence=(d1,))
    e_private = await scene.fact("e_private", "E", "喜欢喝乌龙茶")
    order: list[tuple[str, str]] = []

    def first_run(payload, request):
        offered = scene.offered(payload)
        owner = "D" if d_public in offered or d_private in offered else "E"
        order.append((owner, payload["audience"]))
        if owner == "E":
            return profile_reply([(None, "喜欢喝乌龙茶", [offered[e_private]])])
        if payload["audience"] == "public":
            return profile_reply([("兴趣", "在学吉他", [offered[d_public]])])
        # H11: a reference alias cited as a fact voids the private profile.
        return profile_reply([(None, "在准备搬家", [offered[d_private], "ref_1"])])

    scene.profile_responder = first_run
    await scene.dream_run()
    first_inputs = _inputs(scene)
    await scene.fact("e_more", "E", "周末去茶馆")
    await scene.event("private:D", "D", "搬家的事定了", key="d2")
    scene.profile_requests.clear()

    def second_run(payload, request):
        offered = scene.offered(payload)
        assert e_private not in offered, "E is not maintained"
        return profile_reply([(None, "在准备搬家", [offered[d_private]])])

    scene.profile_responder = second_run
    await scene.dream_run()
    second_inputs = _inputs(scene)
    e_turn = await scene.turn("E", "好久不见")
    d_turn = await scene.turn("D", "在吗")
    e_profile = await scene.profiles.load(scene.persons["E"], "private")

    d_order = [item for item in order if item[0] == "D"]
    assert d_order == [("D", "public"), ("D", "private")]
    d_private_input = next(
        item
        for item in first_inputs
        if item["audience"] == "private"
        and "reference" in item
        and any(row["content"] == scene.contents[d_private] for row in item["facts"])
    )
    assert "在学吉他" in [row["content"] for row in d_private_input["reference"]]
    assert [item["audience"] for item in second_inputs] == ["private"]
    assert e_profile.current_version == 1
    assert profile_blocks(e_turn.segment) == {
        PRIVATE_TITLE: [f"- 喜欢喝乌龙茶（fact:{e_private}）"]
    }
    assert profile_blocks(d_turn.segment) == {
        PUBLIC_TITLE: [f"- [兴趣] 在学吉他（fact:{d_public}）"],
        PRIVATE_TITLE: [f"- 在准备搬家（fact:{d_private}）"],
    }
    contract = await scene.contract()
    scene.write(
        "H17-H18-maintenance",
        {
            "facts": scene.facts,
            # D and E are queued by random root IDs: record inputs in a fixed order.
            "first_inputs": sorted(
                first_inputs, key=lambda item: json.dumps(item, ensure_ascii=False)
            ),
            "second_inputs": second_inputs,
            "private_E": _record(e_turn, contract),
            "private_D": _record(d_turn, contract),
            "tables": await scene.profile_tables(tie_free=True),
        },
    )


# -------------------------------------------------------------------------- H19 H20
async def test_h19_h20_merge_renders_by_root_and_forget_removes(scene):
    """B' merged into B: one name line with both IDs, the old account reads B's profiles,
    B becomes due; forgetting B removes the group lines."""
    g1 = await scene.event("space:G", "B", "我是程序员", key="g1")
    b2 = await scene.event("private:B2", "B2", "我养了一只柴犬", key="b2")
    b_public = await scene.fact("b_public", "B", "是程序员", evidence=(g1,))
    b2_private = await scene.fact("b2_private", "B2", "养了一只柴犬", evidence=(b2,))
    store, persons = scene.profiles, scene.persons
    available = await store.available(persons["B"], "public")
    await store.commit(
        persons["B"],
        "public",
        (scene.entry("是程序员", b_public, section="身份"),),
        source="owner",
        reason="首版",
        expected_version=0,
        signature=available.signature,
    )
    await store.commit(
        persons["B2"],
        "private",
        (scene.entry("养了一只柴犬", b2_private),),
        source="owner",
        reason="合并前",
        expected_version=0,
    )
    await store.replace_dream_members(
        {scene.spaces["G"]: (persons["B"], persons["B2"])}, dream_run_public_id="seed"
    )
    async with scene.database.immediate_session() as writer:
        await merge_persons(writer, persons["B2"], persons["B"])
    due_before = await scene.dream().selection.pending()
    group_turn = await scene.turn("A", "合并后", group="G")
    old_account = await scene.turn("B2", "合并后 B′ 的账号", person="B")
    tables = await scene.profile_tables(tie_free=True)
    forgot = await scene.command("B", "/ai forgetme")
    forgotten = await scene.turn("A", "遗忘后", group="G")

    lines = profile_blocks(group_turn.segment)[MEMBERS_TITLE]
    assert len(lines) == 2
    assert lines[0].endswith(f"（person:{persons['B']}；同一人另有 person:{persons['B2']}）")
    assert lines[1] == f"- [身份] 是程序员（fact:{b_public}）"
    # B's profiles from the old account; B2's own pre-merge row is kept and not rendered.
    assert profile_blocks(old_account.segment) == {
        PUBLIC_TITLE: [f"- [身份] 是程序员（fact:{b_public}）"]
    }
    assert {row["person_id"] for row in tables["memory_profiles"]} == {persons["B"], persons["B2"]}
    assert (persons["B"], "private") in {
        (item.person_id, item.audience) for item in due_before.profiles
    }
    assert forgot and "删除" in forgot[0]
    assert MEMBERS_TITLE not in forgotten.segment
    contract = await scene.contract()
    scene.write(
        "H19-H20-merge-forget",
        {
            "group_after_merge": _record(group_turn, contract),
            "old_account_private": _record(old_account, contract),
            "group_after_forget": _record(forgotten, contract),
            "tables_after_merge": tables,
        },
    )


async def test_h19_cleanup_leaves_the_root_and_the_merged_row_alone(scene):
    """B' (with its own pre-merge public v1) merged into B (public v1): Dream cleanup
    must not write B' 's emptied entries into B's profile, nor touch B' 's kept row."""
    g1 = await scene.event("space:G", "B", "我是程序员", key="g1")
    g2 = await scene.event("space:G", "B2", "我爱下围棋", key="g2")
    b_public = await scene.fact("b_public", "B", "是程序员", evidence=(g1,))
    b2_public = await scene.fact("b2_public", "B2", "爱下围棋", evidence=(g2,))
    store, persons = scene.profiles, scene.persons
    for key, fact_id, text in (("B", b_public, "是程序员"), ("B2", b2_public, "爱下围棋")):
        await store.commit(
            persons[key],
            "public",
            (scene.entry(text, fact_id),),
            source="owner",
            reason="合并前",
            expected_version=0,
            lock=True,
        )
    async with scene.database.immediate_session() as writer:
        await merge_persons(writer, persons["B2"], persons["B"])
    await scene.dream_run()
    tables = await scene.profile_tables(tie_free=True)
    turn = await scene.turn("B", "合并后的 Dream 之后")

    versions = {
        (row["person_id"], row["version"]): row for row in tables["memory_profile_versions"]
    }
    assert sorted(versions) == sorted([(persons["B"], 1), (persons["B2"], 1)])
    assert profile_blocks(turn.segment) == {PUBLIC_TITLE: [f"- 是程序员（fact:{b_public}）"]}
    scene.write(
        "H19-merged-cleanup",
        {"tables": tables, "private_after_dream": _record(turn, await scene.contract())},
    )


# ---------------------------------------------------------------------------- H14
async def test_h14_system_prefix_changes_once_a_day(scene):
    """New speakers, rank changes and new facts change nothing until Dream; a fact
    becoming unavailable changes the next activation once."""
    await scene.person("E", groups=("G",))
    g1 = await scene.event("space:G", "B", "我周末常去爬山", key="g1")
    hike = await scene.fact("hike", "B", "周末常去爬山", evidence=(g1,))
    scene.profile_responder = lambda payload, request: profile_reply(
        [("兴趣", "周末常去爬山", [scene.offered(payload)[hike]])]
    )
    await scene.dream_run()
    first = await scene.turn("A", "第一轮", group="G")
    await _seed(scene, "space:G", "E", 6)
    await _seed(scene, "space:G", "B", 1)
    await scene.fact("cats", "B", "养了两只猫", evidence=(g1,))
    second = await scene.turn("A", "第二轮", group="G")
    assert await scene.chat._memories.invalidate_fact(
        hike, reason=MemoryInvalidationReason.USER_RETRACTED, actor_user_id="10002"
    )
    third = await scene.turn("A", "第三轮", group="G")
    fourth = await scene.turn("A", "第四轮", group="G")
    lists_before = await _members(scene)
    scene.profile_responder = lambda payload, request: profile_reply(
        [("兴趣", "养了两只猫", [scene.offered(payload)[scene.facts["cats"]]])]
    )
    await scene.dream_run()
    fifth = await scene.turn("A", "第五轮", group="G")

    assert system_text(first.requests[0]) == system_text(second.requests[0])
    assert first.segment != third.segment and MEMBERS_TITLE not in third.segment
    assert system_text(third.requests[0]) == system_text(fourth.requests[0])
    assert "person:E" not in str(lists_before)
    assert fifth.segment != fourth.segment
    assert "养了两只猫" in fifth.segment
    contract = await scene.contract()
    scene.write(
        "H14-stability",
        {
            "turns": [_record(turn, contract) for turn in (first, second, third, fourth, fifth)],
            "lists_before_second_dream": lists_before,
            "lists_after": await _members(scene),
        },
    )


# ---------------------------------------------------------------------------- H27
async def test_h27_cap_and_voiding_profile_moves_to_the_tail(database, tmp_path):
    """Cap 2 over three due profiles; the always-voiding one goes behind never-attempted ones."""
    scene = await ProfileScene(database, tmp_path).build(memory_resident_max_selections_per_run=2)
    g1 = await scene.event("space:G", "B", "我爱下围棋", key="g1")
    h1 = await scene.event("space:H", "C", "我常做饭", key="h1")
    h2 = await scene.event("space:H", "D", "我在学吉他", key="h2")
    facts = {
        "B": await scene.fact("b_go", "B", "爱下围棋", evidence=(g1,)),
        "C": await scene.fact("c_cook", "C", "常做饭", evidence=(h1,)),
        "D": await scene.fact("d_guitar", "D", "在学吉他", evidence=(h2,)),
    }
    order = sorted(facts, key=lambda key: scene.persons[key])
    voiding = order[0]
    attempted: list[list[str]] = []

    def respond(payload, request):
        offered = scene.offered(payload)
        owner = next(key for key, fact_id in facts.items() if fact_id in offered)
        attempted[-1].append(owner)
        if owner == voiding:
            return profile_reply([(None, "随便写", ["f_99"])])
        return profile_reply([(None, scene.contents[facts[owner]], [offered[facts[owner]]])])

    scene.profile_responder = respond
    health = []
    for _ in range(3):
        attempted.append([])
        await scene.dream_run()
        health.append(await scene.dream().worker.health())

    assert attempted == [order[:2], [order[2], voiding], [voiding]]
    assert [item.resident_pending_owners for item in health] == [2, 1, 1]
    scene.write(
        "H27-cap",
        {
            "attempted_in_root_id_order": attempted == [order[:2], [order[2], voiding], [voiding]],
            "pending_after_each_run": [item.resident_pending_owners for item in health],
        },
    )


# ---------------------------------------------------------------------------- H29
async def test_h29_profiles_stay_out_of_contract_compact_and_current_message(scene):
    """The recovery contract ignores profile changes; compaction never carries them."""
    from yuki.conversation.projection_models import PromptProjectionModel

    b1 = await scene.event("private:B", "B", "我在学大提琴", key="b1")
    cello = await scene.fact("cello", "B", "在学大提琴", evidence=(b1,))
    before = await scene.contract()
    await scene.profiles.commit(
        scene.persons["B"],
        "private",
        (scene.entry("私下在学大提琴", cello),),
        source="owner",
        reason="首版",
        expected_version=0,
    )
    after = await scene.contract()
    for index in range(6):
        await scene.event("private:B", "B", f"较早的聊天 {index}：" + "讨论练琴计划。" * 40)
    compact_requests = []

    def respond(request):
        if "Host 会话上下文压缩要求" in str(request.messages[-1].content or ""):
            compact_requests.append(request)
            return "PUBLIC_CHECKPOINT：讨论过练琴计划。"
        return "NO_REPLY"

    scene.responder = respond
    turn = await scene.turn("B", "压缩前")
    replies = await scene.command("B", "/ai compact")
    compacted_turn = await scene.turn("B", "压缩后")
    async with scene.database.read_session() as reader:
        checkpoints = [
            row.selected_summary_text or ""
            for row in (await reader.scalars(select(PromptProjectionModel))).all()
        ]

    assert before == after
    assert compact_requests, replies
    assert PRIVATE_TITLE in turn.segment and PRIVATE_TITLE not in turn.current
    for request in compact_requests:
        compacted = "\n".join(str(m.content or "") for m in request.messages if m.role != "system")
        assert PRIVATE_TITLE not in compacted and "私下在学大提琴" not in compacted
    assert all(PRIVATE_TITLE not in text and "（fact:" not in text for text in checkpoints)
    assert compacted_turn.segment == turn.segment
    scene.write(
        "H29-contract-compact",
        {
            "recovery_contract_sha256": [_sha(before), _sha(after)],
            "turn": _record(turn, after),
            "after_compact": _record(compacted_turn, after),
        },
    )


# ---------------------------------------------------------------------- H32 H34
async def test_h32_h34_eligibility_changes_follow_render_cleanup_and_input(scene):
    """A fact losing eligibility leaves the next activation and the next cleanup; a fact
    gaining public eligibility joins the next public input."""
    b1 = await scene.event("private:B", "B", "我在学大提琴", key="b1")
    g1 = await scene.event("space:G", "B", "我周末打羽毛球", key="g1")
    a1 = await scene.event("private:A", "A", "B 说他周末打羽毛球", key="a1")
    z = await scene.fact("z", "B", "周末打羽毛球", evidence=(g1,))
    w = await scene.fact("w", "B", "在学大提琴", evidence=(b1,))

    def respond(payload, request):
        offered = scene.offered(payload)
        return profile_reply(
            [(None, scene.contents[fact_id], [alias]) for fact_id, alias in offered.items()]
        )

    scene.profile_responder = respond
    await scene.dream_run()
    before_group = await scene.turn("A", "群里", group="G")
    await _foreign(scene, z, a1)
    g2 = await scene.event("space:G", "B", "其实我在学大提琴", key="g2")
    await _evidence(scene, w, g2, "10002")
    after_group = await scene.turn("A", "群里再问", group="G")
    after_private = await scene.turn("B", "在吗")
    scene.profile_requests.clear()
    await scene.dream_run()
    second_inputs = _inputs(scene)
    store, person = scene.profiles, scene.persons["B"]
    public = await store.history(person, "public")
    private = await store.history(person, "private")

    assert f"（fact:{z}）" in before_group.segment
    assert MEMBERS_TITLE not in after_group.segment
    assert PRIVATE_TITLE not in after_private.segment
    assert [item.source for item in public] == ["dream", "program", "dream"]
    assert [item.source for item in private] == ["dream", "program"]
    assert private[-1].entries == ()
    assert [item["audience"] for item in second_inputs] == ["public"]
    assert [row["content"] for row in second_inputs[0]["facts"]] == [scene.contents[w]]
    contract = await scene.contract()
    scene.write(
        "H32-H34-eligibility",
        {
            "facts": scene.facts,
            "before_group": _record(before_group, contract),
            "after_group": _record(after_group, contract),
            "after_private": _record(after_private, contract),
            "second_inputs": second_inputs,
            "tables": await scene.profile_tables(tie_free=True),
        },
    )


# ---------------------------------------------------------------------- H33 H35
async def test_h33_h35_owner_change_during_call_and_lock(scene, caplog):
    """An owner write during the model call wins; a locked profile gets no call until
    unlocked."""
    g1 = await scene.event("space:G", "B", "我周末常去爬山，也爱拍照", key="g1")
    hike = await scene.fact("hike", "B", "周末常去爬山", evidence=(g1,))
    store, person = scene.profiles, scene.persons["B"]
    scene.profile_responder = lambda payload, request: profile_reply(
        [(None, "周末常去爬山", [scene.offered(payload)[hike]])]
    )
    await scene.dream_run()
    photo = await scene.fact("photo", "B", "爱拍风景照", evidence=(g1,))
    scene.profile_responder = lambda payload, request: profile_reply(
        [(None, "爱拍风景照", [scene.offered(payload)[photo]])]
    )
    pause = len(scene.provider.ownership) + 1
    scene.provider.arrived[pause] = asyncio.Event()
    scene.provider.release[pause] = asyncio.Event()
    caplog.set_level(logging.INFO, logger="yuki.memory.profile_selection")
    running = asyncio.create_task(scene.dream_run())
    await scene.provider.wait(pause)
    owner = await store.commit(
        person,
        "public",
        (scene.entry("周末爬山，也拍照", hike, photo),),
        source="owner",
        reason="owner rollback to v1",
        expected_version=1,
        lock=True,
    )
    scene.provider.release[pause].set()
    await asyncio.wait_for(running, 30)
    after_race = await store.load(person, "public")
    calls_before = len(scene.profile_requests)
    await scene.fact("tea", "B", "爱喝乌龙茶", evidence=(g1,))
    await scene.dream_run()
    locked_calls = len(scene.profile_requests) - calls_before
    await store.set_locked(person, "public", False)
    scene.profile_responder = lambda payload, request: current_reply(payload, "不变")
    await scene.dream_run()
    unlocked = await store.load(person, "public")

    assert owner.profile.current_version == 2
    assert after_race.current_version == 2 and after_race.locked
    assert [entry.text for entry in after_race.entries] == ["周末爬山，也拍照"]
    assert any(
        "memory_profile_abandoned" in record.getMessage()
        and "code=version_changed" in record.getMessage()
        for record in caplog.records
    )
    assert locked_calls == 0
    assert unlocked.current_version == 2 and not unlocked.locked
    assert unlocked.considered_signature == (await store.available(person, "public")).signature
    scene.write(
        "H33-H35-owner",
        {
            "facts": scene.facts,
            "locked_calls": locked_calls,
            "tables": await scene.profile_tables(tie_free=True),
        },
    )
