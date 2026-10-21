"""Author family top-N with one large Conversation; synthetic, no FTS claims."""

import json
import pathlib
import sqlite3
import uuid

OUT = pathlib.Path(__file__).parent
target = str(uuid.uuid5(uuid.NAMESPACE_URL, "yuki448-probe/0"))
live = (
    "(ce.suppression_status IS NULL OR ce.suppression_status='keeper') AND ce.event_kind='message'"
)
out = []
for n in (50000, 200000, 400000):
    source = sqlite3.connect(OUT / ("probe_" + str(n) + ".sqlite3"))
    c = sqlite3.connect(":memory:")
    source.backup(c)
    source.close()
    c.execute("UPDATE chat_events SET canonical_conversation_id=?", (target,))
    c.execute(
        (
            "CREATE INDEX ix_probe_scope_author ON "
            "chat_events(canonical_conversation_id,author_person_id) WHERE "
        )
        + live.replace("ce.", "")
    )
    c.execute("ANALYZE")
    c.commit()

    def run(name, sql, args, *, c=c, n=n):
        steps = [0]
        c.set_progress_handler(lambda: steps.__setitem__(0, steps[0] + 1) or 0, 1)
        rows = c.execute(sql, args).fetchall()
        c.set_progress_handler(None, 0)
        out.append(
            dict(
                rows=n,
                distribution="one-conversation",
                name=name,
                sql=sql,
                args=args,
                plan=[list(r) for r in c.execute("EXPLAIN QUERY PLAN " + sql, args)],
                vm_steps_exact=steps[0],
                returned=len(rows),
                ids=[r[0] for r in rows],
            )
        )

    select = "SELECT ce.id"
    base = " FROM chat_events ce WHERE ce.canonical_conversation_id=? AND ce.id>0 AND " + live
    order = " ORDER BY ce.id DESC LIMIT 21"
    run(
        "author_in_auto",
        select + base + " AND ce.author_person_id IN (?,?,?,?)" + order,
        (target, "p0", "p1", "p2", "p3"),
    )
    run(
        "author_in_force_index",
        select
        + (
            " FROM chat_events ce INDEXED BY ix_probe_scope_author WHERE "
            "ce.canonical_conversation_id=? AND ce.id>0 AND "
        )
        + live
        + " AND ce.author_person_id IN (?,?,?,?)"
        + order,
        (target, "p0", "p1", "p2", "p3"),
    )
    pieces = []
    args = []
    for person in ("p0", "p1", "p2", "p3"):
        pieces.append(
            "SELECT * FROM (" + select + base + " AND ce.author_person_id=?" + order + ")"
        )
        args.extend((target, person))
    run(
        "author_union_top21",
        "SELECT * FROM (" + " UNION ALL ".join(pieces) + ") ORDER BY id DESC LIMIT 21",
        args,
    )
    c.close()
(OUT / "author_dense_results.json").write_text(
    json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(json.dumps([{k: r[k] for k in ("rows", "name", "vm_steps_exact", "returned")} for r in out]))
