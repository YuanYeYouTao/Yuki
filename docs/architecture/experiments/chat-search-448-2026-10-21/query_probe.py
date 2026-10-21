"""New #448 offline query-shape probe; not an implementation or old probe.

Current relevant columns/index/FTS DDL are projected from main 31507101.
Unrelated columns, CHECK/FK dependencies and application write triggers are omitted.
Synthetic events only. VM progress counts exclude FTS virtual-table internal work.
Posting table contains selected probe terms, so its size/write cost is NOT a full
normalization/ngram-index estimate. This probe isolates scope/order access paths.
"""

import datetime as dt
import json
import pathlib
import sqlite3
import statistics
import sys
import time
import uuid

OUT = pathlib.Path(__file__).parent
LIVE = (
    "(ce.suppression_status IS NULL OR ce.suppression_status='keeper') AND ce.event_kind='message'"
)
SCOPES = [str(uuid.uuid5(uuid.NAMESPACE_URL, "yuki448-probe/" + str(i))) for i in range(100)]
TARGET = SCOPES[0]
DDL = (
    "\nCREATE TABLE canonical_conversations(id VARCHAR(36) PRIMARY KEY, "
    "starts_after_event_id INTEGER NOT NULL, person_id VARCHAR(36), kind "
    "VARCHAR(16));\nCREATE TABLE persons(id VARCHAR(36) PRIMARY KEY, "
    "merged_into_person_id VARCHAR(36));\nCREATE INDEX "
    "ix_persons_merged_into_person_id ON "
    "persons(merged_into_person_id);\nCREATE TABLE chat_events(id INTEGER "
    "PRIMARY KEY, canonical_conversation_id VARCHAR(36), "
    "author_person_id VARCHAR(36), author_kind VARCHAR(16) NOT NULL, "
    "sender_user_id VARCHAR(64) NOT NULL, scope_type VARCHAR(16) NOT "
    "NULL, group_id VARCHAR(64), private_peer_user_id VARCHAR(64), "
    "occurred_at DATETIME NOT NULL, suppression_status VARCHAR(16), "
    "event_kind VARCHAR(32) NOT NULL, content TEXT NOT NULL, "
    "audio_transcript TEXT NOT NULL, segments_json TEXT NOT NULL, "
    "direction VARCHAR(16) NOT NULL);\nCREATE INDEX "
    "ix_chat_events_scope_time ON "
    "chat_events(scope_type,occurred_at);\nCREATE INDEX "
    "ix_chat_events_group_time ON "
    "chat_events(group_id,occurred_at);\nCREATE INDEX "
    "ix_chat_events_sender_time ON "
    "chat_events(sender_user_id,occurred_at);\nCREATE INDEX "
    "ix_chat_events_private_peer_time ON "
    "chat_events(private_peer_user_id,occurred_at);\nCREATE INDEX "
    "ix_chat_events_conversation_message_sender_time ON "
    "chat_events(canonical_conversation_id,sender_user_id,occurred_at,id)"
    " WHERE suppression_status='keeper' AND event_kind='message';\nCREATE "
    "INDEX ix_chat_events_social_source_author ON "
    "chat_events(author_person_id,id) WHERE direction='inbound' AND "
    "author_kind='person';\nCREATE INDEX "
    "ix_chat_events_canonical_conversation_id ON "
    "chat_events(canonical_conversation_id);\nCREATE TABLE "
    "conversation_media_items(source_event_id INTEGER NOT NULL, "
    "attachment_index INTEGER NOT NULL, conversation_id VARCHAR(36) NOT "
    "NULL, segment_index INTEGER NOT NULL, kind VARCHAR(16) NOT NULL, "
    "source_snapshot_json TEXT, PRIMARY "
    "KEY(source_event_id,attachment_index));\nCREATE INDEX "
    "ix_conversation_media_scope_event ON "
    "conversation_media_items(conversation_id,source_event_id);\nCREATE "
    "VIRTUAL TABLE chat_events_fts USING "
    "fts5(content,audio_transcript,content='chat_events',content_rowid='i"
    "d',tokenize='trigram');\n"
)


def measured(c, name, sql, args=()):
    plan = [list(r) for r in c.execute("EXPLAIN QUERY PLAN " + sql, args)]
    ticks = 0

    def progress():
        nonlocal ticks
        ticks += 1
        return 0

    c.set_progress_handler(progress, 100)
    rows = c.execute(sql, args).fetchall()
    c.set_progress_handler(None, 0)
    durations = []
    for _ in range(3):
        start = time.perf_counter()
        assert c.execute(sql, args).fetchall() == rows
        durations.append((time.perf_counter() - start) * 1000)
    return {
        "name": name,
        "sql": sql,
        "args": args,
        "plan": plan,
        "vm_steps_approx": ticks * 100,
        "vm_quantum": 100,
        "returned": len(rows),
        "first_ids": [r[0] for r in rows[:3]],
        "median_ms_warm": statistics.median(durations),
    }


def run(n):
    path = OUT / ("probe_" + str(n) + ".sqlite3")
    c = sqlite3.connect(path)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA cache_size=-32768")
    c.executescript(DDL)
    c.executemany(
        "INSERT INTO canonical_conversations VALUES (?,0,?,?)", [(s, "p0", "group") for s in SCOPES]
    )
    c.executemany(
        "INSERT INTO persons VALUES (?,?)",
        [(f"p{i}", "p0" if i in (1, 2, 3) else None) for i in range(128)],
    )
    epoch = dt.datetime(2026, 1, 1)

    def records():
        for i in range(1, n + 1):
            turn = i // 100
            scope = SCOPES[i % 100]
            sec = turn // 3 - (500 if i % 997 == 0 else 0)
            stamp = (epoch + dt.timedelta(seconds=sec)).strftime("%Y-%m-%d %H:%M:%S.%f")
            body = "共同词 基础聊天正文 编号" + str(i)
            if i % 5003 == 0:
                body += " 稀有词"
            media = '[{"type":"image","data":{"name":"a.png"}}]' if turn % 25 == 0 else "[]"
            yield (
                i,
                scope,
                f"p{turn % 128}",
                "person",
                f"acct{turn % 128}",
                "group",
                f"g{i % 100}",
                None,
                stamp,
                "keeper",
                "message",
                body,
                "",
                media,
                "inbound",
            )

    c.executemany("INSERT INTO chat_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", records())
    c.execute(
        "INSERT INTO conversation_media_items SELECT "
        "id,0,canonical_conversation_id,0,'image',NULL FROM chat_events "
        "WHERE segments_json!='[]'"
    )
    c.execute("INSERT INTO chat_events_fts(chat_events_fts) VALUES('rebuild')")
    c.commit()
    c.execute("ANALYZE")
    c.commit()
    results = []
    base = " FROM chat_events AS ce WHERE ce.canonical_conversation_id=? AND ce.id>0 AND " + LIVE
    order = " ORDER BY ce.occurred_at DESC,ce.id DESC LIMIT 21"
    key = c.execute(
        "SELECT occurred_at,id" + base + " ORDER BY occurred_at DESC,id DESC LIMIT 1 OFFSET ?",
        (TARGET, n // 100 * 3 // 4),
    ).fetchone()
    select = "SELECT ce.id,ce.occurred_at"

    def add(name, sql, args=()):
        results.append(measured(c, name, sql, args))

    add("baseline_scope_time_first", select + base + order, (TARGET,))
    add(
        "baseline_short_absent",
        select + base + " AND instr(ce.content,?)>0" + order,
        (TARGET, "无"),
    )
    add(
        "baseline_short_common",
        select + base + " AND instr(ce.content,?)>0" + order,
        (TARGET, "共"),
    )
    bounds = (
        "WITH search_bounds AS MATERIALIZED (SELECT (SELECT ce.id"
        + base
        + " ORDER BY ce.id ASC LIMIT 1) AS first_id,(SELECT ce.id"
        + base
        + " ORDER BY ce.id DESC LIMIT 1) AS last_id) "
    )
    current_fts = (
        bounds
        + select
        + base
        + (
            " AND ce.id IN (SELECT rowid FROM chat_events_fts WHERE "
            "chat_events_fts MATCH ? AND rowid>=(SELECT first_id FROM "
            "search_bounds) AND rowid<=(SELECT last_id FROM search_bounds))"
        )
        + order
    )
    add("current_fts_common_global", current_fts, (TARGET, TARGET, TARGET, '"共同词"'))
    add("current_fts_absent_global", current_fts, (TARGET, TARGET, TARGET, '"不存在词"'))
    c.execute(
        "CREATE INDEX ix_probe_scope_time ON "
        "chat_events(canonical_conversation_id,occurred_at,id) WHERE "
        "(suppression_status IS NULL OR suppression_status='keeper') AND "
        "event_kind='message'"
    )
    c.execute(
        "CREATE INDEX ix_probe_scope_author_time ON "
        "chat_events(canonical_conversation_id,author_person_id,occurred_at,i"
        "d) WHERE (suppression_status IS NULL OR "
        "suppression_status='keeper') AND event_kind='message'"
    )
    c.execute("ANALYZE")
    c.commit()
    add("indexed_scope_time_first", select + base + order, (TARGET,))
    add(
        "indexed_short_absent", select + base + " AND instr(ce.content,?)>0" + order, (TARGET, "无")
    )
    add(
        "indexed_short_common", select + base + " AND instr(ce.content,?)>0" + order, (TARGET, "共")
    )
    add(
        "keyset_rowvalue_deep",
        select + base + " AND (ce.occurred_at,ce.id)<(?,?)" + order,
        (TARGET, *key),
    )
    add(
        "keyset_or_deep",
        select + base + " AND (ce.occurred_at<? OR (ce.occurred_at=? AND ce.id<?))" + order,
        (TARGET, key[0], *key),
    )
    add(
        "offset_deep",
        select + base + " ORDER BY ce.occurred_at DESC,ce.id DESC LIMIT 21 OFFSET ?",
        (TARGET, n // 100 * 3 // 4),
    )
    family = (
        "SELECT id FROM persons WHERE id=(SELECT "
        "coalesce(merged_into_person_id,id) FROM persons WHERE id=?) OR "
        "merged_into_person_id=(SELECT coalesce(merged_into_person_id,id) "
        "FROM persons WHERE id=?)"
    )
    family_sql = select + base + " AND ce.author_person_id IN (" + family + ")" + order
    add("family_in_subquery", family_sql, (TARGET, "p0", "p0"))
    add(
        "family_in_literal",
        select + base + " AND ce.author_person_id IN (?,?,?,?)" + order,
        (TARGET, "p0", "p1", "p2", "p3"),
    )
    pieces = []
    args = []
    for author in ("p0", "p1", "p2", "p3"):
        pieces.append(
            "SELECT * FROM (" + select + base + " AND ce.author_person_id=?" + order + ")"
        )
        args.extend((TARGET, author))
    add(
        "family_per_author_top21_union",
        "SELECT * FROM ("
        + " UNION ALL ".join(pieces)
        + ") ORDER BY occurred_at DESC,id DESC LIMIT 21",
        args,
    )
    add(
        "family_force_scope_time",
        select
        + (
            " FROM chat_events AS ce INDEXED BY ix_probe_scope_time WHERE "
            "ce.canonical_conversation_id=? AND ce.id>0 AND "
        )
        + LIVE
        + " AND ce.author_person_id IN (?,?,?,?)"
        + order,
        (TARGET, "p0", "p1", "p2", "p3"),
    )
    add("indexed_fts_common_global", current_fts, (TARGET, TARGET, TARGET, '"共同词"'))
    add(
        "has_image_json",
        select
        + base
        + (
            " AND EXISTS (SELECT 1 FROM json_each(ce.segments_json) AS segment "
            "WHERE json_extract(segment.value,'$.type')='image')"
        )
        + order,
        (TARGET,),
    )
    add(
        "has_image_media_pk",
        select
        + base
        + (
            " AND EXISTS (SELECT 1 FROM conversation_media_items AS m WHERE "
            "m.source_event_id=ce.id AND m.kind='image' AND "
            "m.source_snapshot_json IS NULL)"
        )
        + order,
        (TARGET,),
    )
    anchors = [100, n // 2 // 100 * 100, n // 100 * 100]
    bounded = []
    args = []
    for anchor in anchors:
        for op, direction in (("<", "DESC"), (">", "ASC")):
            bounded.append(
                "SELECT * FROM (SELECT ce.id"
                + base
                + " AND ce.id"
                + op
                + "? ORDER BY ce.id "
                + direction
                + " LIMIT 6)"
            )
            args.extend((TARGET, anchor))
    add(
        "around_three_bounded_union",
        "SELECT DISTINCT id FROM (" + " UNION ALL ".join(bounded) + ") ORDER BY id",
        args,
    )
    add(
        "around_bad_minmax_scan",
        "SELECT ce.id" + base + " AND ce.id BETWEEN ? AND ? ORDER BY ce.id",
        (TARGET, anchors[0], anchors[-1]),
    )
    c.execute(
        "CREATE VIRTUAL TABLE scoped_fts USING "
        "fts5(scope_key,content,audio_transcript,tokenize='trigram')"
    )
    c.execute(
        "INSERT INTO scoped_fts(rowid,scope_key,content,audio_transcript) "
        "SELECT id,canonical_conversation_id,content,audio_transcript FROM "
        "chat_events"
    )
    c.commit()
    scope_match = 'scope_key : "' + TARGET + '" AND content : "共同词"'
    add(
        "fts_scope_posting_intersection",
        select
        + base
        + " AND ce.id IN (SELECT rowid FROM scoped_fts WHERE scoped_fts MATCH ?)"
        + order,
        (TARGET, scope_match),
    )
    add(
        "fts_scope_posting_absent",
        select
        + base
        + " AND ce.id IN (SELECT rowid FROM scoped_fts WHERE scoped_fts MATCH ?)"
        + order,
        (TARGET, 'scope_key : "' + TARGET + '" AND content : "不存在词"'),
    )
    c.execute(
        "CREATE TABLE scope_postings(conversation_id VARCHAR(36),term "
        "TEXT,occurred_at DATETIME,event_id INTEGER,PRIMARY "
        "KEY(conversation_id,term,occurred_at,event_id)) WITHOUT ROWID"
    )
    c.execute(
        "INSERT INTO scope_postings SELECT "
        "canonical_conversation_id,'共',occurred_at,id FROM chat_events"
    )
    c.execute(
        "INSERT INTO scope_postings SELECT "
        "canonical_conversation_id,'共同词',occurred_at,id FROM chat_events"
    )
    c.execute(
        "INSERT INTO scope_postings SELECT "
        "canonical_conversation_id,'稀有词',occurred_at,id FROM chat_events "
        "WHERE instr(content,'稀有词')>0"
    )
    c.commit()
    add(
        "btree_scoped_short_common",
        (
            "SELECT event_id,occurred_at FROM scope_postings WHERE "
            "conversation_id=? AND term=? ORDER BY occurred_at DESC,event_id "
            "DESC LIMIT 21"
        ),
        (TARGET, "共"),
    )
    add(
        "btree_scoped_short_absent",
        (
            "SELECT event_id,occurred_at FROM scope_postings WHERE "
            "conversation_id=? AND term=? ORDER BY occurred_at DESC,event_id "
            "DESC LIMIT 21"
        ),
        (TARGET, "无"),
    )
    add(
        "btree_scoped_short_deep",
        (
            "SELECT event_id,occurred_at FROM scope_postings WHERE "
            "conversation_id=? AND term=? AND (occurred_at,event_id)<(?,?) ORDER "
            "BY occurred_at DESC,event_id DESC LIMIT 21"
        ),
        (TARGET, "共", *key),
    )
    ids = {r["name"]: r["first_ids"] for r in results}
    assert ids["keyset_rowvalue_deep"] == ids["keyset_or_deep"]
    assert (
        ids["family_in_subquery"]
        == ids["family_per_author_top21_union"]
        == ids["family_force_scope_time"]
    )
    assert ids["has_image_json"] == ids["has_image_media_pk"]
    assert (
        ids["current_fts_common_global"]
        == ids["fts_scope_posting_intersection"]
        == ids["btree_scoped_short_common"]
    )
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    schema = "\n".join(
        row[0] + ";"
        for row in c.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name"
        )
    )
    (OUT / ("schema_" + str(n) + ".sql")).write_text(schema, encoding="utf-8")
    c.close()
    result = {
        "rows": n,
        "conversations": 100,
        "target_rows": n // 100,
        "deep_cursor": key,
        "results": results,
        "db_bytes": path.stat().st_size,
    }
    (OUT / ("results_" + str(n) + ".json")).write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "rows": n,
                "results": [
                    {k: r[k] for k in ("name", "returned", "vm_steps_approx", "median_ms_warm")}
                    for r in results
                ],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return result


meta = {
    "python": sys.version,
    "sqlite": sqlite3.sqlite_version,
    "source_head": "31507101",
    "scope": "synthetic related-DDL query-shape experiment; no production data",
    ("vm_limit"): (
        "progress handler counts VDBE instructions, not virtual-table internal doclist work"
    ),
    "timing": "three warm-cache repeats; not production latency",
    ("full_index_limit"): (
        "scope_postings stores selected terms only; no full gram storage/write claim"
    ),
}
(OUT / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
all_results = [run(int(n)) for n in sys.argv[1:]]
(OUT / "all_results.json").write_text(
    json.dumps({"metadata": meta, "runs": all_results}, ensure_ascii=False, indent=2),
    encoding="utf-8",
)
