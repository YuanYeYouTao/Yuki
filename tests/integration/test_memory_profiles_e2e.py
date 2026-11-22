"""E2E: person profiles (memory layering P4, #227).

Authoritative spec: docs/architecture/Yuki-记忆分层P4人物画像任务书-2026-11-22.md (§4 data
model and migration 0164, §5.3 pinned member lists, §6 eligibility, §7.4 entry check,
§4.5 forgetting a person, §13 cases H1-H35). This first part covers the data layer;
rendering and the Dream selection step extend the same cases.

Every case runs a real SQLite database; facts and evidence go through the real
``MemoryFactService`` with evidence on real fixture chat events (tool receipts and
foreign-evidence rows are direct writes, as legacy and imported production rows are).
Each case writes normalized JSON (the three P4 tables, available fact IDs and
signatures, check results; person/space IDs replaced by fixture labels, times
stripped) to ``test-results/memory-profiles/<case>.json``; identical input gives
byte-identical artifacts. All fixture text is invented.

Failure cases, written before the implementation:
  H22a the 0164 upgrade keeps a person core-set row, changes a space/SELF row, loses the
       expression unique index ``uq_memory_resident_sets_owner`` or still accepts a person
       row; the downgrade leaves a P4 table or the narrowed CHECK behind; the migrated
       schema and the ORM schema of the four touched tables differ.
  H22b the tables accept a second profile for one (person, audience), an unknown
       audience, a negative current version, version 0, a duplicate version, an unknown
       version source, a member position outside 1-5, a taken position, one person twice
       in one list or an unknown member source.
  H23/H33/H12 a write rewrites an earlier version; a stale expected version or a lock
       taken after the read still writes; identical entries write a version or a reorder
       does not; the private profile shares the public one's versions.
  H10/H11 malformed entries are stored: eleven entries, 41 characters after whitespace
       folding, empty text, a newline, an unknown section, no facts, duplicate facts, an
       empty reason, an unknown person.
  H1/H2/H5/H6/H31/H34/H32 the available sets admit: a private-only fact into public, a
       fact with someone else's private evidence (or an untraceable row) into either, a
       person_group fact, an evidence-less fact into public, a non-public explicit fact
       into either, a third-party / contested / episode fact; or miss: a group fact from
       any group, mixed group + own private evidence (P4-Q2), a group-evidenced explicit
       fact; or do not follow later evidence changes (signature unchanged).
  H19/H21 a merged-in person's facts do not count for the root, the old ID does not
       resolve to the root profile, the old profile is dropped; a disabled person still
       has available facts.
  H7/H9/H32 the entry check keeps an entry with one failing ID, drops one whose ID has
       an eligible successor instead of mapping it, admits another person's fact or a fact
       of the other audience, or writes anything.
  H13/H15/H16/H26 member lists: a pin above five, with duplicates after root resolution
       or with an unknown person is stored; a Dream replacement overwrites a pinned list or
       touches a space it was not given; unpin changes the list.
  H20 forgetting B leaves B's profiles, the profiles of persons whose facts lost evidence
       to B's receipts or events, any of their versions, or B's member rows; or deletes an
       unaffected person's profile.
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, select
from tests.support.memory_profile_scene import ProfileScene, write_artifact
from tests.support.person_merge import merge_persons

from yuki.memory.enums import (
    MemoryAuthority,
    MemoryEvidenceRelation,
    MemoryInvalidationReason,
    MemoryKind,
    MemorySourceType,
)
from yuki.memory.models import MemoryEvidenceCreate
from yuki.persistence.models import MemoryEvidenceModel

EXPLICIT = {"source_type": MemorySourceType.EXPLICIT, "authority": MemoryAuthority.EXPLICIT}
TOUCHED_TABLES = (
    "memory_resident_sets",
    "memory_profiles",
    "memory_profile_versions",
    "memory_space_members",
)


@pytest.fixture
async def scene(database, tmp_path):
    return await ProfileScene(database, tmp_path).build()


def _signature(fact_ids) -> str:
    return hashlib.sha256(",".join(str(item) for item in sorted(fact_ids)).encode()).hexdigest()


def _rows(path: Path, sql: str, *args):
    with sqlite3.connect(path) as connection:
        return connection.execute(sql, args).fetchall()


def _schema(path: Path) -> dict[str, dict[str, object]]:
    """Columns, indexes (with expression text) and foreign keys of the touched tables."""
    result: dict[str, dict[str, object]] = {}
    with sqlite3.connect(path) as connection:
        for table in TOUCHED_TABLES:
            columns = [
                (row[1], row[2].upper(), row[3], row[5])
                for row in connection.execute(f"PRAGMA table_info('{table}')")
            ]
            indexes = sorted(
                (
                    bool(row[2]),
                    tuple(
                        item[2]
                        for item in connection.execute(f"PRAGMA index_xinfo('{row[1]}')")
                        if item[5]
                    ),
                    " ".join(
                        (
                            connection.execute(
                                "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
                                (row[1],),
                            ).fetchone()[0]
                            or ""
                        ).split()
                    ).split(" ON ", 1)[-1]
                    if row[3] == "c"
                    else "",
                )
                for row in connection.execute(f"PRAGMA index_list('{table}')")
            )
            foreign = sorted(
                (row[2], row[3], row[4], row[6])
                for row in connection.execute(f"PRAGMA foreign_key_list('{table}')")
            )
            result[table] = {"columns": columns, "indexes": indexes, "foreign_keys": foreign}
    return result


_NOW = "'2026-11-22 05:00:00'"
_PROBES = {
    "resident_space": (
        "INSERT INTO memory_resident_sets (owner_kind, owner_space_id, fact_ids_json, source,"
        f" considered_through_fact_id, revision, created_at, updated_at) VALUES ('space', 's1',"
        f" '[]', 'dream', 0, 0, {_NOW}, {_NOW})"
    ),
    "resident_self_owner": (
        "INSERT INTO memory_resident_sets (owner_kind, fact_ids_json, source,"
        " considered_through_fact_id, revision, created_at, updated_at) VALUES ('self', '[]',"
        f" 'owner', 0, 0, {_NOW}, {_NOW})"
    ),
    "resident_space_duplicate": (
        "INSERT INTO memory_resident_sets (owner_kind, owner_space_id, fact_ids_json, source,"
        f" considered_through_fact_id, revision, created_at, updated_at) VALUES ('space', 's1',"
        f" '[]', 'owner', 0, 0, {_NOW}, {_NOW})"
    ),
    "resident_person": (
        "INSERT INTO memory_resident_sets (owner_kind, owner_person_id, fact_ids_json, source,"
        f" considered_through_fact_id, revision, created_at, updated_at) VALUES ('person', 'p1',"
        f" '[]', 'dream', 0, 0, {_NOW}, {_NOW})"
    ),
    "resident_self_dream": (
        "INSERT INTO memory_resident_sets (owner_kind, fact_ids_json, source,"
        " considered_through_fact_id, revision, created_at, updated_at) VALUES ('self', '[]',"
        f" 'dream', 0, 0, {_NOW}, {_NOW})"
    ),
    "profile_public": (
        "INSERT INTO memory_profiles (id, person_id, audience, current_version, locked,"
        f" created_at, updated_at) VALUES (1, 'p1', 'public', 0, 0, {_NOW}, {_NOW})"
    ),
    "profile_private": (
        "INSERT INTO memory_profiles (id, person_id, audience, current_version, locked,"
        f" created_at, updated_at) VALUES (2, 'p1', 'private', 0, 0, {_NOW}, {_NOW})"
    ),
    "profile_duplicate": (
        "INSERT INTO memory_profiles (person_id, audience, current_version, locked,"
        f" created_at, updated_at) VALUES ('p1', 'public', 0, 0, {_NOW}, {_NOW})"
    ),
    "profile_audience": (
        "INSERT INTO memory_profiles (person_id, audience, current_version, locked,"
        f" created_at, updated_at) VALUES ('p2', 'group', 0, 0, {_NOW}, {_NOW})"
    ),
    "profile_negative_version": (
        "INSERT INTO memory_profiles (person_id, audience, current_version, locked,"
        f" created_at, updated_at) VALUES ('p2', 'public', -1, 0, {_NOW}, {_NOW})"
    ),
    "version_1": (
        "INSERT INTO memory_profile_versions (profile_id, version, entries_json, source,"
        f" reason, created_at) VALUES (1, 1, '[]', 'dream', 'r', {_NOW})"
    ),
    "version_owner": (
        "INSERT INTO memory_profile_versions (profile_id, version, entries_json, source,"
        f" reason, created_at) VALUES (1, 2, '[]', 'owner', 'r', {_NOW})"
    ),
    "version_program": (
        "INSERT INTO memory_profile_versions (profile_id, version, entries_json, source,"
        f" reason, created_at) VALUES (1, 3, '[]', 'program', 'r', {_NOW})"
    ),
    "version_duplicate": (
        "INSERT INTO memory_profile_versions (profile_id, version, entries_json, source,"
        f" reason, created_at) VALUES (1, 1, '[]', 'dream', 'r', {_NOW})"
    ),
    "version_zero": (
        "INSERT INTO memory_profile_versions (profile_id, version, entries_json, source,"
        f" reason, created_at) VALUES (2, 0, '[]', 'dream', 'r', {_NOW})"
    ),
    "version_source": (
        "INSERT INTO memory_profile_versions (profile_id, version, entries_json, source,"
        f" reason, created_at) VALUES (2, 1, '[]', 'model', 'r', {_NOW})"
    ),
    **{
        f"member_{position}": (
            "INSERT INTO memory_space_members (space_id, position, person_id, source,"
            f" updated_at) VALUES ('s1', {position}, 'm{position}', 'dream', {_NOW})"
        )
        for position in range(1, 6)
    },
    "member_position_0": (
        "INSERT INTO memory_space_members (space_id, position, person_id, source, updated_at)"
        f" VALUES ('s2', 0, 'm1', 'dream', {_NOW})"
    ),
    "member_position_6": (
        "INSERT INTO memory_space_members (space_id, position, person_id, source, updated_at)"
        f" VALUES ('s1', 6, 'm6', 'dream', {_NOW})"
    ),
    "member_position_taken": (
        "INSERT INTO memory_space_members (space_id, position, person_id, source, updated_at)"
        f" VALUES ('s1', 1, 'm9', 'dream', {_NOW})"
    ),
    "member_person_twice": (
        "INSERT INTO memory_space_members (space_id, position, person_id, source, updated_at)"
        f" VALUES ('s2', 1, 'm1', 'dream', {_NOW}), ('s2', 2, 'm1', 'dream', {_NOW})"
    ),
    "member_source": (
        "INSERT INTO memory_space_members (space_id, position, person_id, source, updated_at)"
        f" VALUES ('s3', 1, 'm1', 'program', {_NOW})"
    ),
}
_ACCEPTED = {
    "resident_space",
    "resident_self_owner",
    "profile_public",
    "profile_private",
    "version_1",
    "version_owner",
    "version_program",
    *(f"member_{position}" for position in range(1, 6)),
}


def _probe(path: Path) -> dict[str, bool]:
    """Run each insert in its own transaction (foreign keys off): name -> accepted."""
    outcome: dict[str, bool] = {}
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        for name, sql in _PROBES.items():
            connection.execute("BEGIN")
            try:
                connection.execute(sql)
            except sqlite3.IntegrityError:
                connection.execute("ROLLBACK")
                outcome[name] = False
            else:
                connection.execute("COMMIT")
                outcome[name] = True
    finally:
        connection.close()
    return outcome


def _database_path(database) -> Path:
    return Path(database.url.removeprefix("sqlite+aiosqlite:///"))


# -------------------------------------------------------------------------- H22
async def test_h22_upgrade_drops_person_core_sets_and_keeps_the_index(
    database, tmp_path, monkeypatch
):
    """H22: 0164 deletes person rows only, narrows both CHECKs and rebuilds the index."""
    path = tmp_path / "actual-0163.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path.as_posix()}")
    config = Config("alembic.ini")
    await asyncio.to_thread(command.upgrade, config, "0163")
    insert = (
        "INSERT INTO memory_resident_sets (owner_kind, owner_person_id, owner_space_id,"
        " fact_ids_json, source, considered_through_fact_id, revision, selected_at,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    seeded = [
        ("person", "p-dream", None, "[11, 12]", "dream", 12, 3, _NOW, _NOW, _NOW),
        ("person", "p-owner", None, "[13]", "owner", 0, 1, None, _NOW, _NOW),
        ("space", None, "s-1", "[21, 22]", "dream", 22, 2, _NOW, _NOW, _NOW),
        ("self", None, None, "[31]", "owner", 0, 1, None, _NOW, _NOW),
    ]
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.executemany(insert, seeded)
    columns = (
        "SELECT id, owner_kind, owner_person_id, owner_space_id, fact_ids_json, source,"
        " considered_through_fact_id, revision, selected_at, created_at, updated_at"
        " FROM memory_resident_sets ORDER BY id"
    )
    before = _rows(path, columns)

    await asyncio.to_thread(command.upgrade, config, "head")
    upgraded = _rows(path, columns)
    index = _rows(
        path,
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='uq_memory_resident_sets_owner'",
    )
    migrated_schema = _schema(path)
    orm_schema = _schema(_database_path(database))
    probed = tmp_path / "probe-0164.db"
    shutil.copyfile(path, probed)
    with sqlite3.connect(probed) as connection:
        connection.execute("DELETE FROM memory_resident_sets")
    migrated_probe = _probe(probed)
    orm_probe = _probe(_database_path(database))

    await asyncio.to_thread(command.downgrade, config, "0163")
    downgraded_tables = {
        row[0] for row in _rows(path, "SELECT name FROM sqlite_master WHERE type='table'")
    }
    downgraded = _rows(path, columns)
    downgraded_index = _rows(
        path,
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='uq_memory_resident_sets_owner'",
    )
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(insert, ("person", "p-new", None, "[]", "dream", 0, 0, None, _NOW, _NOW))

    assert upgraded == [row for row in before if row[1] != "person"]
    assert index and "UNIQUE" in index[0][0] and "COALESCE(owner_person_id" in index[0][0]
    assert migrated_schema == orm_schema
    assert migrated_probe == orm_probe
    assert {name for name, accepted in migrated_probe.items() if accepted} == _ACCEPTED
    assert not {"memory_profiles", "memory_profile_versions", "memory_space_members"} & (
        downgraded_tables
    )
    assert downgraded == upgraded
    assert downgraded_index and downgraded_index[0][0] == index[0][0]
    write_artifact(
        "H22-upgrade",
        {
            "before": before,
            "upgraded": upgraded,
            "index": index,
            "schema": migrated_schema,
            "probe": migrated_probe,
            "downgraded": downgraded,
        },
        {},
    )


# ---------------------------------------------------------------- H12 H23 H33
async def test_h12_h23_h33_versions_append_compare_and_lock(scene):
    """Versions are appended, never rewritten; writes compare the version and the lock."""
    from yuki.memory.profiles import ProfileConflict

    g1 = await scene.event("space:G", "B", "我周末常去爬山，也爱拍照", key="g1")
    f1 = await scene.fact("f1", "B", "周末常去爬山", evidence=(g1,))
    f2 = await scene.fact("f2", "B", "爱拍风景照", evidence=(g1,))
    store, person = scene.profiles, scene.persons["B"]
    first = (scene.entry("周末常去爬山", f1, section="兴趣"),)
    second = (*first, scene.entry("爱拍风景照", f2))
    attempted = datetime(2026, 11, 22, 5, tzinfo=UTC)

    v1 = await store.commit(
        person,
        "public",
        first,
        source="dream",
        reason="首版",
        expected_version=0,
        signature=_signature([f1]),
        attempted_at=attempted,
        require_unlocked=True,
        dream_run_public_id="run-1",
    )
    v2 = await store.commit(
        person, "public", second, source="dream", reason="加入拍照", expected_version=1
    )
    snapshot = await scene.profile_tables()
    with pytest.raises(ProfileConflict) as stale:
        await store.commit(person, "public", first, source="dream", reason="旧", expected_version=1)
    after_stale = await scene.profile_tables()
    same = await store.commit(
        person,
        "public",
        second,
        source="dream",
        reason="不变",
        expected_version=2,
        signature=_signature([f1, f2]),
        attempted_at=attempted,
    )
    reordered = await store.commit(
        person, "public", tuple(reversed(second)), source="dream", reason="调序", expected_version=2
    )
    rolled_back = await store.commit(
        person,
        "public",
        first,
        source="owner",
        reason="owner rollback to v1",
        expected_version=3,
        lock=True,
    )
    with pytest.raises(ProfileConflict) as locked:
        await store.commit(
            person,
            "public",
            second,
            source="dream",
            reason="锁定期间",
            expected_version=4,
            require_unlocked=True,
        )
    program = await store.commit(
        person, "public", (), source="program", reason="程序删除：依据失效", expected_version=4
    )
    private = await store.commit(
        person, "private", first, source="owner", reason="私人", expected_version=0
    )
    unlocked = await store.set_locked(person, "public", False)
    await store.record_attempt(person, "private", attempted)
    history = await store.history(person, "public")
    final = await scene.profile_tables()

    assert (v1.written, v2.written, same.written, reordered.written) == (True, True, False, True)
    assert stale.value.code == "version_changed" and after_stale == snapshot
    assert same.profile.current_version == 2
    assert same.profile.considered_signature == _signature([f1, f2])
    assert reordered.profile.current_version == 3
    assert rolled_back.profile.current_version == 4 and rolled_back.profile.locked
    assert locked.value.code == "locked"
    assert program.profile.current_version == 5 and program.profile.entries == ()
    assert private.profile.current_version == 1
    assert not unlocked.locked and unlocked.current_version == 5
    assert [item.version for item in history] == [1, 2, 3, 4, 5]
    assert [item.source for item in history] == ["dream", "dream", "dream", "owner", "program"]
    assert history[0].entries == history[3].entries == first
    assert history[0].dream_run_public_id == "run-1"
    assert (await store.load_version(person, "public", 2)).entries == second
    assert (await store.load(person, "private")).attempted_at == attempted
    # Earlier versions are byte-identical after every later write.
    public_versions = [
        row for row in final["memory_profile_versions"] if row["audience"] == "public"
    ]
    assert public_versions[:2] == snapshot["memory_profile_versions"]
    scene.write("H12-H23-H33-versions", {"facts": scene.facts, "tables": final})


# -------------------------------------------------------------------- H10 H11
async def test_h10_h11_malformed_entries_are_never_stored(scene):
    """Every structural rule of §7.1/§8.4 rejects the whole write; nothing is stored."""
    from yuki.memory.profiles import (
        MAX_PROFILE_ENTRIES,
        MAX_PROFILE_ENTRY_CHARS,
        ProfileValidationError,
    )

    g1 = await scene.event("space:G", "B", "我喜欢下围棋", key="g1")
    f1 = await scene.fact("f1", "B", "喜欢下围棋", evidence=(g1,))
    store, person = scene.profiles, scene.persons["B"]
    ok = scene.entry("喜欢下围棋", f1)
    cases = {
        "too_many_entries": ((ok,) * (MAX_PROFILE_ENTRIES + 1), "r", person),
        "text_too_long": ((scene.entry("棋" * (MAX_PROFILE_ENTRY_CHARS + 1), f1),), "r", person),
        "empty_text": ((scene.entry("   ", f1),), "r", person),
        "multiline_text": ((scene.entry("喜欢\n下围棋", f1),), "r", person),
        "unknown_section": ((scene.entry("喜欢下围棋", f1, section="爱好"),), "r", person),
        "empty_facts": ((scene.entry("喜欢下围棋"),), "r", person),
        "duplicate_facts": ((scene.entry("喜欢下围棋", f1, f1),), "r", person),
        "empty_reason": ((ok,), " ", person),
        "unknown_person": ((ok,), "r", "00000000-0000-4000-8000-000000000000"),
    }
    codes = {}
    for code, (entries, reason, who) in cases.items():
        with pytest.raises(ProfileValidationError) as rejected:
            await store.commit(
                who, "public", entries, source="dream", reason=reason, expected_version=0
            )
        codes[code] = rejected.value.code
    tables = await scene.profile_tables()
    # Whitespace folds before counting: 40 characters plus doubled spaces still fit.
    folded = await store.commit(
        person,
        "public",
        (scene.entry("  " + "棋" * 20 + "   " + "棋" * 19 + " ", f1),),
        source="owner",
        reason="边界",
        expected_version=0,
    )

    assert codes == {code: code for code in cases}
    assert tables == {
        "memory_profiles": [],
        "memory_profile_versions": [],
        "memory_space_members": [],
    }
    assert folded.profile.entries[0].text == "棋" * 20 + " " + "棋" * 19
    scene.write("H10-H11-malformed", {"codes": codes, "tables": await scene.profile_tables()})


# ----------------------------------------------------- H1 H2 H5 H6 H31 H34 H32
async def test_h01_h02_h05_h06_h31_h32_h34_available_facts_follow_evidence(scene):
    """§6: public needs a direct group event and no foreign private row; private needs
    only own private rows (or none); person scope only; explicit facts only in public."""
    b1 = await scene.event("private:B", "B", "我在偷偷学大提琴", key="b1")
    g1 = await scene.event("space:G", "B", "我周末常去爬山", key="g1")
    h1 = await scene.event("space:H", "B", "我是做前端开发的", key="h1")
    a1 = await scene.event("private:A", "A", "B 说过他想换个城市生活", key="a1")
    memories = scene.chat._memories
    ids = {
        "group": await scene.fact("group", "B", "周末常去爬山", evidence=(g1,)),
        "other_group": await scene.fact("other_group", "B", "做前端开发", evidence=(h1,)),
        "mixed": await scene.fact("mixed", "B", "周末爬山，也在学琴", evidence=(g1, b1)),
        "own_private": await scene.fact("own_private", "B", "在学大提琴", evidence=(b1,)),
        "no_evidence": await scene.fact("no_evidence", "B", "喜欢别人叫他小北"),
        "foreign": await scene.fact(
            "foreign", "B", "想换个城市", evidence=(g1,), foreign_evidence=(a1,)
        ),
        "person_group": await scene.fact("person_group", "B@G", "在 G 群张罗活动", evidence=(g1,)),
        "explicit_group": await scene.fact(
            "explicit_group", "B", "请叫他北哥", evidence=(g1,), **EXPLICIT
        ),
        "explicit_private": await scene.fact(
            "explicit_private", "B", "早上别发消息", evidence=(b1,), **EXPLICIT
        ),
        "explicit_none": await scene.fact("explicit_none", "B", "生日不用提醒", **EXPLICIT),
        "third_party": await scene.fact(
            "third_party",
            "B",
            "据说养了一只猫",
            evidence=(g1,),
            authority=MemoryAuthority.THIRD_PARTY,
        ),
        "contested": await scene.fact("contested", "B", "不喝咖啡", evidence=(g1,)),
        "episode": await scene.fact(
            "episode", "B", "上周在群里讲了个笑话", evidence=(g1,), kind=MemoryKind.EPISODE
        ),
        "receipt_group": await scene.fact("receipt_group", "B", "常分享摄影作品", evidence=(g1,)),
        "receipt_only_group": await scene.fact("receipt_only_group", "B", "收藏胶片相机"),
        "own_receipt": await scene.fact("own_receipt", "B", "最近在练指法"),
        "a_group": await scene.fact("a_group", "A", "也爱爬山", evidence=(g1,)),
    }
    await memories.contest_fact(ids["contested"], reason_code="profile_e2e")
    await scene.receipt_evidence(ids["receipt_group"], g1, space="G")
    await scene.receipt_evidence(ids["receipt_only_group"], g1, space="G")
    await scene.receipt_evidence(ids["own_receipt"], b1, person="B")
    store, person = scene.profiles, scene.persons["B"]

    public = await store.available(person, "public")
    private = await store.available(person, "private")
    # H34: a private-only fact later confirmed in a group gains public eligibility.
    async with memories.write_session((ids["own_private"],)) as writer:
        await memories.append_evidence_bundle(
            ids["own_private"],
            (
                MemoryEvidenceCreate(
                    event_id=g1,
                    source_speaker_user_id="10002",
                    relation=MemoryEvidenceRelation.SELF_STATEMENT,
                    authority=MemoryAuthority.SELF_REPORT,
                    excerpt="在学大提琴",
                ),
            ),
            confirmed_at=datetime.now(UTC),
            session=writer,
        )
    confirmed_public = await store.available(person, "public")
    confirmed_private = await store.available(person, "private")
    # H32: a later foreign private row removes eligibility; losing every evidence row
    # moves a fact from public to private.
    async with scene.database.immediate_session() as writer:
        writer.add(
            MemoryEvidenceModel(
                fact_id=ids["group"],
                event_id=a1,
                source_speaker_user_id="10001",
                relation="self_statement",
                authority="self_report",
                excerpt="周末常去爬山",
                created_at=datetime.now(UTC),
            )
        )
        await writer.execute(
            delete(MemoryEvidenceModel).where(MemoryEvidenceModel.fact_id == ids["other_group"])
        )
    changed_public = await store.available(person, "public")
    changed_private = await store.available(person, "private")

    expected_public = sorted(
        ids[name] for name in ("group", "other_group", "mixed", "explicit_group", "receipt_group")
    )
    expected_private = sorted(ids[name] for name in ("own_private", "no_evidence", "own_receipt"))
    assert list(public.fact_ids) == expected_public
    assert public.signature == _signature(expected_public)
    assert list(private.fact_ids) == expected_private
    assert private.signature == _signature(expected_private)
    assert list(confirmed_public.fact_ids) == sorted([*expected_public, ids["own_private"]])
    assert ids["own_private"] not in confirmed_private.fact_ids
    assert confirmed_public.signature != public.signature
    assert confirmed_private.signature != private.signature
    assert ids["group"] not in changed_public.fact_ids + changed_private.fact_ids
    assert ids["other_group"] in changed_private.fact_ids
    assert ids["other_group"] not in changed_public.fact_ids
    scene.write(
        "H01-H34-available",
        {
            "facts": scene.facts,
            "initial": {"public": public, "private": private},
            "confirmed_in_group": {"public": confirmed_public, "private": confirmed_private},
            "evidence_changed": {"public": changed_public, "private": changed_private},
        },
    )


# -------------------------------------------------------------------- H19 H21
async def test_h19_h21_available_facts_follow_merge_and_enable(scene):
    """A merged-in person's facts count for the root; the old ID reads the root profile;
    the old profile row stays; a disabled person has nothing available."""
    b1 = await scene.event("private:B", "B", "我最近在学做饭", key="b1")
    b2 = await scene.event("private:B2", "B2", "我养了一只柴犬", key="b2")
    g1 = await scene.event("space:G", "B", "我是程序员", key="g1")
    b_private = await scene.fact("b_private", "B", "在学做饭", evidence=(b1,))
    b2_private = await scene.fact("b2_private", "B2", "养了一只柴犬", evidence=(b2,))
    b_public = await scene.fact("b_public", "B", "是程序员", evidence=(g1,))
    store = scene.profiles
    await store.commit(
        scene.persons["B2"],
        "private",
        (scene.entry("养了一只柴犬", b2_private),),
        source="owner",
        reason="合并前",
        expected_version=0,
    )
    await store.commit(
        scene.persons["B"],
        "private",
        (scene.entry("在学做饭", b_private),),
        source="owner",
        reason="合并前",
        expected_version=0,
    )
    before = await store.available(scene.persons["B"], "private")

    async with scene.database.immediate_session() as writer:
        root = await merge_persons(writer, scene.persons["B2"], scene.persons["B"])
    merged = await store.available(scene.persons["B"], "private")
    by_old_id = await store.load(scene.persons["B2"], "private")
    tables_after_merge = await scene.profile_tables()
    await scene.harness.private_users.set_enabled(scene.persons["B"], False)
    disabled = (
        await store.available(scene.persons["B"], "public"),
        await store.available(scene.persons["B"], "private"),
    )
    await scene.harness.private_users.set_enabled(scene.persons["B"], True)
    enabled = await store.available(scene.persons["B"], "public")

    assert root == scene.persons["B"]
    assert list(before.fact_ids) == [b_private]
    assert list(merged.fact_ids) == sorted([b_private, b2_private])
    assert merged.signature != before.signature
    assert by_old_id is not None and by_old_id.person_id == scene.persons["B"]
    assert [entry.text for entry in by_old_id.entries] == ["在学做饭"]
    assert sorted(row["person_id"] for row in tables_after_merge["memory_profiles"]) == sorted(
        [scene.persons["B"], scene.persons["B2"]]
    )
    assert disabled[0].fact_ids == () and disabled[1].fact_ids == ()
    assert list(enabled.fact_ids) == [b_public]
    scene.write(
        "H19-H21-merge-enable",
        {
            "facts": scene.facts,
            "before": before,
            "merged": merged,
            "tables_after_merge": tables_after_merge,
            "disabled": disabled,
            "enabled": enabled,
        },
    )


# ------------------------------------------------------------------ H7 H9 H32
async def test_h07_h09_h32_entry_check_is_all_or_nothing_per_entry(scene):
    """§7.4: every ID of an entry resolves to an available active successor, else the
    whole entry drops; the check never writes."""
    g1 = await scene.event("space:G", "B", "我爱吃辣，周末打羽毛球", key="g1")
    a1 = await scene.event("private:A", "A", "B 跟我说他最近失眠", key="a1")
    b1 = await scene.event("private:B", "B", "我在准备搬家", key="b1")
    g2 = await scene.event("space:G", "A", "我也爱打羽毛球", key="g2")
    memories = scene.chat._memories
    x = await scene.fact("x", "B", "爱吃辣", evidence=(g1,))
    y = await scene.fact("y", "B", "口味偏重", evidence=(g1,))
    z = await scene.fact("z", "B", "周末打羽毛球", evidence=(g1,))
    w = await scene.fact("w", "B", "作息规律", evidence=(g1,))
    p = await scene.fact("p", "B", "在准备搬家", evidence=(b1,))
    other = await scene.fact("other", "A", "也爱打羽毛球", evidence=(g2,))
    entries = (
        scene.entry("口味偏辣偏重", x, y, section="兴趣"),
        scene.entry("周末打羽毛球", z),
        scene.entry("作息规律", w),
        scene.entry("在准备搬家", p),
        scene.entry("和 A 都爱羽毛球", z, other),
        scene.entry("周末运动", y, z),
    )
    store, person = scene.profiles, scene.persons["B"]
    untouched = await store.check(person, "public", entries)

    assert await memories.invalidate_fact(
        x, reason=MemoryInvalidationReason.USER_RETRACTED, actor_user_id="10002"
    )
    successor = await memories.correct_fact(z, content="周末常打羽毛球", actor_user_id="10002")
    assert successor is not None
    async with scene.database.immediate_session() as writer:
        writer.add(
            MemoryEvidenceModel(
                fact_id=w,
                event_id=a1,
                source_speaker_user_id="10001",
                relation="self_statement",
                authority="self_report",
                excerpt="作息",
                created_at=datetime.now(UTC),
            )
        )
    tables = await scene.profile_tables()
    checked = await store.check(person, "public", entries)
    private = await store.check(person, "private", (scene.entry("在准备搬家", p),))

    assert [entry.text for entry in untouched.entries] == [
        "口味偏辣偏重",
        "周末打羽毛球",
        "作息规律",
        "周末运动",
    ]
    assert [(item.entry.text, item.fact_ids) for item in untouched.dropped] == [
        ("在准备搬家", (p,)),
        ("和 A 都爱羽毛球", (other,)),
    ]
    assert [(entry.text, entry.fact_ids) for entry in checked.entries] == [
        ("周末打羽毛球", (successor.id,)),
        ("周末运动", (y, successor.id)),
    ]
    assert [(item.entry.text, item.fact_ids) for item in checked.dropped] == [
        ("口味偏辣偏重", (x,)),
        ("作息规律", (w,)),
        ("在准备搬家", (p,)),
        ("和 A 都爱羽毛球", (other,)),
    ]
    assert checked.replaced == ((z, successor.id),)
    assert [entry.fact_ids for entry in private.entries] == [(p,)] and not private.dropped
    assert (
        tables
        == await scene.profile_tables()
        == {
            "memory_profiles": [],
            "memory_profile_versions": [],
            "memory_space_members": [],
        }
    )
    scene.write(
        "H07-H09-H32-check",
        {
            "facts": {**scene.facts, "successor": successor.id},
            "untouched": untouched,
            "checked": checked,
            "private": private,
        },
    )


# ------------------------------------------------------------- H13 H15 H16 H26
async def test_h13_h15_h16_h26_member_lists(scene):
    """Dream replaces only the spaces it ranked and never a pinned list; pins resolve
    roots, hold at most five distinct people and unpin keeps the rows."""
    from yuki.memory.profiles import MAX_SPACE_MEMBERS, ProfileValidationError

    async with scene.database.immediate_session() as writer:
        await merge_persons(writer, scene.persons["B2"], scene.persons["B"])
    store = scene.profiles
    persons, spaces = scene.persons, scene.spaces
    written = await store.replace_dream_members(
        {spaces["G"]: (persons["B"], persons["A"], persons["C"]), spaces["H"]: (persons["D"],)},
        dream_run_public_id="run-1",
    )
    first = await scene.profile_tables()
    pinned = await store.pin_members(spaces["G"], (persons["C"], persons["B2"]))
    codes = {}
    for code, wanted in (
        ("too_many_members", tuple(persons[key] for key in "ABCD") * 2),
        ("duplicate_member", (persons["B"], persons["B2"])),
        ("unknown_person", ("00000000-0000-4000-8000-000000000000",)),
        ("no_members", ()),
    ):
        with pytest.raises(ProfileValidationError) as rejected:
            await store.pin_members(spaces["G"], wanted)
        codes[code] = rejected.value.code
    second = await store.replace_dream_members(
        {spaces["G"]: (persons["A"],), spaces["H"]: (persons["B"], persons["D"])},
        dream_run_public_id="run-2",
    )
    during_pin = await scene.profile_tables()
    unpinned = await store.unpin_members(spaces["G"])
    after_unpin = await scene.profile_tables()
    third = await store.replace_dream_members(
        {spaces["G"]: (persons["A"],)}, dream_run_public_id="run-3"
    )
    final = await store.all_members()

    assert MAX_SPACE_MEMBERS == 5
    assert sorted(written) == sorted([spaces["G"], spaces["H"]])
    assert pinned.person_ids == (persons["C"], persons["B"]) and pinned.source == "owner"
    assert codes == {code: code for code in codes}
    assert second == (spaces["H"],)
    assert [
        (row["position"], row["person_id"], row["source"])
        for row in during_pin["memory_space_members"]
        if row["space_id"] == spaces["G"]
    ] == [(1, persons["C"], "owner"), (2, persons["B"], "owner")]
    assert unpinned is not None and unpinned.source == "dream"
    assert unpinned.person_ids == pinned.person_ids
    assert [row["source"] for row in after_unpin["memory_space_members"]].count("owner") == 0
    assert third == (spaces["G"],)
    assert {item.space_id: item.person_ids for item in final} == {
        spaces["G"]: (persons["A"],),
        spaces["H"]: (persons["B"], persons["D"]),
    }
    scene.write(
        "H13-H16-members",
        {"first": first, "during_pin": during_pin, "after_unpin": after_unpin, "codes": codes},
    )


# -------------------------------------------------------------------------- H20
async def test_h20_forgetting_a_person_deletes_affected_profiles(scene):
    """§4.5: forgetting B deletes B's profiles and those of persons whose facts lose
    evidence to B (receipts or events), with every version; member rows cascade."""
    b1 = await scene.event("private:B", "B", "我下个月要去旅行", key="b1")
    gb = await scene.event("space:G", "B", "C 是我们群里最会做饭的", key="gb")
    gc = await scene.event("space:G", "C", "我周末会做一桌菜请大家", key="gc")
    ga = await scene.event("space:G", "A", "我和 B 常一起下棋", key="ga")
    hd = await scene.event("space:H", "D", "我在学吉他", key="hd")
    b_fact = await scene.fact("b_fact", "B", "喜欢旅行", evidence=(gb,))
    c_fact = await scene.fact("c_fact", "C", "擅长做饭", evidence=(gc,))
    await scene.receipt_evidence(c_fact, gb, space="G", source_event_ids=(gb,))
    a_fact = await scene.fact("a_fact", "A", "常和人下棋", evidence=(ga,))
    async with scene.database.immediate_session() as writer:
        writer.add(
            MemoryEvidenceModel(
                fact_id=a_fact,
                event_id=gb,
                source_speaker_user_id="10002",
                relation="third_party_statement",
                authority="third_party",
                excerpt="下棋",
                created_at=datetime.now(UTC),
            )
        )
    d_fact = await scene.fact("d_fact", "D", "在学吉他", evidence=(hd,))
    store, persons = scene.profiles, scene.persons
    for key, fact_id, text in (
        ("B", b_fact, "喜欢旅行"),
        ("C", c_fact, "擅长做饭"),
        ("A", a_fact, "常下棋"),
        ("D", d_fact, "在学吉他"),
    ):
        await store.commit(
            persons[key],
            "public",
            (scene.entry(text, fact_id),),
            source="owner",
            reason="首版",
            expected_version=0,
        )
    for version in (1, 2):
        await store.commit(
            persons["B"],
            "private",
            (scene.entry(f"第 {version} 版", b_fact),),
            source="owner",
            reason="私人",
            expected_version=version - 1,
        )
    await store.commit(persons["C"], "private", (), source="owner", reason="空", expected_version=0)
    await store.replace_dream_members(
        {scene.spaces["G"]: (persons["B"], persons["C"], persons["A"])},
        dream_run_public_id="run-1",
    )
    before = await scene.profile_tables()
    assert b1

    forgot = await scene.command("B", "/ai forgetme")
    after = await scene.profile_tables()
    c_fact_left = await scene.chat._memories.get_fact(c_fact)

    assert forgot and "删除" in forgot[0]
    assert {row["person_id"] for row in after["memory_profiles"]} == {persons["D"]}
    assert {row["person_id"] for row in after["memory_profile_versions"]} == {persons["D"]}
    assert [(row["position"], row["person_id"]) for row in after["memory_space_members"]] == [
        (2, persons["C"]),
        (3, persons["A"]),
    ]
    assert c_fact_left is None
    assert await store.load(persons["C"], "public") is None
    scene.write("H20-forget", {"facts": scene.facts, "before": before, "after": after})


async def test_h22_person_core_sets_are_gone_from_the_runtime(scene):
    """H22: no person core-set row can exist; resident show/set no longer take --person."""
    from tests.support.memory_resident_scene import run_cli

    from yuki.persistence.models import MemoryResidentSetModel

    now = datetime.now(UTC)
    with pytest.raises(Exception) as rejected:
        async with scene.database.immediate_session() as writer:
            writer.add(
                MemoryResidentSetModel(
                    owner_kind="person",
                    owner_person_id=scene.persons["B"],
                    fact_ids_json="[]",
                    source="dream",
                    considered_through_fact_id=0,
                    revision=0,
                    created_at=now,
                    updated_at=now,
                )
            )
    cli = await run_cli(
        scene.database.url,
        "memory",
        "resident",
        "show",
        "--space",
        scene.spaces["G"],
        "--person",
        scene.persons["B"],
    )
    async with scene.database.read_session() as reader:
        stored = (await reader.scalars(select(MemoryResidentSetModel))).all()

    assert "CHECK constraint failed" in str(rejected.value)
    assert cli.returncode != 0 and "unrecognized arguments: --person" in cli.stderr
    assert stored == []
    scene.write("H22-runtime", {"cli_returncode": cli.returncode, "rows": len(stored)})
