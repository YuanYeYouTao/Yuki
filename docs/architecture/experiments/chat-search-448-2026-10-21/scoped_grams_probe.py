"""Independent in-memory scoped gram experiment; no Yuki imports or production I/O.

Synthetic posting-locality evidence only. Wall times include the local runtime and
must not be treated as production latency. VDBE/progress counters cannot account
for all work inside an FTS virtual-table method; none are used as a cost proof.
The counting UDF below measures ledger predicate evaluations in a separate run,
not hidden FTS work. The probe does not model OpenCC, ASR or real permissions.
"""

import argparse
import json
import platform
import random
import sqlite3
import statistics
import time
from pathlib import Path

TARGET_ROWS = 200
REPEATS = 20
POOL = "今天下午晚上事情工作学习电脑消息链接图片音乐预算快乐讨论天气北京上海你好谢谢"


def make_rows(scopes):
    rng = random.Random(44899)
    out = []
    for ordinal in range(TARGET_ROWS):
        for scope in range(scopes):
            local_rng = random.Random(44899 + ordinal) if scope == 0 else rng
            body = (
                "电动车预算三千北京"
                + ("仅外域" if scope else "")
                + "".join(local_rng.choices(POOL, k=8))
            )
            out.append((ordinal * scopes + scope + 1, scope, body))
    return out


def grams(text, scope=None):
    prefix = "" if scope is None else "s" + format(scope, "032x")
    return sorted(
        {
            prefix + "g" + str(k) + text[j : j + k].encode().hex()
            for k in (1, 2, 3)
            for j in range(len(text) - k + 1)
        }
    )


def expression(query, scope=None):
    k = min(3, len(query))
    prefix = "" if scope is None else "s" + format(scope, "032x")
    return " AND ".join(
        '"' + prefix + "g" + str(k) + query[j : j + k].encode().hex() + '"'
        for j in range(len(query) - k + 1)
    )


def benchmark(scopes):
    rows = make_rows(scopes)
    output = {"scopes": scopes, "events": len(rows), "target_scope_rows": TARGET_ROWS, "modes": {}}
    for scoped in (False, True):
        db = sqlite3.connect(":memory:")
        db.execute(
            "create table ev(id integer primary key, scope integer not null, body text not null)"
        )
        db.execute("create index ev_scope_id on ev(scope,id)")
        db.executemany("insert into ev values(?,?,?)", rows)
        db.commit()
        base = (
            db.execute("pragma page_count").fetchone()[0]
            * db.execute("pragma page_size").fetchone()[0]
        )
        db.execute(
            "create virtual table ft using fts5(tokens, content='',"
            "contentless_delete=1,detail=none,tokenize='ascii')"
        )
        started = time.perf_counter()
        db.executemany(
            "insert into ft(rowid,tokens) values(?,?)",
            ((i, " ".join(grams(body, scope if scoped else None))) for i, scope, body in rows),
        )
        db.commit()
        build = time.perf_counter() - started
        size = (
            db.execute("pragma page_count").fetchone()[0]
            * db.execute("pragma page_size").fetchone()[0]
            - base
        )
        out = {"build_seconds": round(build, 6), "index_allocated_bytes": size, "queries": {}}
        for query in ("京", "预算", "仅外域", "三千北京"):
            expr = expression(query, 0 if scoped else None)
            sql = (
                "select ev.id from ft cross join ev on ev.id=ft.rowid "
                "where ft match ? and ev.scope=0 order by ft.rowid desc limit 21"
            )
            db.execute(sql, (expr,)).fetchall()
            timings = []
            for _ in range(REPEATS):
                start = time.perf_counter()
                found = db.execute(sql, (expr,)).fetchall()
                timings.append((time.perf_counter() - start) * 1000)
            expected = [i for i, scope, body in reversed(rows) if scope == 0 and query in body][:21]
            assert [r[0] for r in found] == expected
            checks = {"ledger_scope_predicate_evaluations": 0}

            def count_scope(value, checks=checks):
                checks["ledger_scope_predicate_evaluations"] += 1
                return value

            db.create_function("count_scope", 1, count_scope)
            counted = sql.replace("ev.scope=0", "count_scope(ev.scope)=0")
            counted_rows = db.execute(counted, (expr,)).fetchall()
            assert counted_rows == found
            out["queries"][query] = {
                "posting_candidates_total": db.execute(
                    "select count(*) from ft where ft match ?", (expr,)
                ).fetchone()[0],
                "returned_count": len(found),
                "median_ms": round(statistics.median(timings), 6),
                **checks,
                "query_plan": db.execute("explain query plan " + sql, (expr,)).fetchall(),
            }
        out["rowid_lookup_without_match"] = db.execute(
            "select rowid from ft where rowid=1"
        ).fetchall()
        output["modes"]["scoped" if scoped else "global"] = out
        db.close()
    return output


def delete_cases():
    output = []
    for secure in (False, True):
        db = sqlite3.connect(":memory:")
        db.execute(
            "create virtual table ft using fts5(tokens,content='',"
            "contentless_delete=1,detail=none,tokenize='ascii')"
        )
        if secure:
            db.execute("insert into ft(ft,rank) values('secure-delete',1)")
        db.execute("insert into ft(rowid,tokens) values(1,'gsecret777 uniqueprivatetoken')")
        db.commit()
        before = db.execute("select rowid,tokens from ft where ft match 'gsecret777'").fetchall()
        db.execute("delete from ft where rowid=1")
        db.commit()
        db.execute("create virtual table v using fts5vocab(ft,'row')")
        blocks = [r[0] for r in db.execute("select block from ft_data")]
        output.append(
            {
                "secure_delete_option": secure,
                "before": before,
                "after_match": db.execute(
                    "select rowid from ft where ft match 'gsecret777'"
                ).fetchall(),
                "after_rowid": db.execute("select rowid from ft where rowid=1").fetchall(),
                "vocab_after": db.execute("select term,doc from v").fetchall(),
                "token_literal_remains_in_ft_data": any(
                    isinstance(b, bytes) and b"gsecret777" in b for b in blocks
                ),
            }
        )
        db.close()
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    result = {
        "environment": {
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "platform": platform.platform(),
            "storage": ":memory:",
            "target_scope_rows": TARGET_ROWS,
            "repeats": REPEATS,
        },
        "limitations": [
            "Synthetic candidate locality and local wall times only; not production acceptance.",
            "No VDBE count is a proof of FTS internal posting/page work.",
            "Ledger predicate counting is a separate instrumented execution, excluded from timing.",
            "No OpenCC, ASR, source authorization, WAL, real disk I/O, "
            "or concurrent writers modeled.",
        ],
        "cases": [benchmark(scopes) for scopes in (100, 1000)],
        "delete_cases": delete_cases(),
    }
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "out": str(args.out),
                "environment": result["environment"],
                "cases": [
                    {
                        "scopes": c["scopes"],
                        "events": c["events"],
                        "global_nohit": c["modes"]["global"]["queries"]["仅外域"],
                        "scoped_nohit": c["modes"]["scoped"]["queries"]["仅外域"],
                    }
                    for c in result["cases"]
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
