"""Read-only appendix: duplicate lower-bound versus max(floor,anchor)."""

import json
import pathlib
import sqlite3
import uuid

OUT = pathlib.Path(__file__).parent
scope = str(uuid.uuid5(uuid.NAMESPACE_URL, "yuki448-probe/0"))
results = []
for n in (50000, 200000, 400000):
    c = sqlite3.connect(
        "file:" + str(OUT / ("probe_" + str(n) + ".sqlite3")) + "?mode=ro", uri=True
    )
    for anchor in (n // 2 // 100 * 100, n // 100 * 100):
        for variant in ("duplicated_lower", "single_max_lower"):
            where = "ce.id>? AND ce.id>?" if variant == "duplicated_lower" else "ce.id>?"
            args = (scope, 0, anchor) if variant == "duplicated_lower" else (scope, max(0, anchor))
            sql = (
                (
                    "SELECT ce.id FROM chat_events ce INDEXED BY "
                    "ix_chat_events_canonical_conversation_id WHERE "
                    "ce.canonical_conversation_id=? AND "
                )
                + where
                + (
                    " AND (ce.suppression_status IS NULL OR "
                    "ce.suppression_status='keeper') AND ce.event_kind='message' ORDER "
                    "BY ce.id ASC LIMIT 6"
                )
            )
            count = [0]
            c.set_progress_handler(lambda count=count: count.__setitem__(0, count[0] + 1) or 0, 1)
            rows = c.execute(sql, args).fetchall()
            c.set_progress_handler(None, 0)
            results.append(
                dict(
                    rows=n,
                    anchor=anchor,
                    variant=variant,
                    sql=sql,
                    args=args,
                    plan=[list(r) for r in c.execute("EXPLAIN QUERY PLAN " + sql, args)],
                    vm_steps_exact=count[0],
                    ids=[r[0] for r in rows],
                )
            )
    c.close()
(OUT / "around_results.json").write_text(
    json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(json.dumps(results, ensure_ascii=False))
