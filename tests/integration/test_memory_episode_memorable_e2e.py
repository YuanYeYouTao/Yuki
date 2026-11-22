"""E2E: the P2.1 episode index — recent window, long-term impressions, month counts.

Authoritative spec: docs/architecture/Yuki-记忆分层P2.1经历目录精简任务书-2026-11-23.md
§13 (cases 1-33 = K1-K33) with every §12 item at its recommendation or decision: one
``memory_episode_indexes`` row per conversation audience (Q1) holding the recent list,
at most ten long-term impressions and the month counts; an episode is compared once,
when it leaves the last 15 (Q2); the pool mixes current impressions and leavers sorted
by date, without old/new marks, scores, counts or entry times (Q5); successors are
handled by root (§5.2, Q6, Q15); the owner locks with ``set`` / ``lock`` (Q11).

Every case runs real SQLite, real write paths (``MemoryFactService``, the real
self-reflection over claimed chat batches, Dream operations, Main ``save_memory``), real
Dream runs drained through the worker loop body (its selection step writes the rows)
and real ``MessageProcessor`` -> ``ContextAssembler`` -> ``MainAgentTurns.compose`` ->
request assembly; only model replies are scripted. Each case writes normalized JSON
(system-end index text, current-message tail, the fact IDs of the three parts, the
normalized row, the comparison inputs the scripted model received, the
``recovery_contract`` hash; IDs replaced by labels, times stripped, today as
``<today>``) to ``test-results/memory-episode-memorable/<K-case>.json``; identical input
gives byte-identical artifacts.

Failure cases, written before the implementation:
  K1  a new episode changes the index within the day or enters the impressions directly;
  K2  a leaver is not compared, or compared twice;
  K3  a lost leaver comes back (window backfill, later pools); it is not counted;
  K4  replacement never happens (squatting);
  K5  the comparison input carries scores, counts, entry times or old/new marks;
  K6  an invalid output is partly adopted;
  K7  over budget the call count explodes or leavers are lost;
  K8  with the cap used up a row is half old, half new;
  K9  a private-origin episode reaches the group's impressions or comparison input;
  K10 another group's or a tool-receipt-evidenced episode reaches the group index;
  K11 a listed episode whose evidence changed stays displayed;
  K12 an impression loses its chain when replaced, or its successor counts as new;
  K13 an emptied impression slot is backfilled from archived episodes;
  K14 re-mentioning an old event brings it back to the window or a second comparison;
  K33 an impression's successor is also treated as a new episode;
  K15 an episode merged the same night is lost or duplicated;
  K16 one episode in two audiences makes them influence each other;
  K17 a locked row is still rewritten by the model, or its window stops;
  K18 Dream overwrites an owner change made during its call;
  K19 a first-version plan is applied wrongly;
  K20 without a row the render fails or reads the whole history;
  K21 month counts are wrong;
  K22 (statement counts, test_memory_episode_index_scale_e2e.py);
  K23 forgetting leaves residue;
  K24 person merge duplicates or picks the wrong row;
  K25 a disabled person's episodes stay listed, or a reversible hiding (owner switch,
      contest) archives them for good;
  K26 order depends on insertion order or on the model's output order;
  K27 a selection-step failure breaks Dream or writes half a row;
  K28 index changes move the recovery hash (plus P2 cases 16-19, G16-G19);
  K29 a render failure breaks the turn;
  K30 last year's dates or empty titles break the layout;
  K31 the dropped column is still read;
  K32 ``status`` metrics are wrong.

Failure baselines of the leakage cases (they pass on an implementation that renders
nothing, so the red baseline is the implementation with one filter removed):
  K9  drop the group evidence rule in the selection step: ``x_g`` (SELF, visible in G,
      evidence only in B's private chat) and ``y`` (G evidence plus B-private evidence)
      are the oldest visible episodes, leave the window and appear in G's pool.
  K10 compare evidence by kind instead of space, or accept tool receipts: ``h_in_g`` /
      ``t`` enter G's window and pool.
  K11 skip the render-time evidence re-check: ``m`` stays in the impressions after its
      private evidence row appears.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from alembic import command
from alembic.config import Config
from tests.integration.test_memory_episode_index_e2e import _verify_check
from tests.support.memory_episode_scene import (
    INDEX_HEADER,
    MEMORABLE_SECTION,
    RECENT_SECTION,
    REST_SECTION,
    TIMEZONE,
    UNTITLED,
    EpisodeScene,
    item_lines,
    local_day,
    memorable_reply,
    month_counts,
    pool_of,
    render_record,
    section_ids,
    turn_record,
)
from tests.support.memory_resident_scene import (
    request_text,
    run_cli,
    selection_reply,
)

from yuki.memory.enums import (
    MemoryAuthority,
    MemoryEvidenceRelation,
    MemoryInvalidationReason,
    MemorySourceType,
)
from yuki.persistence.models import MemoryEvidenceModel, MemoryToolReceiptModel

REMENTION_RULE = (
    "后来重提、回忆或复述一件已有记忆的经历，是那条原记忆的重复：merge 到原条，"
    "anchor 取记录更早的那条。"
)
COMPARISON_RULE = (
    "只是重提、回忆或复述另一条候选里那件事的经历，不因此更值得记得；"
    "两条讲同一件事时选记录更早的那条。"
)
_OWNER = {
    "space:G": ("G", "B"),
    "space:H": ("H", "C"),
    "private:A": ("A", "A"),
    "private:B": ("B", "B"),
    "private:B2": ("B2", "B2"),
}


@pytest.fixture
async def scene(database, tmp_path):
    return await EpisodeScene(database, tmp_path).build()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _at(day: str, hour: int = 12) -> datetime:
    year, month, date = (int(part) for part in day.split("-"))
    return datetime(year, month, date, hour, tzinfo=ZoneInfo(TIMEZONE))


async def _source(scene: EpisodeScene, key: str) -> int:
    """One fixture chat event in ``key`` that every filled episode cites."""
    name = f"source:{key}"
    if name not in scene.events:
        await scene.event(key, _OWNER[key][1], f"{key} 里的一段日常聊天", key=name)
    return scene.events[name]


async def _fill(
    scene: EpisodeScene,
    key: str,
    count: int,
    *,
    start: str,
    prefix: str,
    owner: str | None = None,
    visibility: str = "global",
    pad: int = 0,
    step_hours: int = 24,
) -> list[int]:
    """``count`` episodes visible in ``key``, one per day from ``start`` (local noon)."""
    evidence = (await _source(scene, "space:G" if owner == "B@G" else key),)
    base = _at(start)
    ids = []
    for index in range(count):
        name = f"{prefix}{index:03d}"
        ids.append(
            await scene.episode(
                name,
                owner or _OWNER[key][0],
                f"{name}：{key} 发生的第 {index} 件事" + "，细节" * pad,
                title=f"经历 {name}",
                evidence=evidence,
                visibility=visibility,
                valid_from=base + timedelta(hours=step_hours * index),
            )
        )
    return ids


def _keep(*wanted: int):
    """Comparison rule for the scripted model: keep exactly ``wanted`` (if offered)."""
    return lambda pool: [item for item in pool if item in wanted]


async def _run(scene: EpisodeScene) -> tuple[int, list[list[int | None]]]:
    """One real Dream run; returns its comparison calls and their pools."""
    start = len(scene.memorable_requests)
    await scene.advance()
    return len(scene.memorable_requests) - start, scene.pooled_ids(start)


async def _invalidate(scene: EpisodeScene, fact_id: int) -> None:
    assert await scene.chat._memories.invalidate_fact(
        fact_id, reason=MemoryInvalidationReason.USER_RETRACTED, actor_user_id="10001"
    )


async def _foreign_evidence(scene: EpisodeScene, fact_id: int, event_id: int) -> None:
    """A direct evidence row write: proves render-time filters stand alone."""
    async with scene.database.immediate_session() as writer:
        writer.add(
            MemoryEvidenceModel(
                fact_id=fact_id,
                event_id=event_id,
                source_speaker_user_id="10002",
                relation=MemoryEvidenceRelation.AGENT_REFLECTION.value,
                authority=MemoryAuthority.AGENT_REFLECTION.value,
                confidence=0.9,
                excerpt="私聊里的一句话",
                created_at=datetime.now(UTC),
            )
        )


async def _receipt_evidence(scene: EpisodeScene, fact_id: int, trigger_event_id: int) -> None:
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
        writer.add(
            MemoryEvidenceModel(
                fact_id=fact_id,
                tool_receipt_id=receipt.id,
                source_speaker_user_id="80001",
                relation=MemoryEvidenceRelation.AGENT_REFLECTION.value,
                authority=MemoryAuthority.AGENT_REFLECTION.value,
                confidence=0.9,
                excerpt="群里看过的一段聊天记录",
                created_at=now,
            )
        )


def _parts(rendered) -> dict[str, list[int]]:
    return {
        "memorable": list(rendered.memorable_fact_ids),
        "window": list(rendered.window_fact_ids),
        "tail": list(rendered.recent_fact_ids),
    }


def _turn_parts(turn) -> dict[str, list[int]]:
    return {
        "memorable": section_ids(turn.index, MEMORABLE_SECTION),
        "window": section_ids(turn.index, RECENT_SECTION),
        "tail": turn.recent_ids,
    }


def _shown(rendered) -> set[int]:
    return {*rendered.memorable_fact_ids, *rendered.window_fact_ids, *rendered.recent_fact_ids}


def _pools(scene: EpisodeScene, start: int = 0) -> list[list[dict]]:
    return [pool_of(request) for request in scene.memorable_requests[start:]]


async def _status(scene: EpisodeScene) -> dict:
    result = await run_cli(scene.database.url, "memory", "episodes", "status")
    assert result.returncode == 0, result
    return json.loads(result.stdout)


def _status_row(payload: dict, owner_id: str) -> dict:
    rows = [item for item in payload["indexes"] if item["owner_id"] == owner_id]
    assert len(rows) == 1, payload
    return rows[0]


def _skip_episode_step(monkeypatch) -> None:
    """Inject a fault into the episode step of the Dream selection (K12, K27)."""
    from yuki.memory.episode_selection import EpisodeSelection

    async def broken(self, *args, **kwargs):
        raise RuntimeError("injected episode selection fault")

    monkeypatch.setattr(EpisodeSelection, "run", broken)


async def _seeded(scene: EpisodeScene, key: str, count: int, keep: tuple[int, ...] = (), **fields):
    """``count`` episodes, one Dream run; the scripted model keeps leavers at ``keep``."""
    ids = await _fill(scene, key, count, start="2026-09-01", prefix=key[-1].lower(), **fields)
    scene.memorable_responder = scene.choose(_keep(*(ids[index] for index in keep)))
    calls, _pools_seen = await _run(scene)
    assert calls == (1 if count > 15 else 0)
    row = await scene.index_row(key)
    assert row is not None
    assert [item["fact_id"] for item in row["memorable"]] == [ids[index] for index in keep]
    assert row["recent"] == ids[-15:]
    return ids


# --------------------------------------------------------------------------- K1
async def test_k01_new_episodes_stay_in_the_tail(scene):
    """K1: new episodes after the nightly run change only the tail; two turns, one system."""
    from yuki.memory.self_reflection.service import _INSTRUCTION as REFLECTION_INSTRUCTION

    ids = await _fill(scene, "space:G", 3, start="2026-11-01", prefix="g")
    calls, _ = await _run(scene)
    row = await scene.index_row("space:G")
    await scene.event("space:G", "B", "今晚群里一起玩了猜歌游戏", key="n1")
    await scene.event("space:G", "A", "群里又讨论周末去哪儿玩", key="n2")
    start = len(scene.provider.requests)
    created = await scene.reflect(
        {
            "space:G": [
                {"title": "群里猜歌", "content": "我和群友玩了猜歌。", "cites": ["猜歌"]},
                {"title": "讨论周末", "content": "群里讨论周末去哪。", "cites": ["周末去哪"]},
            ]
        }
    )
    reflection = [
        request
        for request in scene.provider.requests[start:]
        if '"events"' in str(request.messages[-1].content or "")
    ]
    new = created["space:G"]
    first = await scene.episode_turn("A", "第一轮", group="G")
    second = await scene.episode_turn("A", "第二轮", group="G")

    assert calls == 0 and row is not None and row["recent"] == ids
    assert first.system == second.system
    assert _turn_parts(first) == {"memorable": [], "window": ids, "tail": new}
    assert first.recent == second.recent
    assert await scene.index_row("space:G") == row
    # The self-reflection instruction constant reaches the request; one batch still
    # writes several episodes (the instruction wording itself is replayed offline).
    expected = REFLECTION_INSTRUCTION.format(
        timezone=scene.settings.memory_self_reflection_timezone
    ).strip()
    assert reflection and all(expected in request_text(item) for item in reflection)
    assert len(new) == 2
    contract = await scene.contract()
    scene.write_memorable(
        "K01",
        {
            "row": row,
            "first": turn_record(first, contract),
            "second": turn_record(second, contract),
            "system_sha256": _sha(first.system),
        },
    )


# ----------------------------------------------------------------- K2 K3 K5
async def test_k02_k03_k05_leavers_compared_once_never_return(scene):
    """K2/K3/K5: one comparison per leaver, a mixed pool of four keys, no backfill."""
    ids = await _seeded(scene, "space:G", 17, keep=(1,))
    first_pool = _pools(scene)
    more = await _fill(scene, "space:G", 3, start="2026-10-01", prefix="h")
    scene.memorable_responder = scene.choose(_keep(ids[1], ids[3]))
    calls, pools = await _run(scene)
    second = await scene.index_row("space:G")
    second_pool = _pools(scene, 1)

    # K2: the pool is exactly the current impression plus the three leavers.
    assert calls == 1 and pools == [[ids[1], ids[2], ids[3], ids[4]]]
    assert [item["fact_id"] for item in second["memorable"]] == [ids[1], ids[3]]
    assert second["recent"] == [*ids[5:], *more]
    # A run without new episodes: no call, ``through`` still moves, revision stays.
    await scene.fact("plain", "self", "我喜欢安静地看书")
    calls, _ = await _run(scene)
    third = await scene.index_row("space:G")
    assert calls == 0
    assert third["revision"] == second["revision"]
    assert third["through"] == await scene.max_fact_id() > second["through"]
    assert {**third, "through": 0} == {**second, "through": 0}

    # K3: a window entry is invalidated: no backfill; lost leavers never come back.
    await _invalidate(scene, ids[6])
    calls, _ = await _run(scene)
    fourth = await scene.index_row("space:G")
    assert calls == 0 and fourth["recent"] == [item for item in third["recent"] if item != ids[6]]
    later = await _fill(scene, "space:G", 2, start="2026-10-20", prefix="k")
    scene.memorable_responder = scene.choose(_keep(ids[1], ids[3]))
    calls, pools = await _run(scene)
    fifth = await scene.index_row("space:G")
    turn = await scene.episode_turn("A", "最近群里怎么样", group="G")

    assert calls == 1 and pools == [[ids[1], ids[3], ids[5]]]
    assert ids[2] not in pools[0] and ids[4] not in pools[0]
    assert fifth["recent"] == [*ids[7:], *more, *later]
    assert section_ids(turn.index, RECENT_SECTION) == fifth["recent"]
    # ids[0], ids[2] (lost), ids[4] (lost), ids[5] (lost): visible, counted, never listed.
    assert month_counts(turn.index) == {"2026-09": 4}
    assert index_lines_ok(turn.index)

    # K5: one list in date order; every candidate has exactly four keys.
    for pool in (*first_pool, *second_pool, *_pools(scene, 1)):
        assert [item["ref"] for item in pool] == [f"c_{n}" for n in range(1, len(pool) + 1)]
        assert all(set(item) == {"ref", "title", "content", "date"} for item in pool)
        assert [item["date"] for item in pool] == sorted(item["date"] for item in pool)
    for request in scene.memorable_requests:
        payload = json.loads(request.messages[-1].content)
        assert set(payload) == {"candidates"}
        text = str(request.messages[-1].content)
        for marker in ("importance", "since", "source", "count", "kept", "new_", "existing"):
            assert marker not in text
        assert COMPARISON_RULE in request_text(request)
    scene.write_memorable(
        "K02-K03-K05",
        {
            "facts": {"ids": ids, "more": more, "later": later},
            "rows": [second, third, fourth, fifth],
            "pools": [first_pool, second_pool, *_pools(scene, 2)],
            "turn": turn_record(turn, await scene.contract()),
        },
    )


def index_lines_ok(index: str) -> bool:
    """Header, sections in order, nothing else."""
    lines = index.split("\n")
    titles = [line for line in lines if line.startswith("[")]
    order = [MEMORABLE_SECTION, RECENT_SECTION, REST_SECTION]
    return lines[0].startswith(INDEX_HEADER) and titles == [t for t in order if t in titles]


# ------------------------------------------------------------------ K4 K32
async def test_k04_k32_replacement_happens_and_status_counts_it(scene):
    """K4: the model swaps one impression; K32: status reflects it exactly."""
    ids = await _seeded(scene, "space:G", 17, keep=(0, 1))
    await scene.age_memorable("space:G", days=30)
    before = await scene.index_row("space:G")
    started = datetime.now(UTC)
    new = await _fill(scene, "space:G", 1, start="2026-10-01", prefix="n")
    scene.memorable_responder = scene.choose(_keep(ids[1], ids[2]))
    calls, pools = await _run(scene)
    after = await scene.index_row("space:G")
    turn = await scene.episode_turn("A", "群里有什么值得记得的", group="G")
    status = await _status(scene)
    row = _status_row(status, scene.spaces["G"])

    entries = {item["fact_id"]: item for item in after["memorable"]}
    old = {item["fact_id"]: item for item in before["memorable"]}
    assert calls == 1 and pools == [[ids[0], ids[1], ids[2]]]
    assert list(entries) == [ids[1], ids[2]]
    assert entries[ids[1]] == old[ids[1]]
    assert entries[ids[2]]["source"] == "dream"
    assert datetime.fromisoformat(entries[ids[2]]["since"]) >= started
    assert after["revision"] == before["revision"] + 1
    assert _turn_parts(turn)["memorable"] == [ids[1], ids[2]]
    assert month_counts(turn.index) == {"2026-09": 1}  # ids[0], replaced
    assert after["recent"] == [*ids[3:], *new]
    # K32
    assert row["swapped_in_7d"] == 1
    assert row["memorable"] == 2 and row["locked"] is False
    assert row["recent"] == 15 and row["pending"] == 0
    assert row["through_fact_id"] == after["through"]
    assert row["earliest_memorable_month"] == "2026-09"
    assert row["earliest_memorable_since"] == old[ids[1]]["since"]
    assert row["behind_watermark"] == max(
        0, status["watermark"]["through_fact_id"] - after["through"]
    )
    scene.write_memorable(
        "K04-K32",
        {
            "before": before,
            "after": after,
            "pools": _pools(scene),
            "turn": turn_record(turn, await scene.contract()),
            "status_row": row,
        },
    )


# ------------------------------------------------------------------------ K5
async def test_k05_pool_interleaves_impressions_and_leavers_by_date(scene):
    """K5: a back-dated leaver sorts before a current impression; no old/new marks."""
    ids = await _seeded(scene, "space:G", 16, keep=(0,))
    early = await _fill(scene, "space:G", 1, start="2026-08-15", prefix="e")
    late = await _fill(scene, "space:G", 1, start="2026-10-01", prefix="l")
    scene.memorable_responder = scene.choose(_keep(ids[0]))
    calls, pools = await _run(scene)
    pool = _pools(scene, 1)[0]

    # ids[1] leaves too: 15 window + early + late = 17 -> early and ids[1] leave.
    assert calls == 1 and pools == [[early[0], ids[0], ids[1]]]
    assert all(set(item) == {"ref", "title", "content", "date"} for item in pool)
    assert [item["date"] for item in pool] == ["2026-08-15", "2026-09-01", "2026-09-02"]
    assert (await scene.index_row("space:G"))["recent"][-1] == late[0]
    scene.write_memorable("K05", {"pool": pool, "row": await scene.index_row("space:G")})


# ------------------------------------------------------------------------ K6
async def test_k06_invalid_outputs_void_the_whole_result(scene, caplog):
    """K6: unknown, duplicate, too many, empty reason: the row does not move at all."""
    ids = await _seeded(scene, "space:G", 16, keep=(0,))
    new = await _fill(scene, "space:G", 1, start="2026-10-01", prefix="n")
    stable = await scene.index_row("space:G")
    caplog.set_level(logging.INFO)
    outcomes = []
    for code, reply in (
        ("unknown_alias", memorable_reply(["c_1", "c_9"])),
        ("duplicate_alias", memorable_reply(["c_1", "c_1"])),
        ("too_many", memorable_reply([f"c_{n}" for n in range(1, 12)])),
        ("empty_reason", memorable_reply(["c_1"], reason="  ")),
    ):
        scene.memorable_responder = lambda request, reply=reply: reply
        caplog.clear()
        calls, pools = await _run(scene)
        turn = await scene.episode_turn("A", f"看看目录 {code}", group="G")
        logged = [
            record.getMessage()
            for record in caplog.records
            if "memory_episode_memorable_rejected" in record.getMessage()
        ]
        assert calls == 1 and pools == [[ids[0], ids[1]]], code
        assert await scene.index_row("space:G") == stable, code
        assert turn.recent_ids == new, code
        assert len(logged) == 1 and f"code={code}" in logged[0], (code, logged)
        outcomes.append({"code": code, "tail": turn.recent_ids})
    scene.memorable_responder = scene.choose(_keep(ids[0], ids[1]))
    calls, pools = await _run(scene)
    accepted = await scene.index_row("space:G")

    assert calls == 1 and pools == [[ids[0], ids[1]]]
    assert [item["fact_id"] for item in accepted["memorable"]] == [ids[0], ids[1]]
    assert accepted["recent"] == [*ids[2:], *new]
    assert (await scene.render("space:G")).recent_fact_ids == ()
    scene.write_memorable("K06", {"stable": stable, "outcomes": outcomes, "accepted": accepted})


# ------------------------------------------------------------------------ K7
async def test_k07_over_budget_one_call_with_a_prefix(database, tmp_path):
    """K7: leavers beyond the input budget wait at the front of the list, unrendered."""
    scene = await EpisodeScene(database, tmp_path).build(memory_dream_max_input_characters=800)
    ids = await _fill(scene, "space:G", 25, start="2026-09-01", prefix="g", pad=30)
    scene.memorable_responder = scene.choose(lambda pool: pool[:2])
    rows: list[dict] = []
    offered: list[list[int | None]] = []
    first_offers: list[int] = []  # each leaver at the call that first offered it
    archived: set[int] = set()
    while True:
        calls, pools = await _run(scene)
        row = await scene.index_row("space:G")
        rows.append(row)
        if calls == 0:
            break
        assert calls == 1
        pool = pools[0]
        offered.append(pool)
        assert not set(pool) & archived  # a lost candidate is never offered again
        kept = {item["fact_id"] for item in row["memorable"]}
        archived |= {item for item in pool if item not in kept}
        first_offers.extend(item for item in pool if item not in first_offers)
        rendered = await scene.render("space:G")
        assert list(rendered.window_fact_ids) == ids[-15:]
        assert row["recent"][:-15] == [item for item in ids[:10] if item not in first_offers]
        assert not set(row["recent"][:-15]) & set(rendered.index_fact_ids)
        assert len(offered) < 10
    status = _status_row(await _status(scene), scene.spaces["G"])

    assert len(offered) >= 3  # the budget really split the leavers
    assert first_offers == ids[:10]  # every leaver offered, in date order
    assert rows[-1]["recent"] == ids[-15:] and status["pending"] == 0
    scene.write_memorable("K07", {"offered": offered, "rows": rows, "status": status})


# ------------------------------------------------------------------------ K8
async def test_k08_cap_leaves_unreached_rows_untouched(database, tmp_path):
    """K8: one slot left after the core set: one audience compared, two untouched."""
    scene = await EpisodeScene(database, tmp_path).build(memory_resident_max_selections_per_run=2)
    g = await _source(scene, "space:G")
    await scene.fact("core1", "G", "群里约定每周五晚上一起打游戏", evidence=(g,))
    scene.selection_responder = lambda request: selection_reply(["new_1"])
    for key in ("space:G", "space:H", "private:A"):
        await _fill(scene, key, 15, start="2026-09-01", prefix=key[-1].lower())
    calls, _ = await _run(scene)
    before = {key: await scene.index_row(key) for key in ("space:G", "space:H", "private:A")}
    await scene.fact("core2", "G", "群里的人都喜欢猫", evidence=(g,))
    new = {
        key: await _fill(scene, key, 1, start="2026-10-01", prefix=f"n{key[-1].lower()}")
        for key in before
    }
    owners = {key: scene.index_owner(key)[1] for key in before}
    first = min(before, key=lambda key: (before[key]["through"], owners[key]))
    scene.memorable_responder = scene.choose(lambda pool: [])
    core_requests = len(scene.selection_requests)
    calls, _ = await _run(scene)
    after = {key: await scene.index_row(key) for key in before}
    renders = {key: await scene.render(key) for key in before}

    assert calls == 1
    assert len(scene.selection_requests) == core_requests + 1
    for key in before:
        if key == first:
            assert after[key]["revision"] == before[key]["revision"] + 1
            assert renders[key].recent_fact_ids == ()
        else:
            assert after[key] == before[key], key
            assert list(renders[key].recent_fact_ids) == new[key], key
    # Ties on ``through`` fall to the owner ID (a random UUID): record what holds for
    # whichever audience was served, so reruns stay byte-identical.
    scene.write_memorable(
        "K08",
        {
            "comparison_calls": calls,
            "core_set_calls": len(scene.selection_requests) - core_requests,
            "served": [
                {
                    "revision_delta": after[first]["revision"] - before[first]["revision"],
                    "tail": len(renders[first].recent_fact_ids),
                }
            ],
            "untouched": sorted(
                [
                    {
                        "row_unchanged": after[key] == before[key],
                        "tail": len(renders[key].recent_fact_ids),
                    }
                    for key in before
                    if key != first
                ],
                key=lambda item: json.dumps(item, sort_keys=True),
            ),
        },
    )


# ------------------------------------------------------------------------ K9
async def test_k09_private_origin_never_reaches_group_input_or_index(scene, tmp_path):
    """K9: B's private SELF episode and a G episode with B-private evidence stay out of G."""
    b1 = await scene.event("private:B", "B", "我其实很怕狗，别在群里说", key="b1")
    g = await _source(scene, "space:G")
    x_g = await scene.episode(
        "x_g",
        "self",
        "听说群里有人怕狗",
        title="群里有人怕狗",
        visibility="space:G",
        foreign_evidence=(b1,),
        valid_from=_at("2026-08-01"),
    )
    y = await scene.episode(
        "y",
        "G",
        "周末群友一起爬山",
        title="群里爬山",
        evidence=(g,),
        foreign_evidence=(b1,),
        valid_from=_at("2026-08-02"),
    )
    b_self = await scene.episode(
        "b_self",
        "self",
        "B 私下告诉我他怕狗",
        title="B 怕狗",
        visibility="private:B",
        evidence=(b1,),
        valid_from=_at("2026-08-03"),
    )
    ids = await _fill(scene, "space:G", 16, start="2026-09-01", prefix="g")
    scene.memorable_responder = scene.choose(lambda pool: pool)
    calls, pools = await _run(scene)
    group = await scene.episode_turn("A", "群里最近怎么样", group="G")
    private = await scene.render("private:B")
    row = await scene.index_row("space:G")

    assert calls == 1 and pools == [[ids[0]]]
    leaked = {x_g, y, b_self}
    assert not leaked & {*row["recent"], *(item["fact_id"] for item in row["memorable"])}
    assert not leaked & set(group.index_ids + group.recent_ids)
    assert month_counts(group.index) == {} and row["months"] == {}
    assert b_self in private.window_fact_ids
    verified = await _verify_check(scene, tmp_path)
    assert verified["checked_entries"] >= 16
    scene.write_memorable(
        "K09",
        {
            "independent_check": verified,
            "facts": {"x_g": x_g, "y": y, "b_self": b_self},
            "pools": _pools(scene),
            "row": row,
            "group": turn_record(group, await scene.contract()),
            "private_B": render_record(private),
        },
    )


# ----------------------------------------------------------------------- K10
async def test_k10_other_group_and_tool_receipt_episodes_stay_out(scene, tmp_path):
    """K10: H's episodes and a G episode with a tool-receipt evidence row stay out of G."""
    h = await _source(scene, "space:H")
    g = await _source(scene, "space:G")
    g2 = await scene.event("space:G", "A", "帮我翻一下上周的聊天记录", key="g2")
    h_ep = await scene.episode(
        "h_ep",
        "H",
        "H 群定了线下聚餐",
        title="H 群聚餐",
        evidence=(h,),
        valid_from=_at("2026-08-01"),
    )
    h_self = await scene.episode(
        "h_self",
        "self",
        "我在 H 群帮忙记人数",
        title="帮 H 群记人数",
        visibility="space:H",
        evidence=(h,),
        valid_from=_at("2026-08-02"),
    )
    t = await scene.episode(
        "t",
        "self",
        "我翻了聊天记录帮群里回顾",
        title="帮群里回顾",
        visibility="space:G",
        evidence=(g,),
        valid_from=_at("2026-08-03"),
    )
    await _receipt_evidence(scene, t, g2)
    h_in_g = await scene.episode(
        "h_in_g",
        "H",
        "H 群的人来 G 群打招呼",
        title="H 群来打招呼",
        foreign_evidence=(g,),
        valid_from=_at("2026-08-04"),
    )
    ids = await _fill(scene, "space:G", 16, start="2026-09-01", prefix="g")
    scene.memorable_responder = scene.choose(lambda pool: pool)
    calls, pools = await _run(scene)
    group = await scene.episode_turn("B", "有活动吗", group="G")
    other = await scene.render("space:H")
    row = await scene.index_row("space:G")

    leaked = {h_ep, h_self, t, h_in_g}
    assert calls == 1 and pools == [[ids[0]]]
    assert not leaked & {*row["recent"], *(item["fact_id"] for item in row["memorable"])}
    assert not leaked & set(group.index_ids + group.recent_ids) and row["months"] == {}
    assert list(other.window_fact_ids) == [h_ep, h_self]
    verified = await _verify_check(scene, tmp_path)
    scene.write_memorable(
        "K10",
        {
            "independent_check": verified,
            "facts": {"h_ep": h_ep, "h_self": h_self, "t": t, "h_in_g": h_in_g},
            "row": row,
            "group": turn_record(group, await scene.contract()),
            "group_H": render_record(other),
        },
    )


# ----------------------------------------------------------------------- K11
async def test_k11_changed_evidence_hides_now_and_drops_at_the_run(scene):
    """K11: an impression gains a private evidence row: hidden next turn, dropped at night."""
    ids = await _seeded(scene, "space:G", 16, keep=(0,))
    m = ids[0]
    b1 = await scene.event("private:B", "B", "群里那件事其实是我私下说的", key="b1")
    before = await scene.episode_turn("A", "看看长期印象", group="G")
    await _foreign_evidence(scene, m, b1)
    after = await scene.episode_turn("A", "再看看长期印象", group="G")
    still = await scene.index_row("space:G")
    calls, _ = await _run(scene)
    dropped = await scene.index_row("space:G")

    assert _turn_parts(before)["memorable"] == [m]
    assert m not in after.index_ids + after.recent_ids
    assert [item["fact_id"] for item in still["memorable"]] == [m]
    assert calls == 0 and dropped["memorable"] == []
    assert m not in dropped["recent"] and dropped["months"] == {}
    scene.write_memorable(
        "K11",
        {
            "before": turn_record(before, await scene.contract()),
            "after": turn_record(after, await scene.contract()),
            "row": dropped,
        },
    )


# ----------------------------------------------------------------------- K12
def _dream_action(
    scene: EpisodeScene,
    operation: str,
    sources: tuple[int, ...],
    *,
    anchor: int | None = None,
    content: str = "两件事合在一起看是同一段经历。",
):
    """A scripted Dream reply that addresses facts by their stored text, not position."""

    def respond(request):
        refs = scene.dream_refs(request)
        action: dict = {"operation": operation, "source_refs": [refs[item] for item in sources]}
        if anchor is not None:
            action["anchor_ref"] = refs[anchor]
        if operation == "synthesize":
            action.update(content=content, importance=4)
        if operation == "recompose":
            action["outputs"] = [
                {
                    "focus": "重组后的经历",
                    "source_refs": action["source_refs"],
                    "content": content,
                    "importance": 4,
                }
            ]
        return json.dumps({"actions": [action]}, ensure_ascii=False)

    return respond


@pytest.mark.parametrize("operation", ["merge", "synthesize", "recompose"])
async def test_k12_impression_successor_takes_its_place(scene, monkeypatch, operation):
    """K12: render shows the successor; the run swaps the ID, keeps ``since``, no call."""
    ids = await _seeded(scene, "space:G", 17, keep=(1,))
    x, archived = ids[1], ids[0]
    before = await scene.index_row("space:G")
    scene.dream_responder = _dream_action(
        scene,
        operation,
        (x, archived),
        anchor={"merge": archived, "synthesize": x, "recompose": None}[operation],
    )
    with monkeypatch.context() as patch:
        _skip_episode_step(patch)
        run = await scene.dream_run(((f"g{1:03d}", f"g{0:03d}"),))
    assert run.status.value == "completed"
    created = await scene.fact_ids_after(ids[-1])
    successor = archived if operation == "merge" else created[-1]
    shown = await scene.render("space:G")
    stale = await scene.index_row("space:G")
    calls, _ = await _run(scene)
    after = await scene.index_row("space:G")

    assert list(shown.memorable_fact_ids) == [successor]
    assert stale == before
    assert calls == 0
    assert after["memorable"] == [{**before["memorable"][0], "fact_id": successor}]
    assert after["recent"] == before["recent"]
    scene.write_memorable(
        f"K12-{operation}",
        {"before": before, "render": render_record(shown), "after": after},
    )


# ----------------------------------------------------------------------- K13
async def test_k13_emptied_slot_is_not_backfilled(scene):
    """K13: an invalidated impression without successor leaves an empty slot."""
    ids = await _seeded(scene, "space:G", 17, keep=(0, 1))
    await _invalidate(scene, ids[1])
    hidden = await scene.render("space:G")
    calls, _ = await _run(scene)
    emptied = await scene.index_row("space:G")
    await _fill(scene, "space:G", 1, start="2026-10-01", prefix="n")
    scene.memorable_responder = scene.choose(lambda pool: pool)
    later_calls, pools = await _run(scene)
    refilled = await scene.index_row("space:G")

    assert list(hidden.memorable_fact_ids) == [ids[0]]
    assert calls == 0 and [item["fact_id"] for item in emptied["memorable"]] == [ids[0]]
    assert later_calls == 1 and pools == [[ids[0], ids[2]]]
    assert [item["fact_id"] for item in refilled["memorable"]] == [ids[0], ids[2]]
    scene.write_memorable("K13", {"emptied": emptied, "pools": _pools(scene), "refilled": refilled})


# ----------------------------------------------------------------------- K14
async def _remention(scene: EpisodeScene, x: int) -> int:
    await scene.event("space:G", "B", "还记得九月一号那次一起做的事吗", key="remention")
    created = await scene.reflect(
        {
            "space:G": [
                {
                    "title": "群里重提旧事",
                    "content": "群友又提起九月一号那次的事。",
                    "cites": ["九月一号那次"],
                }
            ]
        }
    )
    (y,) = created["space:G"]
    scene.facts["y"] = y
    scene.facts["x"] = x
    return y


@pytest.mark.parametrize(
    "variant",
    ["a-merge-same-night", "b-recompose-same-night", "c-merge-later", "d-recompose-later"],
)
async def test_k14_remention_never_brings_the_old_event_back(scene, variant):
    """K14: x archived, y re-mentions x; four Dream paths, none returns x or y to a pool.

    The fixture episodes are SELF episodes visible in G, like the re-mention the real
    self-reflection writes, so Dream may cluster them (same scope and visibility).
    """
    ids = await _seeded(scene, "space:G", 17, keep=(1,), owner="self", visibility="space:G")
    x = ids[0]
    y = await _remention(scene, x)
    operation = "merge" if "merge" in variant else "recompose"
    if variant.startswith(("c", "d")):
        scene.memorable_responder = scene.choose(_keep(ids[1]))
        calls, pools = await _run(scene)  # y is new: it enters, ids[2] leaves
        assert calls == 1 and pools == [[ids[1], ids[2]]]
        assert (await scene.index_row("space:G"))["recent"][-1] == y
    before = await scene.index_row("space:G")
    start = len(scene.memorable_requests)
    # merge: y is the re-mention, merged into the original x (anchor x).
    scene.dream_responder = _dream_action(
        scene,
        operation,
        (x, y),
        anchor=x if operation == "merge" else None,
        content="九月一号那次的事，后来群里又提起过。",
    )
    dream_start = len(scene.dream_requests)
    run = await scene.dream_run((("x", "y"),))
    after = await scene.index_row("space:G")
    calls_after = len(scene.memorable_requests) - start
    again, _ = await _run(scene)
    final = await scene.index_row("space:G")
    outputs = [item for item in await scene.fact_ids_after(y)]

    assert run.status.value == "completed"
    assert REMENTION_RULE in request_text(scene.dream_requests[dream_start])
    assert calls_after == 0 and again == 0
    listed = {*after["recent"], *(item["fact_id"] for item in after["memorable"])}
    assert x not in listed and y not in listed and not set(outputs) & listed
    assert after["recent"] == [item for item in before["recent"] if item != y]
    assert after["memorable"] == before["memorable"] and final == {
        **after,
        "through": final["through"],
    }
    scene.write_memorable(f"K14-{variant}", {"before": before, "after": after, "outputs": outputs})


# ----------------------------------------------------------------------- K33
async def test_k33_successor_of_an_impression_is_not_a_new_episode(scene):
    """K33: an impression's successor that is also new stays only in the impressions.

    Dream's real anchor is the smallest source ID (``DreamService._anchor``), whatever
    the model names, so "anchored on the new y" is reached the way production can:
    (1) the owner puts today's x into the impressions and unlocks; Dream synthesizes
    today's older y with x: the product's root is y (new) and it succeeds x; (2) an
    impression with a larger ID than a window entry w is merged into w.
    """
    ids = await _fill(scene, "space:G", 17, start="2026-09-01", prefix="g")
    xb = (await _fill(scene, "space:G", 1, start="2026-08-20", prefix="b"))[0]
    scene.memorable_responder = scene.choose(_keep(ids[0], xb))
    await _run(scene)
    seeded = await scene.index_row("space:G")
    assert [item["fact_id"] for item in seeded["memorable"]] == [xb, ids[0]]
    g = await _source(scene, "space:G")
    y = await scene.episode("y", "G", "今天下午群里约了下次聚会", title="约聚会", evidence=(g,))
    x = await scene.episode("x", "G", "今天群里定下了聚会地点", title="定地点", evidence=(g,))
    conversation = scene.conversations["space:G"]
    owner_set = await run_cli(
        scene.database.url,
        "memory",
        "episodes",
        "set",
        "--conversation",
        conversation,
        "--facts",
        f"{xb},{ids[0]},{x}",
    )
    unlocked = await run_cli(
        scene.database.url, "memory", "episodes", "unlock", "--conversation", conversation
    )
    before = await scene.index_row("space:G")
    since = {item["fact_id"]: item["since"] for item in before["memorable"]}
    scene.dream_responder = _dream_action(
        scene, "synthesize", (y, x), anchor=y, content="今天群里约了下次聚会，也定了地点。"
    )
    start = len(scene.memorable_requests)
    await scene.dream_run((("y", "x"),))
    z = max(await scene.fact_ids_after(x))
    synthesized = await scene.index_row("space:G")
    synth_calls = len(scene.memorable_requests) - start
    tail = await scene.render("space:G")

    assert owner_set.returncode == 0 and unlocked.returncode == 0, (owner_set, unlocked)
    assert synth_calls == 0
    assert {item["fact_id"]: item["since"] for item in synthesized["memorable"]} == {
        xb: since[xb],
        ids[0]: since[ids[0]],
        z: since[x],
    }
    assert z not in synthesized["recent"] and synthesized["recent"] == before["recent"]
    assert z not in tail.recent_fact_ids and list(tail.memorable_fact_ids).count(z) == 1
    # (2) xb is merged into window entry w (smaller ID): w replaces xb, leaves the window.
    w = before["recent"][3]
    scene.facts["w"] = w
    scene.facts["xb"] = xb
    scene.dream_responder = _dream_action(scene, "merge", (xb, w), anchor=w)
    start = len(scene.memorable_requests)
    await scene.dream_run((("xb", "w"),))
    merged = await scene.index_row("space:G")
    turn = await scene.episode_turn("A", "长期印象", group="G")

    assert len(scene.memorable_requests) == start
    assert {item["fact_id"]: item["since"] for item in merged["memorable"]} == {
        w: since[xb],
        ids[0]: since[ids[0]],
        z: since[x],
    }
    assert merged["recent"] == [item for item in before["recent"] if item != w]
    assert len(set(turn.index_ids)) == len(turn.index_ids)
    assert w in _turn_parts(turn)["memorable"] and w not in _turn_parts(turn)["window"]
    scene.write_memorable(
        "K33",
        {
            "before": before,
            "synthesized": synthesized,
            "merged": merged,
            "turn": turn_record(turn, await scene.contract()),
        },
    )


# ----------------------------------------------------------------------- K15
async def test_k15_same_night_merge_lists_the_product_once(scene):
    """K15: a1, a2 written today, synthesized the same night into z: z once, not in the tail."""
    ids = await _fill(scene, "space:G", 3, start="2026-11-01", prefix="g")
    await _run(scene)
    g = await _source(scene, "space:G")
    await scene.episode("a1", "G", "今天下午群里一起看球", title="一起看球", evidence=(g,))
    await scene.episode("a2", "G", "看球时大家点了外卖", title="点外卖", evidence=(g,))
    a1, a2 = scene.facts["a1"], scene.facts["a2"]
    scene.dream_responder = _dream_action(
        scene, "synthesize", (a1, a2), anchor=a1, content="今天下午群里一起看球，还点了外卖。"
    )
    await scene.dream_run((("a1", "a2"),))
    z = max(await scene.fact_ids_after(0))
    row = await scene.index_row("space:G")
    turn = await scene.episode_turn("A", "今天群里", group="G")

    assert row["recent"] == [*ids, z]
    assert turn.recent_ids == [] and section_ids(turn.index, RECENT_SECTION).count(z) == 1
    scene.write_memorable("K15", {"row": row, "turn": turn_record(turn, await scene.contract())})


# ----------------------------------------------------------------------- K16
async def test_k16_person_group_episode_compared_per_audience(scene):
    """K16: B's episode in G belongs to G and to B's private chat; each compares alone."""
    pg = await scene.episode(
        "pg",
        "B@G",
        "B 在群里组织了一次桌游",
        title="B 组织桌游",
        evidence=(await _source(scene, "space:G"),),
        valid_from=_at("2026-08-01"),
    )
    g_ids = await _fill(scene, "space:G", 16, start="2026-09-01", prefix="g")
    await _fill(scene, "private:B", 15, start="2026-09-01", prefix="b")

    def respond(request):
        pool = scene.pool(request)
        offered = [item["fact_id"] for item in pool]
        keep = [pg] if g_ids[0] in offered else []
        return memorable_reply([item["ref"] for item in pool if item["fact_id"] in keep])

    scene.memorable_responder = respond
    calls, pools = await _run(scene)
    group = await scene.index_row("space:G")
    private = await scene.index_row("private:B")

    assert calls == 2
    assert sorted(pools, key=len) == [[pg], [pg, g_ids[0]]]
    assert [item["fact_id"] for item in group["memorable"]] == [pg]
    assert private["memorable"] == [] and private["months"] == {"2026-08": 1}
    scene.write_memorable("K16", {"pools": _pools(scene), "group": group, "private_B": private})


# ----------------------------------------------------------------------- K17
async def test_k17_k32_lock_archives_without_calls_unlock_restores(scene):
    """K17: ``set`` locks; the window moves and leavers are archived; ``unlock`` compares."""
    ids = await _seeded(scene, "space:G", 16, keep=(0,))
    conversation = scene.conversations["space:G"]
    url = scene.database.url
    set_result = await run_cli(
        url, "memory", "episodes", "set", "--conversation", conversation, "--facts", str(ids[3])
    )
    shown = await run_cli(url, "memory", "episodes", "show", "--conversation", conversation)
    locked = await scene.index_row("space:G")
    new = await _fill(scene, "space:G", 2, start="2026-10-01", prefix="n")
    calls, _ = await _run(scene)
    moved = await scene.index_row("space:G")
    status_locked = _status_row(await _status(scene), scene.spaces["G"])
    unlock = await run_cli(url, "memory", "episodes", "unlock", "--conversation", conversation)
    await _fill(scene, "space:G", 1, start="2026-10-10", prefix="u")
    scene.memorable_responder = scene.choose(_keep(ids[3]))
    unlocked_calls, pools = await _run(scene)

    assert set_result.returncode == 0, set_result
    payload = json.loads(shown.stdout)
    assert payload["locked"] is True
    assert [(item["fact_id"], item["source"]) for item in payload["memorable"]] == [
        (ids[3], "owner")
    ]
    assert locked["locked"] and [item["fact_id"] for item in locked["memorable"]] == [ids[3]]
    assert locked["recent"] == [item for item in ids[1:] if item != ids[3]]
    assert calls == 0
    assert moved["recent"] == [*[item for item in ids[2:] if item != ids[3]], *new]
    assert moved["memorable"] == locked["memorable"]
    assert status_locked["locked"] is True and status_locked["swapped_in_7d"] == 1
    assert status_locked["memorable"] == 1 and status_locked["through_fact_id"] == moved["through"]
    assert unlock.returncode == 0, unlock
    assert unlocked_calls == 1 and pools == [[ids[2], ids[3]]]
    scene.write_memorable(
        "K17",
        {
            "show": payload,
            "locked": locked,
            "moved": moved,
            "status": status_locked,
            "pools": pools,
        },
    )


# ----------------------------------------------------------------------- K18
async def test_k18_owner_change_during_the_call_wins(scene, caplog):
    """K18: the owner sets impressions while the comparison call runs: Dream gives up."""
    ids = await _seeded(scene, "space:G", 16, keep=(0,))
    await _fill(scene, "space:G", 1, start="2026-10-01", prefix="n")
    conversation = scene.conversations["space:G"]
    pause = len(scene.provider.ownership) + 1
    scene.provider.arrived[pause] = asyncio.Event()
    scene.provider.release[pause] = asyncio.Event()
    scene.memorable_responder = scene.choose(lambda pool: pool)
    caplog.set_level(logging.INFO)
    running = asyncio.create_task(scene.advance())
    await scene.provider.wait(pause)
    owner = await run_cli(
        scene.database.url,
        "memory",
        "episodes",
        "set",
        "--conversation",
        conversation,
        "--facts",
        str(ids[5]),
    )
    written = await scene.index_row("space:G")
    scene.provider.release[pause].set()
    await asyncio.wait_for(running, 30)
    kept = await scene.index_row("space:G")
    calls, _ = await _run(scene)

    assert owner.returncode == 0, owner
    assert kept == written
    assert [(item["fact_id"], item["source"]) for item in kept["memorable"]] == [(ids[5], "owner")]
    assert any(
        "memory_episode_memorable_rejected" in record.getMessage()
        and "code=revision_changed" in record.getMessage()
        for record in caplog.records
    )
    assert calls == 0
    scene.write_memorable("K18", {"owner_row": written, "after_run": kept})


# ----------------------------------------------------------------------- K19
async def test_k19_first_version_generate_and_apply(scene, tmp_path):
    """K19: generate writes nothing; a stale plan is refused; an edited one is locked."""
    from tests.support.model_executor import InjectedModelExecutor

    from yuki.memory.episode_selection import EpisodeGeneration, EpisodePlan
    from yuki.services.concurrency import ConcurrencyManager

    g_ids = await _fill(scene, "space:G", 18, start="2026-09-01", prefix="g")
    a_ids = await _fill(scene, "private:A", 17, start="2026-09-01", prefix="a")
    scene.memorable_responder = scene.choose(_keep(g_ids[1], a_ids[1]))
    generation = EpisodeGeneration(
        settings=scene.settings,
        database=scene.database,
        models=InjectedModelExecutor(scene.provider),
        concurrency=ConcurrencyManager(),
    )

    async def table() -> list:
        async with scene.database.read_session() as reader:
            from sqlalchemy import text

            return list((await reader.execute(text("SELECT * FROM memory_episode_indexes"))).all())

    stale = await generation.generate(None)
    assert await table() == []
    stale_path = tmp_path / "stale.json"
    stale.plan.write(stale_path)
    await _run(scene)  # Dream builds both rows: their revisions change
    refused = await run_cli(scene.database.url, "memory", "episodes", "apply", str(stale_path))
    after_refused = {key: await scene.index_row(key) for key in ("space:G", "private:A")}

    fresh = await generation.generate(None)
    fresh_path = tmp_path / "fresh.json"
    fresh.plan.write(fresh_path)
    payload = json.loads(fresh_path.read_text("utf-8"))
    for item in payload["indexes"]:
        if item["owner_id"] == scene.persons["A"]:
            item["memorable"] = [a_ids[0]]
    fresh_path.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
    applied = await run_cli(scene.database.url, "memory", "episodes", "apply", str(fresh_path))
    group = await scene.index_row("space:G")
    private = await scene.index_row("private:A")

    assert refused.returncode == 1, refused
    assert {item["code"] for item in json.loads(refused.stdout)["rejected"]} == {"revision_changed"}
    assert after_refused["space:G"]["revision"] == 1
    assert applied.returncode == 0, applied
    assert isinstance(EpisodePlan.read(fresh_path).indexes, tuple)
    assert [item["fact_id"] for item in group["memorable"]] == [g_ids[1]] and not group["locked"]
    assert group["recent"] == g_ids[-15:]
    assert [(item["fact_id"], item["source"]) for item in private["memorable"]] == [
        (a_ids[0], "owner")
    ]
    assert private["locked"]
    scene.write_memorable(
        "K19",
        {
            "refused": json.loads(refused.stdout),
            "applied": json.loads(applied.stdout),
            "group": group,
            "private_A": private,
        },
    )


# ----------------------------------------------------------------------- K20
async def test_k20_without_a_row_the_index_is_empty(scene):
    """K20: unseeded: empty index, tail by the global watermark; the first run builds rows."""
    from tests.integration.test_memory_episode_index_scale_e2e import business, recorded

    g = await _source(scene, "space:G")
    g1 = await scene.episode("g1", "G", "群里第一次聚会", title="第一次聚会", evidence=(g,))
    with recorded(scene.database) as statements:
        empty = await scene.render("space:G")
    no_row_count = len(business(statements))
    calls, _ = await _run(scene)
    built = await scene.render("space:G")
    with recorded(scene.database) as statements:
        await scene.render("space:G")
    row_count = len(business(statements))
    h = await _source(scene, "space:H")
    h1 = await scene.episode("h1", "H", "H 群的第一件事", title="H 第一件事", evidence=(h,))
    fresh_audience = await scene.render("space:H")
    await _run(scene)
    h_built = await scene.render("space:H")

    assert empty.index_text == "" and list(empty.recent_fact_ids) == [g1]
    assert calls == 0 and list(built.window_fact_ids) == [g1] and built.recent_fact_ids == ()
    assert fresh_audience.index_text == "" and list(fresh_audience.recent_fact_ids) == [h1]
    assert list(h_built.window_fact_ids) == [h1]
    assert no_row_count <= row_count + 1
    scene.write_memorable(
        "K20",
        {
            "empty": render_record(empty),
            "built": render_record(built),
            "fresh_audience": render_record(fresh_audience),
            "h_built": render_record(h_built),
            "statements": {"no_row": no_row_count, "row": row_count},
        },
    )


# ----------------------------------------------------------------------- K21
async def test_k21_month_counts_add_up(database, tmp_path):
    """K21: 300 visible: 15 + 10 lines, months hold the rest; next day one fewer."""
    scene = await EpisodeScene(database, tmp_path).build(memory_dream_max_input_characters=80_000)
    ids = await _fill(scene, "private:A", 300, start="2026-01-01", prefix="o")
    chosen = ids[0:285:28][:10]
    scene.memorable_responder = scene.choose(_keep(*chosen))
    calls, _ = await _run(scene)
    turn = await scene.episode_turn("A", "列一下经历")
    start = _at("2026-01-01")
    listed = {*ids[-15:], *chosen}

    def expected(skip: set[int]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for index, fact_id in enumerate(ids):
            if fact_id in listed or fact_id in skip:
                continue
            month = (start + timedelta(days=index)).strftime("%Y-%m")
            counts[month] = counts.get(month, 0) + 1
        return counts

    assert calls == 1
    assert section_ids(turn.index, MEMORABLE_SECTION) == chosen
    assert section_ids(turn.index, RECENT_SECTION) == ids[-15:]
    assert len(item_lines(turn.index)) == 25
    assert month_counts(turn.index) == expected(set())
    assert sum(month_counts(turn.index).values()) == 275
    archived = ids[1]
    await _invalidate(scene, archived)
    later_calls, _ = await _run(scene)
    next_day = await scene.episode_turn("A", "再列一下")

    assert later_calls == 0
    assert month_counts(next_day.index) == expected({archived})
    assert sum(month_counts(next_day.index).values()) == 274
    scene.write_memorable(
        "K21",
        {
            "first": turn_record(turn, await scene.contract()),
            "next_day": turn_record(next_day, await scene.contract()),
        },
    )


# ----------------------------------------------------------------------- K23
async def test_k23_forgetting_removes_the_row_and_the_ids(scene):
    """K23: ``/ai forgetme`` by B: B's private row is gone, G stops showing B's episode."""
    await _fill(scene, "private:B", 3, start="2026-09-01", prefix="b")
    pg = await scene.episode(
        "pg",
        "B@G",
        "B 在群里讲了个冷笑话",
        title="B 讲冷笑话",
        evidence=(await _source(scene, "space:G"),),
        valid_from=_at("2026-08-01"),
    )
    await _fill(scene, "space:G", 3, start="2026-09-01", prefix="g")
    await _run(scene)
    before = await scene.index_row("space:G")
    forgot = await scene.command("B", "/ai forgetme")
    turn = await scene.episode_turn("A", "群里还好吗", group="G")
    stale = await scene.index_row("space:G")
    await _run(scene)
    cleaned = await scene.index_row("space:G")

    assert forgot and "删除" in forgot[0]
    assert pg in before["recent"]
    assert await scene.index_row("private:B") is None
    assert pg not in turn.index_ids + turn.recent_ids and pg in stale["recent"]
    assert pg not in cleaned["recent"]
    scene.write_memorable(
        "K23",
        {"before": before, "turn": turn_record(turn, await scene.contract()), "cleaned": cleaned},
    )


# ----------------------------------------------------------------------- K24
async def test_k24_merged_person_renders_the_root_row(scene):
    """K24: B' merged into B: B''s account renders B's row, no duplicate IDs."""
    from tests.support.person_merge import merge_persons

    b_ids = await _fill(scene, "private:B", 2, start="2026-09-01", prefix="b")
    c_ids = await _fill(scene, "private:B2", 2, start="2026-09-05", prefix="c")
    await _run(scene)
    old_row = await scene.index_row("private:B2")
    async with scene.database.immediate_session() as writer:
        assert (
            await merge_persons(writer, scene.persons["B2"], scene.persons["B"])
            == scene.persons["B"]
        )
    by_root = await scene.render("private:B")
    by_old = await scene.render("private:B2")
    old_turn = await scene.episode_turn("B2", "合并后 B′ 的账号", person="B")
    await _run(scene)
    root_row = await scene.index_row("private:B")
    stale_row = await scene.index_row("private:B2")

    assert by_root == by_old
    assert list(by_root.window_fact_ids) == b_ids
    assert old_turn.index == by_root.index_text
    assert len(set(old_turn.index_ids)) == len(old_turn.index_ids)
    assert root_row["recent"] == b_ids and root_row["months"] == {"2026-09": len(c_ids)}
    assert stale_row == old_row
    scene.write_memorable(
        "K24", {"render": render_record(by_root), "root_row": root_row, "stale_row": stale_row}
    )


# ----------------------------------------------------------------------- K25
async def test_k25_disabled_person_hidden_and_not_offered(scene):
    """K25: disabling B hides B's episodes and keeps them out of the comparison; a run
    while B is disabled keeps them listed (hidden), and after B is enabled again they
    render as before; a hidden leaver waits and is compared once when visible again."""
    pg = await scene.episode(
        "pg",
        "B@G",
        "B 在群里分享了搬家照片",
        title="B 搬家照片",
        evidence=(await _source(scene, "space:G"),),
        valid_from=_at("2026-09-20"),
    )
    b_ids = await _fill(scene, "private:B", 2, start="2026-09-01", prefix="b")
    g_ids = await _fill(scene, "space:G", 14, start="2026-09-01", prefix="g")
    await _run(scene)
    enabled_group = await scene.render("space:G")
    enabled_private = await scene.render("private:B")
    await scene.harness.private_users.set_enabled(scene.persons["B"], False)
    disabled_group = await scene.episode_turn("A", "群里谁搬家了", group="G")
    disabled_private = await scene.render("private:B")
    await scene.harness.private_users.set_enabled(scene.persons["B"], True)
    restored_group = await scene.render("space:G")
    restored_private = await scene.render("private:B")
    # A run while B is disabled: B's episodes are not offered and stay listed.
    await scene.harness.private_users.set_enabled(scene.persons["B"], False)
    pg_new = await scene.episode(
        "pg_new",
        "B@G",
        "B 在群里又发了新家的照片",
        title="新家照片",
        evidence=(await _source(scene, "space:G"),),
        valid_from=_at("2026-08-01"),
    )
    more = await _fill(scene, "space:G", 2, start="2026-10-01", prefix="n")
    scene.memorable_responder = scene.choose(lambda pool: [])
    calls, pools = await _run(scene)
    held_group = await scene.index_row("space:G")
    held_private = await scene.index_row("private:B")
    hidden_private = await scene.render("private:B")
    await scene.harness.private_users.set_enabled(scene.persons["B"], True)
    back_group = await scene.render("space:G")
    back_private = await scene.render("private:B")
    later = await scene.episode(
        "pg_later",
        "B@G",
        "B 恢复后在群里聊天",
        title="B 回来了",
        evidence=(await _source(scene, "space:G"),),
    )
    later_calls, later_pools = await _run(scene)
    final = await scene.index_row("space:G")

    assert pg in enabled_group.window_fact_ids and set(b_ids) <= set(
        enabled_private.window_fact_ids
    )
    assert pg not in disabled_group.index_ids + disabled_group.recent_ids
    assert disabled_private.index_text == ""
    assert restored_group == enabled_group and restored_private == enabled_private
    # pg_new (the oldest) leaves the window hidden: not offered, it waits in front.
    assert calls == 1 and pools == [[g_ids[0], g_ids[1]]]
    assert held_group["recent"] == [pg_new, *g_ids[2:], pg, *more]
    assert held_private["recent"] == [pg_new, *b_ids, pg]
    assert hidden_private.index_text == "" and hidden_private.recent_text == ""
    assert list(back_group.window_fact_ids) == [*g_ids[2:], pg, *more]
    assert list(back_private.window_fact_ids) == [pg_new, *b_ids, pg]
    # Visible again, the waiting leaver gets its one comparison.
    assert later_calls == 1 and later_pools == [[pg_new, g_ids[2]]]
    assert later in final["recent"] and set(more) <= set(final["recent"])
    assert pg_new not in final["recent"]
    scene.write_memorable(
        "K25",
        {
            "enabled": render_record(enabled_group),
            "disabled": turn_record(disabled_group, await scene.contract()),
            "held": held_group,
            "back": render_record(back_group),
            "pools": _pools(scene),
            "final": final,
        },
    )


async def test_k25_contested_impression_returns_after_resolution(scene):
    """K25: a contested impression is hidden and not offered; resolved, it is back."""
    ids = await _seeded(scene, "space:G", 16, keep=(0,))
    m = ids[0]
    memories = scene.chat._memories
    assert await memories.contest_fact(m, reason_code="episode_e2e", actor_user_id="10001")
    hidden = await scene.render("space:G")
    quiet_calls, _ = await _run(scene)
    kept = await scene.index_row("space:G")
    (n,) = await _fill(scene, "space:G", 1, start="2026-10-01", prefix="n")
    scene.memorable_responder = scene.choose(lambda pool: pool)
    calls, pools = await _run(scene)
    contested = await scene.index_row("space:G")
    await memories.resolve_conflicts(m, (), actor_user_id="10001")
    resolved = await scene.render("space:G")

    assert m not in _shown(hidden)
    assert quiet_calls == 0 and [item["fact_id"] for item in kept["memorable"]] == [m]
    assert calls == 1 and pools == [[ids[1]]]
    assert [item["fact_id"] for item in contested["memorable"]] == [m, ids[1]]
    assert contested["memorable"][0] == kept["memorable"][0]
    assert list(resolved.memorable_fact_ids) == [m, ids[1]]
    assert list(resolved.window_fact_ids) == [*ids[2:], n]
    scene.write_memorable(
        "K25-contested",
        {"kept": kept, "contested": contested, "resolved": render_record(resolved)},
    )


# ----------------------------------------------------------------------- K26
async def test_k26_order_independent_of_insertion_and_model_output(scene, tmp_path):
    """K26: reversed insertion renders the same bytes; impressions sort by date."""
    ids = await _fill(scene, "space:G", 18, start="2026-09-01", prefix="g")

    def reversed_refs(request):
        pool = scene.pool(request)
        return memorable_reply([item["ref"] for item in reversed(pool)])

    scene.memorable_responder = reversed_refs
    await _run(scene)
    conversation = scene.conversations["space:G"]
    outputs = []
    for name, reverse in (("forward.db", False), ("reverse.db", True)):
        url = await scene.reordered_copy(tmp_path / name, reverse=reverse)
        result = await run_cli(url, "memory", "episodes", "render", "--conversation", conversation)
        assert result.returncode == 0, result
        outputs.append(result.stdout)
    rendered = await scene.render("space:G")

    assert outputs[0] == outputs[1]
    assert rendered.index_text in outputs[0]
    assert list(rendered.memorable_fact_ids) == ids[:3]
    scene.write_memorable(
        "K26",
        {"cli_stdout_sha256": [_sha(item) for item in outputs], "render": render_record(rendered)},
    )


# ----------------------------------------------------------------------- K27
async def test_k27_selection_fault_keeps_dream_and_the_rows(scene, monkeypatch):
    """K27: the episode step fails: the run completes, rows stay, status shows the lag."""
    g = await _source(scene, "space:G")
    await _fill(scene, "space:G", 15, start="2026-09-01", prefix="g")
    await _run(scene)
    before = await scene.index_row("space:G")
    core = await scene.fact("core", "G", "群里每周五晚上一起打游戏", evidence=(g,))
    scene.selection_responder = lambda request: selection_reply(["new_1"])
    await _fill(scene, "space:G", 2, start="2026-10-01", prefix="n")
    _skip_episode_step(monkeypatch)
    run = await scene.dream_run()
    after = await scene.index_row("space:G")
    status = await _status(scene)
    core_rows = await scene.table()

    assert run.status.value == "completed"
    assert after == before
    row = _status_row(status, scene.spaces["G"])
    assert row["behind_watermark"] > 0
    assert any(item["fact_ids"] == [core] for item in core_rows)
    scene.write_memorable("K27", {"row": after, "status_row": row})


# ----------------------------------------------------------------------- K28
async def test_k28_index_changes_never_move_the_recovery_hash(scene):
    """K28: impressions and the window change; the recovery contract does not (+ G16-G19)."""
    hashes = [await scene.contract()]
    await _seeded(scene, "space:G", 17, keep=(0,))
    hashes.append(await scene.contract())
    await _fill(scene, "space:G", 2, start="2026-10-01", prefix="n")
    scene.memorable_responder = scene.choose(lambda pool: pool[:1])
    await _run(scene)
    hashes.append(await scene.contract())

    assert len(set(hashes)) == 1
    scene.write_memorable("K28", {"recovery_contract_sha256": [_sha(item) for item in hashes]})


# ----------------------------------------------------------------------- K29
async def test_k29_row_read_failure_keeps_the_turn(scene, monkeypatch, caplog):
    """K29: the row read fails: the turn replies, no index, the resident segment stays."""
    import yuki.memory.episode_index as episode_index

    a1 = await _source(scene, "private:A")
    await scene.fact(
        "rule",
        "A",
        "周报周五交",
        evidence=(a1,),
        source_type=MemorySourceType.EXPLICIT,
        authority=MemoryAuthority.EXPLICIT,
    )
    await _fill(scene, "private:A", 3, start="2026-09-01", prefix="a")
    await _run(scene)
    healthy = await scene.episode_turn("A", "渲染正常")

    async def broken(*args, **kwargs):
        raise RuntimeError("episode index row read failed")

    monkeypatch.setattr(episode_index, "load_index_row", broken)
    caplog.set_level(logging.DEBUG)
    from yuki.domain.messages import ChatResponse

    scene.responder = lambda request: ChatResponse("收到", 0)
    turn = await scene.episode_turn("A", "目录读取出错也要回复")

    assert healthy.index and healthy.resident
    assert turn.index == "" and turn.recent == "" and turn.resident == healthy.resident
    assert turn.result.reason == "chat"
    assert any("memory_episode_index_render_failed" in r.getMessage() for r in caplog.records)
    scene.write_memorable(
        "K29",
        {
            "healthy": turn_record(healthy, await scene.contract()),
            "failed": turn_record(turn, await scene.contract()),
        },
    )


# ----------------------------------------------------------------------- K30
async def test_k30_last_year_dates_and_untitled_lines(scene):
    """K30: an impression from last year shows ``YYYY-MM-DD``; untitled shows the placeholder."""
    g = await _source(scene, "space:G")
    last_year = await scene.episode(
        "ly", "G", "去年一起跨年", title="一起跨年", evidence=(g,), valid_from=_at("2025-12-31")
    )
    await _fill(scene, "space:G", 15, start="2026-09-01", prefix="g")
    scene.memorable_responder = scene.choose(_keep(last_year))
    await _run(scene)
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
    saved = replies[0]["fact_id"]
    tail = await scene.render("private:A")
    await _run(scene)
    group = await scene.episode_turn("A", "群里的长期印象", group="G")
    private = await scene.render("private:A")
    as_of_year = datetime.now(UTC).astimezone(ZoneInfo(TIMEZONE)).year
    expected_day = "2025-12-31" if as_of_year != 2025 else "12-31"

    assert f"- {expected_day} 一起跨年（fact:{last_year}）" in group.index.split("\n")
    assert f"- {local_day()} {UNTITLED}（fact:{saved}）" in item_lines(tail.recent_text)
    assert f"- {local_day()} {UNTITLED}（fact:{saved}）" in item_lines(private.index_text)
    scene.write_memorable(
        "K30",
        {
            "group": turn_record(group, await scene.contract()),
            "tail": render_record(tail),
            "private": render_record(private),
            "save_turn_tail": saved_turn.recent_ids,
        },
    )


# ----------------------------------------------------------------------- K31
def _columns(path: Path, table: str) -> list[tuple]:
    with sqlite3.connect(path) as connection:
        return [
            (row[1], row[2].upper(), row[3], row[5])
            for row in connection.execute(f"PRAGMA table_info('{table}')")
        ]


def _indexes(path: Path, table: str) -> list[tuple]:
    with sqlite3.connect(path) as connection:
        return sorted(
            (
                name,
                " ".join((sql or "").split()).split(" ON ", 1)[-1],
            )
            for name, sql in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name=?", (table,)
            )
        )


async def test_k31_upgrade_drops_last_injected_at_and_downgrade_restores_it(
    database, tmp_path, monkeypatch
):
    """K31: 0165 drops the column, adds the table and indexes; downgrade adds it back empty."""
    from tests.support.memory_episode_scene import MEMORABLE_RESULTS, write_episode_artifact

    from yuki.memory.audit import MemoryAuditService

    path = tmp_path / "actual-0164.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path.as_posix()}")
    config = Config("alembic.ini")
    await asyncio.to_thread(command.upgrade, config, "0164")
    old_columns = [column[0] for column in _columns(path, "memory_facts")]
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "INSERT INTO memory_facts (scope_type, kind, memory_key, category, content,"
            " normalized_content, importance, confidence, source_type, authority, status,"
            " conflict_state, created_at, updated_at, last_confirmed_at, last_injected_at,"
            " validation_version, review_state, canonical_subject_space_id) VALUES ('group',"
            " 'episode', 'k31', 'k31', '旧库里的一条经历', '旧库里的一条经历', 3, 1.0,"
            " 'automatic', 'group_report', 'active', 'clear', '2026-10-01 00:00:00',"
            " '2026-10-01 00:00:00', '2026-10-01 00:00:00', '2026-10-09 00:00:00',"
            " 'memory-v2-quality-v1', 'verified', 's-1')"
        )
    await asyncio.to_thread(command.upgrade, config, "head")
    upgraded_columns = _columns(path, "memory_facts")
    kept = _rows(path, "SELECT id, content FROM memory_facts")
    orm_path = Path(database.url.removeprefix("sqlite+aiosqlite:///"))
    # The new table must equal the ORM exactly; legacy memory_facts only by column names
    # and the new partial index (its older columns predate this migration).
    schema = {
        "memory_episode_indexes": (
            _columns(path, "memory_episode_indexes"),
            _indexes(path, "memory_episode_indexes"),
        ),
        "memory_facts": (
            sorted(column[0] for column in upgraded_columns),
            [item for item in _indexes(path, "memory_facts") if "supersedes" in item[0]],
        ),
    }
    orm_schema = {
        "memory_episode_indexes": (
            _columns(orm_path, "memory_episode_indexes"),
            _indexes(orm_path, "memory_episode_indexes"),
        ),
        "memory_facts": (
            sorted(column[0] for column in _columns(orm_path, "memory_facts")),
            [item for item in _indexes(orm_path, "memory_facts") if "supersedes" in item[0]],
        ),
    }
    await asyncio.to_thread(command.downgrade, config, "0164")
    downgraded = _columns(path, "memory_facts")
    restored = _rows(path, "SELECT last_injected_at FROM memory_facts")
    tables = {row[0] for row in _rows(path, "SELECT name FROM sqlite_master WHERE type='table'")}
    indexes = {row[0] for row in _rows(path, "SELECT name FROM sqlite_master WHERE type='index'")}

    scene = await EpisodeScene(database, tmp_path).build()
    fact_id = await scene.episode(
        "audit", "G", "审计用的一条经历", title="审计", evidence=(await _source(scene, "space:G"),)
    )
    explained = await MemoryAuditService(scene.chat._memories.repository).explain(fact_id)

    assert "last_injected_at" in old_columns
    assert "last_injected_at" not in [column[0] for column in upgraded_columns]
    assert kept == [(1, "旧库里的一条经历")]
    assert schema == orm_schema
    assert schema["memory_facts"][1] == [
        (
            "ix_memory_facts_supersedes_id",
            "memory_facts (supersedes_id) WHERE supersedes_id IS NOT NULL",
        )
    ]
    assert "last_injected_at" in [column[0] for column in downgraded]
    assert restored == [(None,)]
    assert "memory_episode_indexes" not in tables and "ix_memory_facts_supersedes_id" not in indexes
    assert explained is not None and "last_injected_at" not in explained
    write_episode_artifact(
        "K31-upgrade",
        {"upgraded_columns": upgraded_columns, "schema": schema, "downgraded": downgraded},
        {},
        results=MEMORABLE_RESULTS,
    )


def _rows(path: Path, sql: str) -> list[tuple]:
    with sqlite3.connect(path) as connection:
        return connection.execute(sql).fetchall()
