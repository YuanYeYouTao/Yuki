"""Independent scope-token AND global grams comparison; imports only our new TEMP generator."""

import json
import platform
import sqlite3
import statistics
import time
from pathlib import Path

from scoped_grams_probe import REPEATS, TARGET_ROWS, expression, grams, make_rows

out = {
    "environment": {
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "storage": ":memory:",
        "target_scope_rows": TARGET_ROWS,
        "repeats": REPEATS,
    },
    "limitations": [
        "Synthetic only; no OpenCC/ASR/runtime/production I/O.",
        "VDBE and ledger predicate counts do not capture internal FTS intersections.",
    ],
    "cases": [],
}
for scopes in (100, 1000):
    rows = make_rows(scopes)
    db = sqlite3.connect(":memory:")
    db.execute("create table ev(id integer primary key,scope integer not null,body text not null)")
    db.execute("create index ev_scope on ev(scope)")
    db.executemany("insert into ev values(?,?,?)", rows)
    db.commit()
    base = (
        db.execute("pragma page_count").fetchone()[0] * db.execute("pragma page_size").fetchone()[0]
    )
    db.execute(
        "create virtual table ft using fts5(tokens,content='',"
        "contentless_delete=1,detail=none,tokenize='ascii')"
    )
    t = time.perf_counter()
    db.executemany(
        "insert into ft(rowid,tokens) values(?,?)",
        ((i, " ".join([*grams(body), "s" + format(scope, "032x")])) for i, scope, body in rows),
    )
    db.commit()
    case = {
        "events": len(rows),
        "scopes": scopes,
        "build_seconds": round(time.perf_counter() - t, 6),
        "index_allocated_bytes": db.execute("pragma page_count").fetchone()[0]
        * db.execute("pragma page_size").fetchone()[0]
        - base,
        "queries": {},
    }
    for query in ("京", "预算", "仅外域", "三千北京"):
        expr = '"s' + format(0, "032x") + '" AND (' + expression(query) + ")"
        sql = (
            "select ev.id from ft cross join ev on ev.id=ft.rowid "
            "where ft match ? and ev.scope=0 order by ft.rowid desc limit 21"
        )
        db.execute(sql, (expr,)).fetchall()
        timings = []
        for _ in range(REPEATS):
            t = time.perf_counter()
            found = db.execute(sql, (expr,)).fetchall()
            timings.append((time.perf_counter() - t) * 1000)
        expected = [i for i, scope, body in reversed(rows) if scope == 0 and query in body][:21]
        assert [r[0] for r in found] == expected
        checks = {"ledger_scope_predicate_evaluations": 0}

        def count_scope(value, checks=checks):
            checks["ledger_scope_predicate_evaluations"] += 1
            return value

        db.create_function("count_scope", 1, count_scope)
        assert (
            db.execute(sql.replace("ev.scope=0", "count_scope(ev.scope)=0"), (expr,)).fetchall()
            == found
        )
        case["queries"][query] = {
            "match_candidates_total": db.execute(
                "select count(*) from ft where ft match ?", (expr,)
            ).fetchone()[0],
            "returned_count": len(found),
            "median_ms": round(statistics.median(timings), 6),
            **checks,
            "query_plan": db.execute("explain query plan " + sql, (expr,)).fetchall(),
        }
    out["cases"].append(case)
    db.close()
Path(__file__).with_suffix(".json").write_text(
    json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(json.dumps(out, ensure_ascii=False, indent=2))
