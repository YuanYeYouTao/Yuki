"""Appendix: ledger-ID ordering, current implicit rowid index and scope-prefixed
FTS gram lookup. Selected terms only, not a full normalization/write-size probe.
"""

import json
import pathlib
import sqlite3
import statistics
import time
import uuid

OUT = pathlib.Path(__file__).parent
TARGET = str(uuid.uuid5(uuid.NAMESPACE_URL, "yuki448-probe/0"))
LIVE = (
    "(ce.suppression_status IS NULL OR ce.suppression_status='keeper') AND ce.event_kind='message'"
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
    return dict(
        name=name,
        sql=sql,
        args=args,
        plan=plan,
        vm_steps_approx=ticks * 100,
        vm_quantum=100,
        returned=len(rows),
        first_ids=[r[0] for r in rows[:3]],
        median_ms_warm=statistics.median(durations),
    )


allruns = []
for n in (50000, 200000, 400000):
    source = sqlite3.connect(OUT / ("probe_" + str(n) + ".sqlite3"))
    c = sqlite3.connect(OUT / ("id_" + str(n) + ".sqlite3"))
    source.backup(c)
    source.close()
    c.execute("PRAGMA cache_size=-32768")
    c.execute("DROP INDEX ix_probe_scope_time")
    c.execute("DROP INDEX ix_probe_scope_author_time")
    c.execute("ANALYZE")
    c.commit()
    results = []

    def add(name, sql, args=(), *, results=results, c=c):
        results.append(measured(c, name, sql, args))

    sel = "SELECT ce.id,ce.occurred_at"
    base = " FROM chat_events AS ce WHERE ce.canonical_conversation_id=? AND ce.id>0 AND " + LIVE
    order = " ORDER BY ce.id DESC LIMIT 21"
    key = n // 4 // 100 * 100
    add("current_scope_id_first", sel + base + order, (TARGET,))
    add("current_scope_id_deep", sel + base + " AND ce.id<?" + order, (TARGET, key))
    add(
        "current_scope_id_absent", sel + base + " AND instr(ce.content,?)>0" + order, (TARGET, "无")
    )
    add(
        "current_scope_id_author_family",
        sel + base + " AND ce.author_person_id IN (?,?,?,?)" + order,
        (TARGET, "p0", "p1", "p2", "p3"),
    )
    c.execute(
        "CREATE INDEX ix_probe_visible_scope ON "
        "chat_events(canonical_conversation_id) WHERE (suppression_status IS "
        "NULL OR suppression_status='keeper') AND event_kind='message'"
    )
    c.execute("ANALYZE")
    c.commit()
    add("visible_scope_id_first", sel + base + order, (TARGET,))
    add("visible_scope_id_deep", sel + base + " AND ce.id<?" + order, (TARGET, key))
    add(
        "visible_scope_id_absent", sel + base + " AND instr(ce.content,?)>0" + order, (TARGET, "无")
    )
    anchors = [100, n // 2 // 100 * 100, n // 100 * 100]
    pieces = []
    args = []
    for anchor in anchors:
        for op, direction in (("<", "DESC"), (">", "ASC")):
            pieces.append(
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
        "id_around_bounded_union",
        "SELECT DISTINCT id FROM (" + " UNION ALL ".join(pieces) + ") ORDER BY id",
        args,
    )
    prefix = "s" + TARGET.replace("-", "") + "g"
    c.execute(
        "CREATE VIRTUAL TABLE scope_grams USING "
        "fts5(terms,content='',tokenize='ascii',detail=none,columnsize=0)"
    )
    rows = []
    for eid, scope, body in c.execute(
        "SELECT id,canonical_conversation_id,content FROM chat_events"
    ):
        stem = "s" + scope.replace("-", "") + "g"
        terms = stem + "u5171 " + stem + "u5171u540cu8bcd"
        if "稀有词" in body:
            terms += " " + stem + "u7a00u6709u8bcd"
        rows.append((eid, terms))
        if len(rows) == 2000:
            c.executemany("INSERT INTO scope_grams(rowid,terms) VALUES (?,?)", rows)
            rows = []
    if rows:
        c.executemany("INSERT INTO scope_grams(rowid,terms) VALUES (?,?)", rows)
    c.commit()
    exact = prefix + "u5171u540cu8bcd"
    fbase = (
        (
            " FROM scope_grams CROSS JOIN chat_events AS ce ON "
            "ce.id=scope_grams.rowid WHERE scope_grams MATCH ? AND "
            "ce.canonical_conversation_id=? AND "
        )
        + LIVE
        + " AND scope_grams.rowid>0"
    )
    add(
        "scoped_gram_id_common",
        sel + fbase + " ORDER BY scope_grams.rowid DESC LIMIT 21",
        (exact, TARGET),
    )
    add(
        "scoped_gram_id_common_deep",
        sel + fbase + " AND scope_grams.rowid<? ORDER BY scope_grams.rowid DESC LIMIT 21",
        (exact, TARGET, key),
    )
    add(
        "scoped_gram_id_absent",
        sel + fbase + " ORDER BY scope_grams.rowid DESC LIMIT 21",
        (prefix + "u65e0", TARGET),
    )
    add(
        "scoped_gram_time_common",
        sel + fbase + " ORDER BY ce.occurred_at DESC,ce.id DESC LIMIT 21",
        (exact, TARGET),
    )
    add(
        "scoped_gram_id_author_family",
        sel
        + fbase
        + " AND ce.author_person_id IN (?,?,?,?) ORDER BY scope_grams.rowid DESC LIMIT 21",
        (exact, TARGET, "p0", "p1", "p2", "p3"),
    )
    add(
        "global_current_fts_id_common",
        sel
        + base
        + " AND ce.id IN (SELECT rowid FROM chat_events_fts WHERE chat_events_fts MATCH ?)"
        + order,
        (TARGET, '"共同词"'),
    )
    raw_scope = 'scope_key : "' + TARGET + '" AND content : "共同词"'
    rsbase = (
        (
            " FROM scoped_fts CROSS JOIN chat_events AS ce ON "
            "ce.id=scoped_fts.rowid WHERE scoped_fts MATCH ? AND "
            "ce.canonical_conversation_id=? AND "
        )
        + LIVE
        + " AND scoped_fts.rowid>0"
    )
    add(
        "raw_scoped_intersection_id_common",
        sel + rsbase + " ORDER BY scoped_fts.rowid DESC LIMIT 21",
        (raw_scope, TARGET),
    )
    add(
        "raw_scoped_intersection_id_absent",
        sel + rsbase + " ORDER BY scoped_fts.rowid DESC LIMIT 21",
        ('scope_key : "' + TARGET + '" AND content : "不存在词"', TARGET),
    )
    # Non-message density isolates benefit of a partial visible-message index.
    c.execute("UPDATE chat_events SET event_kind='external_event' WHERE id>?", (n // 2,))
    c.execute("ANALYZE")
    c.commit()
    add(
        "density_half_current_conv",
        sel
        + (
            " FROM chat_events AS ce INDEXED BY "
            "ix_chat_events_canonical_conversation_id WHERE "
            "ce.canonical_conversation_id=? AND ce.id>0 AND "
        )
        + LIVE
        + order,
        (TARGET,),
    )
    add(
        "density_half_partial_conv",
        sel
        + (
            " FROM chat_events AS ce INDEXED BY ix_probe_visible_scope WHERE "
            "ce.canonical_conversation_id=? AND ce.id>0 AND "
        )
        + LIVE
        + order,
        (TARGET,),
    )
    data = dict(
        rows=n,
        scope_rows=n // 100,
        results=results,
        notes=(
            "selected scope-prefixed terms only; partial-density case makes "
            "latest half external-event; not full posting-storage/write benchmark"
        ),
    )
    (OUT / ("id_results_" + str(n) + ".json")).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    c.close()
    allruns.append(data)
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
(OUT / "all_id_results.json").write_text(
    json.dumps(allruns, ensure_ascii=False, indent=2), encoding="utf-8"
)
