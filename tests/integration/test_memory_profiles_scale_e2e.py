"""E2E: query counts, plans and timings of the profile paths (memory layering P4, #227).

The owner's performance requirements for P4: every turn's render issues a constant
number of queries whatever the history, profile, entry or member counts (no N+1), and
reads O(rendered entries and IDs) rows for the profile part; the member window counts
messages through an index range, the available sets are computed in one batch, and no
statement scans a large table.

``test_render_query_count_is_constant_for_profiles`` always runs: it renders one member with one
entry and then five members with ten entries each (and a private chat with one versus
twenty entries) and requires the same number of statements.

``test_scale_render_and_selection`` runs with ``YUKI_PROFILE_SCALE_E2E=1``. It builds a
database near production size (about 160k chat events, 2.5k facts, 15k evidence rows,
30 people, 5 groups, 10 entries per profile), then measures every conversation's
render, the selection step's preparation (window, member counts, maintained set, due
profiles, batched cleanup check) and the CLI's ``latest_window``; it runs
``EXPLAIN QUERY PLAN`` on every statement those paths issued and fails on a full scan
of a table that grows with history. Results: ``test-results/memory-profiles/scale.json``
(timings are machine-dependent and are not part of the byte-stable artifacts).

Bulk rows are copied with plain SQL from rows written through the real paths (events,
facts), since writing 160k events one by one through the ingress path would take
hours; every copied row keeps the constraints of its template. All text is invented.
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event, select
from tests.support.canonical_identity import ensure_person, ensure_space
from tests.support.event_seed import seed_scoped_event
from tests.support.memory_profile_scene import RESULTS, ProfileScene

from yuki.domain.conversations import ConversationScope
from yuki.persistence.models import ChatEventModel

BOT = "80001"
_UUID4 = (
    "lower(hex(randomblob(4))) || '-' || lower(hex(randomblob(2))) || '-4' || "
    "substr(lower(hex(randomblob(2))), 2) || '-' || substr('89ab', 1 + (abs(random()) % 4), 1) || "
    "substr(lower(hex(randomblob(2))), 2) || '-' || lower(hex(randomblob(6)))"
)
# Tables whose size grows with history; a SCAN of one of them is a failure.
LARGE = (
    "chat_events",
    "memory_facts",
    "memory_evidence",
    "memory_tool_receipts",
    "memory_profile_versions",
    "memory_dream_operations",
    "memory_dream_operation_sources",
)


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


def _business(statements: list[tuple[str, Any]]) -> list[tuple[str, Any]]:
    """Statements other than transaction control and pragmas."""
    return [
        (sql, params)
        for sql, params in statements
        if not sql.lstrip()
        .upper()
        .startswith(("BEGIN", "COMMIT", "ROLLBACK", "PRAGMA", "SAVEPOINT", "RELEASE"))
    ]


async def _render_count(scene: ProfileScene, key: str) -> tuple[int, str]:
    with recorded(scene.database) as statements:
        rendered = await scene.resident.render(scene.audience(key))
    return len(_business(statements)), rendered.text


@pytest.fixture
async def scene(database, tmp_path):
    return await ProfileScene(database, tmp_path).build()


async def test_render_query_count_is_constant_for_profiles(scene):
    """One member/one entry versus five members/ten entries: the same statement count."""
    persons, spaces, store = scene.persons, scene.spaces, scene.profiles
    g1 = await scene.event("space:G", "B", "我们群里的人都爱运动", key="g1")
    b1 = await scene.event("private:B", "B", "我私下在学大提琴", key="b1")
    keys = ("A", "B", "C", "D", "B2")
    facts: dict[str, list[int]] = {}
    for key in keys:
        facts[key] = [
            await scene.fact(f"{key}_{index}", key, f"{key} 的公开爱好 {index}", evidence=(g1,))
            for index in range(20)
        ]
    private = [
        await scene.fact(f"B_private_{index}", "B", f"B 的私下爱好 {index}", evidence=(b1,))
        for index in range(20)
    ]

    async def write(key: str, audience: str, ids: list[int], count: int) -> None:
        current = await store.load(persons[key], audience)
        await store.commit(
            persons[key],
            audience,
            tuple(
                scene.entry(f"条目 {index}", ids[2 * index], ids[2 * index + 1])
                for index in range(count)
            ),
            source="owner",
            reason="规模",
            expected_version=current.current_version if current is not None else 0,
        )

    await write("B", "public", facts["B"], 1)
    await store.replace_dream_members({spaces["G"]: (persons["B"],)}, dream_run_public_id="small")
    small_group, small_group_text = await _render_count(scene, "G")
    small_private, _ = await _render_count(scene, "B")
    for key in keys:
        await write(key, "public", facts[key], 10)
    await write("B", "private", private, 10)
    await store.replace_dream_members(
        {spaces["G"]: tuple(persons[key] for key in keys)}, dream_run_public_id="large"
    )
    large_group, large_group_text = await _render_count(scene, "G")
    large_private, large_private_text = await _render_count(scene, "B")

    assert small_group_text.count("\n- ") < large_group_text.count("\n- ")
    assert large_private_text.count("\n- ") >= 20
    assert small_group == large_group, (small_group, large_group)
    assert small_private == large_private, (small_private, large_private)
    scene.write(
        "query-counts",
        {
            "group": {"one_member_one_entry": small_group, "five_members_ten_entries": large_group},
            "private": {"one_entry": small_private, "twenty_entries": large_private},
        },
    )


# ------------------------------------------------------------------------- scale
SCALE = os.environ.get("YUKI_PROFILE_SCALE_E2E") == "1"
PEOPLE = 30
GROUPS = 5
EVENTS = 160_000
FACTS = 2_500
EVIDENCE = 15_000
EPISODES = 600


def _plans(path: Path, statements: list[tuple[str, Any]]) -> list[dict[str, Any]]:
    """EXPLAIN QUERY PLAN of each distinct statement, with its full-scan details."""
    seen: dict[str, dict[str, Any]] = {}
    connection = sqlite3.connect(path)
    try:
        for sql, params in _business(statements):
            if sql in seen or not sql.lstrip().upper().startswith(("SELECT", "WITH")):
                continue
            rows = connection.execute(f"EXPLAIN QUERY PLAN {sql}", params or ()).fetchall()
            details = [row[3] for row in rows]
            seen[sql] = {
                "sql": " ".join(sql.split())[:300],
                "plan": details,
                "large_scans": [
                    item
                    for item in details
                    if item.startswith("SCAN ") and item.split()[1] in LARGE
                ],
            }
    finally:
        connection.close()
    return list(seen.values())


async def _timed(database, coroutine) -> tuple[Any, float, list[tuple[str, Any]]]:
    with recorded(database) as statements:
        started = time.perf_counter()
        result = await coroutine
        elapsed = (time.perf_counter() - started) * 1000
    return result, elapsed, statements


@pytest.mark.skipif(not SCALE, reason="set YUKI_PROFILE_SCALE_E2E=1 for the production-size run")
async def test_scale_render_and_selection(scene):
    from yuki.memory.profile_admin import latest_window
    from yuki.memory.profile_selection import (
        dream_window,
        due_profiles,
        maintained_profiles,
        window_activity,
    )
    from yuki.memory.profiles import check_many

    rng = random.Random(227)
    database, store = scene.database, scene.profiles
    path = Path(database.url.removeprefix("sqlite+aiosqlite:///"))
    started = time.perf_counter()
    # People and groups beyond the fixture, with private chats and memberships.
    people: list[tuple[str, str]] = [
        (scene.persons[key], qq)
        for key, qq in (
            ("A", "10001"),
            ("B", "10002"),
            ("C", "1003"),
            ("D", "1005"),
        )
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
    templates: list[tuple[int, str, str | None]] = []  # (event, person, group or None)
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
    # ~160k events: copies of the templates spread over 60 days (groups 85%).
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
                f"SELECT {cols}, {_UUID4}, "
                f"datetime('now', '-' || (abs(random()) % 5184000) || ' seconds'), "
                f"datetime('now', '-' || (abs(random()) % 5184000) || ' seconds') "
                f"FROM chat_events, n WHERE chat_events.id = ?",
                (count, template),
            )
        connection.commit()
        event_rows = connection.execute(
            "SELECT e.id, e.author_person_id, c.kind FROM chat_events e "
            "JOIN canonical_conversations c ON c.id = e.canonical_conversation_id "
            "WHERE e.author_kind = 'person' ORDER BY random() LIMIT 40000"
        ).fetchall()
        total_events = connection.execute("SELECT count(*) FROM chat_events").fetchone()[0]
    by_author: dict[str, dict[str, list[int]]] = {}
    for event_id, author, kind in event_rows:
        by_author.setdefault(author, {"private": [], "space": []})[kind].append(event_id)
    # ~2.5k facts through the real write path; ~15k evidence rows copied onto them.
    from yuki.memory.enums import MemoryAuthority, MemoryScopeType, MemorySourceType
    from yuki.memory.models import MemoryFactCreate

    memories = scene.chat._memories
    fact_owner: dict[int, str] = {}
    for index in range(FACTS):
        person_id, qq = people[index % len(people)]
        created = await memories.remember(
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
        fact_owner[created.id] = person_id
    with sqlite3.connect(path) as connection:
        rows = []
        for fact_id, person_id in fact_owner.items():
            pool = by_author.get(person_id, {"private": [], "space": []})
            for _ in range(EVIDENCE // FACTS):
                kind = "space" if rng.random() < 0.8 or not pool["private"] else "private"
                if pool[kind]:
                    rows.append((fact_id, rng.choice(pool[kind])))
        connection.executemany(
            "INSERT OR IGNORE INTO memory_evidence (fact_id, event_id, source_speaker_user_id,"
            " relation, confidence, authority, excerpt, created_at) VALUES (?, ?, 'scale',"
            " 'self_statement', 1.0, 'self_report', '虚构摘录', datetime('now'))",
            rows,
        )
        connection.commit()
        total_evidence = connection.execute("SELECT count(*) FROM memory_evidence").fetchone()[0]
    # ~600 group episodes in G (the main group's episode index candidates).
    from yuki.memory.enums import MemoryKind

    episodes = []
    for index in range(EPISODES):
        created = await memories.remember(
            MemoryFactCreate(
                memory_key=f"scale-episode:{index}",
                category="scale",
                content=f"规模经历 {index}：群里一起做了一件虚构的事",
                title=f"虚构经历 {index}",
                scope_type=MemoryScopeType.GROUP,
                group_id="20001",
                canonical_subject_space_id=scene.spaces["G"],
                kind=MemoryKind.EPISODE,
                source_type=MemorySourceType.AUTOMATIC,
                authority=MemoryAuthority.GROUP_REPORT,
            )
        )
        episodes.append(created.id)
    with sqlite3.connect(path) as connection:
        g_events = [
            row[0]
            for row in connection.execute(
                "SELECT e.id FROM chat_events e WHERE e.group_id = '20001' LIMIT 2000"
            )
        ]
        connection.executemany(
            "INSERT OR IGNORE INTO memory_evidence (fact_id, event_id, source_speaker_user_id,"
            " relation, confidence, authority, excerpt, created_at) VALUES (?, ?, 'scale',"
            " 'group_statement', 1.0, 'group_report', '虚构摘录', datetime('now'))",
            [(fact_id, rng.choice(g_events)) for fact_id in episodes for _ in range(2)],
        )
        connection.commit()
    # Profiles: 10 public entries per person (and 10 private where possible), lists.
    async with database.read_session() as session:
        from yuki.memory.profiles import profile_fact_sets

        sets = await profile_fact_sets(session, [person for person, _ in people])
    for person_id, _ in people:
        for audience in ("public", "private"):
            ids = list(sets[(person_id, audience)])
            if len(ids) < 2:
                continue
            await store.commit(
                person_id,
                audience,
                tuple(
                    scene.entry(
                        f"规模条目 {index}",
                        ids[(2 * index) % len(ids)],
                        ids[(2 * index + 1) % len(ids)],
                    )
                    for index in range(10)
                    if ids[(2 * index) % len(ids)] != ids[(2 * index + 1) % len(ids)]
                ),
                source="owner",
                reason="规模",
                expected_version=0,
            )
    async with database.read_session() as session:
        spaces = {
            str(row[0]): str(row[1])
            for row in (
                await session.execute(
                    select(ChatEventModel.group_id, ChatEventModel.canonical_conversation_id)
                    .where(ChatEventModel.group_id.in_(groups))
                    .distinct()
                )
            ).all()
        }
    build_seconds = time.perf_counter() - started
    # The selection step's preparation and the CLI window.
    window, window_ms, window_sql = await _timed(database, dream_window(database))
    activity, activity_ms, activity_sql = await _timed(database, window_activity(database, window))
    await store.replace_dream_members(activity.ranking, dream_run_public_id="scale")
    maintained, maintained_ms, maintained_sql = await _timed(
        database, maintained_profiles(database, activity)
    )
    due, due_ms, due_sql = await _timed(database, due_profiles(database, maintained))
    profiles = [item for item in await store.profiles() if item.entries]

    async def cleanup_check():
        async with database.read_session() as session:
            return await check_many(
                session, [(item.person_id, item.audience, item.entries) for item in profiles]
            )

    _, check_ms, check_sql = await _timed(database, cleanup_check())
    cli_window, cli_ms, cli_sql = await _timed(database, latest_window(database))
    # Every conversation's render.
    async with database.read_session() as session:
        from yuki.conversation.canonical_db_models import CanonicalConversationModel

        conversations = list(await session.scalars(select(CanonicalConversationModel.id)))
    renders = []
    render_sql: list[tuple[str, Any]] = []
    for conversation_id in conversations:
        rendered, elapsed, statements = await _timed(
            database, scene.resident.render_conversation(conversation_id)
        )
        render_sql.extend(statements)
        renders.append(
            {
                "ms": round(elapsed, 2),
                "queries": len(_business(statements)),
                "profile_entries": sum(len(item.entries) for item in rendered.profiles),
            }
        )
    from yuki.memory.episode_index import EpisodeIndex

    index = EpisodeIndex(database, timezone="Asia/Shanghai")
    episode_render, episode_ms, episode_sql = await _timed(
        database, index.render_conversation(spaces["20001"])
    )
    render_sql.extend(episode_sql)
    plans = _plans(
        path,
        [
            *window_sql,
            *activity_sql,
            *maintained_sql,
            *due_sql,
            *check_sql,
            *cli_sql,
            *render_sql,
        ],
    )
    scans = [item for item in plans if item["large_scans"]]
    result = {
        "rows": {
            "chat_events": total_events,
            "facts": len(fact_owner),
            "evidence": total_evidence,
            "people": len(people),
            "groups": len(groups),
            "profiles_with_entries": len(profiles),
            "group_conversations": len(spaces),
        },
        "build_seconds": round(build_seconds, 1),
        "selection_prep_ms": {
            "window": round(window_ms, 2),
            "member_counts": round(activity_ms, 2),
            "maintained": round(maintained_ms, 2),
            "due": round(due_ms, 2),
            "cleanup_check_all_profiles": round(check_ms, 2),
            "cli_latest_window": round(cli_ms, 2),
        },
        "selection_prep_queries": {
            "window": len(_business(window_sql)),
            "member_counts": len(_business(activity_sql)),
            "maintained": len(_business(maintained_sql)),
            "due": len(_business(due_sql)),
            "cleanup_check_all_profiles": len(_business(check_sql)),
            "cli_latest_window": len(_business(cli_sql)),
        },
        "due_profiles": len(due),
        "render": {
            "conversations": len(renders),
            "max_ms": max(item["ms"] for item in renders),
            "mean_ms": round(sum(item["ms"] for item in renders) / len(renders), 2),
            "queries": sorted({item["queries"] for item in renders}),
            "max_profile_entries": max(item["profile_entries"] for item in renders),
        },
        "episode_index_main_group": {
            "candidates": EPISODES,
            "listed": len(episode_render.index_fact_ids) + len(episode_render.recent_fact_ids),
            "ms": round(episode_ms, 2),
            "queries": len(_business(episode_sql)),
        },
        "plans": plans,
        "large_scans": scans,
        "member_lists": {space: len(roots) for space, roots in activity.ranking.items()},
        "cli_window_spaces": len(cli_window.spaces),
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "scale.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    assert total_events >= EVENTS * 0.9 and total_evidence >= EVIDENCE * 0.8
    assert scans == [], json.dumps(scans, ensure_ascii=False, indent=2)
