"""Query-shape appendix: author-ID and date/media filtering in ledger-ID order."""

import json
import pathlib
import sqlite3
import uuid

OUT = pathlib.Path(__file__).parent
TARGET = str(uuid.uuid5(uuid.NAMESPACE_URL, "yuki448-probe/0"))
LIVE = (
    "(ce.suppression_status IS NULL OR ce.suppression_status='keeper') AND ce.event_kind='message'"
)
allruns = []
for n in (50000, 200000, 400000):
    source = sqlite3.connect(OUT / ("probe_" + str(n) + ".sqlite3"))
    c = sqlite3.connect(":memory:")
    source.backup(c)
    source.close()
    c.execute(
        (
            "CREATE INDEX ix_probe_scope_author ON "
            "chat_events(canonical_conversation_id,author_person_id) WHERE "
        )
        + LIVE.replace("ce.", "")
    )
    c.execute("ANALYZE")
    c.commit()
    results = []

    def add(name, sql, args, *, c=c, results=results):
        ticks = [0]
        c.set_progress_handler(lambda: ticks.__setitem__(0, ticks[0] + 1) or 0, 1)
        rows = c.execute(sql, args).fetchall()
        c.set_progress_handler(None, 0)
        results.append(
            dict(
                name=name,
                sql=sql,
                args=args,
                plan=[list(r) for r in c.execute("EXPLAIN QUERY PLAN " + sql, args)],
                vm_steps_exact=ticks[0],
                returned=len(rows),
                first_ids=[r[0] for r in rows[:3]],
            )
        )

    sel = "SELECT ce.id,ce.occurred_at"
    base = " FROM chat_events ce WHERE ce.canonical_conversation_id=? AND ce.id>0 AND " + LIVE
    order = " ORDER BY ce.id DESC LIMIT 21"
    add(
        "author_id_in",
        sel + base + " AND ce.author_person_id IN (?,?,?,?)" + order,
        (TARGET, "p0", "p1", "p2", "p3"),
    )
    add(
        "author_id_in_force_author",
        sel
        + (
            " FROM chat_events ce INDEXED BY ix_probe_scope_author WHERE "
            "ce.canonical_conversation_id=? AND ce.id>0 AND "
        )
        + LIVE
        + " AND ce.author_person_id IN (?,?,?,?)"
        + order,
        (TARGET, "p0", "p1", "p2", "p3"),
    )
    pieces = []
    params = []
    for author in ("p0", "p1", "p2", "p3"):
        pieces.append("SELECT * FROM (" + sel + base + " AND ce.author_person_id=?" + order + ")")
        params.extend((TARGET, author))
    add(
        "author_id_top21_union",
        "SELECT * FROM (" + " UNION ALL ".join(pieces) + ") ORDER BY id DESC LIMIT 21",
        params,
    )
    add(
        "media_candidate_existing_index",
        (
            "SELECT DISTINCT m.source_event_id FROM conversation_media_items m "
            "WHERE m.conversation_id=? AND m.source_snapshot_json IS NULL AND "
            "m.kind='image' AND m.source_event_id>0 ORDER BY m.source_event_id "
            "DESC LIMIT 21"
        ),
        (TARGET,),
    )
    date_range = ("2026-01-01 00:00:20.000000", "2026-01-01 00:00:30.000000")
    args = (TARGET, *date_range)
    pred = " AND ce.occurred_at>=? AND ce.occurred_at<?"
    add("date_id_auto", sel + base + pred + order, args)
    add(
        "date_id_force_conv",
        sel
        + (
            " FROM chat_events ce INDEXED BY "
            "ix_chat_events_canonical_conversation_id WHERE "
            "ce.canonical_conversation_id=? AND ce.id>0 AND "
        )
        + LIVE
        + pred
        + order,
        args,
    )
    add(
        "date_id_force_time",
        sel
        + (
            " FROM chat_events ce INDEXED BY ix_probe_scope_time WHERE "
            "ce.canonical_conversation_id=? AND ce.id>0 AND "
        )
        + LIVE
        + pred
        + order,
        args,
    )
    add(
        "date_absent_force_conv",
        sel
        + (
            " FROM chat_events ce INDEXED BY "
            "ix_chat_events_canonical_conversation_id WHERE "
            "ce.canonical_conversation_id=? AND ce.id>0 AND "
        )
        + LIVE
        + pred
        + order,
        (TARGET, "2020-01-01 00:00:00.000000", "2020-01-02 00:00:00.000000"),
    )
    add(
        "date_absent_force_time",
        sel
        + (
            " FROM chat_events ce INDEXED BY ix_probe_scope_time WHERE "
            "ce.canonical_conversation_id=? AND ce.id>0 AND "
        )
        + LIVE
        + pred
        + order,
        (TARGET, "2020-01-01 00:00:00.000000", "2020-01-02 00:00:00.000000"),
    )
    data = dict(rows=n, results=results)
    allruns.append(data)
    print(
        json.dumps(
            {
                "rows": n,
                "results": [
                    {k: r[k] for k in ("name", "returned", "vm_steps_exact")} for r in results
                ],
            }
        ),
        flush=True,
    )
    c.close()
(OUT / "metadata_results.json").write_text(
    json.dumps(allruns, ensure_ascii=False, indent=2), encoding="utf-8"
)
