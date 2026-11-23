"""E2E: the episode index and today's new episodes in every Main turn (memory layering P2).

Authoritative spec: docs/architecture/Yuki-记忆分层P2经历目录任务书-2026-11-22.md §10
(cases 1-24 = G1-G24) with every §9 item at its recommendation, as amended by the
P2.1 task book (docs/architecture/Yuki-记忆分层P2.1经历目录精简任务书-2026-11-23.md):
the index is the audience's stored row written by the nightly Dream step (recent 15,
long-term impressions, month counts; its own ``through`` replaces the global
watermark, which stays only for audiences without a row), so G11, G14, G20 and G22
follow P2.1; the P2.1 cases K1-K33 live in test_memory_episode_memorable_e2e.py.
Titles live in one
``memory_facts.title`` column written by the call that writes the episode (Q9a/Q9b);
the watermark is the newest finished Dream run's ``snapshot_max_fact_id`` (P2-Q1);
groups never list person-scope episodes (P2-Q2) nor episodes with tool-receipt
evidence (P2-Q3); 20 recent lines (P2-Q4); the grounding rule
mentions the index (P2-Q5); ``search_memory`` semantics stay (P2-Q6); writers store
titles (P2-Q7). The index and the recent block render on every Main turn with no
runtime switch (owner decision 2026-11-22), so G24 keeps only its writer half.

Every case runs real SQLite and real write paths (``MemoryFactService``, the real
self-reflection over claimed chat batches, the batch extraction worker, Dream, Main
``save_memory``), real Dream runs for the watermark, and real ``MessageProcessor`` ->
``ContextAssembler`` -> ``MainAgentTurns.compose`` -> request assembly; only model
replies are scripted. Each case writes normalized JSON (index text from the end of the
system message, recent text from the current message, both fact ID lists, the
watermark and the ``recovery_contract`` hash; person/space/conversation IDs replaced
by labels, timestamps stripped, today's date as ``<today>``) to
``test-results/memory-episode-index/<G-case>.json``; identical input gives
byte-identical artifacts.

Failure cases, written before the implementation. The index or the recent block:
  G1  lists a SELF episode from a private chat in a group;
  G2  lists a group episode whose evidence mixes this group and a private chat;
  G3  lists another group's episodes in this group;
  G4  lists in a group an episode with a tool-receipt evidence row;
  G5  lists in the owner's private chat an episode evidenced in a third person's chat;
  G6  lists a third-party episode anywhere;
  G7  changes the index bytes within a day when new episodes are written;
  G8  depends on insertion order or process; orders one day by anything but fact ID;
  G9  lists invalidated, superseded, contested or quarantined episodes, or reorders;
  G10 keeps a forgotten person's title or text;
  G11 duplicates a merged person's episodes or renders them by the old owner;
  G12 lists a disabled person's episodes;
  G13 lists an ID that ``search_memory(fact_id=ID)`` cannot read in that chat;
  G14 exceeds 20 recent lines or miscounts the overflow;
  G15 makes the turn fail (or drops the resident segment) when the renderer fails;
  G16 moves the recovery contract hash;
  G17 changes the system prefix inside one Work activation;
  G18 leaks into a compact checkpoint;
  G19 reaches subagent or plugin turns, or lands in the wrong conversation's turns;
  G20 is wrong without a row, or moves while a Dream run is still running;
  G21 breaks the layout for a missing title or a multi-line title; backfill writes on
      dry runs, touches titled rows, ``updated_at`` or the Dream signature;
  G22 shows one episode in both the index and the recent block;
  G23 leaves Dream outputs without titles;
  G24 stops writers (self-reflection, extraction) storing titles.

Failure baselines for the leakage cases (§10, P1 task book §15 #20). These pass on the
old implementation that renders nothing at all, so their red baseline is the renderer
with one filter removed; each removal below turns the named case red:
  G1  drop the group evidence condition: the G-visible SELF episode ``x_g`` whose only
      evidence is B's private chat appears in G; drop the SELF visibility condition: the
      B-private SELF episode ``x_in_g`` evidenced in G appears in G.
  G2  weaken "every evidence row is a direct event in G" to "at least one": ``y`` shows.
  G3  drop the owner condition (group episodes of any space): ``h_in_g`` (H's episode
      evidenced in G) shows in G; drop the SELF visibility condition: ``s_in_g`` shows;
      compare evidence by kind instead of space: ``h_g`` (evidence only in H) shows.
  G4  accept tool receipts whose trigger event is in G: ``t`` shows before conversion.
  G5  drop "no evidence from someone else's private chat": ``z`` shows in A's chat.
  G6  drop the authority condition: ``t_pg`` shows in G and in B's private chat.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, update
from tests.support.memory_episode_scene import (
    INDEX_HEADER,
    RECENT_HEADER,
    TIMEZONE,
    UNTITLED,
    EpisodeScene,
    capture_episodes,
    fact_marks,
    install_memory_segments,
    item_lines,
    local_day,
    render_record,
    turn_record,
    write_episode_artifact,
)
from tests.support.memory_resident_scene import (
    request_text,
    run_cli,
    system_text,
    tool_call,
    tool_result,
)

from yuki.domain.messages import ChatResponse
from yuki.memory.enums import (
    MemoryAuthority,
    MemoryEvidenceRelation,
    MemoryInvalidationReason,
    MemorySourceType,
)
from yuki.persistence.models import (
    ChatEventModel,
    MemoryEvidenceModel,
    MemoryToolReceiptModel,
)

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "verify_episode_check.py"
EXPLICIT = {"source_type": MemorySourceType.EXPLICIT, "authority": MemoryAuthority.EXPLICIT}
RECENT_LINES = 20


@pytest.fixture
async def scene(database, tmp_path):
    return await EpisodeScene(database, tmp_path).build()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _at(day: str, hour: int = 12) -> datetime:
    """A local fixture time (``YYYY-MM-DD``) in the deployment timezone."""
    year, month, date = (int(part) for part in day.split("-"))
    return datetime(year, month, date, hour, tzinfo=ZoneInfo(TIMEZONE))


async def _assert_matches_render(scene: EpisodeScene, key: str, turn) -> None:
    """The system message ends with the index; the current message carries the recent block."""
    rendered = await scene.render(key)
    assert turn.index == rendered.index_text
    assert turn.recent == rendered.recent_text
    assert turn.index_ids == list(rendered.index_fact_ids)
    assert turn.recent_ids == list(rendered.recent_fact_ids)
    if rendered.index_text:
        assert turn.system.count(rendered.index_text) == 1
        assert turn.system.rstrip().endswith(rendered.index_text)
    else:
        assert INDEX_HEADER not in turn.system
    if rendered.recent_text:
        assert turn.current.count(rendered.recent_text) == 1
    else:
        assert RECENT_HEADER not in turn.current


def _line(fact_id: int, title: str, day: str | None = None) -> str:
    return f"- {day or local_day()} {title}（fact:{fact_id}）"


async def _receipt_evidence(scene: EpisodeScene, fact_id: int, trigger_event_id: int) -> int:
    """A tool-receipt evidence row whose trigger event is ``trigger_event_id`` (direct write)."""
    now = datetime.now(UTC)
    async with scene.database.immediate_session() as writer:
        receipt = MemoryToolReceiptModel(
            conversation_key_hash="e" * 64,
            trigger_event_id=trigger_event_id,
            bot_user_id="80001",
            canonical_space_id=scene.spaces["G"],
            provider_id="episode-e2e",
            tool_name="read_chat_history",
            success=True,
            result_excerpt="群里看过的一段聊天记录",
            result_characters=12,
            created_at=now,
            expires_at=now + timedelta(days=30),
        )
        writer.add(receipt)
        await writer.flush()
        evidence = MemoryEvidenceModel(
            fact_id=fact_id,
            tool_receipt_id=receipt.id,
            source_speaker_user_id="80001",
            relation=MemoryEvidenceRelation.AGENT_REFLECTION.value,
            authority=MemoryAuthority.AGENT_REFLECTION.value,
            confidence=0.9,
            excerpt="群里看过的一段聊天记录",
            created_at=now,
        )
        writer.add(evidence)
        await writer.flush()
        return evidence.id


async def _verify_check(scene: EpisodeScene, tmp_path) -> dict:
    """``episodes check`` plus the independent acceptance script (task book §11.2)."""
    import asyncio

    check = await run_cli(scene.database.url, "memory", "episodes", "check")
    assert check.returncode == 0, check
    report = tmp_path / "episode-check.json"
    report.write_text(check.stdout, encoding="utf-8")
    async with scene.database.engine.connect() as connection:
        await connection.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
    database_file = scene.database.url.removeprefix("sqlite+aiosqlite:///")
    script = SCRIPT
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script),
        "--database",
        database_file,
        "--check",
        str(report),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    assert process.returncode == 0, (stdout, stderr)
    verified = json.loads(stdout)
    assert verified["violations"] == 0, verified
    return {"checked_entries": verified["checked_entries"], "violations": verified["violations"]}


# --------------------------------------------------------------------------- G1
async def test_g01_private_self_episode_never_reaches_group(scene, tmp_path):
    """Case 1: B's private SELF episode stays out of G; B's own private chat lists it."""
    b1 = await scene.event("private:B", "B", "我其实很怕狗，这件事别在群里说", key="b1")
    await scene.event("space:G", "B", "今晚群里一起看了一部老电影", key="g1")
    created = await scene.reflect(
        {
            "private:B": [
                {"title": "B 私下说怕狗", "content": "B 私下告诉我他怕狗。", "cites": ["怕狗"]}
            ],
            "space:G": [
                {
                    "title": "群里看老电影",
                    "content": "我和群友一起看了老电影。",
                    "cites": ["老电影"],
                }
            ],
        }
    )
    (x,), (g_self,) = created["private:B"], created["space:G"]
    g1 = scene.events["g1"]
    x_in_g = await scene.episode(
        "x_in_g",
        "self",
        "替 B 保守一个秘密",
        title="替 B 保密",
        visibility="private:B",
        foreign_evidence=(g1,),
    )
    x_g = await scene.episode(
        "x_g",
        "self",
        "听说群里有人怕狗",
        title="群里有人怕狗",
        visibility="space:G",
        foreign_evidence=(b1,),
    )
    watermark = await scene.advance()

    group = await scene.episode_turn("A", "大家晚上好", group="G")
    private = await scene.episode_turn("B", "在吗")

    assert group.index_ids == [g_self] and group.recent_ids == []
    assert x not in group.index_ids + group.recent_ids
    assert x_g not in group.index_ids + group.recent_ids
    assert x_in_g not in group.index_ids + group.recent_ids
    assert _line(g_self, "群里看老电影") in group.index.split("\n")
    # Group evidence is fine in B's own private chat.
    assert private.index_ids == [x, x_in_g]
    assert _line(x, "B 私下说怕狗") in private.index.split("\n")
    await _assert_matches_render(scene, "space:G", group)
    await _assert_matches_render(scene, "private:B", private)
    verified = await _verify_check(scene, tmp_path)
    contract = await scene.contract()
    scene.write(
        "G01",
        {
            "facts": {"x": x, "g_self": g_self, "x_g": x_g, "x_in_g": x_in_g},
            "group_G": turn_record(group, contract, watermark),
            "private_B": turn_record(private, contract, watermark),
            "independent_check": verified,
        },
    )


# --------------------------------------------------------------------------- G2
async def test_g02_group_needs_every_evidence_in_this_group(scene):
    """Case 2: one G event plus one B-private event is not enough for G's index."""
    g1 = await scene.event("space:G", "B", "周末大家一起去爬了山", key="g1")
    b1 = await scene.event("private:B", "B", "群里那次爬山其实是我张罗的", key="b1")
    y = await scene.episode(
        "y", "G", "周末群友一起爬山", title="群里爬山", evidence=(g1,), foreign_evidence=(b1,)
    )
    g_ok = await scene.episode("g_ok", "G", "周末群里组织了爬山", title="周末爬山", evidence=(g1,))
    watermark = await scene.advance()

    group = await scene.episode_turn("B", "这周还爬山吗", group="G")

    assert group.index_ids == [g_ok]
    assert y not in group.index_ids + group.recent_ids
    await _assert_matches_render(scene, "space:G", group)
    scene.write(
        "G02",
        {"facts": scene.facts, "group_G": turn_record(group, await scene.contract(), watermark)},
    )


# --------------------------------------------------------------------------- G3
async def test_g03_other_group_episodes_never_reach_this_group(scene):
    """Case 3: H's group and SELF episodes, and G episodes evidenced in H, stay out of G."""
    h1 = await scene.event("space:H", "C", "H 群下周线下聚餐定在周六", key="h1")
    g1 = await scene.event("space:G", "A", "G 群这周先发活动预告", key="g1")
    h = await scene.episode("h", "H", "H 群定了线下聚餐", title="H 群聚餐", evidence=(h1,))
    s = await scene.episode(
        "s",
        "self",
        "我在 H 群帮忙记聚餐人数",
        title="帮 H 群记人数",
        visibility="space:H",
        evidence=(h1,),
    )
    h_g = await scene.episode(
        "h_g", "G", "听说有线下聚餐", title="听说聚餐", foreign_evidence=(h1,)
    )
    # Evidenced in G: only the owner / SELF visibility conditions keep these out of G.
    h_in_g = await scene.episode(
        "h_in_g", "H", "H 群的人来 G 群打招呼", title="H 群来打招呼", foreign_evidence=(g1,)
    )
    s_in_g = await scene.episode(
        "s_in_g",
        "self",
        "我替 H 群在 G 群发了通知",
        title="替 H 群发通知",
        visibility="space:H",
        foreign_evidence=(g1,),
    )
    g_ok = await scene.episode("g_ok", "G", "G 群发了活动预告", title="G 群预告", evidence=(g1,))
    watermark = await scene.advance()

    group = await scene.episode_turn("B", "最近有活动吗", group="G")
    other = await scene.render("space:H")

    assert group.index_ids == [g_ok]
    assert not {h, s, h_g, h_in_g, s_in_g} & set(group.index_ids + group.recent_ids)
    assert other.index_fact_ids == (h, s)
    await _assert_matches_render(scene, "space:G", group)
    scene.write(
        "G03",
        {
            "facts": scene.facts,
            "group_G": turn_record(group, await scene.contract(), watermark),
            "group_H": render_record(other),
        },
    )


# --------------------------------------------------------------------------- G4
async def test_g04_tool_receipt_evidence_keeps_episode_out_of_group(scene):
    """Case 4: a tool-receipt evidence row (trigger event in G) excludes; direct evidence admits."""
    g1 = await scene.event("space:G", "B", "大家一起回顾了上周的讨论", key="g1")
    g2 = await scene.event("space:G", "A", "帮我翻一下上周的聊天记录", key="g2")
    t = await scene.episode(
        "t",
        "self",
        "我翻了上周的聊天记录帮大家回顾",
        title="帮群里回顾讨论",
        visibility="space:G",
        evidence=(g1,),
    )
    evidence_id = await _receipt_evidence(scene, t, g2)

    # No row yet: the tail lists visible episodes; the receipt keeps t out.
    before = await scene.episode_turn("B", "上周聊了什么", group="G")
    async with scene.database.immediate_session() as writer:
        await writer.execute(
            update(MemoryEvidenceModel)
            .where(MemoryEvidenceModel.id == evidence_id)
            .values(event_id=g2, tool_receipt_id=None)
        )
    watermark = await scene.advance()
    after = await scene.episode_turn("B", "再说一次", group="G")

    assert t not in before.index_ids + before.recent_ids
    assert after.index_ids == [t]
    await _assert_matches_render(scene, "space:G", after)
    contract = await scene.contract()
    scene.write(
        "G04",
        {
            "facts": scene.facts,
            "with_tool_receipt": turn_record(before, contract, watermark),
            "all_direct": turn_record(after, contract, watermark),
        },
    )


# --------------------------------------------------------------------------- G5
async def test_g05_third_person_private_evidence_stays_out_of_owner_chat(scene):
    """Case 5: A's episode evidenced in B's private chat is not listed in A's private chat."""
    b1 = await scene.event("private:B", "B", "A 跟我说过他上周去面试了", key="b1")
    a1 = await scene.event("private:A", "A", "我昨天去看了一场话剧", key="a1")
    z = await scene.episode("z", "A", "上周去面试", title="A 去面试", foreign_evidence=(b1,))
    a_ok = await scene.episode("a_ok", "A", "去看了一场话剧", title="看话剧", evidence=(a1,))
    watermark = await scene.advance()

    private = await scene.episode_turn("A", "早")

    assert private.index_ids == [a_ok]
    assert z not in private.index_ids + private.recent_ids
    await _assert_matches_render(scene, "private:A", private)
    scene.write(
        "G05",
        {
            "facts": scene.facts,
            "private_A": turn_record(private, await scene.contract(), watermark),
        },
    )


# --------------------------------------------------------------------------- G6
async def test_g06_third_party_episodes_never_listed(scene):
    """Case 6: B's third-party person_group episode is in no conversation."""
    g1 = await scene.event("space:G", "A", "B 上周在群里组织了桌游", key="g1")
    b1 = await scene.event("private:B", "B", "我上周末去钓鱼了", key="b1")
    t_pg = await scene.episode(
        "t_pg",
        "B@G",
        "被说组织了桌游",
        title="B 组织桌游",
        evidence=(g1,),
        authority=MemoryAuthority.THIRD_PARTY,
    )
    b_ok = await scene.episode("b_ok", "B", "上周末去钓鱼", title="B 钓鱼", evidence=(b1,))
    watermark = await scene.advance()

    group = await scene.episode_turn("A", "桌游好玩吗", group="G")
    rendered = {key: await scene.render(key) for key in ("private:A", "private:B", "space:H")}

    assert t_pg not in group.index_ids + group.recent_ids
    for value in rendered.values():
        assert t_pg not in value.index_fact_ids + value.recent_fact_ids
    assert rendered["private:B"].index_fact_ids == (b_ok,)
    scene.write(
        "G06",
        {
            "facts": scene.facts,
            "group_G": turn_record(group, await scene.contract(), watermark),
            **{key: render_record(value) for key, value in rendered.items()},
        },
    )


# --------------------------------------------------------------------------- G7
async def test_g07_new_episodes_change_only_the_recent_block(scene):
    """Case 7: index bytes stay for the day; reflection's new episodes go to the recent block."""
    a1 = await scene.event("private:A", "A", "上周我们讨论了新家的装修", key="a1")
    e0 = await scene.episode("e0", "A", "和 A 讨论新家装修", title="讨论装修", evidence=(a1,))
    watermark = await scene.advance()
    before = await scene.episode_turn("A", "早上好")
    await scene.event("private:A", "A", "今天下午去爬了香山", key="a2")
    await scene.event("private:A", "A", "晚上在家做了红烧肉", key="a3")
    created = await scene.reflect(
        {
            "private:A": [
                {"title": "A 去爬香山", "content": "A 说下午爬了香山。", "cites": ["香山"]},
                {"title": "A 做红烧肉", "content": "A 说晚上做了红烧肉。", "cites": ["红烧肉"]},
            ]
        }
    )
    new = created["private:A"]
    first = await scene.episode_turn("A", "第一轮")
    second = await scene.episode_turn("A", "第二轮")

    assert before.index_ids == [e0] and before.recent_ids == []
    assert first.index == second.index == before.index
    assert first.recent == second.recent
    assert first.recent_ids == new
    assert first.recent.split("\n")[0] == RECENT_HEADER
    assert _line(new[0], "A 去爬香山") in first.recent.split("\n")
    await _assert_matches_render(scene, "private:A", second)
    contract = await scene.contract()
    scene.write(
        "G07",
        {
            "facts": {"e0": e0, "new": new},
            "before": turn_record(before, contract, watermark),
            "first": turn_record(first, contract, watermark),
            "second": turn_record(second, contract, watermark),
        },
    )


# --------------------------------------------------------------------------- G8
async def test_g08_insertion_order_and_process_do_not_matter(scene, tmp_path):
    """Case 8: reversed insertion renders identically in other processes; one day by fact ID."""
    a1 = await scene.event("private:A", "A", "这几天发生了不少事", key="a1")
    late = await scene.episode(
        "late",
        "A",
        "下午去了图书馆",
        title="去图书馆",
        evidence=(a1,),
        valid_from=_at("2026-11-03", 18),
    )
    early = await scene.episode(
        "early",
        "A",
        "早上去跑步",
        title="早上跑步",
        evidence=(a1,),
        valid_from=_at("2026-11-03", 7),
    )
    before = await scene.episode(
        "before",
        "A",
        "前一天看了展览",
        title="看展览",
        evidence=(a1,),
        valid_from=_at("2026-11-02", 15),
    )
    await scene.advance()
    conversation = scene.conversations["private:A"]
    first = await scene.reordered_copy(tmp_path / "forward.db", reverse=False)
    second = await scene.reordered_copy(tmp_path / "reverse.db", reverse=True)
    outputs = []
    for url in (first, second):
        result = await run_cli(url, "memory", "episodes", "render", "--conversation", conversation)
        assert result.returncode == 0, result
        outputs.append(result.stdout)
    rendered = await scene.episodes.render_conversation(conversation)

    assert outputs[0] == outputs[1]
    assert rendered.index_text and rendered.index_text in outputs[0]
    assert rendered.index_fact_ids == (before, late, early)
    assert item_lines(rendered.index_text) == [
        _line(before, "看展览", "11-02"),
        _line(late, "去图书馆", "11-03"),
        _line(early, "早上跑步", "11-03"),
    ]
    write_episode_artifact(
        "G08",
        {
            "cli_stdout_sha256": [_sha(value) for value in outputs],
            "render": render_record(rendered),
        },
        scene.labels(),
    )


# --------------------------------------------------------------------------- G9
async def test_g09_lifecycle_changes_drop_lines_and_keep_order(scene):
    """Case 9: invalidated, superseded, contested and quarantined episodes vanish next turn."""
    a1 = await scene.event("private:A", "A", "最近的几件事", key="a1")
    names = ("k1", "p", "s", "k2", "q", "r", "k3")
    ids = [
        await scene.episode(name, "A", f"A 的经历 {name}", title=f"经历 {name}", evidence=(a1,))
        for name in names
    ]
    watermark = await scene.advance()
    before = await scene.episode_turn("A", "早")
    memories = scene.chat._memories
    assert await memories.invalidate_fact(
        scene.facts["p"], reason=MemoryInvalidationReason.USER_RETRACTED, actor_user_id="10001"
    )
    await scene.set_fact(scene.facts["s"], status="superseded")
    assert await memories.contest_fact(scene.facts["q"], reason_code="episode_e2e_contest")
    await scene.set_fact(scene.facts["r"], review_state="quarantined")

    after = await scene.episode_turn("A", "午")

    assert before.index_ids == ids
    assert after.index_ids == [scene.facts[name] for name in ("k1", "k2", "k3")]
    kept = [line for line in item_lines(before.index) if fact_marks(line)[0] in after.index_ids]
    assert item_lines(after.index) == kept
    await _assert_matches_render(scene, "private:A", after)
    contract = await scene.contract()
    scene.write(
        "G09",
        {
            "facts": scene.facts,
            "before": turn_record(before, contract, watermark),
            "after": turn_record(after, contract, watermark),
        },
    )


# --------------------------------------------------------------------------- G10
async def test_g10_forgetting_a_person_removes_rows_and_lines(scene):
    """Case 10: /ai forgetme deletes B's episode rows; no conversation lists them."""
    b1 = await scene.event("private:B", "B", "我上个月去了趟海边", key="b1")
    g1 = await scene.event("space:G", "B", "我在群里讲了一个冷笑话", key="g1")
    g2 = await scene.event("space:G", "A", "刚才那个冷笑话把大家都逗笑了", key="g2")
    a1 = await scene.event("private:A", "A", "我上周去听了音乐会", key="a1")
    b_ep = await scene.episode("b_ep", "B", "上个月去海边", title="B 去海边", evidence=(b1,))
    b_pg = await scene.episode("b_pg", "B@G", "在群里讲冷笑话", title="B 讲冷笑话", evidence=(g1,))
    # Evidence by A: forgetting B removes B's own events from other episodes' evidence.
    g_ok = await scene.episode("g_ok", "G", "群里笑成一片", title="群里讲笑话", evidence=(g2,))
    a_ok = await scene.episode("a_ok", "A", "去听音乐会", title="听音乐会", evidence=(a1,))
    watermark = await scene.advance()
    before = await scene.episode_turn("A", "群里还好吗", group="G")

    forgot = await scene.command("B", "/ai forgetme")
    after = await scene.episode_turn("A", "群里还在吗", group="G")
    owner = await scene.render("private:A")

    assert before.index_ids == [b_pg, g_ok]
    assert forgot and "删除" in forgot[0]
    assert await scene.fact_row(b_ep) is None and await scene.fact_row(b_pg) is None
    assert after.index_ids == [g_ok]
    assert "B 讲冷笑话" not in after.index and "B 去海边" not in after.index
    assert owner.index_fact_ids == (a_ok,)
    contract = await scene.contract()
    scene.write(
        "G10",
        {
            "facts": scene.facts,
            "before": turn_record(before, contract, watermark),
            "after": turn_record(after, contract, watermark),
            "private_A": render_record(owner),
        },
    )


# --------------------------------------------------------------------------- G11
async def test_g11_merged_person_renders_once_by_root(scene):
    """Case 11: B' merged into B renders B's row, without duplicates, from both accounts.

    P2.1 (§4.2, §17 #5): B''s episodes written before B's row ``through`` are not new
    episodes of B; they are counted by month at the next nightly run, not listed.
    """
    from tests.support.person_merge import merge_persons

    b1 = await scene.event("private:B", "B", "我上周学会了游泳", key="b1")
    b2 = await scene.event("private:B2", "B2", "我前天去看了牙医", key="b2")
    b_ep = await scene.episode("b_ep", "B", "上周学会游泳", title="学会游泳", evidence=(b1,))
    b2_ep = await scene.episode("b2_ep", "B2", "前天看牙医", title="看牙医", evidence=(b2,))
    watermark = await scene.advance()

    async with scene.database.immediate_session() as writer:
        root = await merge_persons(writer, scene.persons["B2"], scene.persons["B"])
    assert root == scene.persons["B"]
    root_turn = await scene.episode_turn("B", "合并后 B")
    old_turn = await scene.episode_turn("B2", "合并后 B′ 的账号", person="B")
    by_root = await scene.render("private:B")
    by_old = await scene.render("private:B2")

    assert by_root == by_old
    assert root_turn.index == old_turn.index == by_root.index_text
    assert root_turn.index_ids == [b_ep] and b2_ep not in root_turn.recent_ids
    assert len(set(root_turn.index_ids)) == len(root_turn.index_ids)
    contract = await scene.contract()
    scene.write(
        "G11",
        {
            "facts": scene.facts,
            "root_chat": turn_record(root_turn, contract, watermark),
            "old_account_chat": turn_record(old_turn, contract, watermark),
        },
    )


# --------------------------------------------------------------------------- G12
async def test_g12_disabled_person_episodes_hidden_until_enabled(scene):
    """Case 12: disabling B hides B's person and person_group episodes; enabling restores."""
    b1 = await scene.event("private:B", "B", "我昨天搬了新家", key="b1")
    g1 = await scene.event("space:G", "B", "我在群里分享了搬家照片", key="g1")
    b_ep = await scene.episode("b_ep", "B", "昨天搬新家", title="B 搬家", evidence=(b1,))
    b_pg = await scene.episode(
        "b_pg", "B@G", "在群里分享搬家照片", title="分享照片", evidence=(g1,)
    )
    g_ok = await scene.episode("g_ok", "G", "群里看了搬家照片", title="群里看照片", evidence=(g1,))
    watermark = await scene.advance()
    enabled = await scene.render("private:B")

    await scene.harness.private_users.set_enabled(scene.persons["B"], False)
    disabled_private = await scene.render("private:B")
    disabled_group = await scene.episode_turn("A", "群里谁搬家了", group="G")
    await scene.harness.private_users.set_enabled(scene.persons["B"], True)
    restored_group = await scene.episode_turn("A", "再看看", group="G")
    restored_private = await scene.render("private:B")

    assert enabled.index_fact_ids == (b_ep, b_pg)
    assert disabled_private.index_fact_ids == () and disabled_private.index_text == ""
    assert disabled_group.index_ids == [g_ok]
    assert restored_group.index_ids == [b_pg, g_ok]
    assert restored_private == enabled
    contract = await scene.contract()
    scene.write(
        "G12",
        {
            "facts": scene.facts,
            "enabled_private_B": render_record(enabled),
            "disabled_private_B": render_record(disabled_private),
            "disabled_group": turn_record(disabled_group, contract, watermark),
            "restored_group": turn_record(restored_group, contract, watermark),
        },
    )


# --------------------------------------------------------------------------- G13
async def test_g13_every_listed_id_is_readable_in_the_same_chat(scene):
    """Case 13: search_memory(fact_id=ID) reads every index and recent ID in that chat."""
    a1 = await scene.event("private:A", "A", "我上周去了一趟苏州", key="a1")
    g1 = await scene.event("space:G", "B", "群里周五一起打了羽毛球", key="g1")
    await scene.episode("a_old", "A", "上周去苏州", title="去苏州", evidence=(a1,))
    await scene.episode("g_old", "G", "周五打羽毛球", title="打羽毛球", evidence=(g1,))
    await scene.episode("pg_old", "B@G", "周五约大家打球", title="B 约球", evidence=(g1,))
    await scene.advance()
    await scene.episode("a_new", "A", "苏州吃了面", title="苏州吃面", evidence=(a1,))
    await scene.episode(
        "s_new",
        "self",
        "我陪群友聊了羽毛球",
        title="聊羽毛球",
        visibility="space:G",
        evidence=(g1,),
    )

    results = {}
    for key, speaker, group in (("private:A", "A", None), ("space:G", "B", "G")):
        rendered = await scene.render(key)
        listed = (*rendered.index_fact_ids, *rendered.recent_fact_ids)
        assert rendered.index_fact_ids and rendered.recent_fact_ids
        replies, turn = await scene.episode_tool_turn(
            speaker,
            f"读一下经历 {key}",
            [("search_memory", {"fact_id": fact_id}) for fact_id in listed],
            group=group,
        )
        assert turn.index == rendered.index_text and turn.recent == rendered.recent_text
        for fact_id, reply in zip(listed, replies, strict=True):
            rows = [row for row in reply.get("memories", ()) if row["fact_id"] == fact_id]
            assert rows, (fact_id, reply)
        results[key] = {
            "turn": turn_record(turn, await scene.contract(), rendered.through_fact_id),
            "read_fact_ids": [
                row["fact_id"] for reply in replies for row in reply.get("memories", ())
            ],
        }
    scene.write("G13", {"facts": scene.facts, **results})


# --------------------------------------------------------------------------- G14
async def test_g14_recent_budget_and_overflow(scene):
    """Case 14: 30 episodes after the nightly run -> 20 recent lines plus the overflow line.

    The index part (120 lines, month counts) is replaced by P2.1 (K21).
    """
    a1 = await scene.event("private:A", "A", "这一年里的很多事", key="a1")
    old = [
        await scene.episode(
            f"o{index:03d}",
            "A",
            f"第 {index} 件旧事",
            title=f"旧事 {index:03d}",
            evidence=(a1,),
            valid_from=_at("2026-01-01") + timedelta(days=index),
        )
        for index in range(3)
    ]
    watermark = await scene.advance()
    fresh = [
        await scene.episode(
            f"n{index:02d}",
            "A",
            f"第 {index} 件新事",
            title=f"新事 {index:02d}",
            evidence=(a1,),
            valid_from=_at("2026-11-01") + timedelta(hours=index),
        )
        for index in range(30)
    ]

    turn = await scene.episode_turn("A", "列一下经历")

    assert turn.index_ids == old
    assert turn.recent_ids == fresh[-RECENT_LINES:]
    assert turn.recent.split("\n")[1] == "（另有 10 条，可用 search_memory 查）"
    await _assert_matches_render(scene, "private:A", turn)
    scene.write("G14", {"private_A": turn_record(turn, await scene.contract(), watermark)})


# --------------------------------------------------------------------------- G15
async def test_g15_render_failure_keeps_the_turn_and_the_resident_segment(
    database, tmp_path, monkeypatch, caplog
):
    """Case 15: a failing index renderer drops only its two blocks for this turn."""
    from yuki.memory.episode_index import EpisodeIndex

    scene = await EpisodeScene(database, tmp_path).build()
    a1 = await scene.event("private:A", "A", "记住：周报周五交，我上周去了杭州", key="a1")
    await scene.fact("rule", "A", "周报周五交", evidence=(a1,), **EXPLICIT)
    await scene.episode("trip", "A", "上周去杭州", title="去杭州", evidence=(a1,))
    await scene.advance()
    await scene.episode("fresh", "A", "杭州喝了龙井", title="喝龙井", evidence=(a1,))
    healthy = await scene.episode_turn("A", "渲染正常")

    def broken_read_session(*args, **kwargs):
        raise RuntimeError("episode index read session failed")

    method = EpisodeIndex.render_conversation

    async def faulted(self, *args, **kwargs):
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(self._database, "read_session", broken_read_session)
            return await method(self, *args, **kwargs)

    monkeypatch.setattr(EpisodeIndex, "render_conversation", faulted)
    scene.responder = lambda request: ChatResponse("收到", 0)
    caplog.set_level(logging.DEBUG)

    turn = await scene.episode_turn("A", "渲染出错也要回复")

    assert healthy.index_ids and healthy.recent_ids and healthy.resident
    assert turn.index == "" and turn.recent == ""
    assert INDEX_HEADER not in turn.system and RECENT_HEADER not in turn.current
    assert turn.resident == healthy.resident
    assert "渲染出错也要回复" in turn.current
    assert turn.result.reason == "chat"
    failures = [
        record
        for record in caplog.records
        if "memory_episode_index_render_failed" in record.getMessage()
    ]
    assert len(failures) == 1
    scene.write(
        "G15",
        {
            "healthy": turn_record(healthy, await scene.contract()),
            "failed": turn_record(turn, await scene.contract()),
            "resident_kept": turn.resident == healthy.resident,
            "render_failed_logs": len(failures),
        },
    )


# --------------------------------------------------------------------------- G16
async def test_g16_recovery_contract_ignores_index_changes(scene):
    """Case 16: watermark and new episodes never move the hash."""
    a1 = await scene.event("private:A", "A", "上周我们去露营了", key="a1")
    await scene.episode("camp", "A", "上周去露营", title="去露营", evidence=(a1,))
    hashes = [await scene.contract()]
    await scene.advance()
    hashes.append(await scene.contract())
    await scene.episode("fire", "A", "露营时生了篝火", title="生篝火", evidence=(a1,))
    hashes.append(await scene.contract())

    assert len(set(hashes)) == 1
    scene.write("G16", {"recovery_contract_sha256": [_sha(value) for value in hashes]})


# --------------------------------------------------------------------------- G17
async def test_g17_work_keeps_its_system_prefix_across_a_watermark_move(database, tmp_path):
    """Case 17: one activation keeps its prefix; the continuation renders the new watermark.

    The first activation is interrupted by a retryable provider outage before its
    third dispatch; while it waits, a new episode is written and a Dream run moves the
    watermark. WorkScheduler continues the same Work as a new activation.
    """
    import asyncio

    from tests.unit.test_history_dispatch_ownership import retry_due_now
    from tests.unit.test_work_owner_recovery import original_scheduler

    from yuki.llm.base import LLMUnavailableError
    from yuki.runtime.work_repository import WorkRepository
    from yuki.runtime.work_schema_v1 import work

    scene = await EpisodeScene(database, tmp_path).build(work_enabled=True)
    a1 = await scene.event("private:A", "A", "上周我们整理了周报模板", key="a1")
    e0 = await scene.episode("e0", "A", "整理周报模板", title="整理模板", evidence=(a1,))
    first_watermark = await scene.advance()
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
    e1 = await scene.episode("e1", "A", "周报改用表格", title="周报改表格", evidence=(a1,))
    second_watermark = await scene.advance()
    scene.provider.release[pause].set()
    first = capture_episodes((await asyncio.wait_for(running, 30)).requests)
    async with database.read_session() as reader:
        interrupted = dict((await reader.execute(select(work))).mappings().one())
    assert interrupted["state"] == "queued", interrupted["state"]

    await retry_due_now(database)
    repository = WorkRepository(database)
    await original_scheduler(repository, scene.env, scene.harness, scene.chat).drain_once()
    final = await repository.get(interrupted["id"])
    phase["name"] = "after"
    later = await scene.episode_turn("A", "新的一轮")

    assert final["state"] == "completed"
    assert first.index_ids == [e0]
    observed = [capture_episodes([request]).index for request in phase["work"]]
    for index in observed[:2]:
        assert index == first.index
    # The resumed Work appends to its paid chain (#646): the system prefix stays
    # byte-identical; only a new chain (a later turn) renders the new watermark.
    resumed = observed[2]
    assert fact_marks(resumed) == [e0]
    assert set(observed[2:]) == {first.index}
    assert later.index_ids == [e0, e1]
    contract = await scene.contract()
    scene.write(
        "G17",
        {
            "facts": scene.facts,
            "watermarks": [first_watermark, second_watermark],
            "work_first_index": first.index,
            "resumed_activation_index": resumed,
            "work_requests": len(phase["work"]),
            "work_state": final["state"],
            "later": turn_record(later, contract, second_watermark),
        },
    )


# --------------------------------------------------------------------------- G18
async def test_g18_public_compact_checkpoint_never_holds_the_blocks(database, tmp_path):
    """Case 18 (public part): compacted input and checkpoints carry neither block."""
    from yuki.conversation.projection_models import PromptProjectionModel

    scene = await EpisodeScene(database, tmp_path).build()
    a1 = await scene.event("private:A", "A", "上周讨论了周报格式", key="a1")
    await scene.episode("old", "A", "讨论周报格式", title="讨论周报", evidence=(a1,))
    await scene.advance()
    await scene.episode("new", "A", "定下周报模板", title="定模板", evidence=(a1,))
    for index in range(6):
        await scene.event("private:A", "A", f"较早的聊天 {index}：" + "讨论周报格式。" * 40)
    compact_requests = []

    def respond(request):
        if "Host 会话上下文压缩要求" in str(request.messages[-1].content or ""):
            compact_requests.append(request)
            return "PUBLIC_CHECKPOINT：讨论过周报格式。"
        return "NO_REPLY"

    scene.responder = respond
    before = await scene.episode_turn("A", "压缩前")
    replies = await scene.command("A", "/ai compact")
    after = await scene.episode_turn("A", "压缩后")
    async with database.read_session() as reader:
        checkpoints = [
            row.selected_summary_text or ""
            for row in (await reader.scalars(select(PromptProjectionModel))).all()
        ]

    assert compact_requests, replies
    assert before.index and before.recent
    for request in compact_requests:
        compacted = "\n".join(str(m.content or "") for m in request.messages if m.role != "system")
        assert INDEX_HEADER not in compacted and RECENT_HEADER not in compacted
        assert "（fact:" not in compacted
    assert any("PUBLIC_CHECKPOINT" in text for text in checkpoints)
    for text in checkpoints:
        assert INDEX_HEADER not in text and RECENT_HEADER not in text and "（fact:" not in text
    assert after.index == before.index and after.recent == before.recent
    await _assert_matches_render(scene, "private:A", after)
    contract = await scene.contract()
    scene.write(
        "G18",
        {
            "before": turn_record(before, contract),
            "after": turn_record(after, contract),
            "compact_requests": len(compact_requests),
        },
    )


async def test_g18_execution_compact_never_writes_the_blocks(database, tmp_path, monkeypatch):
    """Case 18 (Work part): the execution compact instruction carries neither block."""
    import yuki.services.context_compaction as compaction
    from yuki.runtime.work_repository import WorkCapacityError
    from yuki.services.context_compaction import (
        EXECUTION_COMPACT_INSTRUCTION,
        PUBLIC_COMPACT_INSTRUCTION,
    )

    scene = await EpisodeScene(database, tmp_path).build(
        work_enabled=True, context_compact_trigger_tokens=96000
    )
    a1 = await scene.event("private:A", "A", "上周写过一段长回答", key="a1")
    await scene.episode("old", "A", "写长回答", title="写长回答", evidence=(a1,))
    await scene.advance()
    await scene.episode("new", "A", "回答被采纳", title="回答被采纳", evidence=(a1,))
    await scene.event("private:A", "A", ("PUBLIC_OLD " + "historical decision. " * 9500).rstrip())
    answer = "LONG_ANSWER " + "independent branch; " * 2400
    kinds: list[str] = []
    instructions: list[str] = []
    state = {"sent": False, "read": False}

    def respond(request):
        content = str(request.messages[-1].content or "")
        if content.startswith(PUBLIC_COMPACT_INSTRUCTION):
            kinds.append("public_compact")
            return "PUBLIC_CHECKPOINT"
        if content.startswith(EXECUTION_COMPACT_INSTRUCTION):
            kinds.append("execution_compact")
            instructions.append(content)
            return "PRIVATE_CHECKPOINT：已发送长回答。"
        kinds.append("normal")
        if not state["sent"]:
            state["sent"] = True
            return tool_call("send", {"text": answer}, "long-send")
        if not state["read"]:
            # A small observed round: E can then cover the large send round and
            # bring the request below the trigger (it cannot cover the prefix).
            state["read"] = True
            return tool_call("read_chat", {"limit": 1, "query": "很长的回答"}, "small-read")
        return "NO_REPLY"

    def refuse_public(*args, **kwargs):
        raise WorkCapacityError("model_request_capacity")

    monkeypatch.setattr(compaction, "public_compact_plan", refuse_public)
    scene.responder = respond
    first = await scene.episode_turn("A", "写一段很长的回答")
    normals = [
        request
        for request, kind in zip(scene.provider.requests[-len(kinds) :], kinds, strict=True)
        if kind == "normal"
    ]

    assert "execution_compact" in kinds, kinds
    assert first.index and first.recent
    for content in instructions:
        assert INDEX_HEADER not in content and RECENT_HEADER not in content
    following = normals[-1]
    assert system_text(following).count(INDEX_HEADER) == 1
    assert sum(1 for message in following.messages if message.content == first.current) == 1
    scene.write(
        "G18-work",
        {"first": turn_record(first, await scene.contract()), "request_kinds": kinds},
    )


# --------------------------------------------------------------------------- G19
async def _entry_scene(database, tmp_path) -> tuple[EpisodeScene, dict[str, int]]:
    scene = await EpisodeScene(database, tmp_path).build(work_enabled=True)
    a1 = await scene.event("private:A", "A", "上周我们去看了海", key="a1")
    g1 = await scene.event("space:G", "B", "群里周五一起看了电影", key="g1")
    ids = {
        "a_ep": await scene.episode("a_ep", "A", "上周去看海", title="去看海", evidence=(a1,)),
        "g_ep": await scene.episode(
            "g_ep", "G", "周五群里看电影", title="群里看电影", evidence=(g1,)
        ),
        "s_ep": await scene.episode(
            "s_ep", "self", "我陪群友聊电影", title="聊电影", visibility="space:G", evidence=(g1,)
        ),
    }
    await scene.advance()
    return scene, ids


async def test_g19_scheduled_turns_follow_their_conversation(database, tmp_path):
    """Case 19 (automation part): A's private schedule has A's index; G's has G's."""
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
    await scene.turn("A", "一分钟后提醒我看海边照片")
    phase["create"] = ("group", "GROUP_SCHEDULE_GOAL")
    await scene.turn("A", "一分钟后在群里提醒大家看电影", group="G")
    phase["create"] = None
    clock.advance(2)
    await dispatch()

    private = capture_episodes([scheduled["private"]])
    group = capture_episodes([scheduled["group"]])
    assert private.index_ids == [ids["a_ep"]]
    assert group.index_ids == [ids["g_ep"], ids["s_ep"]]
    contract = await scene.contract()
    scene.write(
        "G19-schedule",
        {
            "facts": scene.facts,
            "private_schedule": turn_record(private, contract),
            "group_schedule": turn_record(group, contract),
        },
    )


async def test_g19_self_initiative_turn_has_the_group_index(database, tmp_path):
    """Case 19 (SELF part): the autonomous turn in G carries G's index."""
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
    turn = capture_episodes(scene.provider.requests[start : start + 1])

    assert turn.index_ids == [ids["g_ep"], ids["s_ep"]]
    await _assert_matches_render(scene, "space:G", turn)
    scene.write(
        "G19-self",
        {"facts": scene.facts, "self_initiative": turn_record(turn, await scene.contract())},
    )


async def test_g19_subagent_requests_carry_no_index(database, tmp_path):
    """Case 19 (subagent part): the parent has the index, the child never."""
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
            return tool_call("manage_task", {"action": "start", "goal": "整理照片"}, "accept")
        if script["phase"] == "spawn":
            script["phase"] = "wait"
            return tool_call(
                "start_subtask", {"goal": "挑出看海的照片", "acceptance": "返回挑选结果"}, "spawn"
            )
        if script["phase"] == "wait":
            script["child"] = tool_result(request, "spawn")["id"]
            script["phase"] = "waiting"
            return tool_call("wait", {"for": [{"run": script["child"]}]}, "wait-child")
        return "NO_REPLY"

    scene.responder = respond
    parent = await scene.episode_turn("A", "派个子任务整理照片")
    executor = make_child_executor(
        WorkRepository(database),
        chat=scene.chat,
        config=scene.chat._runtime_config,
        runner=scene.chat.runtime.runner,
        load_tools=scene.chat.runtime.runner.main_contract.definitions,
        router=scene.env.router,
    )
    await executor.run(script["child"])

    assert parent.index_ids == [ids["a_ep"]]
    assert children
    for request in children:
        text = request_text(request)
        assert INDEX_HEADER not in text and RECENT_HEADER not in text and "（fact:" not in text
    scene.write(
        "G19-subagent",
        {
            "parent": turn_record(parent, await scene.contract()),
            "child_requests": len(children),
        },
    )


async def test_g19_plugin_turn_carries_no_index(database, tmp_path):
    """Case 19 (plugin part): plugin agent turns never render the index."""
    from tests.integration.test_plugin_agent_domain_grants import source_host

    from yuki.conversation.canonical_db_models import CanonicalConversationModel
    from yuki.llm.fake import FakeLLMProvider
    from yuki.memory.enums import MemoryKind, MemoryScopeType, SelfMemoryVisibility
    from yuki.memory.episode_index import EpisodeIndex
    from yuki.memory.models import MemoryEvidenceCreate, MemoryFactCreate

    provider = FakeLLMProvider()
    env, _harness, chat, source, host = await source_host(database, tmp_path, provider, ())
    install_memory_segments(chat, database)
    async with database.read_session() as reader:
        group_event = await reader.scalar(select(ChatEventModel.id).order_by(ChatEventModel.id))
        conversation = await reader.scalar(
            select(CanonicalConversationModel.id).where(
                CanonicalConversationModel.space_id == env.space
            )
        )
    episode = await chat._memories.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.SELF,
            visibility_type=SelfMemoryVisibility.GROUP,
            visibility_group_id="20001",
            canonical_visibility_space_id=env.space,
            kind=MemoryKind.EPISODE,
            memory_key="episode:plugin",
            category="episode-e2e",
            content="插件轮不该看到的经历",
            title="插件轮不该看到",
            source_type=MemorySourceType.AUTOMATIC,
            authority=MemoryAuthority.AGENT_REFLECTION,
        ),
        evidence=MemoryEvidenceCreate(
            event_id=group_event,
            source_speaker_user_id="10001",
            relation=MemoryEvidenceRelation.AGENT_REFLECTION,
            authority=MemoryAuthority.AGENT_REFLECTION,
            excerpt="插件轮不该看到的经历",
        ),
    )
    control = await EpisodeIndex(database, timezone=TIMEZONE).render_conversation(conversation)
    assert control.recent_fact_ids == (episode.id,)
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
        assert INDEX_HEADER not in text and RECENT_HEADER not in text and "（fact:" not in text
    write_episode_artifact(
        "G19-plugin",
        {"plugin_requests": len(provider.requests), "control_render": render_record(control)},
        {env.person: "person:A", env.space: "space:G", conversation: "conversation:space:G"},
    )


# --------------------------------------------------------------------------- G20
async def test_g20_watermark_without_runs_and_while_a_run_is_stuck(scene):
    """Case 20: no row -> empty index, tail above the global watermark; a running run
    keeps the row."""
    a1 = await scene.event("private:A", "A", "这段时间做了很多事", key="a1")
    first = [
        await scene.episode(
            f"e{index:02d}", "A", f"做过的事 {index}", title=f"事 {index:02d}", evidence=(a1,)
        )
        for index in range(12)
    ]
    no_run = await scene.render("private:A")
    watermark = await scene.advance()
    finished = await scene.render("private:A")
    later = await scene.episode("later", "A", "又做了一件事", title="又一件事", evidence=(a1,))
    stuck = await scene.stuck_run()
    during = await scene.render("private:A")
    status = await run_cli(scene.database.url, "memory", "episodes", "status")

    assert no_run.through_fact_id == 0 and no_run.index_text == ""
    assert no_run.recent_fact_ids == tuple(first)
    assert finished.through_fact_id == watermark == first[-1]
    assert finished.index_fact_ids == tuple(first) and finished.recent_text == ""
    assert stuck.status.value == "running" and stuck.snapshot_max_fact_id == later
    assert during.through_fact_id == watermark
    assert during.index_text == finished.index_text and during.recent_fact_ids == (later,)
    assert status.returncode == 0, status
    payload = json.loads(status.stdout)
    assert payload["watermark"]["through_fact_id"] == watermark
    assert payload["watermark"]["completed_at"]
    assert payload["active_run"]["run_id"] == stuck.public_id
    assert payload["active_run"]["status"] == "running"
    assert payload["episodes_with_title"] == 13 and payload["episodes_without_title"] == 0
    assert [item["through_fact_id"] for item in payload["indexes"]] == [watermark]
    scene.write(
        "G20",
        {
            "no_run": render_record(no_run),
            "finished": render_record(finished),
            "during_stuck_run": render_record(during),
            "status_watermark": payload["watermark"]["through_fact_id"],
            "status_active_run": payload["active_run"]["status"],
        },
    )


# --------------------------------------------------------------------------- G21
async def test_g21_untitled_and_multiline_titles_and_backfill(scene):
    """Case 21: save_memory episodes render untitled; titles fold; backfill rules hold."""
    from tests.support.model_executor import InjectedModelExecutor

    from yuki.memory.dream.repository import fact_signature
    from yuki.memory.episode_titles import EpisodeTitles
    from yuki.services.concurrency import ConcurrencyManager

    a1 = await scene.event("private:A", "A", "周末去公园散步了", key="a1")
    folded = await scene.episode(
        "folded", "A", "周末去公园散步", title="去 公园\n  散步", evidence=(a1,)
    )
    replies, saved_turn = await scene.episode_tool_turn(
        "A",
        "记一下：我今天第一次做了蛋糕",
        [
            (
                "save_memory",
                {
                    "target": {"person_id": scene.persons["A"]},
                    "content": "今天第一次做了蛋糕",
                    "kind": "episode",
                    "quote": "我今天第一次做了蛋糕",
                },
            )
        ],
    )
    assert replies[0].get("outcome") == "committed", replies
    saved = replies[0]["fact_id"]
    untitled = await scene.render("private:A")
    lines = item_lines(untitled.recent_text)

    assert (await scene.titles([saved]))[saved] is None
    assert _line(saved, UNTITLED) in lines
    assert _line(folded, "去 公园 散步") in lines

    memories = scene.chat._memories
    before_row = await scene.fact_row(saved)
    before_signature = fact_signature(await memories.get_fact(saved))
    titled_before = await scene.titles([folded])
    offered: list[list[str]] = []

    def respond(request):
        payload = json.loads(request.messages[-1].content)
        offered.append([item["ref"] for item in payload["episodes"]])
        return json.dumps(
            {
                "items": [
                    {"ref": item["ref"], "title": "第一次做蛋糕"} for item in payload["episodes"]
                ]
            },
            ensure_ascii=False,
        )

    scene.responder = respond
    titles = EpisodeTitles(
        settings=scene.settings,
        database=scene.database,
        models=InjectedModelExecutor(scene.provider),
        concurrency=ConcurrencyManager(),
    )
    dry = await titles.backfill(dry_run=True, limit=None)
    after_dry = await scene.titles([saved])
    applied = await titles.backfill(dry_run=False, limit=None)
    after_row = await scene.fact_row(saved)
    after_signature = fact_signature(await memories.get_fact(saved))
    titled_after = await scene.titles([folded])
    set_title = await run_cli(
        scene.database.url,
        "memory",
        "episodes",
        "set-title",
        "--fact",
        str(folded),
        "--text",
        "公园散步",
    )
    final = await scene.render("private:A")

    assert [(item.fact_id, item.title, item.source) for item in dry.items] == [
        (saved, "第一次做蛋糕", "model")
    ]
    assert after_dry[saved] is None
    assert [(item.fact_id, item.title, item.source) for item in applied.items] == [
        (saved, "第一次做蛋糕", "model")
    ]
    assert after_row["title"] == "第一次做蛋糕"
    assert after_row["updated_at"] == before_row["updated_at"]
    assert after_signature == before_signature
    assert titled_after == titled_before
    assert offered and all(len(refs) == 1 for refs in offered)
    assert set_title.returncode == 0, set_title
    assert _line(saved, "第一次做蛋糕") in item_lines(final.recent_text)
    assert _line(folded, "公园散步") in item_lines(final.recent_text)
    scene.write(
        "G21",
        {
            "facts": {**scene.facts, "saved": saved},
            "save_turn": turn_record(saved_turn, await scene.contract()),
            "untitled_render": render_record(untitled),
            "dry_run_items": [(item.fact_id, item.title, item.source) for item in dry.items],
            "applied_items": [(item.fact_id, item.title, item.source) for item in applied.items],
            "updated_at_unchanged": after_row["updated_at"] == before_row["updated_at"],
            "dream_signature_unchanged": after_signature == before_signature,
            "final_render": render_record(final),
        },
    )


async def test_g21_empty_titles_never_reject_writer_outputs(database, tmp_path):
    """Case 21 (writers): an empty title is a missing title, never a rejected output.

    Failure case written before the fix: a ``min_length=1`` title made one empty title
    (also on a non-episode claim, whose title is discarded anyway) fail the whole paid
    reflection or extraction output, although a missing title is not an admission
    condition (task book §4.2).
    """
    scene = await EpisodeScene(database, tmp_path).build(
        memory_batch_trigger_count=1, memory_batch_max_wait_seconds=0
    )
    await scene.event("private:A", "A", "今天下午去爬了香山", key="a1")
    reflected = await scene.reflect(
        {"private:A": [{"title": "", "content": "A 说下午爬了香山。", "cites": ["香山"]}]}
    )
    await scene.event("private:A", "A", "我昨天第一次去潜水了，也很喜欢喝美式", key="a2")
    newest = max(await scene.fact_ids_after(0))
    claims = [
        {
            "subject_ref": "speaker",
            "scope_type": "person",
            "kind": "episode",
            "memory_key": "diving:first",
            "category": "experience",
            "content": "昨天第一次去潜水",
            "evidence_quote": "我昨天第一次去潜水了",
            "title": "",
            "importance": 4,
            "confidence": 0.9,
            "source_type": "automatic",
        },
        {
            "subject_ref": "speaker",
            "scope_type": "person",
            "kind": "preference",
            "memory_key": "coffee",
            "category": "drink",
            "content": "喜欢喝美式",
            "evidence_quote": "也很喜欢喝美式",
            "title": "",
            "importance": 3,
            "confidence": 0.9,
            "source_type": "automatic",
        },
    ]

    def respond(request):
        # Raw model JSON: the real extraction path does the validation.
        payload = json.loads(request.messages[-1].content)
        source = next(
            item["source_event_id"] for item in payload["events"] if "潜水" in item["content"]
        )
        return json.dumps(
            {"claims": [{"source_event_id": source, "claim": claim} for claim in claims]},
            ensure_ascii=False,
        )

    event = scene.records["a2"]
    worker = scene.harness.processor._memory_worker
    scene.extraction_responder = respond
    try:
        await worker.enqueue(event.id, event.scope.key)
        processed = await worker.process_once()
    finally:
        scene.extraction_responder = None
    extracted = await scene.fact_ids_after(newest)
    kinds = {fact_id: (await scene.fact_row(fact_id))["kind"] for fact_id in extracted}
    titles = await scene.titles([*reflected["private:A"], *extracted])
    rendered = await scene.render("private:A")
    episode_ids = [*reflected["private:A"], *(i for i, k in kinds.items() if k == "episode")]

    assert processed >= 1
    assert sorted(kinds.values()) == ["episode", "preference"]
    assert set(titles.values()) == {None}
    assert rendered.recent_fact_ids == tuple(episode_ids)
    for fact_id in episode_ids:
        assert _line(fact_id, UNTITLED) in item_lines(rendered.recent_text)
    scene.write(
        "G21-empty-titles",
        {
            "facts": {**scene.facts, "reflected": reflected, "extracted": extracted},
            "titles": {str(key): value for key, value in sorted(titles.items())},
            "render": render_record(rendered),
        },
    )


# --------------------------------------------------------------------------- G22
async def test_g22_one_place_per_episode_at_any_time(scene):
    """Case 22: before and after each watermark move, every ID is in exactly one block."""
    a1 = await scene.event("private:A", "A", "最近去了两次博物馆", key="a1")
    e1 = await scene.episode("e1", "A", "第一次去博物馆", title="博物馆一", evidence=(a1,))
    renders = [await scene.render("private:A")]
    await scene.advance()
    e2 = await scene.episode("e2", "A", "第二次去博物馆", title="博物馆二", evidence=(a1,))
    renders.append(await scene.render("private:A"))
    turn = await scene.episode_turn("A", "博物馆好玩吗")
    await scene.advance()
    renders.append(await scene.render("private:A"))

    for rendered in renders:
        assert not set(rendered.index_fact_ids) & set(rendered.recent_fact_ids)
    assert renders[0].recent_fact_ids == (e1,)
    assert renders[1].index_fact_ids == (e1,) and renders[1].recent_fact_ids == (e2,)
    assert renders[2].index_fact_ids == (e1, e2) and renders[2].recent_fact_ids == ()
    assert not set(turn.index_ids) & set(turn.recent_ids)
    assert turn.index_ids == [e1] and turn.recent_ids == [e2]
    scene.write(
        "G22",
        {
            "renders": [render_record(item) for item in renders],
            "turn": turn_record(turn, await scene.contract()),
        },
    )


# --------------------------------------------------------------------------- G23
async def test_g23_dream_outputs_carry_titles(scene, tmp_path, monkeypatch):
    """Case 23: recompose outputs take their focus; synthesis keeps the anchor's title."""
    a1 = await scene.event("private:A", "A", "上周末去了杭州和苏州", key="a1")
    r1 = await scene.episode(
        "r1", "A", "上周末先去杭州逛了西湖，又去苏州逛园林", title="杭州苏州", evidence=(a1,)
    )
    await scene.episode(
        "r2", "A", "在苏州吃了一碗面，回杭州坐了船", title="吃面坐船", evidence=(a1,)
    )
    await scene.episode("s1", "A", "周日去看了一场电影", title="周日看电影", evidence=(a1,))
    s2 = await scene.episode("s2", "A", "周日看的电影是一部老片", title="老电影", evidence=(a1,))
    contents = scene.contents

    def respond(request):
        text = request_text(request)
        if contents[r1] in text:
            return json.dumps(
                {
                    "actions": [
                        {
                            "operation": "recompose",
                            "source_refs": ["memory_1", "memory_2"],
                            "anchor_ref": "memory_1",
                            "outputs": [
                                {
                                    "focus": "杭州西湖与坐船",
                                    "source_refs": ["memory_1", "memory_2"],
                                    "content": "上周末在杭州逛了西湖，回杭州后坐了船。",
                                    "importance": 4,
                                },
                                {
                                    "focus": "苏州园林与吃面",
                                    "source_refs": ["memory_1", "memory_2"],
                                    "content": "上周末在苏州逛了园林，还吃了一碗面。",
                                    "importance": 4,
                                },
                            ],
                        }
                    ]
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "actions": [
                    {
                        "operation": "synthesize",
                        "source_refs": ["memory_1", "memory_2"],
                        "anchor_ref": "memory_1",
                        "content": "周日去看了一场老电影。",
                        "importance": 4,
                    }
                ]
            },
            ensure_ascii=False,
        )

    scene.dream_responder = respond
    run = await scene.dream_run((("r1", "r2"), ("s1", "s2")))
    page = await scene.dream().repository.run_page(run.public_id)
    outputs = {operation.operation.value: operation for operation in page.operations}
    created = await scene.fact_ids_after(s2)
    titles = await scene.titles(created)
    rows = {fact_id: await scene.fact_row(fact_id) for fact_id in created}
    by_content = {row["content"]: fact_id for fact_id, row in rows.items()}
    recomposed = [
        by_content["上周末在杭州逛了西湖，回杭州后坐了船。"],
        by_content["上周末在苏州逛了园林，还吃了一碗面。"],
    ]
    synthesized = by_content["周日去看了一场老电影。"]

    assert run.status.value == "completed", run
    assert {"recompose", "synthesize"} <= outputs.keys()
    assert [titles[item] for item in recomposed] == ["杭州西湖与坐船", "苏州园林与吃面"]
    assert titles[synthesized] == "周日看电影"
    render = await scene.render("private:A")
    assert set(render.index_fact_ids + render.recent_fact_ids) == {*recomposed, synthesized}

    # Backfill restores a focus title without any model call (fake profiles in the CLI).
    from tests.support.model_profiles import write_fake_profiles

    monkeypatch.setenv("MODEL_PROFILES_FILE", str(write_fake_profiles(tmp_path / "models.toml")))
    await scene.set_fact(recomposed[0], title=None)
    backfill = await run_cli(scene.database.url, "memory", "episodes", "backfill-titles")
    assert backfill.returncode == 0, backfill
    assert (await scene.titles([recomposed[0]]))[recomposed[0]] == "杭州西湖与坐船"
    scene.write(
        "G23",
        {
            "facts": {**scene.facts, "recomposed": recomposed, "synthesized": synthesized},
            "titles": {str(key): value for key, value in sorted(titles.items())},
            "render": render_record(render),
            "backfill_stdout": json.loads(backfill.stdout),
        },
    )


# --------------------------------------------------------------------------- G24
async def test_g24_writers_store_titles_and_the_turn_lists_them(database, tmp_path):
    """Case 24: self-reflection and extraction store titles; the next turn lists them."""
    scene = await EpisodeScene(database, tmp_path).build(
        memory_batch_trigger_count=1,
        memory_batch_max_wait_seconds=0,
    )
    a1 = await scene.event("private:A", "A", "上周我们讨论了新家的装修", key="a1")
    e0 = await scene.episode("e0", "A", "讨论新家装修", title="讨论装修", evidence=(a1,))
    watermark = await scene.advance()
    await scene.event("private:A", "A", "今天下午去爬了香山", key="a2")
    reflected = await scene.reflect(
        {"private:A": [{"title": "A 去爬香山", "content": "A 说下午爬了香山。", "cites": ["香山"]}]}
    )
    await scene.event("private:A", "A", "我昨天第一次去潜水了，也很喜欢喝美式", key="a3")
    newest = max(await scene.fact_ids_after(0))
    processed = await scene.extract(
        "a3",
        [
            {
                "subject_ref": "speaker",
                "scope_type": "person",
                "kind": "episode",
                "memory_key": "diving:first",
                "content": "昨天第一次去潜水",
                "evidence_quote": "我昨天第一次去潜水了",
                "title": "第一次潜水",
                "importance": 4,
                "confidence": 0.9,
                "source_type": "automatic",
            },
            {
                "subject_ref": "speaker",
                "scope_type": "person",
                "kind": "preference",
                "memory_key": "coffee",
                "content": "喜欢喝美式",
                "evidence_quote": "也很喜欢喝美式",
                "title": "不该写进偏好的标题",
                "importance": 3,
                "confidence": 0.9,
                "source_type": "automatic",
            },
        ],
    )
    extracted = await scene.fact_ids_after(newest)
    turn = await scene.episode_turn("A", "经历目录")

    titles = await scene.titles([*reflected["private:A"], *extracted])
    rows = {fact_id: await scene.fact_row(fact_id) for fact_id in extracted}
    episode_rows = [fact_id for fact_id, row in rows.items() if row["kind"] == "episode"]
    preference_rows = [fact_id for fact_id, row in rows.items() if row["kind"] == "preference"]

    assert processed >= 1
    assert titles[reflected["private:A"][0]] == "A 去爬香山"
    assert [titles[item] for item in episode_rows] == ["第一次潜水"]
    assert [titles[item] for item in preference_rows] == [None]
    assert turn.index_ids == [e0]
    assert set(turn.recent_ids) == {*reflected["private:A"], *episode_rows}
    contract = await scene.contract()
    scene.write(
        "G24",
        {
            "facts": {**scene.facts, "reflected": reflected, "extracted": extracted},
            "turn": turn_record(turn, contract, watermark),
            "titles": {str(key): value for key, value in sorted(titles.items())},
        },
    )
