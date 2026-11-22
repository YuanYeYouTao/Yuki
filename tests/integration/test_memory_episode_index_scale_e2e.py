"""E2E: statement counts, plans and timings of the P2.1 episode index paths.

Task book: docs/architecture/Yuki-记忆分层P2.1经历目录精简任务书-2026-11-23.md §11 and
case K22. The owner's performance requirements: every turn's render issues a constant
number of statements whatever the history (no N+1, batched ``IN``), reads rows bounded
by the listed IDs plus what was written since the row's ``through``; the nightly step
finds leavers through a primary-key range; month counts are one batch for all
audiences; no statement scans a table that grows with history.

Always run:
- ``test_k22_render_statements_constant``: one audience with 15 visible episodes, one
  with 1000: the same statement count; the large one reads no archived evidence.
- ``test_selection_prep_statements_constant``: 1 leaver and 15 leavers: the same count.
- ``test_month_recount_statements_constant``: the nightly preparation, which also
  recounts the months, for 3 and 20 audiences with rows: the same count.
Each also runs ``EXPLAIN QUERY PLAN`` on every statement and fails on a ``SCAN`` of
``memory_facts``, ``memory_evidence`` or ``chat_events``.

``test_scale_paths`` runs with ``YUKI_EPISODE_SCALE_E2E=1``: a database near production
size (about 160k chat events, 2.5k facts, 1000 episodes with about 10 evidence rows each,
30 people, 5 groups, one group with 600 episodes), measuring every conversation's render,
one nightly preparation and one ``status``. Results:
``test-results/memory-episode-memorable/scale.json`` (timings are machine-dependent and
not part of the byte-stable artifacts). The control is the P2 renderer's per-turn query
reproduced below on the same database. All text is invented.
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import event
from tests.support.canonical_identity import ensure_person, ensure_space
from tests.support.event_seed import seed_scoped_event
from tests.support.memory_episode_scene import MEMORABLE_RESULTS, TIMEZONE, EpisodeScene

from yuki.domain.conversations import ConversationScope
from yuki.memory.enums import MemoryAuthority, MemoryKind, MemoryScopeType, MemorySourceType
from yuki.memory.models import MemoryFactCreate

BOT = "80001"
LARGE = ("chat_events", "memory_facts", "memory_evidence")


@contextmanager
def recorded(database) -> Iterator[list[tuple[str, Any]]]:
    """Every statement (with parameters) the database executes inside the block."""
    statements: list[tuple[str, Any]] = []

    def before(conn, cursor, statement, parameters, context, executemany):
        statements.append((statement, parameters))

    engine = database.engine.sync_engine
    event.listen(engine, "before_cursor_execute", before)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", before)


def business(statements: list[tuple[str, Any]]) -> list[tuple[str, Any]]:
    """Statements other than transaction control and pragmas."""
    return [
        (sql, params)
        for sql, params in statements
        if not sql.lstrip()
        .upper()
        .startswith(("BEGIN", "COMMIT", "ROLLBACK", "PRAGMA", "SAVEPOINT", "RELEASE"))
    ]


def plans(path: Path, statements: list[tuple[str, Any]]) -> list[dict[str, Any]]:
    """Plan, rows returned and large-table scans of each distinct SELECT."""
    seen: dict[str, dict[str, Any]] = {}
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        for sql, params in business(statements):
            if sql in seen or not sql.lstrip().upper().startswith(("SELECT", "WITH")):
                continue
            details = [
                row[3]
                for row in connection.execute(f"EXPLAIN QUERY PLAN {sql}", params or ()).fetchall()
            ]
            seen[sql] = {
                "sql": " ".join(sql.split())[:300],
                "plan": details,
                "rows": len(connection.execute(sql, params or ()).fetchall()),
                "large_scans": [
                    item
                    for item in details
                    if item.startswith("SCAN ") and item.split()[1] in LARGE
                ],
            }
    finally:
        connection.close()
    return list(seen.values())


def _path(database) -> Path:
    return Path(database.url.removeprefix("sqlite+aiosqlite:///"))


def _at(day: str) -> datetime:
    year, month, date = (int(part) for part in day.split("-"))
    return datetime(year, month, date, 12, tzinfo=ZoneInfo(TIMEZONE))


@pytest.fixture
async def scene(database, tmp_path):
    return await EpisodeScene(database, tmp_path).build(memory_dream_max_input_characters=400_000)


async def _episodes(scene: EpisodeScene, key: str, owner: str, count: int, prefix: str):
    source = f"source:{key}"
    if source not in scene.events:
        await scene.event(
            key, owner if key.startswith("private") else "B", f"{key} 日常", key=source
        )
    return [
        await scene.episode(
            f"{prefix}{index:04d}",
            owner,
            f"{prefix}{index:04d}：一条虚构的经历",
            title=f"虚构 {prefix}{index:04d}",
            evidence=(scene.events[source],),
            valid_from=_at("2024-01-01") + timedelta(hours=12 * index),
        )
        for index in range(count)
    ]


async def _render_statements(scene: EpisodeScene, key: str):
    with recorded(scene.database) as statements:
        rendered = await scene.render(key)
    return rendered, business(statements)


# ----------------------------------------------------------------------- K22
async def test_k22_render_statements_constant(scene):
    """15 versus 1000 visible episodes: the same statements, bounded evidence reads."""
    small = await _episodes(scene, "space:G", "G", 15, "s")
    large = await _episodes(scene, "private:A", "A", 1000, "l")
    scene.memorable_responder = scene.choose(lambda pool: pool[:10])
    await scene.advance()
    # Today's tail differs too (1 versus 5): no statement per tail episode either.
    small_tail = await _episodes(scene, "space:G", "G", 1, "st")
    large_tail = await _episodes(scene, "private:A", "A", 5, "lt")
    small_render, small_sql = await _render_statements(scene, "space:G")
    large_render, large_sql = await _render_statements(scene, "private:A")
    planned = plans(_path(scene.database), [*small_sql, *large_sql])
    evidence_params = [params for sql, params in large_sql if "FROM memory_evidence" in sql]

    assert list(small_render.window_fact_ids) == small
    assert list(large_render.window_fact_ids) == large[-15:]
    assert len(large_render.memorable_fact_ids) == 10
    assert list(small_render.recent_fact_ids) == small_tail
    assert set(large_render.recent_fact_ids) == set(large_tail)
    # The private chat resolves its person root: one statement more than the group.
    assert len(large_sql) == len(small_sql) + 1, (len(small_sql), len(large_sql))
    assert len(evidence_params) == 1 and len(evidence_params[0]) <= 25 + len(large_tail) + 1
    assert [item for item in planned if item["large_scans"]] == []
    MEMORABLE_RESULTS.mkdir(parents=True, exist_ok=True)
    (MEMORABLE_RESULTS / "K22-statements.json").write_text(
        json.dumps(
            {
                "render_statements": {"group_15": len(small_sql), "private_1000": len(large_sql)},
                "evidence_ids_read_private_1000": len(evidence_params[0]),
                "plans": [{key: item[key] for key in ("sql", "plan")} for item in planned],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


async def _prepare_statements(scene: EpisodeScene):
    from tests.support.model_executor import InjectedModelExecutor

    from yuki.memory.episode_selection import EpisodeSelection
    from yuki.model_runtime.structured import StructuredTaskRunner
    from yuki.services.concurrency import ConcurrencyManager

    selection = EpisodeSelection(
        settings=scene.settings,
        database=scene.database,
        structured=StructuredTaskRunner(InjectedModelExecutor(scene.provider)),
        concurrency=ConcurrencyManager(),
    )
    with recorded(scene.database) as statements:
        prepared = await selection.prepare()
    return prepared, business(statements)


async def test_selection_prep_statements_constant(scene):
    """§6.1 with 1 leaver and with 15 leavers: the same statements, no large scans."""
    await _episodes(scene, "space:G", "G", 15, "g")
    await scene.advance()
    one = await _episodes(scene, "space:G", "G", 1, "n")
    prepared_one, one_sql = await _prepare_statements(scene)
    fifteen = await _episodes(scene, "space:G", "G", 14, "m")
    prepared_fifteen, fifteen_sql = await _prepare_statements(scene)
    planned = plans(_path(scene.database), [*one_sql, *fifteen_sql])

    leavers = {item.owner.kind: item for item in prepared_one.audiences}
    assert len(leavers["space"].leavers) == 1 and one
    assert len(next(iter(prepared_fifteen.audiences)).leavers) == 15 and fifteen
    assert len(one_sql) == len(fifteen_sql), (len(one_sql), len(fifteen_sql))
    assert [item for item in planned if item["large_scans"]] == []
    MEMORABLE_RESULTS.mkdir(parents=True, exist_ok=True)
    (MEMORABLE_RESULTS / "prep-statements.json").write_text(
        json.dumps(
            {
                "one_leaver": len(one_sql),
                "fifteen_leavers": len(fifteen_sql),
                "plans": [{key: item[key] for key in ("sql", "plan")} for item in planned],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


async def test_month_recount_statements_constant(scene):
    """The nightly preparation (month counts included) for 3 and for 20 audiences with
    rows: the same statements."""
    memories = scene.chat._memories
    persons = []
    async with scene.database.immediate_session() as writer:
        for index in range(20):
            persons.append(
                await ensure_person(
                    writer, str(50000 + index), display_name=f"m-{index}", now=datetime.now(UTC)
                )
            )

    async def write(index: int, person: str) -> None:
        for number in range(3):
            await memories.remember(
                MemoryFactCreate(
                    memory_key=f"month:{index}:{number}",
                    category="scale",
                    content=f"人物 {index} 的第 {number} 件虚构经历",
                    title=f"经历 {index}-{number}",
                    scope_type=MemoryScopeType.PERSON,
                    subject_user_id=str(50000 + index),
                    canonical_subject_person_id=person,
                    kind=MemoryKind.EPISODE,
                    source_type=MemorySourceType.AUTOMATIC,
                    authority=MemoryAuthority.SELF_REPORT,
                    valid_from=_at(f"2026-0{1 + number}-01"),
                )
            )

    for index, person in enumerate(persons[:3]):
        await write(index, person)
    await scene.advance()
    three, three_sql = await _prepare_statements(scene)
    for index, person in enumerate(persons[3:], start=3):
        await write(index, person)
    await scene.advance()
    twenty, twenty_sql = await _prepare_statements(scene)
    planned = plans(_path(scene.database), [*three_sql, *twenty_sql])

    assert len(three.audiences) == 3 and len(twenty.audiences) == 20
    assert len(three_sql) == len(twenty_sql), (len(three_sql), len(twenty_sql))
    assert [item for item in planned if item["large_scans"]] == []


# ------------------------------------------------------------------------- scale
SCALE = os.environ.get("YUKI_EPISODE_SCALE_E2E") == "1"
PEOPLE = 30
GROUPS = 5
EVENTS = 160_000
FACTS = 2_500
EPISODES = 1_000
MAIN_GROUP_EPISODES = 600
EVIDENCE_PER_EPISODE = 10


def _p2_control(path: Path, space_id: str) -> dict[str, int]:
    """Rows the P2 renderer read per turn in the main group (its candidate query)."""
    with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as connection:
        ids = [
            row[0]
            for row in connection.execute(
                "SELECT id FROM memory_facts WHERE kind = 'episode' AND status = 'active' AND"
                " conflict_state = 'clear' AND review_state != 'quarantined' AND"
                " ((scope_type IN ('group', 'person_group') AND canonical_subject_space_id = ?)"
                " OR (scope_type = 'self' AND visibility_type = 'group' AND"
                " canonical_visibility_space_id = ?))",
                (space_id, space_id),
            )
        ]
        evidence = connection.execute(
            f"SELECT count(*) FROM memory_evidence WHERE fact_id IN ({','.join('?' * len(ids))})",
            ids,
        ).fetchone()[0]
    return {"candidate_episodes": len(ids), "evidence_rows": int(evidence)}


async def _timed(database, coroutine) -> tuple[Any, float, list[tuple[str, Any]]]:
    with recorded(database) as statements:
        started = time.perf_counter()
        result = await coroutine
        elapsed = (time.perf_counter() - started) * 1000
    return result, elapsed, business(statements)


@pytest.mark.skipif(not SCALE, reason="set YUKI_EPISODE_SCALE_E2E=1 for the production-size run")
async def test_scale_paths(scene):
    from yuki.cli import _episodes_status
    from yuki.conversation.canonical_db_models import CanonicalConversationModel
    from yuki.memory.episode_index import EpisodeIndex

    rng = random.Random(681)
    database = scene.database
    path = _path(database)
    started = time.perf_counter()
    people: list[tuple[str, str]] = [
        (scene.persons[key], qq) for key, qq in (("A", "10001"), ("B", "10002"), ("C", "1003"))
    ]
    async with database.immediate_session() as writer:
        for index in range(PEOPLE - len(people)):
            qq = str(30000 + index)
            people.append(
                (
                    await ensure_person(
                        writer, qq, display_name=f"scale-{index}", now=datetime.now(UTC)
                    ),
                    qq,
                )
            )
        groups = ["20001", "20010001"]
        for index in range(GROUPS - len(groups)):
            gid = str(40000 + index)
            await ensure_space(writer, gid, name=f"scale-group-{index}")
            groups.append(gid)
    templates: list[tuple[int, str, str | None]] = []
    for number, (person_id, qq) in enumerate(people):
        private = await seed_scoped_event(
            scene.harness.scoped_events,
            scope=ConversationScope.private(BOT, qq),
            origin_key=f"scale-private-{number}",
            sender_user_id=qq,
            direction="inbound",
            content=f"scale private {number}",
        )
        templates.append((private.event.id, person_id, None))
        for gid in rng.sample(groups, 2):
            grouped = await seed_scoped_event(
                scene.harness.scoped_events,
                scope=ConversationScope.group(BOT, gid),
                origin_key=f"scale-group-{number}-{gid}",
                sender_user_id=qq,
                direction="inbound",
                content=f"scale group {number}",
            )
            templates.append((grouped.event.id, person_id, gid))
    _uuid4 = (
        "lower(hex(randomblob(4))) || '-' || lower(hex(randomblob(2))) || '-4' || "
        "substr(lower(hex(randomblob(2))), 2) || '-' || "
        "substr('89ab', 1 + (abs(random()) % 4), 1) || "
        "substr(lower(hex(randomblob(2))), 2) || '-' || lower(hex(randomblob(6)))"
    )
    with sqlite3.connect(path) as connection:
        columns = [
            row[1]
            for row in connection.execute("PRAGMA table_info('chat_events')")
            if row[1] not in ("id", "canonical_event_id", "occurred_at", "observed_at")
        ]
        cols = ", ".join(columns)
        weights = [1 if group is None else 6 for _, _, group in templates]
        per = [round(EVENTS * weight / sum(weights)) for weight in weights]
        for (template, _, _), count in zip(templates, per, strict=True):
            connection.execute(
                f"WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < ?) "
                f"INSERT INTO chat_events ({cols}, canonical_event_id, occurred_at, observed_at) "
                f"SELECT {cols}, {_uuid4}, "
                f"datetime('now', '-' || (abs(random()) % 5184000) || ' seconds'), "
                f"datetime('now', '-' || (abs(random()) % 5184000) || ' seconds') "
                f"FROM chat_events, n WHERE chat_events.id = ?",
                (count, template),
            )
        connection.commit()
        total_events = connection.execute("SELECT count(*) FROM chat_events").fetchone()[0]
        by_group = {
            gid: [
                row[0]
                for row in connection.execute(
                    "SELECT id FROM chat_events WHERE group_id = ? LIMIT 3000", (gid,)
                )
            ]
            for gid in groups
        }
        by_private = {
            person_id: [
                row[0]
                for row in connection.execute(
                    "SELECT e.id FROM chat_events e JOIN canonical_conversations c"
                    " ON c.id = e.canonical_conversation_id"
                    " WHERE c.kind = 'private' AND c.person_id = ? LIMIT 200",
                    (person_id,),
                )
            ]
            for person_id, _ in people
        }
    memories = scene.chat._memories
    for index in range(FACTS - EPISODES):
        person_id, qq = people[index % len(people)]
        await memories.remember(
            MemoryFactCreate(
                memory_key=f"scale:{index}",
                category="scale",
                content=f"规模事实 {index}：一条虚构的长期偏好",
                scope_type=MemoryScopeType.PERSON,
                subject_user_id=qq,
                canonical_subject_person_id=person_id,
                source_type=MemorySourceType.AUTOMATIC,
                authority=MemoryAuthority.SELF_REPORT,
            )
        )
    # 1000 episodes: 600 in the main group G, the rest over other groups and private chats.
    from sqlalchemy import select

    from yuki.persistence.models import ChatEventModel

    async with database.read_session() as session:
        group_space = {
            str(row[0]): str(row[1])
            for row in (
                await session.execute(
                    select(ChatEventModel.group_id, CanonicalConversationModel.space_id)
                    .join(
                        CanonicalConversationModel,
                        CanonicalConversationModel.id == ChatEventModel.canonical_conversation_id,
                    )
                    .where(ChatEventModel.group_id.in_(groups))
                    .distinct()
                )
            ).all()
        }
    episode_rows: list[tuple[int, list[int]]] = []
    base = datetime(2026, 9, 1, tzinfo=UTC)
    for index in range(EPISODES):
        if index < MAIN_GROUP_EPISODES or index % 2 == 0:
            gid = "20001" if index < MAIN_GROUP_EPISODES else groups[1 + index % (GROUPS - 1)]
            create = MemoryFactCreate(
                memory_key=f"scale-episode:{index}",
                category="scale",
                content=f"规模经历 {index}：群里一起做了一件虚构的事",
                title=f"虚构经历 {index}",
                scope_type=MemoryScopeType.GROUP,
                group_id=gid,
                canonical_subject_space_id=group_space[gid],
                kind=MemoryKind.EPISODE,
                source_type=MemorySourceType.AUTOMATIC,
                authority=MemoryAuthority.GROUP_REPORT,
                valid_from=base + timedelta(hours=index),
            )
            pool = by_group[gid]
        else:
            person_id, qq = people[index % len(people)]
            create = MemoryFactCreate(
                memory_key=f"scale-episode:{index}",
                category="scale",
                content=f"规模经历 {index}：私聊里聊起的一件虚构的事",
                title=f"虚构私聊经历 {index}",
                scope_type=MemoryScopeType.PERSON,
                subject_user_id=qq,
                canonical_subject_person_id=person_id,
                kind=MemoryKind.EPISODE,
                source_type=MemorySourceType.AUTOMATIC,
                authority=MemoryAuthority.SELF_REPORT,
                valid_from=base + timedelta(hours=index),
            )
            pool = by_private[person_id]
        created = await memories.remember(create)
        episode_rows.append((created.id, pool))
    with sqlite3.connect(path) as connection:
        connection.executemany(
            "INSERT OR IGNORE INTO memory_evidence (fact_id, event_id, source_speaker_user_id,"
            " relation, confidence, authority, excerpt, created_at) VALUES (?, ?, 'scale',"
            " 'group_statement', 1.0, 'group_report', '虚构摘录', datetime('now'))",
            [
                (fact_id, event_id)
                for fact_id, pool in episode_rows
                for event_id in rng.sample(pool, min(EVIDENCE_PER_EPISODE, len(pool)))
            ],
        )
        connection.commit()
        total_evidence = connection.execute("SELECT count(*) FROM memory_evidence").fetchone()[0]
        total_facts = connection.execute("SELECT count(*) FROM memory_facts").fetchone()[0]
    build_seconds = time.perf_counter() - started
    # The first nightly run (scripted model) builds every row; then a day's writes.
    scene.memorable_responder = scene.choose(lambda pool: pool[:10])
    first_started = time.perf_counter()
    await scene.advance()
    first_run_seconds = time.perf_counter() - first_started
    today: list[int] = []
    for index in range(40):
        created = await memories.remember(
            MemoryFactCreate(
                memory_key=f"scale-new:{index}",
                category="scale",
                content=f"今天的虚构经历 {index}",
                title=f"今天 {index}",
                scope_type=MemoryScopeType.GROUP,
                group_id="20001",
                canonical_subject_space_id=group_space["20001"],
                kind=MemoryKind.EPISODE,
                source_type=MemorySourceType.AUTOMATIC,
                authority=MemoryAuthority.GROUP_REPORT,
            )
        )
        today.append(created.id)
    with sqlite3.connect(path) as connection:
        connection.executemany(
            "INSERT OR IGNORE INTO memory_evidence (fact_id, event_id, source_speaker_user_id,"
            " relation, confidence, authority, excerpt, created_at) VALUES (?, ?, 'scale',"
            " 'group_statement', 1.0, 'group_report', '虚构摘录', datetime('now'))",
            [
                (fact_id, event_id)
                for fact_id in today
                for event_id in rng.sample(by_group["20001"], EVIDENCE_PER_EPISODE)
            ],
        )
        connection.commit()
    from tests.support.model_executor import InjectedModelExecutor

    from yuki.memory.episode_selection import EpisodeSelection
    from yuki.model_runtime.structured import StructuredTaskRunner
    from yuki.services.concurrency import ConcurrencyManager

    selection = EpisodeSelection(
        settings=scene.settings,
        database=database,
        structured=StructuredTaskRunner(InjectedModelExecutor(scene.provider)),
        concurrency=ConcurrencyManager(),
    )
    prepared, prep_ms, prep_sql = await _timed(database, selection.prepare())
    status, status_ms, status_sql = await _timed(database, _episodes_status(database, TIMEZONE))
    async with database.read_session() as session:
        conversations = list(await session.scalars(select(CanonicalConversationModel.id)))
    index = EpisodeIndex(database, timezone=TIMEZONE)
    renders: list[dict[str, Any]] = []
    render_sql: list[tuple[str, Any]] = []
    main_conversation = None
    for conversation_id in conversations:
        rendered, elapsed, statements = await _timed(
            database, index.render_conversation(conversation_id)
        )
        render_sql.extend(statements)
        renders.append(
            {
                "ms": round(elapsed, 2),
                "statements": len(statements),
                "listed": len(rendered.index_fact_ids),
                "tail": len(rendered.recent_fact_ids),
            }
        )
        if len(rendered.memorable_fact_ids) == 10 and len(rendered.recent_fact_ids) == 20:
            main_conversation = conversation_id
            renders[-1]["main_group"] = True
    # One nightly step on the same day's writes: the main group's 40 new episodes push
    # 40 leavers into one comparison call (scripted model), then one short write.
    requests_before = len(scene.memorable_requests)
    nightly_started = time.perf_counter()
    with recorded(database) as nightly_statements:
        outcome = await selection.run("scale", slots=24)
    nightly_ms = (time.perf_counter() - nightly_started) * 1000
    render_plans = plans(path, render_sql)
    prep_plans = plans(path, prep_sql)
    status_plans = plans(path, status_sql)
    month_sql = {sql for sql, _ in prep_sql if "memory_evidence" in sql} - {
        sql for sql, _ in render_sql
    }
    scans = [item for item in (*render_plans, *prep_plans, *status_plans) if item["large_scans"]]
    result = {
        "rows": {
            "chat_events": total_events,
            "facts": total_facts,
            "episodes": EPISODES + 40,
            "evidence": total_evidence,
            "people": len(people),
            "groups": len(groups),
            "index_rows": len(status["indexes"]),
        },
        "build_seconds": round(build_seconds, 1),
        "first_run_seconds": round(first_run_seconds, 1),
        "render": {
            "conversations": len(renders),
            "max_ms": max(item["ms"] for item in renders),
            "mean_ms": round(sum(item["ms"] for item in renders) / len(renders), 2),
            "statements": sorted({item["statements"] for item in renders}),
            "rows_read_max": max(item["rows"] for item in render_plans),
            "main_group_conversation_found": main_conversation is not None,
        },
        "prepare": {
            "ms": round(prep_ms, 2),
            "statements": len(prep_sql),
            "audiences": len(prepared.audiences),
            "leavers": sum(len(item.leavers) for item in prepared.audiences),
        },
        "status": {"ms": round(status_ms, 2), "statements": len(status_sql)},
        "nightly_run": {
            "ms_including_scripted_call": round(nightly_ms, 2),
            "statements": len(business(nightly_statements)),
            "comparison_calls": len(scene.memorable_requests) - requests_before,
            "written": outcome.written,
        },
        "main_group_render": next((item for item in renders if item.get("main_group")), None),
        "p2_control_main_group": _p2_control(path, group_space["20001"]),
        "plans": {"render": render_plans, "prepare": prep_plans, "status": status_plans},
        "month_recount_statements_not_in_render": len(month_sql),
        "large_scans": scans,
    }
    MEMORABLE_RESULTS.mkdir(parents=True, exist_ok=True)
    (MEMORABLE_RESULTS / "scale.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    assert total_events >= EVENTS * 0.9
    assert scans == [], json.dumps(scans, ensure_ascii=False, indent=2)
    assert len(result["render"]["statements"]) <= 2  # group vs private (root lookup)
    assert main_conversation is not None
    assert result["nightly_run"]["comparison_calls"] == 1 and outcome.written == 1
    assert result["prepare"]["leavers"] == 40
